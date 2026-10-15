"""Real SQLite cutover, fencing, incremental writes and portable recovery."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radar_core.storage import documents as d
from radar_core.storage import primary_store as p
from radar_core.storage.sql import SqliteClient, StorageError


class PrimaryStoreTests(unittest.TestCase):
    def setUp(self):
        self.c = SqliteClient()
        self.addCleanup(self.c.close)
        d.initialize(self.c)
        self.source = "owner/repo@main"
        self.files = {"data/a.json": b'{"id":103582791475572379}',
                      "data/b.json": ' {"繁體":"原樣"}\r\n'.encode(),
                      "data/delete.json": b'[]'}
        self.initial = d.publish_snapshot(self.c, self.source, "a" * 40, self.files, "2026-10-15T00:00:00Z")

    def activate(self):
        return p.activate_primary(self.c, self.source, expected_generation=1)

    def write(self, operation="one", generation=1, **kwargs):
        return p.publish_patch(self.c, self.source, base_generation=generation,
                               changed={"data/a.json": b'{"updated":true}'},
                               deleted=("data/delete.json",), operation_id=operation, **kwargs)

    def code(self, expected, fn):
        with self.assertRaises(StorageError) as error:
            fn()
        self.assertEqual(error.exception.code, expected)

    def test_v1_migration_preserves_source_and_requires_explicit_activation(self):
        old = SqliteClient()
        self.addCleanup(old.close)
        old.execute("CREATE TABLE radar_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL)")
        old.execute("INSERT INTO radar_schema VALUES(1,1)")
        for sql in d._SCHEMA:
            old.execute(sql)
        old.execute("INSERT INTO radar_sources VALUES('legacy','revision','digest',1,'time')")
        d.initialize(old)
        self.assertEqual(old.execute("SELECT version FROM radar_schema")[0]["version"], 2)
        self.assertEqual(d._head(old, "legacy")["revision"], "revision")
        self.assertFalse(p.primary_status(old, "legacy")["active"])
        d.initialize(old)
        self.assertEqual(old.execute("SELECT COUNT(*) AS n FROM radar_outbox")[0]["n"], 1)

    def test_activation_rejects_legacy_api_and_old_sql_even_during_valid_lease(self):
        self.activate()
        self.code("SOURCE_PRIMARY", lambda: d.publish_snapshot(self.c, self.source, "new-git", self.files,
                                                               "2026-10-15T01:00:00Z"))
        with p.maintenance(self.c, "write"):
            self.code("SQL_ERROR", lambda: self.c.execute(
                "UPDATE radar_sources SET generation=generation+1,revision='old-import' WHERE source_id=?", (self.source,)))
            self.code("SQL_ERROR", lambda: self.c.execute("DELETE FROM radar_chunks"))
            self.code("SQL_ERROR", lambda: self.c.execute("INSERT INTO radar_chunks(digest,payload) VALUES('old','old')"))
        self.assertEqual(d.read_snapshot(self.c, self.source)[1], self.files)

    def test_incremental_patch_exact_once_deletion_and_unowned_bytes(self):
        self.activate()
        result = self.write(payload_revision="frozen-input")
        again = self.write(payload_revision="frozen-input")
        self.assertEqual(result, again)
        self.assertEqual(result["generation"], 2)
        self.assertEqual(result["deleted"], ["data/delete.json"])
        head, files = p.read_files(self.c, self.source)
        self.assertEqual(files, {"data/a.json": b'{"updated":true}', "data/b.json": self.files["data/b.json"]})
        self.assertEqual(head["manifest"]["data/b.json"]["digest"], d._sha(self.files["data/b.json"]))
        self.code("OPERATION_CONFLICT", lambda: self.write(payload_revision="different-input"))
        self.code("STALE_GENERATION", lambda: self.write(operation="two"))
        self.assertEqual(len(p.projection_pending(self.c, self.source)), 1)

    def test_selective_read_reuses_verified_cache_and_preserves_manifest_coverage(self):
        self.activate()
        with patch.object(d, "_get_chunks", wraps=d._get_chunks) as get:
            head, files = p.read_files(self.c, self.source, ["data/a.json"],
                                       known_files={"data/a.json": self.files["data/a.json"]})
        self.assertEqual(files, {"data/a.json": self.files["data/a.json"]})
        self.assertEqual(set(head["manifest"]), set(self.files))
        self.assertEqual(len(get.call_args_list[1].args[1]), 0)
        _, missing = p.read_files(self.c, self.source, ["data/absent.json"])
        self.assertEqual(missing, {})

    def test_database_only_receipt_has_no_fabricated_git_ack(self):
        self.activate()
        receipt = self.write(projection_required=False)
        self.assertEqual(receipt["delivery_target"], "database_only")
        self.assertFalse(receipt["projection_pending"])
        self.assertEqual(p.projection_pending(self.c, self.source), [])
        self.assertIsNone(self.c.execute("SELECT projection_git_revision FROM radar_operations")[0]["projection_git_revision"])

    def test_atomic_operation_failure_rolls_back_head_and_outbox(self):
        self.activate()
        self.c.execute("""CREATE TRIGGER fail_operation BEFORE INSERT ON radar_operations
            BEGIN SELECT RAISE(ABORT,'test'); END""")
        self.code("SQL_ERROR", self.write)
        self.assertEqual(d._head(self.c, self.source), self.initial)
        self.assertEqual(self.c.execute("SELECT COUNT(*) AS n FROM radar_outbox")[0]["n"], 1)
        self.assertEqual(self.c.execute("SELECT * FROM radar_operations"), [])

    def test_expired_writer_and_expired_gc_are_fenced_by_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shared.sqlite"
            first, second = SqliteClient(path), SqliteClient(path)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            d.initialize(first)
            p.bootstrap_primary(first, "source", {"x.json": b"{}"}, "git")
            with p.maintenance(first, "gc"):
                proof = p.proof(first)
                first.execute("UPDATE radar_leases SET expires_at=0")
                with p.maintenance(second, "gc"):
                    self.assertGreater(p.proof(second)[1], proof[1])
                    target = {**d._head(first, "source"), "generation": 2, "revision": "stale"}
                    self.code("SQL_ERROR", lambda: d._advance(first, target, 1, writer_kind="primary"))
                    digest = first.execute("SELECT digest FROM radar_chunks LIMIT 1")[0]["digest"]
                    self.code("SQL_ERROR", lambda: d._delete_digests(first, "radar_chunks", [digest]))
                    self.code("LEASE_LOST", lambda: p._LEASES.get()[id(first)].renew(force=True))
            self.assertEqual(d.read_snapshot(second, "source")[1], {"x.json": b"{}"})

    def test_gc_cannot_delete_another_live_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shared.sqlite"
            first, second = SqliteClient(path), SqliteClient(path)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            d.initialize(first)
            p.bootstrap_primary(first, "source", {"x.json": b"{}"}, "git")
            with p.maintenance(first, "write"), patch.object(p.time, "sleep"):
                d._ensure_raw_objects(first, {d._sha(b"in-flight"): b"in-flight"})
                self.code("LEASE_BUSY", lambda: d.garbage_collect(second, exclusive=True))
                self.assertTrue(first.execute("SELECT digest FROM radar_objects WHERE digest=?", (d._sha(b"in-flight"),)))

    def test_cross_source_revision_is_rejected(self):
        other = d.publish_snapshot(self.c, "other", "b" * 40, {"secret.json": b'"other"'}, "2026-10-15T00:00:00Z")
        self.activate()
        self.code("REVISION_NOT_FOUND", lambda: p.read_files(self.c, self.source, revision=other["manifest_digest"]))

    def test_evidence_survives_gc_replica_and_portable_export(self):
        self.activate()
        evidence = p.pin_evidence(self.c, self.source, revision=self.initial["manifest_digest"],
                                  paths=["data/a.json"], evidence_id="proof-one")
        self.assertNotEqual(evidence["manifest_digest"], self.initial["manifest_digest"])
        receipt = self.write()
        p.acknowledge_projection(self.c, self.source, "one", "b" * 40,
                                 generation=receipt["generation"], manifest_digest=receipt["manifest_digest"])
        replica = SqliteClient()
        self.addCleanup(replica.close)
        d.initialize(replica)
        report = d.sync_replica(self.c, replica)
        self.assertEqual(report["errors"], [])
        self.assertEqual(p.primary_status(replica, self.source)["mode"], "replica")
        self.code("SOURCE_NOT_PRIMARY", lambda: p.publish_patch(replica, self.source,
                  base_generation=2, changed={}, operation_id="forbidden"))
        d.garbage_collect(self.c, exclusive=True)
        self.assertEqual(self.c.execute("SELECT digest FROM radar_objects WHERE digest=?", (self.initial["manifest_digest"],)), [])
        old_head, old_files = p.read_files(self.c, self.source, ["data/a.json"], revision=self.initial["manifest_digest"])
        self.assertEqual(old_head["generation"], 1)
        self.assertEqual(old_files, {"data/a.json": self.files["data/a.json"]})
        self.assertEqual(p.resolve_git_revision(self.c, self.source, "b" * 40)["generation"], 2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "portable.sqlite"
            d.export_sqlite(self.c, path)
            restored = SqliteClient(path)
            self.addCleanup(restored.close)
            self.assertTrue(p.primary_status(restored, self.source)["active"])
            self.assertEqual(p.read_files(restored, self.source, ["data/a.json"],
                                         revision=self.initial["manifest_digest"])[1], old_files)
            self.assertEqual(p.resolve_git_revision(restored, self.source, "b" * 40)["generation"], 2)
            self.assertEqual(restored.execute("SELECT COUNT(*) AS n FROM radar_operations")[0]["n"], 1)
            restored.execute("PRAGMA query_only=ON")
            self.assertEqual(d.read_local_snapshot(restored, self.source)[1]["data/a.json"], b'{"updated":true}')

    def test_replica_metadata_sweeps_even_without_new_head_event(self):
        self.activate()
        replica = SqliteClient()
        self.addCleanup(replica.close)
        d.initialize(replica)
        d.sync_replica(self.c, replica)
        p.pin_evidence(self.c, self.source, revision=self.initial["manifest_digest"],
                       paths=["data/a.json"], evidence_id="later-proof")
        report = d.sync_replica(self.c, replica)
        self.assertEqual(report["events_acked"], 0)
        self.assertEqual(report["errors"], [])
        self.assertEqual(replica.execute("SELECT evidence_id FROM radar_evidence")[0]["evidence_id"], "later-proof")

    def test_legacy_evidence_is_exact_idempotent_and_survives_cutover_recovery(self):
        files = {"data/twitch_tracking.json": b'{"id":103582791475572379}\n',
                 "data/twitch_steam_discovery.json": ' {"證據":"固定版本"}\r\n'.encode()}
        # A 64-character Git hash must resolve through its source-scoped mapping,
        # not be mistaken for an unrelated object digest.
        revision = "c" * 64
        first = p.import_legacy_evidence(self.c, self.source, git_revision=revision, files=files)
        again = p.import_legacy_evidence(self.c, self.source, git_revision=revision, files=files)
        self.assertEqual(first, again)
        self.assertEqual(first["generation"], 0)
        self.assertEqual(d._head(self.c, self.source), self.initial)
        self.assertEqual(self.c.execute("SELECT COUNT(*) AS n FROM radar_outbox")[0]["n"], 1)
        self.code("EVIDENCE_CONFLICT", lambda: p.import_legacy_evidence(
            self.c, self.source, git_revision=revision,
            files={**files, "data/twitch_tracking.json": b'{}'}))
        self.activate()
        self.write(projection_required=False)
        self.code("SOURCE_PRIMARY", lambda: p.import_legacy_evidence(
            self.c, self.source, git_revision=revision, files=files))
        replica = SqliteClient()
        self.addCleanup(replica.close)
        d.initialize(replica)
        self.assertEqual(d.sync_replica(self.c, replica)["errors"], [])
        d.garbage_collect(self.c, exclusive=True)
        d.garbage_collect(replica, exclusive=True, replica_mode=True)
        for client in (self.c, replica):
            head, result = p.read_files(client, self.source, tuple(files), revision=revision)
            self.assertEqual(head["generation"], 0)
            self.assertEqual(result, files)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy-evidence.sqlite"
            d.export_sqlite(self.c, path)
            restored = SqliteClient(path)
            self.addCleanup(restored.close)
            self.assertEqual(p.read_files(restored, self.source, tuple(files), revision=revision)[1], files)

    def test_legacy_evidence_rejects_missing_or_invalid_fixed_commit_files(self):
        files = {"data/twitch_tracking.json": b'{}', "data/twitch_steam_discovery.json": b'[]'}
        self.code("INCOMPLETE_EVIDENCE", lambda: p.import_legacy_evidence(
            self.c, self.source, git_revision="c" * 40, files={"data/twitch_tracking.json": b'{}'}))
        self.code("INVALID_GIT_REVISION", lambda: p.import_legacy_evidence(
            self.c, self.source, git_revision="main", files=files))
        self.code("INVALID_EVIDENCE", lambda: p.import_legacy_evidence(
            self.c, self.source, git_revision="c" * 40,
            files={**files, "data/twitch_tracking.json": b'not json'}))
        self.code("EVIDENCE_CONFLICT", lambda: p.import_legacy_evidence(
            self.c, self.source, git_revision=self.initial["revision"], files=files))
        self.assertEqual(self.c.execute("SELECT * FROM radar_evidence"), [])
        self.assertEqual(d._head(self.c, self.source), self.initial)


if __name__ == "__main__":
    unittest.main()
