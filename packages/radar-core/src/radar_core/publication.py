"""Publish frozen JSON batches against the newest Git state, with push evidence.

This adapter is intended for disposable CI checkouts. Collection and freezing
the input batch happen before publication; ``apply`` only replays that batch.
It must not make network collection requests or change its observation clock.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence


class PublicationError(RuntimeError):
    """Publication has no acknowledged result; callers must not claim success."""


class GitOperationError(PublicationError):
    """A sanitized Git error that never includes command output or credentials."""


class PublicationScopeError(PublicationError):
    """An apply callback or existing checkout changed a file it does not own."""


def _validate_json(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("snapshot contains a non-finite number")
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("snapshot object keys must be strings")
            _validate_json(item)
        return
    raise TypeError("snapshot must contain only JSON values")


def snapshot_revision(payload: object) -> str:
    """Hash canonical JSON (UTF-8, sorted keys, compact, no NaN or Infinity)."""
    _validate_json(payload)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonempty(value: object, field: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


def _hash(value: object, field: str, *, git: bool = False) -> None:
    _nonempty(value, field)
    pattern = r"(?:[0-9a-f]{40}|[0-9a-f]{64})" if git else r"[0-9a-f]{64}"
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"{field} has an invalid revision")


def _branch(value: object) -> None:
    _nonempty(value, "branch")
    if (
        value.startswith(("-", "/")) or value.endswith(("/", "."))
        or value == "@" or ".." in value or "@{" in value
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or any(char in value for char in "~^:?*[\\")
        or any(not part or part.startswith(".") or part.endswith(".lock")
               for part in value.split("/"))
    ):
        raise ValueError("branch is not a valid Git branch name")


@dataclass(frozen=True)
class PublicationReceipt:
    """Evidence returned only after Git acknowledges the actual pushed HEAD.

    ``payload_revision`` identifies the frozen batch. ``target_snapshot_revision``
    hashes the resulting owned JSON files, including newer data preserved by the
    callback. ``published_revision`` is the actual Git commit, not a fabricated
    self-reference written into that commit.
    """

    input_revision: str
    payload_revision: str
    published_revision: str
    target_ref: str
    changed: bool
    attempts: int
    target_snapshot_revision: str

    def __post_init__(self) -> None:
        _nonempty(self.input_revision, "input_revision")
        _hash(self.payload_revision, "payload_revision")
        _hash(self.published_revision, "published_revision", git=True)
        _hash(self.target_snapshot_revision, "target_snapshot_revision")
        _nonempty(self.target_ref, "target_ref")
        if not self.target_ref.startswith("refs/heads/"):
            raise ValueError("target_ref must name a branch")
        _branch(self.target_ref.removeprefix("refs/heads/"))
        if type(self.changed) is not bool:
            raise TypeError("changed must be a boolean")
        if type(self.attempts) is not int:
            raise TypeError("attempts must be an integer")
        if not 1 <= self.attempts <= 20:
            raise ValueError("attempts must be between 1 and 20")

    def to_dict(self) -> dict[str, str | bool | int]:
        return {
            "schema_version": 1,
            "input_revision": self.input_revision,
            "payload_revision": self.payload_revision,
            "published_revision": self.published_revision,
            "target_ref": self.target_ref,
            "changed": self.changed,
            "attempts": self.attempts,
            "target_snapshot_revision": self.target_snapshot_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> PublicationReceipt:
        if type(value) is not dict:
            raise TypeError("publication receipt must be a dictionary")
        fields = {
            "input_revision", "payload_revision", "published_revision",
            "target_ref", "changed", "attempts", "target_snapshot_revision",
        }
        if set(value) != fields | {"schema_version"}:
            raise ValueError("publication receipt fields do not match its schema")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported publication receipt schema_version")
        return cls(**{field: value[field] for field in fields})


def _paths(paths: Sequence[str]) -> tuple[str, ...]:
    if isinstance(paths, (str, bytes)) or not paths:
        raise ValueError("paths must be a nonempty sequence of owned paths")
    owned = []
    for raw in paths:
        _nonempty(raw, "owned path")
        normalized = raw.rstrip("/")
        parts = normalized.split("/")
        if (
            raw.startswith("/") or "\\" in raw or "\x00" in raw
            or any(part in ("", ".", "..", ".git") for part in parts)
            or str(PurePosixPath(normalized)) != normalized
        ):
            raise ValueError("owned path must be a safe relative path")
        if normalized.startswith("data/"):
            if not normalized.endswith(".json") and PurePosixPath(normalized).suffix:
                raise ValueError("owned data files must be JSON")
        elif not (
            normalized.startswith("experiments/")
            and normalized.endswith("/checkpoint.json")
        ):
            raise ValueError("only data JSON scopes and explicit checkpoints are owned")
        owned.append(normalized)
    return tuple(dict.fromkeys(owned))


def _is_owned(path: str, owned: tuple[str, ...]) -> bool:
    if not path.endswith(".json") or any(p in ("", ".", "..", ".git") for p in path.split("/")):
        return False
    return any(path == item or (not item.endswith(".json") and path.startswith(item + "/"))
               for item in owned)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("published JSON contains duplicate object keys")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("published JSON contains a non-finite number")


class SubprocessGitRepository:
    """Git adapter restricted to explicitly owned JSON in a disposable checkout.

    ``push_command_prefix`` replaces the ``git`` executable for push only, e.g.
    ``('bash', '/absolute/scripts/git_frontend_auth.sh')`` for an askpass wrapper
    which itself runs Git. Tokens should be passed through its environment,
    never through a URL, an argv argument, or this adapter's error messages.
    """

    def __init__(
        self, root: str | Path, *, remote: str = "origin", branch: str = "main",
        push_command_prefix: Sequence[str] = (), disposable_checkout: bool = False,
        environment: Mapping[str, str] | None = None,
        committer_name: str = "github-actions[bot]",
        committer_email: str = "41898282+github-actions[bot]@users.noreply.github.com",
    ) -> None:
        if disposable_checkout is not True:
            raise ValueError("publication requires an explicitly disposable checkout")
        if type(remote) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote) is None:
            raise ValueError("remote must be a safe Git remote name")
        _branch(branch)
        if isinstance(push_command_prefix, (str, bytes)):
            raise TypeError("push_command_prefix must be an argv sequence")
        self.push_command_prefix = tuple(push_command_prefix)
        if any(type(part) is not str or not part or "\x00" in part
               for part in self.push_command_prefix):
            raise ValueError("push_command_prefix has an invalid argument")
        self.root = Path(root).resolve()
        _nonempty(committer_name, "committer_name")
        _nonempty(committer_email, "committer_email")
        self.committer_name = committer_name
        self.committer_email = committer_email
        if environment is not None and (
            not isinstance(environment, Mapping)
            or any(type(key) is not str or type(value) is not str
                   for key, value in environment.items())
        ):
            raise TypeError("environment must map strings to strings")
        self.environment = dict(os.environ)
        self.environment.update(environment or {})
        self.remote = remote
        self.branch = branch
        self.target_ref = "refs/heads/" + branch
        self._untracked_baseline: dict[str, str] = {}
        self._refresh_revision: str | None = None
        top = self._run("rev-parse", "--show-toplevel").strip()
        if Path(top).resolve() != self.root:
            raise ValueError("root must be the Git checkout root")

    def _run(self, *args: str, push: bool = False) -> str:
        command = (*self.push_command_prefix, *args) if push and self.push_command_prefix else ("git", *args)
        operation = "push" if push else args[0]
        try:
            result = subprocess.run(
                command, cwd=self.root, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=120, check=False,
                env=self.environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise GitOperationError(f"Git {operation} could not execute") from None
        if result.returncode != 0:
            raise GitOperationError(f"Git {operation} failed (exit {result.returncode})")
        return result.stdout

    def _tracked_changes(self) -> set[str]:
        changes = self._run("diff", "--name-only", "--no-renames", "-z", "HEAD", "--")
        staged = self._run("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--")
        return set(filter(None, (changes + staged).split("\x00")))

    def _untracked(self) -> set[str]:
        return set(filter(None, self._run("ls-files", "--others", "-z", "--").split("\x00")))

    def _safe_owned_file(self, relative: str) -> Path:
        path = self.root.joinpath(*PurePosixPath(relative).parts)
        if self.root not in path.resolve().parents:
            raise PublicationScopeError("owned JSON must remain inside the checkout")
        current = path
        while current != self.root:
            if current.is_symlink():
                raise PublicationScopeError("owned JSON cannot use symlinks")
            current = current.parent
        return path

    def _validate_owned_locations(self, owned: tuple[str, ...]) -> None:
        """Reject redirects before the callback can write through a directory.

        Directory scopes need their own validation: a tracked symlink such as
        ``data/calendar`` does not end in ``.json`` and therefore is absent from
        the JSON snapshot. Checking only changed/selected JSON files is too late
        if the callback already followed that link outside its checkout.
        """
        for relative in owned:
            path = self._safe_owned_file(relative)
            if not relative.endswith(".json") and path.is_dir():
                # rglob does not follow directory symlinks, but yields the link
                # itself. Reject both tracked and untracked redirects under an
                # owned directory, including links to another checkout location.
                for candidate in path.rglob("*"):
                    if candidate.is_symlink():
                        self._safe_owned_file(candidate.relative_to(self.root).as_posix())
        tracked = filter(None, self._run("ls-tree", "-r", "--name-only", "-z", "HEAD").split("\x00"))
        for relative in tracked:
            if _is_owned(relative, owned):
                self._safe_owned_file(relative)

    def _fingerprint(self, relative: str) -> str:
        """Preserve existing untracked outputs without allowing callback edits."""
        path = self.root / relative
        digest = hashlib.sha256()
        candidates = sorted(path.rglob("*")) if path.is_dir() and not path.is_symlink() else [path]
        for item in candidates:
            if ".git" in item.relative_to(self.root).parts:
                continue
            digest.update(str(item.relative_to(self.root)).encode("utf-8"))
            if item.is_symlink():
                digest.update(str(item.readlink()).encode("utf-8"))
            elif item.is_file():
                digest.update(item.read_bytes())
        return digest.hexdigest()

    def refresh(self, paths: Sequence[str]) -> None:
        owned = _paths(paths)
        if any(not _is_owned(path, owned) for path in self._tracked_changes()):
            raise PublicationScopeError("checkout has changes outside the owned JSON paths")
        untracked = self._untracked()
        self._untracked_baseline = {
            path: self._fingerprint(path) for path in untracked if not _is_owned(path, owned)
        }
        self._run("fetch", "--no-tags", self.remote,
                  f"+{self.target_ref}:refs/remotes/{self.remote}/{self.branch}")
        remote_paths = set(filter(None, self._run(
            "ls-tree", "-r", "--name-only", "-z", f"refs/remotes/{self.remote}/{self.branch}",
        ).split("\x00")))
        if any(
            path.rstrip("/") == item
            or item.startswith(path.rstrip("/") + "/")
            or path.rstrip("/").startswith(item + "/")
            for path in self._untracked_baseline for item in remote_paths
        ):
            raise PublicationScopeError("remote update would overwrite an unowned untracked path")
        for relative in untracked:
            if _is_owned(relative, owned):
                self._safe_owned_file(relative).unlink(missing_ok=True)
        self._run("reset", "--hard", f"refs/remotes/{self.remote}/{self.branch}")
        self._refresh_revision = self.head_revision()
        self._validate_owned_locations(owned)

    def _assert_owned_history(self, owned: tuple[str, ...]) -> None:
        if self._refresh_revision is None:
            raise PublicationError("checkout must be refreshed before staging")
        paths = filter(None, self._run(
            "diff", "--name-only", "--no-renames", "-z", self._refresh_revision, "HEAD", "--",
        ).split("\x00"))
        if any(not _is_owned(path, owned) for path in paths):
            raise PublicationScopeError("commit changed a file outside its owned JSON paths")

    def stage_commit(self, paths: Sequence[str], message: str) -> bool:
        owned = _paths(paths)
        _nonempty(message, "message")
        self._assert_owned_history(owned)
        self._validate_owned_locations(owned)
        changed = self._tracked_changes()
        untracked = self._untracked()
        if any(not _is_owned(path, owned) for path in changed):
            raise PublicationScopeError("apply changed a tracked file outside its owned JSON paths")
        for path in set(self._untracked_baseline) | untracked:
            if _is_owned(path, owned):
                self._safe_owned_file(path)
            elif path not in self._untracked_baseline or path not in untracked or self._fingerprint(path) != self._untracked_baseline[path]:
                raise PublicationScopeError("apply changed an untracked file outside its owned JSON paths")
        all_changed = changed | {path for path in untracked if _is_owned(path, owned)}
        for path in all_changed:
            self._safe_owned_file(path)
        if all_changed:
            self._run("add", "--all", "--", *sorted(all_changed))
        staged = set(filter(None, self._run("diff", "--cached", "--name-only", "--no-renames", "-z", "--").split("\x00")))
        if any(not _is_owned(path, owned) for path in staged):
            raise PublicationScopeError("index contains changes outside its owned JSON paths")
        if not staged:
            return False
        self._run("-c", f"user.name={self.committer_name}",
                  "-c", f"user.email={self.committer_email}", "commit", "-m", message)
        self._assert_owned_history(owned)
        if self._tracked_changes():
            raise PublicationScopeError("commit left additional unstaged changes")
        return True

    def head_revision(self) -> str:
        revision = self._run("rev-parse", "HEAD").strip()
        _hash(revision, "HEAD", git=True)
        return revision

    def target_snapshot_revision(self, paths: Sequence[str]) -> str:
        owned = _paths(paths)
        self._validate_owned_locations(owned)
        tracked = set(filter(None, self._run("ls-tree", "-r", "--name-only", "-z", "HEAD").split("\x00")))
        selected = {path for path in tracked if _is_owned(path, owned)}
        selected.update(path for path in owned if path.endswith(".json"))
        snapshot = {}
        for path in sorted(selected):
            if path not in tracked:
                snapshot[path] = {"present": False}
                continue
            self._safe_owned_file(path)
            raw = self._run("show", f"HEAD:{path}")
            payload = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_pairs)
            _validate_json(payload)
            snapshot[path] = {"present": True, "payload": payload}
        return snapshot_revision(snapshot)

    def push(self, revision: str | None = None) -> bool:
        revision = self.head_revision() if revision is None else revision
        _hash(revision, "push revision", git=True)
        try:
            self._run("push", self.remote, f"{revision}:{self.target_ref}", push=True)
        except GitOperationError:
            return False
        return True


def publish_with_retry(
    repository: SubprocessGitRepository, apply: Callable[[Path], None], *,
    paths: Sequence[str], message: str, input_revision: str, payload_revision: str,
    max_attempts: int = 5,
) -> PublicationReceipt:
    """Replay a frozen batch and return evidence only after acknowledged push.

    Push failures retry from the newest remote state. Apply, JSON validation,
    fetch, scope and commit failures stop immediately. A no-op still pushes and
    retries if the remote advanced; an empty diff alone proves no publication.
    """
    owned = _paths(paths)
    _nonempty(message, "message")
    _nonempty(input_revision, "input_revision")
    _hash(payload_revision, "payload_revision")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts must be between 1 and 20")
    if not callable(apply):
        raise TypeError("apply must be callable")
    for attempt in range(1, max_attempts + 1):
        repository.refresh(owned)
        apply(repository.root)
        changed = repository.stage_commit(owned, message)
        revision = repository.head_revision()
        target_revision = repository.target_snapshot_revision(owned)
        if repository.push(revision):
            if repository.head_revision() != revision:
                raise PublicationError("checkout HEAD changed during publication")
            return PublicationReceipt(
                input_revision=input_revision, payload_revision=payload_revision,
                published_revision=revision, target_ref=repository.target_ref,
                changed=changed, attempts=attempt,
                target_snapshot_revision=target_revision,
            )
    raise PublicationError(f"publication was not acknowledged after {max_attempts} attempts")
