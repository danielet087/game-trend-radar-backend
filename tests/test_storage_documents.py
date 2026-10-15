"""Real SQLite assertions for document publication, replication and recovery."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest

from radar_backend.storage import documents as store
from radar_backend.storage.sql import SqliteClient, StorageError


class InterceptClient:
    """A deliberately nontransactional batch, like a partial transport failure."""

    def __init__(self, client, before=None, after=None):
        self.client, self.before, self.after = client, before, after
        self.batches = []

    def execute(self, sql, params=()):
        if self.before:
            self.before(sql, params)
        rows = self.client.execute(sql, params)
        if self.after:
            self.after(sql, params)
        return rows

    def batch(self, statements):
        self.batches.append(statements)
        return [self.execute(sql, params) for sql, params in statements]


class StorageDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.primary, self.replica = SqliteClient(), SqliteClient()
        store.initialize(self.primary)
        store.initialize(self.replica)
        self.addCleanup(self.primary.close)
        self.addCleanup(self.replica.close)
        self.source = "owner/repo@main"
        self.files = {
            "data/繁體.json": '{ "中文": "原樣保留", "large": 103582791475572379, "x": 1, "x": 2 }\r\n'.encode(),
            "data/empty.json": b"",
            "data/unknown.bin": bytes(range(256)),
        }

    def publish(self, revision="a", files=None, captured="2026-10-15T17:00:00+08:00", **kwargs):
        return store.publish_snapshot(self.primary, self.source, revision,
                                      self.files if files is None else files, captured, **kwargs)

    def code(self, expected, operation):
        with self.assertRaises(StorageError) as context:
            operation()
        self.assertEqual(context.exception.code, expected)

    def test_exact_raw_bytes_idempotence_and_atomic_event(self):
        head = self.publish()
        again = self.publish(captured="2026-10-16T00:00:00Z", expected_generation=0)
        self.assertEqual(head, again)
        self.assertEqual(store.read_snapshot(self.primary, self.source), (head, self.files))
        events = self.primary.execute("SELECT * FROM radar_outbox")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["manifest_digest"], head["manifest_digest"])
        self.assertIsNone(events[0]["acked_at"])
        store.initialize(self.primary)
        self.assertEqual(self.primary.execute("SELECT COUNT(*) AS n FROM radar_outbox")[0]["n"], 1)

    def test_incompatible_schema_requires_explicit_migration(self):
        self.primary.execute("UPDATE radar_schema SET version=2 WHERE singleton=1")
        self.code("UNSUPPORTED_SCHEMA", lambda: store.initialize(self.primary))

    def test_trigger_failure_rolls_back_head_and_revision(self):
        self.primary.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON radar_outbox
            BEGIN SELECT RAISE(ABORT,'forced'); END""")
        self.code("SQL_ERROR", self.publish)
        self.assertEqual(self.primary.execute("SELECT * FROM radar_sources"), [])
        self.assertEqual(self.primary.execute("SELECT * FROM radar_revisions"), [])
        self.assertEqual(self.primary.execute("SELECT * FROM radar_outbox"), [])

    def test_partial_batch_never_publishes_and_retry_recovers(self):
        count = 0

        def fail(sql, params):
            nonlocal count
            if sql.startswith("INSERT OR IGNORE INTO radar_objects"):
                count += 1
                if count == 2:
                    raise StorageError("NETWORK_ERROR")

        interrupted = InterceptClient(self.primary, before=fail)
        self.code("NETWORK_ERROR", lambda: store.publish_snapshot(
            interrupted, self.source, "a", self.files, "2026-10-15T09:00:00Z"))
        self.assertEqual(self.primary.execute("SELECT * FROM radar_sources"), [])
        self.assertGreater(self.primary.execute("SELECT COUNT(*) AS n FROM radar_chunks")[0]["n"], 0)
        self.publish()
        self.assertEqual(store.read_snapshot(self.primary, self.source)[1], self.files)

    def test_revision_conflict_survives_subsequent_generations(self):
        self.publish()
        different = {"data/changed.json": b"{}"}
        self.code("REVISION_CONFLICT", lambda: self.publish(files=different))
        self.publish("b", different, captured="2026-10-15T09:01:00Z")
        self.code("REVISION_CONFLICT", lambda: self.publish("a", different, captured="2026-10-15T09:02:00Z"))

    def test_stale_capture_and_generation_cannot_replace_head(self):
        original = self.publish()
        self.code("STALE_CAPTURE", lambda: self.publish("b", captured="2026-10-15T08:59:00Z"))
        self.code("STALE_GENERATION", lambda: self.publish("b", captured="2026-10-15T09:01:00Z", expected_generation=0))
        self.assertEqual(store.read_snapshot(self.primary, self.source)[0], original)

    def test_racing_cas_keeps_the_winning_snapshot(self):
        self.publish()
        competing = {"data/winner.json": b"winner"}
        raced = False

        def before(sql, params):
            nonlocal raced
            if sql.startswith("INSERT INTO radar_sources") and not raced:
                raced = True
                self.publish("winner", competing, captured="2026-10-15T09:02:00Z", expected_generation=1)

        client = InterceptClient(self.primary, before=before)
        self.code("STALE_GENERATION", lambda: store.publish_snapshot(
            client, self.source, "loser", {"data/loser.json": b"loser"},
            "2026-10-15T09:01:00Z", expected_generation=1))
        head, files = store.read_snapshot(self.primary, self.source)
        self.assertEqual(head["revision"], "winner")
        self.assertEqual(head["generation"], 2)
        self.assertEqual(files, competing)
        self.assertEqual(self.primary.execute("SELECT COUNT(*) AS n FROM radar_outbox")[0]["n"], 2)

    def test_lost_head_response_is_verified_as_success(self):
        lost = False

        def after(sql, params):
            nonlocal lost
            if sql.startswith("INSERT INTO radar_sources") and not lost:
                lost = True
                raise StorageError("NETWORK_ERROR")

        client = InterceptClient(self.primary, after=after)
        head = store.publish_snapshot(client, self.source, "a", self.files, "2026-10-15T09:00:00Z")
        self.assertEqual(head["generation"], 1)
        self.assertEqual(self.primary.execute("SELECT COUNT(*) AS n FROM radar_outbox")[0]["n"], 1)

    def test_replication_copies_latest_generation_without_rollback(self):
        self.publish()
        newest = self.publish("b", {"data/new.json": b"new"}, captured="2026-10-15T09:01:00Z")
        report = store.sync_replica(self.primary, self.replica)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["events_acked"], 2)
        self.assertEqual(store.read_snapshot(self.replica, self.source), (newest, {"data/new.json": b"new"}))
        # Replay the older event after a hypothetical lost acknowledgement.
        self.primary.execute("UPDATE radar_outbox SET acked_at=NULL WHERE generation=1")
        report = store.sync_replica(self.primary, self.replica)
        self.assertEqual(report["events_pending"], 0)
        self.assertEqual(store.read_snapshot(self.replica, self.source)[0], newest)

    def test_replica_partial_write_keeps_primary_pending_and_retries(self):
        head = self.publish()

        def fail(sql, params):
            if sql.startswith("INSERT OR IGNORE INTO radar_objects"):
                raise StorageError("NETWORK_ERROR")

        broken = InterceptClient(self.replica, before=fail)
        report = store.sync_replica(self.primary, broken)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["events_pending"], 1)
        self.assertEqual(self.replica.execute("SELECT * FROM radar_sources"), [])
        self.assertEqual(store.read_snapshot(self.primary, self.source)[0], head)
        self.assertEqual(store.sync_replica(self.primary, self.replica)["status"], "passed")
        self.assertEqual(store.read_snapshot(self.replica, self.source)[1], self.files)

    def test_newer_divergent_replica_cannot_ack_an_older_event(self):
        self.publish()
        store.sync_replica(self.primary, self.replica)
        self.publish("b", {"second.json": b"second"}, captured="2026-10-15T09:01:00Z")
        self.publish("c", {"third.json": b"third"}, captured="2026-10-15T09:02:00Z")
        store.publish_snapshot(self.replica, self.source, "rogue", {"rogue.json": b"divergent"},
                               "2026-10-15T09:01:00Z")
        self.primary.execute("UPDATE radar_outbox SET acked_at=NULL WHERE generation=1")
        report = store.sync_replica(self.primary, self.replica, max_events=1)
        self.assertEqual(report["errors"][0]["code"], "REPLICA_CONFLICT")
        self.assertEqual(report["events_pending"], 3)

    def test_replica_readback_corruption_prevents_ack(self):
        self.publish()
        corrupted = False

        def corrupt(sql, params):
            nonlocal corrupted
            if sql.startswith("INSERT OR IGNORE INTO radar_objects") and not corrupted:
                corrupted = True
                self.replica.execute("UPDATE radar_chunks SET payload=? WHERE digest=(SELECT digest FROM radar_chunks LIMIT 1)",
                                     (base64.b64encode(b"damaged").decode(),))

        report = store.sync_replica(self.primary, InterceptClient(self.replica, after=corrupt))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["errors"][0]["code"], "CORRUPT_CHUNK")
        self.assertEqual(report["events_pending"], 1)
        self.assertEqual(self.replica.execute("SELECT * FROM radar_sources"), [])

    def test_lost_ack_response_remains_idempotent(self):
        self.publish()

        def lose(sql, params):
            if sql.startswith("UPDATE radar_outbox SET acked_at"):
                raise StorageError("NETWORK_ERROR")

        report = store.sync_replica(InterceptClient(self.primary, after=lose), self.replica)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["events_pending"], 0)
        self.assertEqual(store.sync_replica(self.primary, self.replica)["status"], "passed")
        self.assertEqual(self.replica.execute("SELECT generation FROM radar_sources")[0]["generation"], 1)

    def test_chunk_and_request_bounds_with_incompressible_data(self):
        files = {f"data/random-{i}.json": os.urandom(80 * 1024) for i in range(12)}
        client = InterceptClient(self.primary)
        store.publish_snapshot(client, self.source, "a", files, "2026-10-15T09:00:00Z")
        rows = self.primary.execute("SELECT digest,payload FROM radar_chunks")
        self.assertTrue(all(len(row["digest"]) + len(row["payload"]) < 48 * 1024 for row in rows))
        self.assertTrue(all(len(batch) <= 25 for batch in client.batches))
        self.assertTrue(all(len(json.dumps(batch).encode()) < 512 * 1024 for batch in client.batches))
        self.assertEqual(store.read_snapshot(self.primary, self.source)[1], files)

    def test_gc_keeps_pending_graphs_then_reclaims_acked_history(self):
        first = self.publish()
        self.publish("b", {"new.json": b"new"}, captured="2026-10-15T09:01:00Z")
        self.code("EXCLUSIVE_ACCESS_REQUIRED", lambda: store.garbage_collect(self.primary))
        store.garbage_collect(self.primary, exclusive=True)
        self.assertEqual(len(self.primary.execute("SELECT * FROM radar_objects WHERE digest=?", (first["manifest_digest"],))), 1)
        store.sync_replica(self.primary, self.replica)
        report = store.garbage_collect(self.primary, exclusive=True)
        self.assertGreater(report["objects_deleted"], 0)
        self.assertEqual(self.primary.execute("SELECT * FROM radar_objects WHERE digest=?", (first["manifest_digest"],)), [])
        self.assertEqual(store.read_snapshot(self.primary, self.source)[1], {"new.json": b"new"})

    def test_replica_gc_ignores_its_local_outbox_and_bounded_passes_are_safe(self):
        self.publish()
        store.sync_replica(self.primary, self.replica)
        self.publish("b", {"new.json": b"new"}, captured="2026-10-15T09:01:00Z")
        store.sync_replica(self.primary, self.replica)
        for _ in range(12):
            store.garbage_collect(self.replica, exclusive=True, replica_mode=True, max_delete=1)
        self.assertEqual(len(self.replica.execute("SELECT * FROM radar_outbox")), 1)
        self.assertEqual(len(self.replica.execute("SELECT * FROM radar_objects")), 2)
        self.assertEqual(store.read_snapshot(self.replica, self.source)[1], {"new.json": b"new"})
        # The old raw bytes can later reappear after GC without dangling chunks.
        self.publish("c", captured="2026-10-15T09:02:00Z")
        self.assertEqual(store.sync_replica(self.primary, self.replica)["status"], "passed")
        self.assertEqual(store.read_snapshot(self.replica, self.source)[1], self.files)

    def test_export_restores_current_raw_bytes_and_sql_dump(self):
        self.publish()
        store.sync_replica(self.primary, self.replica)
        newest = self.publish("b", captured="2026-10-15T09:01:00Z")
        store.publish_snapshot(self.primary, "second@main", "second", {"foo.json": b"{\n}\n"}, "2026-10-15T09:02:00Z")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "export.sqlite"
            report = store.export_sqlite(self.primary, path)
            self.assertEqual(report["sources"], 2)
            restored = SqliteClient(path)
            self.addCleanup(restored.close)
            self.assertEqual(store.read_snapshot(restored, self.source), (newest, self.files))
            self.assertEqual(store.sync_replica(restored, self.replica)["status"], "passed")
            sql = "\n".join(restored.connection.iterdump())
            recovered = SqliteClient()
            self.addCleanup(recovered.close)
            recovered.connection.executescript(sql)
            self.assertEqual(store.read_snapshot(recovered, self.source), (newest, self.files))
            self.assertEqual(store.read_snapshot(recovered, "second@main")[1], {"foo.json": b"{\n}\n"})
            self.code("EXPORT_EXISTS", lambda: store.export_sqlite(self.primary, path))

    def test_export_rejects_changed_heads(self):
        self.publish()
        reads = 0

        def change(sql, params):
            nonlocal reads
            if sql == "SELECT * FROM radar_sources ORDER BY source_id":
                reads += 1
                if reads == 2:
                    self.publish("b", captured="2026-10-15T09:01:00Z")

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "export.sqlite"
            self.code("STALE_GENERATION", lambda: store.export_sqlite(
                InterceptClient(self.primary, before=change), path))
            self.assertFalse(path.exists())

    def test_paths_and_naive_capture_are_rejected(self):
        self.code("INVALID_PATH", lambda: self.publish(files={"../escape.json": b"{}"}))
        self.code("INVALID_CAPTURE_TIME", lambda: self.publish(captured="2026-10-15T09:00:00"))
        self.code("INVALID_FILE_BYTES", lambda: self.publish(files={"data.json": "not bytes"}))
        self.assertEqual(self.primary.execute("SELECT * FROM radar_sources"), [])


if __name__ == "__main__":
    unittest.main()
