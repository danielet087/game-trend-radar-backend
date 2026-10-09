"""GitHub Contents CAS transport for the independent steam-state branch."""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Callable

import requests

from radar_backend.domain.follower_checkpoint import CheckpointConflict
from radar_backend.state.follower_checkpoint import (
    FollowerCheckpointAcknowledgement, RemoteFollowerCheckpoint,
    checkpoint_bytes, follower_records, git_blob_sha, strict_json_loads,
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
