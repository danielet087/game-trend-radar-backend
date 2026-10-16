"""Independent SQLite clients exercise real fencing and bounded contention."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from radar_core.storage import documents as d
from radar_core.storage import primary_store as p
from radar_core.storage.sql import SqliteClient, StorageError


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class LeaseContentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shared.sqlite"
        self.first = SqliteClient(self.path)
        self.second = SqliteClient(self.path)
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)
        d.initialize(self.first)
        p.bootstrap_primary(self.first, "source", {"data/a.json": b'{"value":1}'}, "initial")

    def controlled_waiter(self, budget=None):
        clock = Clock()
        self.second.clock, self.second.sleep = clock, clock.sleep
        if budget is not None:
            self.second.deadline = budget
        return clock

    def assert_code(self, expected, action):
        with self.assertRaises(StorageError) as error:
            action()
        self.assertEqual(error.exception.code, expected)

    def test_reader_waits_for_independent_writer_longer_than_old_six_second_limit(self):
        ready = threading.Event()

        def writer():
            client = SqliteClient(self.path)
            try:
                with p.maintenance(client, "write"):
                    ready.set()
                    # Reproduce the real failure window, without mocking SQL,
                    # the database clock, sleep, or the contender's retry loop.
                    time.sleep(7)
                    return p.publish_patch(client, "source", base_generation=1,
                        changed={"data/a.json": b'{"value":2}'}, operation_id="concurrent-writer")
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(writer)
            self.assertTrue(ready.wait(5))
            started = time.monotonic()
            head, files = p.read_files(self.second, "source")
            elapsed = time.monotonic() - started
            written = pending.result(timeout=5)
        self.assertGreaterEqual(elapsed, 6.5)
        self.assertLess(elapsed, 20)
        self.assertEqual(head["generation"], written["generation"])
        self.assertEqual(files, {"data/a.json": b'{"value":2}'})

    def test_wait_uses_remaining_client_budget_and_does_not_steal_live_lease(self):
        clock = self.controlled_waiter(budget=2.25)
        with p.maintenance(self.first, "write") as owner:
            with patch.object(self.second, "execute", wraps=self.second.execute) as execute:
                self.assert_code("TIME_BUDGET_EXCEEDED", lambda: p.read_files(self.second, "source"))
                calls_at_deadline = execute.call_count
                self.assert_code("TIME_BUDGET_EXCEEDED", lambda: p.Lease(self.second, "read").acquire())
                self.assertEqual(execute.call_count, calls_at_deadline)
            row = self.first.execute("SELECT owner,fence FROM radar_leases")[0]
            self.assertEqual(row, {"owner": owner.owner, "fence": owner.fence})
        self.assertEqual(clock.now, 2.25)
        self.assertTrue(clock.sleeps)
        self.assertTrue(all(0 < delay <= 5 for delay in clock.sleeps))

    def test_git_ack_waits_for_maintenance_without_replaying_committed_publication(self):
        receipt = p.publish_patch(self.first, "source", base_generation=1,
            changed={"data/a.json": b'{"value":2}'}, operation_id="already-published")
        git_revision = "b" * 40
        clock = self.controlled_waiter(budget=60)
        released = []
        with p.maintenance(self.first, "gc") as owner:
            def finish_maintenance_after_wait(delay):
                clock.sleep(delay)
                if clock.now >= 9 and not released:
                    owner.release()
                    released.append(clock.now)

            self.second.sleep = finish_maintenance_after_wait
            # The external Git publication has already returned its real SHA.
            # Retrying acquisition for ACK must not re-run the database patch
            # (or require the caller to replay the external publication).
            with patch.object(p, "publish_patch", side_effect=AssertionError("Do not replay publication")):
                acknowledged = p.acknowledge_projection(self.second, "source", "already-published", git_revision,
                    generation=receipt["generation"], manifest_digest=receipt["manifest_digest"])
        self.assertGreaterEqual(clock.now, 9)
        self.assertFalse(acknowledged["projection_pending"])
        self.assertEqual(p.projection_pending(self.second, "source"), [])
        self.assertEqual(self.second.execute("SELECT projection_git_revision FROM radar_operations"),
                         [{"projection_git_revision": git_revision}])
        self.assertEqual(p.resolve_git_revision(self.second, "source", git_revision)["generation"], 2)
        self.assertEqual(d._head(self.second, "source")["generation"], 2)
        self.assertEqual(self.second.execute("SELECT COUNT(*) AS n FROM radar_operations")[0]["n"], 1)

    def test_busy_lease_still_has_independent_finite_wait_limit(self):
        clock = self.controlled_waiter(budget=600)
        with p.maintenance(self.first, "gc"), patch.object(p, "LEASE_WAIT_SECONDS", 180):
            self.assert_code("LEASE_BUSY", lambda: p.read_files(self.second, "source"))
        self.assertEqual(clock.now, 180)
        self.assertTrue(all(0 < delay <= 5 for delay in clock.sleeps))
        self.assertEqual(d.read_snapshot(self.first, "source")[1], {"data/a.json": b'{"value":1}'})

    def test_expired_owner_cannot_release_or_renew_replacement_lease(self):
        stale = p.Lease(self.first, "write").acquire()
        self.first.execute("UPDATE radar_leases SET expires_at=0")
        current = p.Lease(self.second, "write").acquire()
        try:
            self.assertGreater(current.fence, stale.fence)
            stale.release()
            self.assert_code("LEASE_LOST", lambda: stale.renew(force=True))
            row = self.second.execute("SELECT owner,fence FROM radar_leases")[0]
            self.assertEqual(row, {"owner": current.owner, "fence": current.fence})
        finally:
            current.release()

    def test_committed_acquire_with_lost_response_recovers_same_fence(self):
        execute = self.second.execute
        lost = []

        def interrupted(sql, params=()):
            rows = execute(sql, params)
            if sql.startswith("INSERT INTO radar_leases") and not lost:
                lost.append(True)
                raise StorageError("NETWORK_ERROR", "Response lost after commit.")
            return rows

        with patch.object(self.second, "execute", side_effect=interrupted):
            lease = p.Lease(self.second, "read").acquire()
        try:
            self.assertEqual(self.second.execute("SELECT owner,fence FROM radar_leases")[0],
                             {"owner": lease.owner, "fence": lease.fence})
        finally:
            lease.release()

    def test_reentrant_maintenance_reuses_lease_and_original_fence(self):
        with p.maintenance(self.first, "write") as outer:
            with p.maintenance(self.first, "read") as inner:
                self.assertIs(inner, outer)
                self.assertEqual(p.read_files(self.first, "source")[1], {"data/a.json": b'{"value":1}'})
            self.assertEqual(self.first.execute("SELECT owner,fence FROM radar_leases")[0],
                             {"owner": outer.owner, "fence": outer.fence})


if __name__ == "__main__":
    unittest.main()
