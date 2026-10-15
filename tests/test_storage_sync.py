"""Orchestration boundaries: standby failures cannot block primary backups."""
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radar_backend.storage.documents import initialize, publish_snapshot, read_snapshot
from radar_backend.storage.sources import SOURCES, source_id
from radar_backend.storage.sql import SqliteClient, StorageError
from scripts import storage_sync as sync


class StorageSyncTests(unittest.TestCase):
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
