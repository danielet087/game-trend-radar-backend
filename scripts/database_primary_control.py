"""Coordinate the reviewed database cutover without exposing credential values.

Only the fixed, user-owned runtime repositories below can receive the two
Turso secrets. Every API error is reduced to a status code. A failed cutover
after pausing leaves writers paused until it is retried or explicitly resumed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from radar_core.storage.sql import NoRedirect, StorageError, TursoClient

OWNER = "danielet087"
RUNTIMES = ("game-trend-radar-backend", "game-trend-radar", "game-trend-radar-twitch-backend",
            "game-trend-radar-content-backend", "game-trend-radar-youtube-backend")
DEPLOYMENTS = {
    "game-trend-radar-backend": ("steam-two-phase.yml", "steam-official-daily-catchup-250.yml", "steam-public-growth.yml"),
    "game-trend-radar": ("radar-insights.yml",),
    "game-trend-radar-twitch-backend": ("collect.yml", "collect-nintendo.yml"),
    "game-trend-radar-content-backend": ("steam-content-enrichment-dispatch.yml", "steam-catalog-reconcile.yml", "steam-description-refresh.yml"),
    "game-trend-radar-youtube-backend": ("collect.yml",),
    "game-trend-radar-igdb-backend": (),
}
PRIMARY_FLAG = "RADAR_DATABASE_PRIMARY"
PAUSE_FLAG = "RADAR_DATABASE_MIGRATION_PAUSE"


class ControlError(RuntimeError):
    def __init__(self, code, *, repository=None, status=None):
        self.code, self.repository, self.status = code, repository, status
        super().__init__(code)


class GitHub:
    def __init__(self, token):
        if not token or any(c.isspace() for c in token):
            raise ControlError("MISSING_GITHUB_CREDENTIAL")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def call(self, method, repository, path, data=None, *, missing=False):
        if repository not in DEPLOYMENTS or not path.startswith("/"):
            raise ControlError("INVALID_DESTINATION")
        url = f"https://api.github.com/repos/{OWNER}/{repository}{path}"
        raw = None if data is None else json.dumps(data, separators=(",", ":")).encode()
        for attempt in range(3):
            request = Request(url, data=raw, method=method, headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "radar-database-cutover",
            })
            try:
                with self.opener.open(request, timeout=20) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ControlError("RESPONSE_TOO_LARGE", repository=repository)
                return json.loads(body) if body else {}
            except HTTPError as error:
                status = error.code
                error.close()
                if missing and status == 404:
                    return None
                if attempt < 2 and (status == 429 or status >= 500):
                    time.sleep(2 ** attempt)
                    continue
                raise ControlError("GITHUB_PERMISSION_OR_REQUEST_FAILED", repository=repository, status=status) from None
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise ControlError("GITHUB_NETWORK_FAILED", repository=repository) from None
                time.sleep(2 ** attempt)
        raise ControlError("GITHUB_NETWORK_FAILED", repository=repository)

    def variable(self, repository, name):
        result = self.call("GET", repository, "/actions/variables/" + name, missing=True)
        return None if result is None else result["value"]

    def set_variable(self, repository, name, value):
        prior = self.variable(repository, name)
        if prior != value:
            path = "/actions/variables" + ("/" + name if prior is not None else "")
            self.call("PATCH" if prior is not None else "POST", repository, path,
                      {"name": name, "value": value})
        if self.variable(repository, name) != value:
            raise ControlError("VARIABLE_NOT_ACKNOWLEDGED", repository=repository)

    def file(self, repository, path):
        result = self.call("GET", repository, "/contents/" + path + "?ref=main")
        return base64.b64decode(result["content"], validate=False).decode("utf-8")


def clients():
    tokens = list(dict.fromkeys(os.environ.get(name, "").strip()
                               for name in ("FRONTEND_REPO_TOKEN", "CONTENT_BACKEND_TOKEN")))
    candidates = [GitHub(token) for token in tokens if token]
    if not candidates:
        raise ControlError("MISSING_GITHUB_CREDENTIAL")
    chosen, keys = {}, {}
    # Complete capability discovery before distributing any credential.
    for repository in RUNTIMES:
        last = None
        for client in candidates:
            try:
                key = client.call("GET", repository, "/actions/secrets/public-key")
                client.call("GET", repository, "/actions/variables?per_page=100")
                chosen[repository], keys[repository] = client, key
                break
            except ControlError as error:
                last = error
        else:
            raise last or ControlError("RUNTIME_CONFIGURATION_UNAVAILABLE", repository=repository)
    return chosen, keys


def prepare(chosen, keys):
    from nacl.public import PublicKey, SealedBox
    secrets = {name: os.environ.get(name, "").strip()
               for name in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")}
    if not all(secrets.values()):
        raise ControlError("MISSING_TURSO_CREDENTIAL")
    # An already-active deployment must not be reset to legacy by a prepare retry.
    for repository, client in chosen.items():
        for name in (PRIMARY_FLAG, PAUSE_FLAG):
            if client.variable(repository, name) is None:
                client.set_variable(repository, name, "0")
        if repository == "game-trend-radar-backend":
            continue
        key = keys[repository]
        box = SealedBox(PublicKey(base64.b64decode(key["key"])))
        for name, value in secrets.items():
            encoded = base64.b64encode(box.encrypt(value.encode())).decode("ascii")
            client.call("PUT", repository, "/actions/secrets/" + name,
                        {"key_id": key["key_id"], "encrypted_value": encoded})
    return {"status": "passed", "configured_runtimes": list(chosen), "credential_names": sorted(secrets)}


def validate_deployments(chosen, revision, workflow_hashes):
    import re
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ControlError("CORE_REVISION_REQUIRED")
    if not isinstance(workflow_hashes, dict) or set(workflow_hashes) != set(RUNTIMES):
        raise ControlError("REVIEWED_WORKFLOW_HASHES_REQUIRED")
    for repository, workflows in DEPLOYMENTS.items():
        client = chosen.get(repository) or chosen["game-trend-radar-twitch-backend"]
        for workflow in workflows:
            body = client.file(repository, ".github/workflows/" + workflow)
            if PRIMARY_FLAG not in body or PAUSE_FLAG not in body:
                raise ControlError("WRITER_NOT_MIGRATED", repository=repository)
            expected = workflow_hashes.get(repository, {}).get(workflow)
            if expected != hashlib.sha256(body.encode()).hexdigest():
                raise ControlError("WORKFLOW_REVIEW_MISMATCH", repository=repository)
        if repository != "game-trend-radar-backend":
            dependency = client.file(repository, "requirements-core.txt")
            if ".git@" + revision + "#subdirectory=packages/radar-core" not in dependency:
                raise ControlError("CORE_PIN_MISMATCH", repository=repository)
    # Also verify every reviewed legacy/manual writer and maintenance gate.
    for repository, workflows in workflow_hashes.items():
        if not isinstance(workflows, dict) or not workflows:
            raise ControlError("REVIEWED_WORKFLOW_HASHES_REQUIRED", repository=repository)
        for filename, expected in workflows.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.yml", filename):
                raise ControlError("INVALID_WORKFLOW_PATH", repository=repository)
            body = chosen[repository].file(repository, ".github/workflows/" + filename)
            if hashlib.sha256(body.encode()).hexdigest() != expected or PAUSE_FLAG not in body:
                raise ControlError("WORKFLOW_REVIEW_MISMATCH", repository=repository)
            if filename not in DEPLOYMENTS[repository] and filename != "source-storage-sync.yml":
                if PRIMARY_FLAG + " != '1'" not in body:
                    raise ControlError("LEGACY_WRITER_NOT_FENCED", repository=repository)


def drain(chosen, *, deadline_seconds=1800):
    deadline = time.monotonic() + deadline_seconds
    own_run = int(os.environ.get("GITHUB_RUN_ID", "0"))
    while True:
        active = []
        for repository, client in chosen.items():
            for status in ("in_progress", "queued", "waiting", "pending", "requested"):
                response = client.call("GET", repository, f"/actions/runs?status={status}&per_page=100")
                runs, total = response.get("workflow_runs"), response.get("total_count")
                if (not isinstance(runs, list) or type(total) is not int or total < 0
                        or total > len(runs) or len(runs) > 100):
                    raise ControlError("INCOMPLETE_WRITER_INVENTORY", repository=repository)
                for run in runs:
                    if repository == "game-trend-radar-backend" and run["id"] == own_run:
                        continue
                    # Finite read-only builds/tests need not block storage authority.
                    path = run.get("path", "").split("@", 1)[0]
                    if path.endswith(("frontend-build.yml", "python-contracts.yml", "backend-contracts.yml",
                                      "test-radar-core.yml", "test.yml")):
                        continue
                    active.append({"repository": repository, "run_id": run["id"]})
        if not active:
            return
        print(json.dumps({"stage": "waiting_for_existing_writers", "runs": active}), flush=True)
        if time.monotonic() >= deadline:
            raise ControlError("WRITER_DRAIN_TIMEOUT")
        time.sleep(30)


def cutover(chosen, revision, workflow_hashes):
    from radar_core.storage import documents, primary_store
    from radar_core.storage.sources import SOURCES, source_id
    from scripts.storage_sync import ingest
    from scripts.storage_legacy_evidence import migrate_legacy_evidence
    validate_deployments(chosen, revision, workflow_hashes)
    for repository, client in chosen.items():
        client.set_variable(repository, PAUSE_FLAG, "1")
    drain(chosen)
    db = TursoClient.from_env(budget_seconds=1500)
    documents.initialize(db)
    results, errors = ingest(db)
    if errors:
        raise ControlError("FINAL_IMPORT_FAILED")
    migrate_legacy_evidence(db, chosen["game-trend-radar"])
    for spec in SOURCES:
        primary_store.activate_primary(db, source_id(spec))
    for repository, client in chosen.items():
        client.set_variable(repository, PRIMARY_FLAG, "1")
    # This final pass precedes resume; a partial flag update cannot run writers.
    if any(client.variable(repository, PRIMARY_FLAG) != "1" for repository, client in chosen.items()):
        raise ControlError("INCOMPLETE_RUNTIME_ACTIVATION")
    for repository, client in chosen.items():
        client.set_variable(repository, PAUSE_FLAG, "0")
    return {"status": "passed", "primary_sources": [source_id(spec) for spec in SOURCES],
            "runtimes": list(chosen), "imported_sources": results}


def resume(chosen):
    from radar_core.storage import documents, primary_store
    from radar_core.storage.sources import SOURCES, source_id
    db = TursoClient.from_env()
    documents.initialize(db)
    active = [primary_store.primary_status(db, source_id(spec))["active"] for spec in SOURCES]
    if any(active) and not all(active):
        raise ControlError("PARTIAL_AUTHORITY_REQUIRES_CUTOVER_RETRY")
    wanted = "1" if all(active) else "0"
    for repository, client in chosen.items():
        client.set_variable(repository, PRIMARY_FLAG, wanted)
    for repository, client in chosen.items():
        client.set_variable(repository, PAUSE_FLAG, "0")
    return {"status": "passed", "primary_enabled": wanted == "1"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("prepare", "cutover", "resume"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    configuration = json.loads(Path("config/database-rollout.json").read_text())
    mode = args.mode or os.environ.get("REQUESTED_MODE") or configuration["phase"]
    report = {"mode": mode, "status": "failed"}
    try:
        chosen, keys = clients()
        if mode == "prepare":
            report.update(prepare(chosen, keys))
        elif mode == "cutover":
            report.update(cutover(chosen, configuration.get("core_revision"), configuration.get("workflow_hashes")))
        elif mode == "resume":
            report.update(resume(chosen))
        else:
            raise ControlError("INVALID_MODE")
    except (ControlError, StorageError) as error:
        report.update(error=error.code)
        if getattr(error, "repository", None):
            report["repository"] = error.repository
        if getattr(error, "status", None):
            report["http_status"] = error.status
    except Exception:
        report.update(error="UNEXPECTED_CONTROL_ERROR")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("## Database primary cutover\n\n```json\n" + json.dumps(report, indent=2) + "\n```\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
