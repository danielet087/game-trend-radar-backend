"""Publish portable daily backups without replacing completed releases.

Only fixed GitHub API/upload hosts receive credentials. Completion is recorded
only after all three uploads: the release body retains the exact SHA256SUMS
text, and each GitHub asset digest must match the receipt. This avoids binary
asset download redirects while checking content, not merely asset filenames.
Callers must serialize publishers for a repository/day (Actions concurrency).
"""
from __future__ import annotations

from datetime import date
import hashlib
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .sql import StorageError

MARKER = "radar-storage-backup:v1"
ASSET_NAMES = ("radar.sql.gz", "manifest.json", "SHA256SUMS")
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class BackupError(StorageError):
    def __init__(self, code: str, message: str = "Daily backup publication failed.", *, status=None):
        super().__init__(code, message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _identifier(value):
    if type(value) is not int or value <= 0:
        raise BackupError("BACKUP_INVALID_RESPONSE")
    return value


def _json(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError):
        raise BackupError("BACKUP_INVALID_RESPONSE") from None


def _checksums(text):
    """Accept standard sha256sum text, with exactly the two payload filenames."""
    if not isinstance(text, str) or len(text) > 1024:
        raise BackupError("BACKUP_CHECKSUM_INVALID")
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](radar\.sql\.gz|manifest\.json)", line)
        if not match or match[2] in result:
            raise BackupError("BACKUP_CHECKSUM_INVALID")
        result[match[2]] = match[1]
    if set(result) != set(ASSET_NAMES[:2]):
        raise BackupError("BACKUP_CHECKSUM_INVALID")
    return result


class BackupReleaseClient:
    def __init__(self, repository: str, day: str, token: str, *, opener=None,
                 sleep=time.sleep, clock=time.monotonic, budget_seconds=300):
        if (not isinstance(repository, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository) is None):
            raise BackupError("BACKUP_INVALID_CONFIG")
        day = day.isoformat() if isinstance(day, date) else day
        try:
            if not isinstance(day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
                raise ValueError
            date.fromisoformat(day)
        except (ValueError, TypeError):
            raise BackupError("BACKUP_INVALID_CONFIG") from None
        if (not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds)
                or budget_seconds <= 0):
            raise BackupError("BACKUP_INVALID_CONFIG")
        self.repository, self.day, self.tag = repository, day, f"radar-storage-v1-{day}"
        self._token = token
        self.opener = opener if opener is not None else build_opener(NoRedirect())
        self.sleep, self.clock, self.deadline = sleep, clock, clock() + budget_seconds
        self.base_path = f"/repos/{repository}/releases"

    def _request(self, method, path, *, payload=None, content=None, content_type=None, upload=False, missing_ok=False):
        # Paths are constructed internally from validated repository, IDs and names.
        if not path.startswith(self.base_path) or method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise BackupError("BACKUP_INVALID_REQUEST")
        host = "uploads.github.com" if upload else "api.github.com"
        body = content if content is not None else (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else None)
        for attempt in range(3):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise BackupError("BACKUP_TIME_BUDGET_EXCEEDED")
            headers = {"Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
                       "User-Agent": "game-trend-radar-backup", "X-GitHub-Api-Version": "2022-11-28"}
            if body is not None:
                headers["Content-Type"] = content_type or "application/json"
            request = Request("https://" + host + path, data=body, method=method, headers=headers)
            try:
                with self.opener.open(request, timeout=min(30, remaining)) as response:
                    status = getattr(response, "status", 200)
                    if status not in {200, 201, 204}:
                        raise BackupError("BACKUP_HTTP_ERROR", status=status)
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise BackupError("BACKUP_RESPONSE_TOO_LARGE")
                if self.clock() >= self.deadline:
                    raise BackupError("BACKUP_TIME_BUDGET_EXCEEDED")
                return _json(raw) if raw else {}
            except HTTPError as error:
                status = error.code
                error.close()
                if status == 404 and missing_ok:
                    return None
                if status in {401, 403}:
                    raise BackupError("BACKUP_AUTHORIZATION_FAILED", status=status) from None
                if status not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                    raise BackupError("BACKUP_HTTP_ERROR", status=status) from None
            except (URLError, TimeoutError, OSError, HTTPException):
                if attempt == 2:
                    raise BackupError("BACKUP_NETWORK_ERROR") from None
            self.sleep(min(2 ** attempt, max(0, self.deadline - self.clock())))
        raise BackupError("BACKUP_NETWORK_ERROR")

    def _release(self):
        result = self._request("GET", f"{self.base_path}/tags/{self.tag}", missing_ok=True)
        if result is None:
            return None
        self._owned(result)
        return result

    def _owned(self, release):
        if (not isinstance(release, dict) or release.get("tag_name") != self.tag
                or not isinstance(release.get("body"), str)
                or release["body"].split("\n", 1)[0] != MARKER):
            raise BackupError("BACKUP_RELEASE_NOT_OWNED")
        _identifier(release.get("id"))
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise BackupError("BACKUP_RELEASE_NOT_OWNED")

    @staticmethod
    def _assets(release):
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise BackupError("BACKUP_INVALID_RESPONSE")
        result = {}
        for asset in assets:
            if (not isinstance(asset, dict) or asset.get("name") not in ASSET_NAMES
                    or asset["name"] in result):
                raise BackupError("BACKUP_UNEXPECTED_ASSETS")
            _identifier(asset.get("id"))
            result[asset["name"]] = asset
        return result

    def _completed(self, release):
        self._owned(release)
        assets = self._assets(release)
        body = release["body"]
        if body in {MARKER, MARKER + "\n"}:
            return False
        # A damaged completed receipt must never be silently overwritten as partial.
        receipt = _json(body.partition("\n")[2])
        if (receipt.get("schema_version") != 1 or receipt.get("day") != self.day
                or not isinstance(receipt.get("assets"), dict)
                or set(receipt["assets"]) != set(ASSET_NAMES)):
            raise BackupError("BACKUP_RECEIPT_INVALID")
        sums = _checksums(receipt.get("checksums"))
        sums["SHA256SUMS"] = hashlib.sha256(receipt["checksums"].encode()).hexdigest()
        if set(assets) != set(ASSET_NAMES):
            raise BackupError("BACKUP_COMPLETED_RELEASE_DAMAGED")
        for name in ASSET_NAMES:
            expected = receipt["assets"][name]
            if (not isinstance(expected, dict) or expected.get("sha256") != sums[name]
                    or type(expected.get("bytes")) is not int or expected["bytes"] <= 0
                    or not self._matches(assets[name], expected)):
                raise BackupError("BACKUP_COMPLETED_RELEASE_DAMAGED")
        return True

    @staticmethod
    def _matches(asset, expected):
        return (asset.get("state") == "uploaded" and type(asset.get("size")) is int
                and asset["size"] == expected["bytes"]
                and asset.get("digest") == "sha256:" + expected["sha256"])

    def exists(self):
        release = self._release()
        return release is not None and self._completed(release)

    def _create(self):
        try:
            self._request("POST", self.base_path, payload={"tag_name": self.tag,
                "name": f"Radar storage backup {self.day}", "body": MARKER,
                "draft": False, "prerelease": False, "make_latest": "false"})
        except BackupError as error:
            # A lost response / duplicate tag may mean creation already committed.
            if error.code == "BACKUP_AUTHORIZATION_FAILED":
                raise
            release = self._release()
            if release is None:
                raise error from None
            return release
        release = self._release()
        if release is None:
            raise BackupError("BACKUP_CREATE_NOT_CONFIRMED")
        return release

    @staticmethod
    def _local_files(files):
        if not isinstance(files, (list, tuple)) or len(files) != len(ASSET_NAMES):
            raise BackupError("BACKUP_INVALID_FILES")
        paths = {}
        try:
            for value in files:
                path = Path(value)
                if path.name not in ASSET_NAMES or path.name in paths or not path.is_file():
                    raise BackupError("BACKUP_INVALID_FILES")
                paths[path.name] = path
            content = {}
            for name in ASSET_NAMES:
                limit = MAX_ASSET_BYTES if name == "radar.sql.gz" else MAX_RESPONSE_BYTES if name == "manifest.json" else 1024
                with paths[name].open("rb") as handle:
                    raw = handle.read(limit + 1)
                if not raw or len(raw) > limit:
                    raise BackupError("BACKUP_INVALID_FILES")
                content[name] = raw
            checksums = content["SHA256SUMS"].decode("ascii")
            expected = _checksums(checksums)
            _json(content["manifest.json"])
            info = {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} for name, raw in content.items()}
            if any(info[name]["sha256"] != expected[name] for name in ASSET_NAMES[:2]):
                raise BackupError("BACKUP_CHECKSUM_INVALID")
            return content, info, checksums
        except (OSError, ValueError, TypeError, UnicodeError):
            raise BackupError("BACKUP_INVALID_FILES") from None

    def publish(self, files):
        release = self._release()
        if release is not None and self._completed(release):
            return {"status": "skipped", "tag": self.tag, "release_id": release["id"]}
        content, expected, checksums = self._local_files(files)
        if release is None:
            release = self._create()
        if self._completed(release):
            return {"status": "skipped", "tag": self.tag, "release_id": release["id"]}
        release_id = release["id"]
        # SHA256SUMS is always uploaded last. Correct uploads from a lost run are
        # reused; incomplete or differing assets are replaced only on owned partials.
        for name in ASSET_NAMES:
            release = self._release()
            if release is None or release["id"] != release_id:
                raise BackupError("BACKUP_RELEASE_CHANGED")
            if self._completed(release):
                return {"status": "skipped", "tag": self.tag, "release_id": release_id}
            assets = self._assets(release)
            asset = assets.get(name)
            if asset is not None and self._matches(asset, expected[name]):
                continue
            if asset is not None:
                self._request("DELETE", f"{self.base_path}/assets/{asset['id']}", missing_ok=True)
            try:
                self._request("POST", f"{self.base_path}/{release_id}/assets?name={name}",
                              content=content[name], content_type=("application/gzip" if name.endswith(".gz")
                              else "application/json" if name.endswith(".json") else "text/plain"), upload=True)
            except BackupError as error:
                if error.code == "BACKUP_AUTHORIZATION_FAILED":
                    raise
                current = self._release()
                candidate = self._assets(current).get(name) if current is not None else None
                if candidate is None or not self._matches(candidate, expected[name]):
                    raise error from None
        current = self._release()
        if current is None or current["id"] != release_id:
            raise BackupError("BACKUP_RELEASE_CHANGED")
        if self._completed(current):
            return {"status": "skipped", "tag": self.tag, "release_id": release_id}
        assets = self._assets(current)
        if set(assets) != set(ASSET_NAMES) or any(not self._matches(assets[name], expected[name]) for name in ASSET_NAMES):
            raise BackupError("BACKUP_UPLOAD_NOT_CONFIRMED")
        receipt = {"schema_version": 1, "day": self.day, "checksums": checksums, "assets": expected}
        self._request("PATCH", f"{self.base_path}/{release_id}", payload={
            "body": MARKER + "\n" + json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "make_latest": "false"})
        current = self._release()
        if current is None or not self._completed(current):
            raise BackupError("BACKUP_COMPLETION_NOT_CONFIRMED")
        return {"status": "published", "tag": self.tag, "release_id": release_id,
                "assets": {name: expected[name]["bytes"] for name in ASSET_NAMES}}


def daily_backup_exists(repository, day, token, **client_options) -> bool:
    return BackupReleaseClient(repository, day, token, **client_options).exists()


def publish_daily_backup(repository, day, token, files: list[Path], **client_options) -> dict:
    return BackupReleaseClient(repository, day, token, **client_options).publish(files)
