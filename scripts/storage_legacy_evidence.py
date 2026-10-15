"""Preserve bounded, live Twitch admission evidence before primary activation.

Inventory comes from the final imported DB snapshots: Steam master, current
intake state, current official queue/results and published content records.
Reports, attempt events, Git history and excluded intake rows are not scanned.
Only tracking/discovery at each explicitly recorded frontend Git commit are
imported; a missing revision never means the current frontend snapshot.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time

from radar_core.domain.twitch_admission import normalize_twitch_admission, validate_twitch_snapshot
from radar_core.storage import primary_store
from radar_core.storage.sources import validate_data
from radar_core.storage.sql import StorageError

FRONTEND = "danielet087/game-trend-radar@main"
BACKEND = "danielet087/game-trend-radar-backend@main"
EVIDENCE_PATHS = ("data/twitch_tracking.json", "data/twitch_steam_discovery.json")
MASTER = "data/steam_upcoming_master.json"
INTAKE = "data/twitch_steam_import_state.json"
CHECKPOINT = "experiments/steam_official_daily_catchup/checkpoint.json"
BACKEND_PATHS = (MASTER, INTAKE, CHECKPOINT)
FRONTEND_PATHS = ("data/games/", "data/steam_upcoming.json", "data/catalog.json")
MAX_REVISIONS = 128
MAX_REFERENCES = 10000
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024
MAX_GITHUB_CALLS = 512
MAX_SECONDS = 900


def _error(code):
    raise StorageError(code, "Live Twitch evidence migration did not complete; writers must remain paused.")


def _json(raw, path):
    validate_data(raw, path)
    value = json.loads(raw)
    if not isinstance(value, dict):
        _error("INVALID_ADMISSION_INVENTORY")
    return value


def _selected(path, scopes):
    if path.startswith("data/games/"):
        return "data/games/" in scopes and re.fullmatch(r"data/games/[1-9][0-9]*\.json", path) is not None
    return any(path == scope or (scope.endswith("/") and path.startswith(scope)) for scope in scopes)


def _documents(client, source, scopes, required):
    # Inspect size/count before fetching data objects. Pin this read to exactly
    # the inspected manifest, even if another maintenance run changed a head.
    head, _ = primary_store.read_files(client, source, ())
    selected = {path: entry for path, entry in head["manifest"].items() if _selected(path, scopes)}
    if not set(required).issubset(selected):
        _error("INCOMPLETE_ADMISSION_INVENTORY")
    if len(selected) > MAX_REFERENCES or sum(entry["raw_bytes"] for entry in selected.values()) > MAX_INPUT_BYTES:
        _error("ADMISSION_INVENTORY_LIMIT")
    _, files = primary_store.read_files(client, source, tuple(selected), revision=head["manifest_digest"])
    if set(files) != set(selected):
        _error("INCOMPLETE_ADMISSION_INVENTORY")
    return {path: _json(raw, path) for path, raw in files.items()}


def inventory(client):
    """Return normalized unique live proofs grouped by their immutable Git SHA."""
    backend = _documents(client, BACKEND, BACKEND_PATHS, BACKEND_PATHS)
    frontend = _documents(client, FRONTEND, FRONTEND_PATHS, ("data/steam_upcoming.json", "data/catalog.json"))
    proofs, references = {}, 0

    def row(value, appid=None, *, depth=0):
        nonlocal references
        if not isinstance(value, dict):
            _error("INVALID_ADMISSION_INVENTORY")
        aid = value.get("appid", appid)
        if value.get("twitch_admission") is not None:
            references += 1
            if references > MAX_REFERENCES:
                _error("ADMISSION_INVENTORY_LIMIT")
            proof = normalize_twitch_admission(value["twitch_admission"], aid)
            if proof is None:
                _error("INVALID_LIVE_ADMISSION")
            revision = proof.get("source_frontend_commit")
            if revision:
                proofs.setdefault(revision, {})[json.dumps(proof, sort_keys=True)] = proof
                if len(proofs) > MAX_REVISIONS:
                    _error("LEGACY_EVIDENCE_REVISION_LIMIT")
        # Only producer-owned current candidate fields; never arbitrary nested
        # logs, attempts, normal-candidate backups or archival metadata.
        if depth < 2:
            for name in ("follower_candidate", "steam_candidate"):
                if value.get(name) is not None:
                    row(value[name], aid, depth=depth + 1)

    def rows(value):
        if isinstance(value, list):
            return ((None, item) for item in value)
        if isinstance(value, dict):
            return value.items()
        _error("INVALID_ADMISSION_INVENTORY")

    for aid, value in rows(backend[MASTER].get("games")):
        row(value, aid)
    for aid, value in rows(backend[INTAKE].get("games")):
        if not isinstance(value, dict):
            _error("INVALID_ADMISSION_INVENTORY")
        if value.get("status") in {"accepted", "pending"}:
            row(value, aid)
    for field in ("pending_candidates", "unresolved_candidates", "official_results"):
        for aid, value in rows(backend[CHECKPOINT].get(field, {})):
            # official_results is the current per-AppID result map, used by
            # dispatch retries; its append-only attempt_events are excluded.
            row(value, aid)
    for path, document in frontend.items():
        if path.startswith("data/games/"):
            row(document)
        else:
            for aid, value in rows(document.get("games")):
                row(value, aid)
    return {revision: list(values.values()) for revision, values in sorted(proofs.items())}, references


class GitEvidence:
    """Read fixed Git objects through the controller's existing GitHub client."""
    def __init__(self, github, check_budget):
        self.github, self.check_budget = github, check_budget
        self.calls, self.total_bytes, self.blobs = 0, 0, {}

    def call(self, path):
        self.check_budget()
        self.calls += 1
        if self.calls > MAX_GITHUB_CALLS:
            _error("LEGACY_EVIDENCE_REQUEST_LIMIT")
        result = self.github.call("GET", "game-trend-radar", path)
        self.check_budget()
        if not isinstance(result, dict):
            _error("INVALID_GIT_EVIDENCE")
        return result

    def read(self, revision):
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            _error("INVALID_GIT_EVIDENCE")
        commit = self.call("/git/commits/" + revision)
        tree = commit.get("tree", {}).get("sha") if isinstance(commit.get("tree"), dict) else None
        if commit.get("sha") != revision or not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40}", tree):
            _error("INVALID_GIT_EVIDENCE")
        result = self.call("/git/trees/" + tree + "?recursive=1")
        if result.get("sha") != tree or result.get("truncated") is not False or not isinstance(result.get("tree"), list):
            _error("INCOMPLETE_GIT_EVIDENCE_TREE")
        entries = {}
        for entry in result["tree"]:
            if not isinstance(entry, dict):
                _error("INVALID_GIT_EVIDENCE")
            path = entry.get("path")
            if path not in EVIDENCE_PATHS:
                continue
            if path in entries or entry.get("mode") not in {"100644", "100755"} or entry.get("type") != "blob":
                _error("INVALID_GIT_EVIDENCE")
            if (not isinstance(entry.get("sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", entry["sha"])
                    or type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES):
                _error("LEGACY_EVIDENCE_SIZE_LIMIT")
            entries[path] = entry
        if set(entries) != set(EVIDENCE_PATHS):
            _error("INCOMPLETE_GIT_EVIDENCE")
        files = {}
        for path in EVIDENCE_PATHS:
            entry = entries[path]
            digest = entry["sha"]
            if digest not in self.blobs:
                blob = self.call("/git/blobs/" + digest)
                if (blob.get("sha") != digest or blob.get("encoding") != "base64"
                        or type(blob.get("size")) is not int or blob["size"] != entry["size"]
                        or not isinstance(blob.get("content"), str)):
                    _error("INVALID_GIT_EVIDENCE")
                try:
                    raw = base64.b64decode("".join(blob["content"].split()), validate=True)
                except (ValueError, UnicodeError):
                    _error("INVALID_GIT_EVIDENCE")
                if len(raw) != entry["size"] or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != digest:
                    _error("GIT_EVIDENCE_DIGEST_MISMATCH")
                self.total_bytes += len(raw)
                if self.total_bytes > MAX_EVIDENCE_BYTES:
                    _error("LEGACY_EVIDENCE_SIZE_LIMIT")
                self.blobs[digest] = raw
            files[path] = self.blobs[digest]
            if len(files[path]) != entry["size"]:
                _error("GIT_EVIDENCE_DIGEST_MISMATCH")
            validate_data(files[path], path)
        return files


def migrate_legacy_evidence(client, github, *, max_seconds=MAX_SECONDS, monotonic=time.monotonic):
    """Fail closed before activation; safe to retry after partial activation."""
    deadline = monotonic() + max_seconds

    def check_budget():
        if monotonic() >= deadline:
            _error("LEGACY_EVIDENCE_TIME_LIMIT")

    proofs, references = inventory(client)
    reader = GitEvidence(github, check_budget)
    active = primary_store.primary_status(client, FRONTEND)["active"]
    imported, reused = 0, 0
    for revision, admissions in proofs.items():
        check_budget()
        try:
            head, files = primary_store.read_files(client, FRONTEND, EVIDENCE_PATHS, revision=revision)
        except StorageError as error:
            if error.code not in {"REVISION_NOT_FOUND", "HISTORY_PRUNED", "MISSING_OBJECT", "MISSING_CHUNK"}:
                raise
            if active:
                _error("PRIMARY_LEGACY_EVIDENCE_MISSING")
            files, head = reader.read(revision), None
        if set(files) != set(EVIDENCE_PATHS):
            _error("INCOMPLETE_GIT_EVIDENCE")
        registry, discovery = (_json(files[path], path) for path in EVIDENCE_PATHS)
        if not all(validate_twitch_snapshot(proof, registry, discovery) for proof in admissions):
            _error("LEGACY_ADMISSION_EVIDENCE_MISMATCH")
        check_budget()
        if active:
            # A cutover retry after activation may only retain an existing,
            # verified immutable DB revision. Never download new Git authority.
            primary_store.pin_evidence(client, FRONTEND, revision=head["manifest_digest"],
                                       paths=EVIDENCE_PATHS, evidence_id="legacy-cutover-" + revision)
            reused += 1
        else:
            primary_store.import_legacy_evidence(client, FRONTEND, git_revision=revision, files=files)
            imported += 1
    check_budget()
    report = {"stage": "legacy_admission_evidence", "status": "passed", "references": references,
              "git_revisions": len(proofs), "imported": imported, "retained": reused,
              "github_requests": reader.calls, "downloaded_bytes": reader.total_bytes}
    print(json.dumps(report), flush=True)
    return report
