"""GitHub Contents CAS transport for the independent steam-state branch."""
from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from radar_backend.domain.follower_checkpoint import CheckpointConflict
from radar_backend.application.follower_checkpoint import (
    DatabaseFollowerCheckpointAcknowledgement, persist_follower_checkpoint,
)
from radar_backend.state.follower_checkpoint import (
    FollowerCheckpointAcknowledgement, RemoteFollowerCheckpoint,
    checkpoint_bytes, follower_records, git_blob_sha, strict_json_loads,
    validate_checkpoint_locations,
)


def database_primary_enabled() -> bool:
    if os.environ.get("RADAR_DATABASE_PRIMARY", "0") in {"0", ""}:
        return False
    from radar_core.storage.primary import enabled
    return enabled()


class DatabaseCheckpointError(RuntimeError):
    """A primary durability failure must not become an ordinary Steam error."""


def _database_client():
    from radar_core.storage.sql import TursoClient
    # A new deadline belongs to each bounded checkpoint operation, rather than
    # expiring while the collector is pacing its Steam requests.
    return TursoClient.from_env(budget_seconds=120)


def _database_identity(repository: str, path: str | Path, branch: str) -> tuple[str, str]:
    if (not isinstance(repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
            or any(part in {".", ".."} for part in repository.split("/"))):
        raise ValueError("Database checkpoint requires a repository identity")
    if (not isinstance(branch, str) or not branch
            or re.fullmatch(r"[A-Za-z0-9_./-]+", branch) is None
            or any(part in {"", ".", ".."} for part in branch.split("/"))
            or ".." in branch):
        raise ValueError("Database checkpoint requires a branch identity")
    local, _ = validate_checkpoint_locations(Path(path))
    try:
        relative = local.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        raise ValueError("Database checkpoint cache must remain inside the checkout") from None
    if (not relative.startswith("data/") or not relative.endswith(".json") or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("Database checkpoint requires a data JSON path")
    return f"{repository}@{branch}", relative


@dataclass(frozen=True)
class _DatabaseFollowerCheckpoint:
    games: dict[str, dict[str, Any]]
    metadata: dict[str, Any]
    generation: int


def _read_database_checkpoint(client, source_id: str, path: str) -> _DatabaseFollowerCheckpoint:
    from radar_core.storage import primary_store
    from radar_core.storage.sql import StorageError
    status = primary_store.primary_status(client, source_id)
    if status.get("active") is not True or status.get("mode") != "primary":
        raise StorageError("PRIMARY_NOT_ACTIVE", "Follower checkpoint database primary is not active.")
    head, files = primary_store.read_files(client, source_id, paths=[path])
    if path not in files:
        raise StorageError("SOURCE_FILE_NOT_FOUND", "Authoritative follower checkpoint is missing.")
    if type(head.get("generation")) is not int or head["generation"] < 1:
        raise ValueError("Database checkpoint requires an acknowledged generation")
    payload = strict_json_loads(files[path].decode("utf-8"))
    games = follower_records(payload, strict=True)
    metadata = ({key: value for key, value in payload.items()
                 if key not in {"version", "updated_at", "games"}}
                if "games" in payload else {})
    return _DatabaseFollowerCheckpoint(games, metadata, head["generation"])


def load_database_follower_cache(*, repository: str, path: str | Path):
    source_id, relative = _database_identity(repository, path, "main")
    return _read_database_checkpoint(_database_client(), source_id, relative).games


def load_database_follower_checkpoint(*, repository: str, path: str | Path, branch: str):
    source_id, relative = _database_identity(repository, path, branch)
    return _read_database_checkpoint(_database_client(), source_id, relative).games


def persist_database_follower_checkpoint(frozen_payload, *, repository: str, path: str | Path,
                                        branch: str, merge_records: Callable):
    from radar_core.publication import snapshot_revision
    from radar_core.storage import primary_store
    from radar_core.storage.sql import StorageError
    source_id, relative = _database_identity(repository, path, branch)
    client = _database_client()
    # The operation identifies the frozen observations, even when a conflict
    # requires rebuilding their merge against a newer primary generation.
    frozen_digest = snapshot_revision(frozen_payload)
    operation_id = "steam-followers-" + hashlib.sha256(
        f"{source_id}\n{relative}\n{frozen_digest}".encode("utf-8")
    ).hexdigest()

    def write(previous, payload):
        raw = checkpoint_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        try:
            receipt = primary_store.publish_patch(
                client, source_id, base_generation=previous.generation,
                changed={relative: raw}, deleted=[], operation_id=operation_id,
                captured_at=payload["updated_at"], payload_revision=frozen_digest,
            )
        except StorageError as exc:
            if exc.code == "STALE_GENERATION":
                raise CheckpointConflict() from None
            raise
        if (not isinstance(receipt, dict) or receipt.get("source_id") != source_id
                or type(receipt.get("generation")) is not int
                or receipt["generation"] < previous.generation
                or not isinstance(receipt.get("revision"), str) or not receipt["revision"]
                or not isinstance(receipt.get("manifest_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt["manifest_digest"]) is None
                or receipt.get("operation_id") != operation_id
                or receipt.get("file_digests") != {relative: digest}
                or receipt.get("deleted") != []):
            raise ValueError("Database follower checkpoint acknowledgement does not match its payload")
        return DatabaseFollowerCheckpointAcknowledgement(
            source_id, receipt["generation"], receipt["revision"], receipt["manifest_digest"],
            operation_id, digest, payload["updated_at"],
        )

    return persist_follower_checkpoint(
        frozen_payload, read_latest=lambda: _read_database_checkpoint(client, source_id, relative),
        write_merged=write, merge_records=merge_records,
    )


class CheckpointTransportError(RuntimeError):
    def __init__(self, kind: str, *, status: int | None = None):
        self.kind = kind
        self.status = status
        super().__init__(f"HTTP {status}" if status is not None else kind)


def failure_detail(exc: Exception) -> str:
    if isinstance(exc, CheckpointConflict):
        return "HTTP 409"
    return str(exc) if isinstance(exc, CheckpointTransportError) else type(exc).__name__


def checkpoint_api_url(repository: str, path: str) -> str | None:
    if not repository:
        return None
    return f"https://api.github.com/repos/{repository}/contents/{path}"


def checkpoint_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise ValueError("Invalid Git SHA")
    return value


def _file_metadata(payload: Any, path: str, raw: bytes) -> str:
    if not isinstance(payload, dict) or payload.get("type") != "file" or payload.get("path") != path:
        raise ValueError("Unexpected Contents file identity")
    size = payload.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size != len(raw):
        raise ValueError("Unexpected Contents file size")
    sha = _sha(payload.get("sha"))
    if sha != git_blob_sha(raw, length=len(sha)):
        raise ValueError("Unexpected Contents blob hash")
    return sha


def load_remote_checkpoint(
    *, url: str, path: str, branch: str, headers: dict[str, str],
    timeout: float, get: Callable | None = None,
) -> RemoteFollowerCheckpoint:
    get = get if get is not None else requests.get
    try:
        response = get(url, headers=headers, params={"ref": branch}, timeout=timeout)
    except requests.RequestException as exc:
        raise CheckpointTransportError(type(exc).__name__) from None
    if response.status_code == 404:
        return RemoteFollowerCheckpoint({})
    if response.status_code != 200:
        raise CheckpointTransportError("ReadRejected", status=response.status_code)
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise ValueError("Contents response must carry base64 file content")
    encoded = payload.get("content")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("Contents file content is missing")
    # GitHub wraps base64 in newlines. No other characters are accepted.
    raw = base64.b64decode(encoded.replace("\n", "").replace("\r", ""), validate=True)
    sha = _file_metadata(payload, path, raw)
    document = strict_json_loads(raw.decode("utf-8"))
    records = follower_records(document, strict=True)
    metadata = {key: value for key, value in document.items() if key not in {"version", "updated_at", "games"}} if "games" in document else {}
    return RemoteFollowerCheckpoint(records, sha, metadata)


def save_remote_checkpoint(
    payload: dict[str, Any], *, previous: RemoteFollowerCheckpoint,
    url: str, path: str, branch: str, headers: dict[str, str],
    timeout: float, reason: str, put: Callable | None = None,
) -> FollowerCheckpointAcknowledgement:
    put = put if put is not None else requests.put
    raw = checkpoint_bytes(payload)
    body: dict[str, Any] = {
        "message": f"checkpoint: save Steam follower progress ({reason})",
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": branch,
    }
    if previous.blob_sha is not None:
        body["sha"] = _sha(previous.blob_sha)
    try:
        response = put(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise CheckpointTransportError(type(exc).__name__) from None
    if response.status_code == 409:
        raise CheckpointConflict()
    if response.status_code not in (200, 201):
        raise CheckpointTransportError("WriteRejected", status=response.status_code)
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("Contents acknowledgement must be an object")
    blob_sha = _file_metadata(result.get("content"), path, raw)
    commit = result.get("commit")
    if not isinstance(commit, dict):
        raise ValueError("Contents acknowledgement has no commit")
    commit_sha = _sha(commit.get("sha"))
    return FollowerCheckpointAcknowledgement(
        commit_sha, blob_sha, hashlib.sha256(raw).hexdigest(), str(payload["updated_at"]),
    )
