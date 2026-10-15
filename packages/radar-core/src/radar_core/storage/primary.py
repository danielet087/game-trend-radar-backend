"""DB-authoritative JSON inputs and Git projections for disposable CI workspaces.

All database errors propagate. Enabling this module never grants permission to
use a Git file as a substitute for an unavailable or inactive primary source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from .sql import StorageError, TursoClient


def enabled():
    value = os.environ.get("RADAR_DATABASE_PRIMARY", "0")
    if value not in {"0", "1", ""}:
        raise StorageError("INVALID_CONFIG", "Primary mode must be explicitly zero or one.")
    return value == "1"


def client():
    """A bounded client, without retaining a deadline across separate jobs."""
    return TursoClient.from_env(budget_seconds=600)


def source_id(repository, branch="main"):
    if not isinstance(repository, str):
        raise StorageError("INVALID_SOURCE", "A repository source is required.")
    if "@" in repository:
        repository, branch = repository.rsplit("@", 1)
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not isinstance(branch, str) or not branch or ".." in branch
            or branch.startswith(("/", ".", "-")) or branch.endswith(("/", "."))
            or re.search(r"[^A-Za-z0-9_./-]", branch)):
        raise StorageError("INVALID_SOURCE", "A safe repository and branch are required.")
    return repository + "@" + branch


def _git(root, *args):
    try:
        result = subprocess.run(["git", *args], cwd=root, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise StorageError("GIT_WORKSPACE_FAILED", "The disposable workspace could not be prepared.") from None
    if result.returncode:
        raise StorageError("GIT_WORKSPACE_FAILED", "The disposable workspace could not be prepared.")
    return result.stdout.decode("utf-8")


def infer_source(root, *, remote="origin", branch="main"):
    url = _git(root, "remote", "get-url", remote).strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", url)
    if not match:
        raise StorageError("INVALID_SOURCE", "The primary workspace requires an explicit GitHub repository source.")
    return source_id(match[1], branch)


def _store():
    from . import primary_store
    return primary_store


def _active(db, source):
    if not _store().primary_status(db, source).get("active"):
        raise StorageError("PRIMARY_NOT_ACTIVE", "The requested database source has not been activated.")


def read_files(repository, paths=None, *, branch="main", revision=None, known_files=None):
    db, source = client(), source_id(repository, branch)
    _active(db, source)
    return _store().read_files(db, source, paths, revision=revision, known_files=known_files)


def read_json(repository, path, *, branch="main", revision=None):
    _, files = read_files(repository, [path], branch=branch, revision=revision)
    if path not in files:
        raise StorageError("FILE_NOT_FOUND", "The database source does not contain the requested document.")
    from .sources import validate_data
    validate_data(files[path], path)
    return json.loads(files[path])


def resolve_git_revision(repository, git_revision, *, branch="main"):
    db, source = client(), source_id(repository, branch)
    _active(db, source)
    return _store().resolve_git_revision(db, source, git_revision)


def write_files(repository, files, *, base_generation, deleted=(), operation_id,
                payload_revision=None, branch="main", projection_required=True):
    db, source = client(), source_id(repository, branch)
    _active(db, source)
    deleted = tuple(deleted)
    if not isinstance(files, dict) or any(not _managed(source, name) for name in (*files, *deleted)):
        raise StorageError("INVALID_PATH", "Primary patches must remain in the source data namespace.")
    from .sources import validate_data
    for name, raw in files.items():
        if not isinstance(raw, bytes):
            raise StorageError("INVALID_FILE_BYTES", "Primary patches require original document bytes.")
        validate_data(raw, name)
    return _store().publish_patch(db, source, base_generation=base_generation,
        changed=files, deleted=deleted, operation_id=operation_id,
        payload_revision=payload_revision, projection_required=projection_required)


def _safe_path(root, name):
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or name.startswith("/") or any(p in {"", ".", "..", ".git"} for p in name.split("/"))
            or str(PurePosixPath(name)) != name or not name.endswith((".json", ".jsonl", ".ndjson"))):
        raise StorageError("INVALID_PATH", "Primary documents require safe JSON paths.")
    path = root / name
    current = path
    while current != root:
        if current.is_symlink():
            raise StorageError("UNSAFE_WORKSPACE", "Primary documents cannot traverse symlinks.")
        current = current.parent
    if root not in path.resolve().parents:
        raise StorageError("UNSAFE_WORKSPACE", "Primary documents must remain inside the workspace.")
    return path


def _managed(source, path):
    if not isinstance(path, str):
        return False
    from .sources import SOURCES
    spec = next((item for item in SOURCES if source_id(item["repository"], item["branch"]) == source), None)
    prefixes = spec.get("prefixes", ("data/",)) if spec else ("data/",)
    files = spec.get("files", ()) if spec else ()
    return path.endswith((".json", ".jsonl", ".ndjson")) and (path in files or any(path.startswith(p) for p in prefixes))


def _metadata_path(root):
    return Path(_git(root, "rev-parse", "--absolute-git-dir").strip()) / "radar-primary.json"


def _baseline(root):
    try:
        value = json.loads(_metadata_path(root).read_bytes())
    except (OSError, ValueError):
        raise StorageError("PRIMARY_BASELINE_MISSING", "Hydrate the primary workspace before committing documents.") from None
    if not isinstance(value, dict) or not isinstance(value.get("head"), dict):
        raise StorageError("PRIMARY_BASELINE_MISSING", "The primary workspace baseline is invalid.")
    return value


def _known_files(root, source):
    """Hash-verified cache candidates; the DB manifest remains authoritative."""
    root = Path(root).resolve()
    paths = set(filter(None, _git(root, "ls-files", "-z").split("\x00")))
    return {name: path.read_bytes() for name in paths if _managed(source, name)
            and (path := _safe_path(root, name)).is_file()}


def _project(root, source, head, files, *, clean_baseline=False):
    root = Path(root).resolve()
    if Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
        raise StorageError("UNSAFE_WORKSPACE", "Hydration requires the disposable repository root.")
    tracked = set(filter(None, _git(root, "ls-files", "-z").split("\x00")))
    selected = {path for path in tracked if _managed(source, path)} | set(files)
    try:
        old = _baseline(root)
        if old.get("source_id") == source:
            selected.update(old["head"].get("manifest", {}))
    except StorageError as error:
        if error.code != "PRIMARY_BASELINE_MISSING":
            raise
    if any(not _managed(source, path) for path in selected):
        raise StorageError("INVALID_PATH", "A database document is outside its repository data namespace.")
    # Check the complete set before changing any files.
    locations = {name: _safe_path(root, name) for name in selected}
    from .sources import validate_data
    for name, raw in files.items():
        validate_data(raw, name)
    touched = []
    for name, path in locations.items():
        raw = files.get(name)
        if raw is None:
            if path.exists():
                path.unlink()
                touched.append(name)
        elif not path.exists() or path.read_bytes() != raw:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            touched.append(name)
    committed = False
    if clean_baseline:
        # Files may already match DB because the caller just committed its
        # candidate there; they still differ from the last local Git commit.
        stage_paths = sorted(name for name in selected if name in tracked or locations[name].exists())
        for offset in range(0, len(stage_paths), 100):
            _git(root, "add", "--all", "--", *stage_paths[offset:offset + 100])
        staged = set(filter(None, _git(root, "diff", "--cached", "--name-only", "-z").split("\x00")))
        if not staged.issubset(selected):
            raise StorageError("UNSAFE_WORKSPACE", "The Git index contains unrelated changes.")
        if staged:
            _git(root, "-c", "user.name=github-actions[bot]", "-c",
                 "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                 "commit", "-m", "data: project committed primary database snapshot")
            committed = True
    metadata = {"source_id": source, "head": head}
    _metadata_path(root).write_text(json.dumps(metadata, ensure_ascii=False) + "\n", encoding="utf-8")
    return committed if clean_baseline else bool(touched)


def hydrate(root, repository=None, *, branch="main", clean_baseline=False):
    source = source_id(repository, branch) if repository else infer_source(root, branch=branch)
    head, files = read_files(source, known_files=_known_files(root, source))
    _project(root, source, head, files, clean_baseline=clean_baseline)
    return head


def _selected(path, paths):
    return any(path == item.rstrip("/") or (not item.rstrip("/").endswith((".json", ".jsonl", ".ndjson"))
        and path.startswith(item.rstrip("/") + "/")) for item in paths)


def _patch(root, baseline, paths):
    root = Path(root).resolve()
    known = baseline["head"].get("manifest", {})
    candidates = {name for name in known if _selected(name, paths)}
    for item in paths:
        normalized = item.rstrip("/")
        if normalized.endswith((".json", ".jsonl", ".ndjson")):
            candidates.add(normalized)
        else:
            # Validate the directory itself even when it is empty.
            _safe_path(root, normalized + "/_scope.json")
            candidates.update(path.relative_to(root).as_posix() for path in (root / normalized).rglob("*")
                              if path.is_file() and path.suffix in {".json", ".jsonl", ".ndjson"})
    changed, deleted = {}, []
    for name in candidates:
        if not _managed(baseline["source_id"], name):
            raise StorageError("INVALID_PATH", "A patch is outside its source namespace.")
        path = _safe_path(root, name)
        if not path.exists():
            if name in known:
                deleted.append(name)
            continue
        raw = path.read_bytes()
        if name not in known or hashlib.sha256(raw).hexdigest() != known[name]["digest"]:
            from .sources import validate_data
            validate_data(raw, name)
            changed[name] = raw
    return changed, deleted


def commit_paths(root, paths, *, operation_id, payload_revision=None):
    baseline = _baseline(Path(root).resolve())
    changed, deleted = _patch(root, baseline, paths)
    receipt = write_files(baseline["source_id"], changed,
        base_generation=baseline["head"]["generation"], deleted=deleted,
        operation_id=operation_id, payload_revision=payload_revision)
    return receipt


class GitPrimary:
    """Internal bridge: DB CAS happens before any public Git push."""
    def __init__(self, source, db=None):
        self.source, self.db = source_id(source), db or client()
        self.head, self.files, self.receipt = None, {}, None
        self.operation_id = self.payload_revision = None
        self.projection_changed = False

    def refresh(self, root):
        _active(self.db, self.source)
        known = {**_known_files(root, self.source), **self.files}
        self.head, self.files = _store().read_files(self.db, self.source, known_files=known)
        self.projection_changed = _project(root, self.source, self.head, self.files, clean_baseline=True)

    def commit(self, root, paths):
        changed, deleted = _patch(root, {"source_id": self.source, "head": self.head}, paths)
        self.receipt = _store().publish_patch(self.db, self.source,
            base_generation=self.head["generation"], changed=changed, deleted=deleted,
            operation_id=self.operation_id, payload_revision=self.payload_revision)
        # A duplicate operation can refer to an older generation. Always project
        # the actual committed source, never the uncommitted candidate on disk.
        self.head, self.files = _store().read_files(self.db, self.source, known_files={**self.files, **changed})
        changed = _project(root, self.source, self.head, self.files, clean_baseline=True)
        return self.projection_changed or changed

    def acknowledge(self, git_revision):
        # The pushed tree contains every source file at this generation. Later
        # generations stay pending, including a concurrent writer after our read.
        for receipt in _store().projection_pending(self.db, self.source):
            if receipt["generation"] <= self.head["generation"]:
                _store().acknowledge_projection(self.db, self.source, receipt["operation_id"], git_revision,
                    generation=self.head["generation"], manifest_digest=self.head["manifest_digest"])


def git_primary(root, *, remote="origin", branch="main"):
    if not enabled():
        return None
    bridge = GitPrimary(infer_source(root, remote=remote, branch=branch))
    status = _store().primary_status(bridge.db, bridge.source)
    if not status.get("active"):
        raise StorageError("PRIMARY_NOT_ACTIVE", "The publication source has not been activated.")
    return bridge


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("hydrate")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        if not enabled():
            print(json.dumps({"status": "disabled"}))
            return 0
        head = hydrate(args.root, args.source, clean_baseline=True)
    except StorageError as error:
        print(json.dumps({"status": "error", "code": error.code}))
        return 1
    print(json.dumps({"status": "hydrated", "source_id": head["source_id"], "generation": head["generation"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
