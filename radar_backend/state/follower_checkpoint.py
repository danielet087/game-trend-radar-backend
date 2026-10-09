"""The existing follower JSON envelope, strict snapshots and local writes."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RemoteFollowerCheckpoint:
    games: dict[str, dict[str, Any]]
    blob_sha: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FollowerCheckpointAcknowledgement:
    commit_sha: str
    blob_sha: str
    payload_revision: str
    observed_at: str


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON constant")


def strict_json_loads(raw: str) -> dict[str, Any]:
    value = json.loads(
        raw, object_pairs_hook=_unique_object, parse_float=_finite_float,
        parse_constant=_invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("Follower checkpoint must be an object")
    return value


def follower_records(payload: dict[str, Any], *, strict: bool = False) -> dict[str, dict[str, Any]]:
    if strict and "games" in payload and "version" in payload:
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise ValueError("Unsupported follower checkpoint version")
    games = payload.get("games", payload)
    if not isinstance(games, dict):
        raise ValueError("Follower checkpoint games must be an object")
    if strict and any(not isinstance(entry, dict) for entry in games.values()):
        raise ValueError("Follower checkpoint records must be objects")
    return {str(appid): deepcopy(entry) for appid, entry in games.items() if isinstance(entry, dict)}


def checkpoint_payload(games: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "games": games,
    }


def checkpoint_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def git_blob_sha(raw: bytes, *, length: int = 40) -> str:
    if length not in (40, 64):
        raise ValueError("Unsupported Git object hash")
    algorithm = hashlib.sha1 if length == 40 else hashlib.sha256
    return algorithm(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def validate_checkpoint_locations(path: Path) -> tuple[Path, Path]:
    """Check lexical ancestors before reading or replacing a local checkpoint."""
    path = Path(path).absolute()
    temporary = path.with_suffix(".tmp")
    if temporary == path:
        raise ValueError("Checkpoint and temporary path collide")
    for candidate in (path, temporary):
        for component in (*reversed(candidate.parents), candidate):
            if component.is_symlink():
                raise ValueError("Checkpoint path must not traverse a symlink")
        if candidate.exists() and not candidate.is_file():
            raise ValueError("Checkpoint path must be a regular file")
    return path, temporary


def load_checkpoint_file(path: Path) -> dict[str, dict[str, Any]]:
    path, _ = validate_checkpoint_locations(path)
    if not path.exists():
        return {}
    return follower_records(strict_json_loads(path.read_text(encoding="utf-8")))


def save_checkpoint_file(path: Path, payload: dict[str, Any]) -> None:
    path, temporary = validate_checkpoint_locations(path)
    raw = checkpoint_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Validate again after directory creation, before opening the temporary file.
    validate_checkpoint_locations(path)
    temporary.write_bytes(raw)
    temporary.replace(path)
