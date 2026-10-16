"""Ingest immutable Git datasets into Turso, replicate D1, and export backups.

All mutations must run under the storage workflow's exclusive concurrency key.
Collectors continue to use their existing Git ingestion and publication path.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

from radar_backend.storage.sql import D1Client, SqliteClient, StorageError, TursoClient
from radar_backend.storage.sources import SOURCES, checkout_name, clone_source, remote_revision, snapshot_from_checkout, source_id
from radar_backend.storage.documents import initialize, publish_snapshot, read_snapshot, sync_replica, export_sqlite, garbage_collect
from radar_core.storage.primary_store import primary_status


def _failure(report, stage, error):
    report["errors"].append({"stage": stage, "code": error.code if isinstance(error, StorageError) else "UNEXPECTED_ERROR"})


def _heads(client):
    return {row["source_id"]: row for row in client.execute("SELECT * FROM radar_sources ORDER BY source_id")}


def ingest(primary, *, local_root=None):
    results, errors = [], []
    existing = _heads(primary)
    with tempfile.TemporaryDirectory(prefix="radar-storage-sources-") as directory:
        for spec in SOURCES:
            identity = source_id(spec)
            previous = existing.get(identity)
            try:
                if primary_status(primary, identity).get("active"):
                    results.append({**previous, "import_status": "database_primary"})
                    continue
                if local_root is None and previous and remote_revision(spec) == previous["revision"]:
                    results.append({**previous, "import_status": "unchanged"})
                    continue
                root = Path(local_root) / checkout_name(spec) if local_root else clone_source(spec, directory)
                snapshot = snapshot_from_checkout(spec, root)
                metadata = publish_snapshot(primary, identity, snapshot["revision"], snapshot["files"],
                                            snapshot["captured_at"], expected_generation=previous["generation"] if previous else 0)
                results.append({**metadata, "import_status": "passed", "files": len(snapshot["files"]), "raw_bytes": snapshot["raw_bytes"]})
                print(json.dumps({"stage": "source_saved", "source_id": identity, "files": len(snapshot["files"])}, ensure_ascii=False), flush=True)
            except Exception as error:
                errors.append({"stage": identity, "code": error.code if isinstance(error, StorageError) else "SOURCE_UNAVAILABLE"})
    return results, errors


def verify_heads(primary, replica, *, require_complete=False, target_heads=None):
    """Verify captured replication targets, or an exact static pair of stores.

    A replica beyond a captured target is accepted only when its complete head
    matches authoritative primary history. Equal generations may never differ.
    """
    left = _heads(primary) if target_heads is None else {row["source_id"]: row for row in target_heads}
    right = _heads(replica)
    if require_complete and set(left) != {source_id(spec) for spec in SOURCES}:
        raise StorageError("INCOMPLETE_SOURCES", "All configured sources must be present before full-system verification.")
    if set(left) != set(right):
        raise StorageError("REPLICA_MISMATCH", "Primary and standby source coverage differs.")
    fields = ("revision", "manifest_digest", "generation", "captured_at")
    for name, head in left.items():
        if target_heads is not None and right[name]["generation"] > head["generation"]:
            # Read D1 before taking this short primary lease; D1 network waits
            # must never retain the primary's publication lock.
            from radar_core.storage.primary_store import maintenance
            with maintenance(primary, "read"):
                authoritative = primary.execute("SELECT revision,manifest_digest,generation,captured_at FROM radar_outbox "
                                                "WHERE source_id=? AND generation=?", (name, right[name]["generation"]))
                if not authoritative:
                    authoritative = primary.execute("SELECT revision,manifest_digest,generation,captured_at FROM radar_sources "
                                                    "WHERE source_id=? AND generation=?", (name, right[name]["generation"]))
                if not authoritative:
                    # GC can remove an acknowledged outbox row after the
                    # primary advances again. Committed operation receipts
                    # retain that generation's authoritative head independently.
                    authoritative = primary.execute("SELECT revision,manifest_digest,generation,captured_at FROM radar_operations "
                                                    "WHERE source_id=? AND generation=? LIMIT 1", (name, right[name]["generation"]))
            if not authoritative or any(authoritative[0][field] != right[name][field] for field in fields):
                raise StorageError("REPLICA_MISMATCH", "Standby progress differs from authoritative primary history.")
            continue
        if any(head[field] != right[name][field] for field in fields):
            raise StorageError("REPLICA_MISMATCH", "Primary and standby snapshots differ.")
    return {"status": "passed", "source_count": len(left),
            "scope": "captured_replication_targets" if target_heads is not None else "current_heads"}


def verify_snapshots(primary, replica, *, require_complete=False):
    """Compare bytes together with the immutable generation actually read.

    Independent databases cannot guarantee a simultaneous capture while writers
    are active. Distinguish that inconclusive case from same-generation damage.
    """
    names, replica_names = set(_heads(primary)), set(_heads(replica))
    if require_complete and names != {source_id(spec) for spec in SOURCES}:
        raise StorageError("INCOMPLETE_SOURCES", "All configured sources must be present before full-system verification.")
    if names != replica_names:
        raise StorageError("REPLICA_MISMATCH", "Primary and standby source coverage differs.")
    files = 0
    for identity in sorted(names):
        left_head, left = read_snapshot(primary, identity)
        right_head, right = read_snapshot(replica, identity)
        if left_head["generation"] != right_head["generation"]:
            raise StorageError("REPLICA_GENERATION_CHANGED", "Independent snapshot captures differ in generation; verify again after replication catches up.")
        if left_head != right_head or left != right:
            raise StorageError("REPLICA_MISMATCH", "Same-generation primary and standby snapshot bytes differ.")
        files += len(left)
    return {"status": "passed", "source_count": len(names), "scope": "matching_immutable_snapshots"}, files


def _make_backup(primary, output):
    """Produce standard SQL and verify restoration before publishing any file."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    sqlite_path = output / "radar.sqlite"
    export_report = export_sqlite(primary, sqlite_path)
    sql_path = output / "radar.sql.gz"
    with sqlite3.connect(sqlite_path) as database, gzip.open(sql_path, "wt", encoding="utf-8", newline="\n", compresslevel=6) as compressed:
        for line in database.iterdump():
            compressed.write(line + "\n")
    # Restore the generated artifact itself, not merely the original database.
    restored_path = output / "restore-check.sqlite"
    with sqlite3.connect(restored_path) as restored, gzip.open(sql_path, "rt", encoding="utf-8") as compressed:
        restored.executescript(compressed.read())
        if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StorageError("RESTORE_FAILED", "SQL backup integrity verification failed.")
    original, restored = SqliteClient(sqlite_path), SqliteClient(restored_path)
    try:
        expected = _heads(original)
        verify_heads(original, restored)
        files, raw_bytes = 0, 0
        for identity in expected:
            _, source_files = read_snapshot(restored, identity)
            files += len(source_files)
            raw_bytes += sum(map(len, source_files.values()))
        from radar_core.storage.documents import SCHEMA_VERSION
        manifest = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                    "format": "SQLite SQL dump, gzip compressed", "restore_verified": True,
                    "sources": list(expected.values()), "file_count": files, "raw_bytes": raw_bytes,
                    "export": export_report}
    finally:
        original.close()
        restored.close()
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
                                 for path in (sql_path, manifest_path)), encoding="ascii")
    restored_path.unlink()
    sqlite_path.unlink()
    return manifest, [sql_path, manifest_path, checksums]


def make_backup(primary, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-check-", dir=output) as scratch:
        manifest, paths = _make_backup(primary, scratch)
        final_paths = []
        # The checksum file is installed last. A partial install cannot be
        # mistaken for a verified release by the checksum-validating publisher.
        for path in paths:
            destination = output / path.name
            path.replace(destination)
            final_paths.append(destination)
        return manifest, final_paths


def capacity(client):
    row = client.execute("SELECT COALESCE(SUM(LENGTH(payload)), 0) AS bytes FROM radar_chunks")[0]
    size = row["bytes"]
    return {"encoded_chunk_bytes": size, "warning": size >= 300 * 1024 * 1024,
            "warning_threshold_bytes": 300 * 1024 * 1024,
            "note": "Encoded payload only; database indexes/metadata add overhead. D1 Free cap is 500 MB per database."}


def run(args):
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode,
              "storage_stage": "git_ingestion_to_turso_with_d1_replica", "errors": []}
    primary = SqliteClient(args.primary_sqlite) if args.primary_sqlite else TursoClient.from_env(budget_seconds=1500)
    replica = None
    try:
        initialize(primary)
        if args.mode == "sync":
            report["sources"], errors = ingest(primary, local_root=args.local_sources_root)
            report["errors"].extend(errors)
        if args.mode == "export" or args.daily_backup:
            try:
                from radar_backend.storage.backup_release import daily_backup_exists, publish_daily_backup
                day = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
                repository = os.environ.get("GITHUB_REPOSITORY", "danielet087/game-trend-radar-backend")
                token = os.environ.get("GH_TOKEN", "")
                if args.publish_backup and args.mode != "export" and daily_backup_exists(repository, day, token):
                    report["backup"] = {"status": "already_verified_today"}
                elif set(_heads(primary)) != {source_id(spec) for spec in SOURCES}:
                    raise StorageError("INCOMPLETE_BACKUP", "All configured sources must have a complete snapshot before publishing a system backup.")
                else:
                    manifest, paths = make_backup(primary, args.backup_dir)
                    report["backup"] = {"status": "passed", "restore_verified": True,
                                        "file_count": manifest["file_count"], "raw_bytes": manifest["raw_bytes"]}
                    if args.publish_backup:
                        report["backup"]["release"] = publish_daily_backup(repository, day, token, paths)
            except Exception as error:
                _failure(report, "backup", error)
        if args.mode in {"sync", "replicate"}:
            try:
                replica = SqliteClient(args.replica_sqlite) if args.replica_sqlite else D1Client.from_env(budget_seconds=600)
                initialize(replica)
                report["replication"] = sync_replica(primary, replica, max_events=100)
                for error in report["replication"].get("errors", []):
                    report["errors"].append({"stage": "replication", **error})
                if report["replication"].get("events_pending_in_target", 0):
                    report["errors"].append({"stage": "replication", "code": "REPLICATION_PENDING"})
                report["consistency"] = verify_heads(primary, replica, require_complete=True,
                                                     target_heads=report["replication"]["target_heads"])
            except Exception as error:
                _failure(report, "replication", error)
        if args.mode == "verify":
            replica = SqliteClient(args.replica_sqlite) if args.replica_sqlite else D1Client.from_env(budget_seconds=600)
            report["consistency"], report["verified_files"] = verify_snapshots(primary, replica, require_complete=True)
        if args.mode in {"sync", "replicate"}:
            for name, client, is_replica in (("primary", primary, False), ("replica", replica, True)):
                if client is None:
                    continue
                try:
                    report[name + "_gc"] = garbage_collect(client, max_delete=10000, replica_mode=is_replica, exclusive=True)
                    report[name + "_capacity"] = capacity(client)
                    if report[name + "_capacity"]["warning"]:
                        report["errors"].append({"stage": name, "code": "CAPACITY_WARNING"})
                except Exception as error:
                    _failure(report, name + "_maintenance", error)
        report["status"] = "passed" if not report["errors"] else "degraded"
        return report
    finally:
        for client in (primary, replica):
            if isinstance(client, SqliteClient):
                client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("sync", "replicate", "verify", "export"), default="sync")
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--local-sources-root", type=Path)
    parser.add_argument("--primary-sqlite", type=Path)
    parser.add_argument("--replica-sqlite", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=Path("output/storage-backup"))
    parser.add_argument("--daily-backup", action="store_true")
    parser.add_argument("--publish-backup", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except Exception as error:
        report = {"status": "failed", "errors": [{"stage": "initialization", "code": error.code if isinstance(error, StorageError) else "UNEXPECTED_ERROR"}]}
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    args.report_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("## Radar database storage\n\n```json\n" + text + "\n```\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
