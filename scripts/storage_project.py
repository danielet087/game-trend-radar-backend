"""Repair pending public Git projections from authoritative database snapshots."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from radar_core.storage import primary_store
from radar_core.storage.sources import SOURCES, source_id
from radar_core.storage.sql import StorageError, TursoClient

BACKEND = "danielet087/game-trend-radar-backend"
TOTAL_SECONDS = 480
MAX_ATTEMPTS = 3
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class Deadline:
    def __init__(self, seconds=TOTAL_SECONDS, *, clock=time.monotonic):
        self.clock, self.end = clock, clock() + seconds

    def remaining(self, limit):
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise StorageError("TIME_BUDGET_EXCEEDED", "Projection maintenance exceeded its time budget.")
        return min(limit, remaining)


def _code(error):
    code = error.code if isinstance(error, StorageError) else "PROJECTION_FAILED"
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,64}", code) else "PROJECTION_FAILED"


def _git_environment(environment, askpass=None, token=None):
    # Never inherit trace/config injection or the database credentials into Git.
    clean = {key: value for key, value in environment.items()
             if not key.startswith(("GIT_", "TURSO_", "CLOUDFLARE_"))
             and key not in {"GH_TOKEN", "GITHUB_TOKEN", "FRONTEND_REPO_TOKEN", "RADAR_STORAGE_GIT_TOKEN"}}
    clean.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                 GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(askpass) if askpass else "/bin/false",
                 GIT_CONFIG_COUNT="0")
    if token is not None:
        clean["RADAR_STORAGE_GIT_TOKEN"] = token
    return clean


def _git(args, *, root=None, environment, deadline, check=True):
    command = ["git", "-c", "credential.helper=", "-c", "credential.username=x-access-token",
               "-c", "core.hooksPath=" + os.devnull, "-c", "http.followRedirects=false",
               "-c", "http.extraheader=", *args]
    try:
        result = subprocess.run(command, cwd=root, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=deadline.remaining(60))
    except (OSError, subprocess.TimeoutExpired):
        raise StorageError("GIT_PROJECTION_TIMEOUT", "Git projection did not complete within its limit.") from None
    if check and result.returncode:
        raise StorageError("GIT_PROJECTION_FAILED", "The public Git projection could not be updated.")
    return result


def _hydrate(root, source, *, environment, deadline):
    # A child process bounds both database hydration and its local Git commands.
    # The core CLI invokes hydrate(..., clean_baseline=True); no business
    # collector or Git-to-database importer is involved.
    clean = _git_environment(environment)
    clean.update({key: value for key, value in environment.items() if key.startswith("TURSO_")})
    try:
        result = subprocess.run([sys.executable, "-m", "radar_core.storage.primary", "hydrate",
                                 "--root", str(root), "--source", source], env=clean,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                                timeout=deadline.remaining(120))
    except (OSError, subprocess.TimeoutExpired):
        raise StorageError("HYDRATION_TIMEOUT", "Database hydration did not complete within its limit.") from None
    if result.returncode:
        # Neither the remote response nor child stderr belongs in the report.
        raise StorageError("HYDRATION_FAILED", "The authoritative snapshot could not be projected.")
    try:
        metadata = json.loads((root / ".git" / "radar-primary.json").read_bytes())
        head = metadata["head"]
        if (metadata["source_id"] != source or type(head["generation"]) is not int
                or head["generation"] <= 0 or not _DIGEST.fullmatch(head["manifest_digest"])):
            raise ValueError
        return head
    except (OSError, KeyError, TypeError, ValueError):
        raise StorageError("INVALID_PROJECTION_HEAD", "Hydration did not identify a valid database snapshot.") from None


def _askpass(directory):
    path = directory / "askpass.sh"
    path.write_text("#!/bin/sh\ncase \"$1\" in\n"
                    "  *Username*) printf '%s\\n' x-access-token ;;\n"
                    "  *Password*) printf '%s\\n' \"$RADAR_STORAGE_GIT_TOKEN\" ;;\n"
                    "  *) exit 1 ;;\nesac\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def project_source(database, spec, *, environment, deadline):
    source = source_id(spec)
    result = {"source_id": source}
    if not primary_store.primary_status(database, source).get("active"):
        return {**result, "status": "inactive"}
    pending = primary_store.projection_pending(database, source)
    if not pending:
        return {**result, "status": "up_to_date", "pending": 0}
    token_name = "GH_TOKEN" if spec["repository"] == BACKEND else "FRONTEND_REPO_TOKEN"
    token = environment.get(token_name, "").strip()
    if not token or "\n" in token or "\r" in token:
        raise StorageError("MISSING_GIT_CREDENTIAL", "The projection credential is unavailable.")
    with tempfile.TemporaryDirectory(prefix="radar-storage-project-") as temporary:
        directory = Path(temporary)
        root = directory / "repository"
        public_environment = _git_environment(environment)
        push_environment = _git_environment(environment, _askpass(directory), token)
        _git(["clone", "--depth", "1", "--no-tags", "--single-branch", "--branch", spec["branch"],
              "https://github.com/" + spec["repository"] + ".git", str(root)],
             environment=public_environment, deadline=deadline)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                _git(["fetch", "--no-tags", "--depth", "1", "origin", "refs/heads/" + spec["branch"]],
                     root=root, environment=public_environment, deadline=deadline)
                _git(["reset", "--hard", "FETCH_HEAD"], root=root,
                     environment=public_environment, deadline=deadline)
            head = _hydrate(root, source, environment=environment, deadline=deadline)
            revision = _git(["rev-parse", "HEAD"], root=root, environment=public_environment,
                            deadline=deadline).stdout.decode("ascii").strip()
            if not _SHA.fullmatch(revision):
                raise StorageError("INVALID_GIT_REVISION", "The projection lacks a real Git revision.")
            pushed = _git(["push", "origin", "HEAD:refs/heads/" + spec["branch"]], root=root,
                          environment=push_environment, deadline=deadline, check=False)
            if pushed.returncode:
                continue
            acknowledged = 0
            # Fetch pending again: include operations committed before this
            # snapshot was read, and leave later generations untouched.
            for operation in primary_store.projection_pending(database, source):
                if operation["generation"] <= head["generation"]:
                    deadline.remaining(1)
                    primary_store.acknowledge_projection(database, source, operation["operation_id"], revision,
                        generation=head["generation"], manifest_digest=head["manifest_digest"])
                    acknowledged += 1
            return {**result, "status": "projected", "attempts": attempt,
                    "generation": head["generation"], "manifest_digest": head["manifest_digest"],
                    "git_revision": revision, "acknowledged": acknowledged,
                    "pending": len(primary_store.projection_pending(database, source))}
    raise StorageError("GIT_PUSH_REJECTED", "Projection push did not succeed within three attempts.")


def run(*, environment=None, database=None, deadline=None, sources=SOURCES):
    environment = dict(os.environ if environment is None else environment)
    deadline = deadline or Deadline()
    if environment.get("RADAR_DATABASE_PRIMARY") != "1":
        raise StorageError("PRIMARY_MODE_REQUIRED", "Projection maintenance requires database primary mode.")
    database = database or TursoClient(environment.get("TURSO_DATABASE_URL", ""),
                                     environment.get("TURSO_AUTH_TOKEN", ""),
                                     budget_seconds=deadline.remaining(TOTAL_SECONDS))
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "passed", "sources": [], "errors": []}
    for spec in sources:
        try:
            deadline.remaining(1)
            report["sources"].append(project_source(database, spec, environment=environment, deadline=deadline))
        except Exception as error:
            failure = {"source_id": source_id(spec), "code": _code(error)}
            report["sources"].append({**failure, "status": "error"})
            report["errors"].append(failure)
    if report["errors"]:
        report["status"] = "degraded"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = run()
    except Exception as error:
        report = {"status": "degraded", "sources": [], "errors": [{"stage": "initialization", "code": _code(error)}]}
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    args.report_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
