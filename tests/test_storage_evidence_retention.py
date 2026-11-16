"""Offline checks for releasing unreferenced evidence pins (no Turso or D1)."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from radar_core.storage import documents, primary_store
from radar_core.storage.sql import SqliteClient, StorageError
from scripts import storage_evidence_retention as retention

FRONTEND = "danielet087/game-trend-radar@main"
INPUTS = ("data/twitch_steam_discovery.json", "data/twitch_tracking.json",
          "data/steam_upcoming.json", "data/twitch_steam_mapping.json")
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _doc(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


class EvidenceRetentionTest(unittest.TestCase):
    def setUp(self):
        self.db = SqliteClient()
        self.addCleanup(self.db.close)
        documents.initialize(self.db)
        files = {path: _doc({"path": path, "version": 1}) for path in INPUTS}
        files["data/catalog.json"] = _doc({"games": []})
        primary_store.bootstrap_primary(self.db, FRONTEND, files, "a" * 40, captured_at=T0.isoformat())
        self.old = self._pin()                                   # referenced later
        self.unreferenced = self._advance(1, {"data/twitch_tracking.json": _doc({"version": 2})})
        # The Twitch archive mentions the unreferenced pin, but archives are not scanned.
        self.current = self._advance(2, {
            "data/twitch_tracking.json": _doc({"version": 3}),
            "data/catalog.json": _doc({"games": [{"twitch_admission": {
                "source_database_revision": self.old["origin_manifest_digest"]}}]}),
            "data/twitch_history/2026-11-02.json": _doc({"mention": self.unreferenced["origin_manifest_digest"]}),
        }, days=5)

    def _advance(self, day, changed, *, days=None):
        head, _ = primary_store.read_files(self.db, FRONTEND, paths=())
        when = T0 + timedelta(days=days if days is not None else day)
        primary_store.publish_patch(self.db, FRONTEND, base_generation=head["generation"], changed=changed,
                                    operation_id=f"op-{day}", captured_at=when.isoformat(),
                                    projection_required=False)
        return self._pin()

    def _pin(self):
        head, _ = primary_store.read_files(self.db, FRONTEND, paths=())
        return primary_store.pin_evidence(self.db, FRONTEND, revision=head["manifest_digest"], paths=INPUTS,
                                          evidence_id="steam-twitch-" + head["manifest_digest"])

    def ids(self):
        return {row["evidence_id"] for row in self.db.execute("SELECT evidence_id FROM radar_evidence")}

    def test_dry_run_reports_without_releasing(self):
        report = retention.run(self.db, apply=False, grace_days=3, now=T0 + timedelta(days=6))
        self.assertEqual(report["evidence_total"], 3)
        self.assertEqual(report["kept"], {"legacy": 0, "recent": 1, "referenced": 1})
        self.assertEqual(report["release_candidates"], 1)
        self.assertEqual(report["released"], 0)
        self.assertGreater(report["estimated_release"]["encoded_chunk_bytes"], 0)
        self.assertEqual(len(self.ids()), 3)

    def test_apply_releases_only_unreferenced_old_pins_and_gc_reclaims_them(self):
        report = retention.run(self.db, apply=True, grace_days=3, now=T0 + timedelta(days=6))
        self.assertEqual(report["released"], 1)
        self.assertEqual(report["released_evidence_ids"], [self.unreferenced["evidence_id"]])
        self.assertEqual(self.ids(), {self.old["evidence_id"], self.current["evidence_id"]})
        # Replication acknowledges old events within the hour; only then can GC
        # reclaim the bytes that the released pin alone retained.
        with primary_store.maintenance(self.db, "replicate"):
            head, _ = primary_store.read_files(self.db, FRONTEND, paths=())
            documents._ack(self.db, FRONTEND, head["generation"])
        collected = documents.garbage_collect(self.db, exclusive=True)
        self.assertGreaterEqual(collected["objects_deleted"], 1)
        # The referenced historical snapshot stays readable; the released one does not.
        _, files = primary_store.read_files(self.db, FRONTEND, paths=INPUTS,
                                            revision=self.old["origin_manifest_digest"])
        self.assertEqual(json.loads(files["data/twitch_tracking.json"])["version"], 1)
        with self.assertRaises(StorageError):
            primary_store.read_files(self.db, FRONTEND, paths=INPUTS,
                                     revision=self.unreferenced["origin_manifest_digest"])

    def test_grace_window_keeps_every_recent_pin(self):
        report = retention.run(self.db, apply=True, grace_days=30, now=T0 + timedelta(days=6))
        self.assertEqual(report["release_candidates"], 0)
        self.assertEqual(len(self.ids()), 3)

    def test_plan_always_keeps_legacy_pins(self):
        rows = [{"evidence_id": "legacy-git:" + "1" * 64, "origin_manifest_digest": "2" * 64,
                 "manifest_digest": "3" * 64, "captured_at": "2026-09-01T00:00:00+00:00"},
                {"evidence_id": "steam-twitch-" + "4" * 64, "origin_manifest_digest": "4" * 64,
                 "manifest_digest": "5" * 64, "captured_at": "2026-09-01T00:00:00+00:00"},
                {"evidence_id": "steam-twitch-" + "6" * 64, "origin_manifest_digest": "6" * 64,
                 "manifest_digest": "7" * 64, "captured_at": "2026-09-01T00:00:00+00:00"}]
        keep, release = retention.plan(rows, {"7" * 64}, now=T0, grace=timedelta(days=3))
        self.assertEqual([row["evidence_id"] for row in keep["legacy"]], [rows[0]["evidence_id"]])
        self.assertEqual([row["evidence_id"] for row in keep["referenced"]], [rows[2]["evidence_id"]])
        self.assertEqual([row["evidence_id"] for row in release], [rows[1]["evidence_id"]])

    def test_release_limit_refuses_without_deleting(self):
        later = T0 + timedelta(days=30)
        report = retention.run(self.db, apply=True, grace_days=3, now=later, max_release=1)
        self.assertEqual((report["status"], report["error"]), ("refused", "RELEASE_LIMIT_EXCEEDED"))
        self.assertEqual((report["release_candidates"], report["released"]), (2, 0))
        self.assertEqual(len(self.ids()), 3)
        report = retention.run(self.db, apply=True, grace_days=3, now=later, max_release=2)
        self.assertEqual((report["status"], report["released"]), ("passed", 2))
        self.assertEqual(self.ids(), {self.old["evidence_id"]})

    def test_a_source_outside_the_scan_blocks_any_release(self):
        with mock.patch.object(retention, "SOURCES", ()):
            with self.assertRaises(StorageError) as caught:
                retention.run(self.db, apply=True, grace_days=3, now=T0 + timedelta(days=6))
        self.assertEqual(caught.exception.code, "UNSCANNED_SOURCE")
        self.assertEqual(len(self.ids()), 3)

    def test_naive_capture_times_and_invalid_grace_fail_closed(self):
        rows = [{"evidence_id": "steam-twitch-x", "origin_manifest_digest": "a" * 64,
                 "manifest_digest": "b" * 64, "captured_at": "2026-09-01T00:00:00"}]
        with self.assertRaises(StorageError):
            retention.plan(rows, set(), now=T0, grace=timedelta(days=3))
        with self.assertRaises(ValueError):
            retention.run(self.db, grace_days=0)
        with self.assertRaises(ValueError):
            retention.run(self.db, max_release=0)


if __name__ == "__main__":
    unittest.main()
