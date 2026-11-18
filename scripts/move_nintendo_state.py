"""Move the IGDB candidate ledger out of the frontend source.

``data/nintendo_master.json`` (about 7 MB, replaced by every daily IGDB run)
is private collection state. It moves to the database-only source
``danielet087/game-trend-radar-igdb-backend@nintendo-state`` so that it stops
growing the frontend's Git history and leaves the site. The IGDB publisher
follows the frontend copy: while it exists the frontend copy is authoritative
and every run mirrors it to the database-only source; once it is gone the
database-only source is authoritative.

Actions (each only reports unless --apply is given):

* bootstrap: copy the frontend database copy into a new database-only source
  and read it back. An existing source is never replaced.
* retire: check that the database-only copy, the frontend database copy and
  the frontend Git tip are the same bytes, then delete the frontend copy in
  one frontend publication (database and Git). Git history is not rewritten.
* restore: put the database-only copy back into the frontend source, which
  returns the publisher to the mirror stage.

Only names, digests and byte counts are printed; never credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from radar_core.storage import documents, primary_store
from radar_core.storage.sources import SOURCES, source_id
from radar_core.storage.sql import StorageError, TursoClient

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_REPOSITORY = "danielet087/game-trend-radar"
FRONTEND = FRONTEND_REPOSITORY + "@main"
STATE = "danielet087/game-trend-radar-igdb-backend@nintendo-state"
STATE_SPEC = next(spec for spec in SOURCES if source_id(spec) == STATE)
PATH = "data/nintendo_master.json"
ACTIONS = ("bootstrap", "retire", "restore")
ATTEMPTS = 6
RETRY_SECONDS = 30


def digest(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _copy(client, source):
    if documents._head(client, source) is None:
        return None, None
    head, files = primary_store.read_files(client, source, paths=[PATH])
    return head, files.get(PATH)


def bootstrap(client, *, apply=False):
    state_head, state_raw = _copy(client, STATE)
    frontend_head, frontend_raw = _copy(client, FRONTEND)
    if state_head is not None:
        return {"status": "exists", "generation": state_head["generation"],
                "active": primary_store.primary_status(client, STATE)["active"],
                "state_sha256": digest(state_raw), "frontend_sha256": digest(frontend_raw)}
    if frontend_raw is None:
        raise StorageError("NO_NINTENDO_STATE", "The frontend source holds no IGDB candidate ledger to copy.")
    summary = {"source_id": STATE, "bytes": len(frontend_raw), "sha256": digest(frontend_raw),
               "from_generation": frontend_head["generation"]}
    if not apply:
        return {"status": "dry_run", **summary}
    revision = f"bootstrap:{FRONTEND}:generation-{frontend_head['generation']}"
    head = primary_store.bootstrap_primary(client, STATE, {PATH: frontend_raw}, revision)
    _, copied = primary_store.read_files(client, STATE)
    if copied != {PATH: frontend_raw}:
        raise StorageError("BOOTSTRAP_MISMATCH", "The database-only source does not read back the copied bytes.")
    return {"status": "bootstrapped", **summary, "generation": head["generation"], "active": head["active"]}


def git_tip_bytes(root):
    """The bytes at the fetched remote tip, or None without the file.

    A primary-mode refresh commits the database snapshot locally, so HEAD is
    not the published Git tip; the remote-tracking ref is.
    """
    result = subprocess.run(["git", "show", "refs/remotes/origin/main:" + PATH], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=120)
    return result.stdout if result.returncode == 0 else None


def check_copies(root, client):
    """Raise unless the three copies are the same bytes; runs on every publication attempt."""
    frontend_raw = (root / PATH).read_bytes() if (root / PATH).exists() else None
    git_raw = git_tip_bytes(root)
    state_head, state_raw = _copy(client, STATE)
    report = {"state_generation": state_head and state_head["generation"], "bytes": len(state_raw or b""),
              "state_sha256": digest(state_raw), "frontend_database_sha256": digest(frontend_raw),
              "frontend_git_sha256": digest(git_raw)}
    if state_raw is None or not state_raw == frontend_raw == git_raw:
        raise StorageError("COPIES_DIFFER", "The IGDB ledger copies differ: " + json.dumps(report))
    return report


def retire_callback(root, client, reports, *, apply):
    if not (root / PATH).exists() and git_tip_bytes(root) is None:
        reports.append({"status": "already_retired"})
        return
    report = check_copies(root, client)
    if apply:
        (root / PATH).unlink()
    reports.append({"status": "retired" if apply else "identical", **report})


def restore_callback(root, client, reports):
    if (root / PATH).exists():
        reports.append({"status": "not_retired", "frontend_sha256": digest((root / PATH).read_bytes())})
        return
    state_head, state_raw = _copy(client, STATE)
    if state_raw is None:
        raise StorageError("NO_NINTENDO_STATE", "The database-only source holds no IGDB candidate ledger.")
    (root / PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / PATH).write_bytes(state_raw)
    reports.append({"status": "restored", "state_generation": state_head["generation"],
                    "bytes": len(state_raw), "sha256": digest(state_raw)})


def _revision(action):
    from radar_core.publication import snapshot_revision
    return snapshot_revision({"schema": 1, "action": action, "code": os.environ.get("GITHUB_SHA", "local"),
                              "run": os.environ.get("GITHUB_RUN_ID", "0"),
                              "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1")})


def publish(client, action, *, apply, repository):
    """retire or restore on the frontend publication lane; a dry run only refreshes and checks."""
    from radar_core.publication import publish_with_retry
    reports = []
    if action == "retire":
        callback = lambda root: retire_callback(root, client, reports, apply=apply)  # noqa: E731
        message = "data: move the IGDB candidate ledger to its database source"
    else:
        callback = lambda root: restore_callback(root, client, reports)  # noqa: E731
        message = "data: return the IGDB candidate ledger to the frontend source"
    if not apply:
        repository.refresh((PATH,))
        if action == "retire":
            callback(repository.root)
        else:
            exists = (repository.root / PATH).exists()
            reports.append({"status": "not_retired" if exists else "dry_run",
                            "state_sha256": digest(_copy(client, STATE)[1])})
        return reports[-1]
    revision = _revision(action)
    receipt = publish_with_retry(repository, callback, paths=(PATH,), message=message,
                                 input_revision=revision, payload_revision=revision)
    return {**reports[-1], "published_revision": receipt.published_revision, "changed": receipt.changed,
            "attempts": receipt.attempts}


def frontend_checkout(directory):
    """A disposable frontend checkout whose pushes authenticate through the askpass wrapper."""
    from radar_backend.publication.steam import frontend_repository
    if os.environ.get("RADAR_DATABASE_PRIMARY") != "1":
        # Without the database primary a publication would only change Git.
        raise StorageError("PRIMARY_REQUIRED", "The frontend publication requires RADAR_DATABASE_PRIMARY=1.")
    if not os.environ.get("FRONTEND_REPO_TOKEN", "").strip():
        raise StorageError("MISSING_GIT_CREDENTIAL", "FRONTEND_REPO_TOKEN is not configured.")
    root = Path(directory) / "frontend"
    subprocess.run(["git", "clone", "--depth", "1", "--no-tags", "--branch", "main",
                    f"https://github.com/{FRONTEND_REPOSITORY}.git", str(root)],
                   check=True, stdout=subprocess.DEVNULL, timeout=300)
    return frontend_repository(root, auth_script=ROOT / "scripts/git_frontend_auth.sh")


def run(client, action, *, apply=False, repository=None, attempts=ATTEMPTS, sleep=time.sleep):
    if action not in ACTIONS:
        raise ValueError("unknown action")
    documents.initialize(client)
    for attempt in range(1, attempts + 1):
        try:
            if action == "bootstrap":
                return {**bootstrap(client, apply=apply), "attempts": attempt}
            if repository is not None:
                return {**publish(client, action, apply=apply, repository=repository), "attempts": attempt}
            with tempfile.TemporaryDirectory(prefix="radar-nintendo-state.") as directory:
                return {**publish(client, action, apply=apply, repository=frontend_checkout(directory)),
                        "attempts": attempt}
        except StorageError as error:
            # A collector holding the storage lease finishes within minutes.
            if error.code not in {"LEASE_BUSY", "LEASE_WAIT"} or attempt == attempts:
                raise
            sleep(RETRY_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(TursoClient.from_env(budget_seconds=900), args.action, apply=args.apply)
    except StorageError as error:
        print(json.dumps({"status": "error", "code": error.code, "detail": str(error)}))
        return 1
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            mode = "apply" if args.apply else "dry run"
            stream.write(f"### IGDB ledger {args.action} ({mode})\n\n```json\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
