"""Create the database-only Twitch views source from the frontend source.

The four high-churn Twitch directories move out of Git. This copies their
current committed bytes from the frontend source into a new primary source
that is never projected to Git, then reads them back and compares. An existing
views source is left untouched. Without --apply it only reports what it would
copy.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from radar_core.storage import documents, primary_store
from radar_core.storage.sources import SOURCES, source_id
from radar_core.storage.sql import StorageError, TursoClient

FRONTEND = "danielet087/game-trend-radar@main"
VIEWS_SPEC = next(spec for spec in SOURCES if spec.get("database_only")
                  and spec["repository"] == "danielet087/game-trend-radar")
VIEWS = source_id(VIEWS_SPEC)
PREFIXES = tuple(VIEWS_SPEC["prefixes"])
ATTEMPTS = 6
RETRY_SECONDS = 30


def _views(files):
    return {path: raw for path, raw in files.items() if path.startswith(PREFIXES)}


def bootstrap(client, *, apply=False):
    existing = documents._head(client, VIEWS)
    if existing:
        return {"status": "exists", "source_id": VIEWS, "generation": existing["generation"],
                "active": primary_store.primary_status(client, VIEWS)["active"]}
    frontend, files = primary_store.read_files(client, FRONTEND, paths=PREFIXES)
    views = _views(files)
    if not views:
        raise StorageError("NO_TWITCH_VIEWS", "The frontend source holds no Twitch view documents to copy.")
    summary = {"source_id": VIEWS, "files": len(views), "raw_bytes": sum(map(len, views.values())),
               "from_generation": frontend["generation"],
               "by_prefix": {prefix: sum(path.startswith(prefix) for path in views) for prefix in PREFIXES}}
    if not apply:
        return {"status": "dry_run", **summary}
    revision = f"bootstrap:{FRONTEND}:generation-{frontend['generation']}"
    head = primary_store.bootstrap_primary(client, VIEWS, views, revision)
    _, copied = primary_store.read_files(client, VIEWS)
    if copied != views:
        raise StorageError("BOOTSTRAP_MISMATCH", "The views source does not read back the copied bytes.")
    return {"status": "bootstrapped", **summary, "generation": head["generation"], "active": head["active"]}


def run(client, *, apply=False, attempts=ATTEMPTS, sleep=time.sleep):
    documents.initialize(client)
    for attempt in range(1, attempts + 1):
        try:
            return {**bootstrap(client, apply=apply), "attempts": attempt}
        except StorageError as error:
            # A collector holding the storage lease finishes within minutes.
            if error.code not in {"LEASE_BUSY", "LEASE_WAIT"} or attempt == attempts:
                raise
            sleep(RETRY_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(TursoClient.from_env(budget_seconds=900), apply=args.apply)
    except StorageError as error:
        print(json.dumps({"status": "error", "code": error.code}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
