"""Read complete, immutable Git source snapshots without changing collectors."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess

from .sql import StorageError


SOURCES = (
    {"repository": "danielet087/game-trend-radar", "branch": "main", "prefixes": ("data/",)},
    {"repository": "danielet087/game-trend-radar-backend", "branch": "main", "prefixes": ("data/", "experiments/", "reports/")},
    {"repository": "danielet087/game-trend-radar-backend", "branch": "steam-state", "prefixes": ("data/",)},
    {"repository": "danielet087/game-trend-radar-content-backend", "branch": "main", "prefixes": ("data/",),
     "files": ("content_backend/descriptions_zh_tw.json",)},
    {"repository": "danielet087/game-trend-radar-igdb-backend", "branch": "main", "prefixes": ("data/",)},
    {"repository": "danielet087/game-trend-radar-twitch-backend", "branch": "main", "prefixes": ("data/",)},
    {"repository": "danielet087/game-trend-radar-youtube-backend", "branch": "main", "prefixes": ("data/",), "allow_empty": True},
)
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024


def source_id(spec):
    return spec["repository"] + "@" + spec["branch"]


def checkout_name(spec):
    return spec["repository"].split("/")[1] + ("-" + spec["branch"] if spec["branch"] != "main" else "")


def _git(args, *, root=None, timeout=180):
    try:
        result = subprocess.run(["git", *args], cwd=root, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise StorageError("SOURCE_UNAVAILABLE", "The source repository could not be read within its time limit.") from None
    if result.returncode:
        raise StorageError("SOURCE_UNAVAILABLE", "The source repository could not be read; its previous snapshot is retained.")
    return result.stdout


def remote_revision(spec):
    raw = _git(["ls-remote", "--exit-code", "https://github.com/" + spec["repository"] + ".git",
                "refs/heads/" + spec["branch"]], timeout=60).decode("ascii").strip().split()
    if len(raw) != 2 or not re.fullmatch(r"[0-9a-f]{40}", raw[0]):
        raise StorageError("INVALID_SOURCE", "Source branch did not resolve to one immutable commit.")
    return raw[0]


def clone_source(spec, parent):
    """Each online run owns a fresh temporary directory; never reset a checkout."""
    target = Path(parent) / checkout_name(spec)
    if target.exists():
        raise StorageError("SOURCE_DIRECTORY_EXISTS", "Online source checkout directory must be new.")
    _git(["clone", "--depth", "1", "--no-tags", "--single-branch", "--branch", spec["branch"],
          "https://github.com/" + spec["repository"] + ".git", str(target)], timeout=240)
    return target


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _bad_number(_):
    raise ValueError("nonfinite number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def validate_data(raw, path):
    try:
        if path.endswith(".json"):
            json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_number, parse_float=_finite_float)
        else:
            for line in raw.splitlines():
                if line.strip():
                    json.loads(line, object_pairs_hook=_pairs, parse_constant=_bad_number, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise StorageError("INVALID_SOURCE_JSON", "A source document is invalid; no partial source snapshot is published.") from None


def snapshot_from_checkout(spec, root):
    root = Path(root)
    revision = _git(["rev-parse", "HEAD"], root=root).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise StorageError("INVALID_SOURCE", "A fixed source Git revision is required.")
    captured_at = datetime.now(timezone.utc).isoformat()
    tree = _git(["ls-tree", "-r", "-z", revision], root=root)
    files, total = {}, 0
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        info, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        selected = path in spec.get("files", ()) or any(path.startswith(p) for p in spec.get("prefixes", ()))
        if not selected or not path.endswith((".json", ".jsonl", ".ndjson")):
            continue
        parts = PurePosixPath(path).parts
        mode, kind, blob = info.decode("ascii").split()
        if mode not in {"100644", "100755"} or kind != "blob" or path.startswith("/") or ".." in parts or "\\" in path:
            raise StorageError("INVALID_SOURCE", "Source documents must be regular files at safe relative paths.")
        raw = _git(["cat-file", "blob", blob], root=root)
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_SOURCE_BYTES:
            raise StorageError("SOURCE_SIZE_LIMIT", "Source exceeds the bounded import size; its previous snapshot is retained.")
        validate_data(raw, path)
        files[path] = raw
    if not files and not spec.get("allow_empty", False):
        raise StorageError("EMPTY_SOURCE", "A previously populated source must not be silently replaced by an empty snapshot.")
    return {"source_id": source_id(spec), "revision": revision, "captured_at": captured_at,
            "files": files, "raw_bytes": total}


def file_manifest(files):
    return {path: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            for path, raw in sorted(files.items())}
