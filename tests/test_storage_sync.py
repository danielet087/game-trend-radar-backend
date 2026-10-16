"""Orchestration boundaries: standby failures cannot block primary backups."""
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radar_backend.storage import documents as store
from radar_backend.storage.documents import initialize, publish_snapshot, read_snapshot
from radar_backend.storage.sources import SOURCES, source_id
from radar_backend.storage.sql import SqliteClient, StorageError
from scripts import storage_sync as sync


class StorageSyncTests(unittest.TestCase):
    def databases(self):
        primary, replica = SqliteClient(), SqliteClient()
        self.addCleanup(primary.close)
        self.addCleanup(replica.close)
        initialize(primary)
        initialize(replica)
        return primary, replica

    def test_fixed_targets_pass_with_new_work_queued_during_replication(self):
        primary, replica = self.databases()
        for spec in SOURCES:
            publish_snapshot(primary, source_id(spec), "first", {"data/x.json": b'{}'}, "2026-10-15T10:00:00Z")
        changed_source = source_id(SOURCES[0])
        copy_graph = store._put_graph
        published = False

        def copy_with_new_arrival(client, objects, chunks):
            nonlocal published
            if client is replica and not published:
                published = True
                publish_snapshot(primary, changed_source, "second", {"data/x.json": b'{"new":true}'},
                                 "2026-10-15T10:01:00Z")
            return copy_graph(client, objects, chunks)

        args = Namespace(mode="replicate", primary_sqlite=None, replica_sqlite=None,
                         local_sources_root=None, daily_backup=False, publish_backup=False, backup_dir=Path("unused"))
        with patch.object(sync.TursoClient, "from_env", return_value=primary), \
                patch.object(sync.D1Client, "from_env", return_value=replica), \
                patch.object(store, "_put_graph", side_effect=copy_with_new_arrival):
            report = sync.run(args)
        self.assertTrue(published)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["replication"]["events_pending_in_target"], 0)
        self.assertEqual(report["replication"]["events_deferred"], 1)
        self.assertEqual(report["consistency"]["scope"], "captured_replication_targets")

    def test_fixed_targets_reject_same_generation_with_other_digest(self):
        primary, replica = self.databases()
        target = publish_snapshot(primary, "owner/repo@main", "first", {"x": b"primary"}, "2026-10-15T10:00:00Z")
        publish_snapshot(replica, "owner/repo@main", "rogue", {"x": b"replica"}, "2026-10-15T10:00:00Z")
        with self.assertRaises(StorageError) as error:
            sync.verify_heads(primary, replica, target_heads=[target])
        self.assertEqual(error.exception.code, "REPLICA_MISMATCH")

    def test_initial_backlog_beyond_event_limit_keeps_orchestration_degraded(self):
        primary, replica = self.databases()
        for spec in SOURCES:
            publish_snapshot(primary, source_id(spec), "first", {"data/x.json": b'{}'}, "2026-10-15T10:00:00Z")
        args = Namespace(mode="replicate", primary_sqlite=None, replica_sqlite=None,
                         local_sources_root=None, daily_backup=False, publish_backup=False, backup_dir=Path("unused"))
        with patch.object(sync.TursoClient, "from_env", return_value=primary), \
                patch.object(sync.D1Client, "from_env", return_value=replica), \
                patch.object(sync, "sync_replica", side_effect=lambda left, right, **kwargs: store.sync_replica(left, right, max_events=1)):
            report = sync.run(args)
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["replication"]["events_pending_in_target"], len(SOURCES) - 1)
        self.assertEqual(report["replication"]["events_deferred"], 0)
        self.assertIn({"stage": "replication", "code": "REPLICATION_PENDING"}, report["errors"])

    def test_replica_beyond_fixed_target_requires_authoritative_history(self):
        primary, replica = self.databases()
        target = publish_snapshot(primary, "owner/repo@main", "first", {"x": b"first"}, "2026-10-15T10:00:00Z")
        publish_snapshot(primary, "owner/repo@main", "second", {"x": b"second"}, "2026-10-15T10:01:00Z")
        store.sync_replica(primary, replica)
        self.assertEqual(sync.verify_heads(primary, replica, target_heads=[target])["status"], "passed")
        publish_snapshot(replica, "owner/repo@main", "rogue", {"x": b"rogue"}, "2026-10-15T10:02:00Z")
        with self.assertRaises(StorageError) as error:
            sync.verify_heads(primary, replica, target_heads=[target])
        self.assertEqual(error.exception.code, "REPLICA_MISMATCH")

    def test_verify_distinguishes_different_capture_generations_from_corruption(self):
        primary, replica = self.databases()
        publish_snapshot(primary, "owner/repo@main", "first", {"x": b"first"}, "2026-10-15T10:00:00Z")
        store.sync_replica(primary, replica)
        original_read = sync.read_snapshot
        advanced = False

        def capture_with_concurrent_publication(client, identity):
            nonlocal advanced
            result = original_read(client, identity)
            if client is primary and not advanced:
                advanced = True
                publish_snapshot(primary, identity, "second", {"x": b"second"}, "2026-10-15T10:01:00Z")
                store.sync_replica(primary, replica)
            return result

        with patch.object(sync, "read_snapshot", side_effect=capture_with_concurrent_publication), \
                self.assertRaises(StorageError) as error:
            sync.verify_snapshots(primary, replica)
        self.assertEqual(error.exception.code, "REPLICA_GENERATION_CHANGED")
        consistency, files = sync.verify_snapshots(primary, replica)
        self.assertEqual(consistency["status"], "passed")
        self.assertEqual(files, 1)

    def test_valid_replica_ahead_of_target_survives_acknowledged_outbox_gc(self):
        from radar_core.storage import primary_store
        primary, replica = self.databases()
        target = publish_snapshot(primary, "owner/repo@main", "first", {"data/x.json": b"first"}, "2026-10-15T10:00:00Z")
        primary_store.activate_primary(primary, "owner/repo@main")
        second = primary_store.publish_patch(primary, "owner/repo@main", base_generation=1,
            changed={"data/x.json": b"second"}, operation_id="second", projection_required=False)
        store.sync_replica(primary, replica)
        primary_store.publish_patch(primary, "owner/repo@main", base_generation=2,
            changed={"data/x.json": b"third"}, operation_id="third", projection_required=False)
        store.garbage_collect(primary, exclusive=True)
        self.assertEqual(primary.execute("SELECT generation FROM radar_outbox WHERE generation=2"), [])
        self.assertEqual(store._head(primary, "owner/repo@main")["generation"], 3)
        self.assertEqual(store._head(replica, "owner/repo@main")["generation"], 2)
        self.assertEqual(sync.verify_heads(primary, replica, target_heads=[target])["status"], "passed")

        # The receipt fallback also protects replication's advanced-head
        # readback; it must not roll D1 back to a previously captured target.
        with primary_store.maintenance(replica, "replicate"):
            result = store._replicate_one(primary, replica, "owner/repo@main", target, {}, {})
        self.assertEqual(result["manifest_digest"], second["manifest_digest"])
        self.assertEqual(result["generation"], 2)

        # A receipt for this generation must match every canonical field.
        corrupted = {**store._head(replica, "owner/repo@main"), "manifest_digest": "0" * 64}
        with patch.object(sync, "_heads", return_value={"owner/repo@main": corrupted}), \
                self.assertRaises(StorageError) as error:
            sync.verify_heads(primary, replica, target_heads=[target])
        self.assertEqual(error.exception.code, "REPLICA_MISMATCH")

    def test_incomplete_or_empty_stores_cannot_pass_complete_verification(self):
        primary, replica = SqliteClient(), SqliteClient()
        self.addCleanup(primary.close)
        self.addCleanup(replica.close)
        initialize(primary)
        initialize(replica)
        with self.assertRaises(StorageError) as error:
            sync.verify_heads(primary, replica, require_complete=True)
        self.assertEqual(error.exception.code, "INCOMPLETE_SOURCES")

    def test_d1_config_failure_does_not_block_ingest_or_earlier_backup(self):
        primary = SqliteClient()
        initialize(primary)
        order = []

        def ingest(client, **kwargs):
            order.append("ingest")
            for spec in SOURCES:
                publish_snapshot(client, source_id(spec), "fixed-revision", {"data/x.json": b'{}'}, "2026-10-15T10:00:00Z")
            return list(sync._heads(client).values()), []

        def backup(client, output):
            order.append("backup")
            return {"file_count": 7, "raw_bytes": 14}, []

        def d1(**kwargs):
            order.append("d1")
            raise StorageError("INVALID_CONFIG", "Do not print secret-token")

        args = Namespace(mode="sync", primary_sqlite=None, replica_sqlite=None,
                         local_sources_root=None, daily_backup=True, publish_backup=False, backup_dir=Path("unused"))
        with patch.object(sync.TursoClient, "from_env", return_value=primary), \
                patch.object(sync.D1Client, "from_env", side_effect=d1), \
                patch.object(sync, "ingest", side_effect=ingest), \
                patch.object(sync, "make_backup", side_effect=backup):
            report = sync.run(args)
        self.assertEqual(order, ["ingest", "backup", "d1"])
        self.assertEqual(report["backup"]["status"], "passed")
        self.assertEqual(len(report["sources"]), 7)
        self.assertEqual(report["primary_gc"]["status"], "passed")
        self.assertEqual(report["status"], "degraded")
        self.assertNotIn("secret-token", str(report))

    def test_sql_export_is_repeatable_and_preserves_original_bytes(self):
        primary = SqliteClient()
        self.addCleanup(primary.close)
        initialize(primary)
        files = {"data/game.json": '{ "中文": "測試", "id":"103582791475572379" }\r\n'.encode()}
        publish_snapshot(primary, "owner/repo@main", "revision", files, "2026-10-15T10:00:00Z")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            first, paths = sync.make_backup(primary, output)
            second, repeated_paths = sync.make_backup(primary, output)
            self.assertTrue(first["restore_verified"] and second["restore_verified"])
            self.assertEqual(paths, repeated_paths)
            self.assertEqual({path.name for path in output.iterdir()}, {"radar.sql.gz", "manifest.json", "SHA256SUMS"})
            self.assertEqual(second["raw_bytes"], sum(map(len, files.values())))
        self.assertEqual(read_snapshot(primary, "owner/repo@main")[1], files)


if __name__ == "__main__":
    unittest.main()
