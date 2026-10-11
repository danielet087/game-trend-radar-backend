"""Offline boundaries for external slots and persisted daily reset ownership."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from scripts.external_schedule import (
    daily_reset_required, daily_slot, growth_collection_complete, schedule_decision,
    stamp_growth_publication,
)


def accounted_growth_report():
    """One official measurement and one evidenced unavailable official group."""
    return {
        "coverage_schema_version": 2,
        "collection_date_taipei": "2026-10-11",
        "generated_at": "2026-10-11T01:20:00+08:00",
        "reason": "completed", "eligible": 2, "eligible_appids": [10, 20],
        "collection_complete": True, "measurement_complete": False,
        "measurements": [{"appid": 10, "followers": 5001,
                          "at": "2026-10-11T01:16:00+08:00", "source": "steam_community"}],
        "unavailable": [{
            "appid": 20, "reason": "official_group_not_found",
            "group_resolution": {
                "status": "not_found", "source": "Steam ISteamUser/ResolveVanityURL",
                "checked_at": "2026-10-11T01:17:00+08:00", "attempted": True,
                "http": 200, "api_result": 42, "retry_at": "2026-10-18T01:17:00+08:00",
            },
        }],
        "pending": [], "errors": [],
        "published_eligible_appids": [10, 20], "published_resolvable_appids": [10],
    }


class ExternalScheduleTests(unittest.TestCase):
    def decision(self, workflow, slot, now, refresh=False):
        return schedule_decision(workflow, "workflow_dispatch", "cloudflare", slot,
                                 refresh, datetime.fromisoformat(now.replace("Z", "+00:00")))[0]

    def test_followers_current_hour_and_late_hour(self):
        slot = "2026-10-02T19:00:00Z"  # Taiwan October 3, 03:00.
        self.assertTrue(self.decision("official-followers", slot, "2026-10-02T19:59:59Z"))
        self.assertFalse(self.decision("official-followers", slot, "2026-10-02T20:00:00Z"))
        self.assertFalse(self.decision("official-followers", slot, "2026-10-03T19:00:00Z"))
        self.assertFalse(self.decision("official-followers", slot, "2026-10-02T18:59:59Z"))

    def test_manual_bypass_and_legacy_schedule_window(self):
        midnight = datetime(2026, 10, 2, 16, tzinfo=timezone.utc)
        self.assertTrue(schedule_decision("official-followers", "workflow_dispatch", "manual", "", False, midnight)[0])
        self.assertFalse(schedule_decision("official-followers", "schedule", "", "", False, midnight)[0])

    def test_daily_refresh_required_and_taiwan_day_boundary(self):
        slot = "2026-10-02T16:00:00Z"
        with self.assertRaises(ValueError):
            self.decision("daily-discovery", slot, slot)
        self.assertTrue(self.decision("daily-discovery", slot, "2026-10-03T15:59:59Z", True))
        self.assertFalse(self.decision("daily-discovery", slot, "2026-10-03T16:00:00Z", True))
        self.assertTrue(self.decision("public-growth", "2026-10-02T17:15:00Z", "2026-10-02T18:00:00Z"))
        with self.assertRaises(ValueError):
            self.decision("public-growth", slot, "2026-10-02T18:00:00Z")

    def test_external_slot_rejects_wrong_window_or_missing_timezone(self):
        for slot in ("", "2026-10-03T03:00:00", "2026-10-03T03:00:00+08:00",
                     "2026-10-02T18:00:00Z", "2026-10-02T19:01:00Z"):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                self.decision("official-followers", slot, "2026-10-02T19:10:00Z")

    def test_six_hour_recovery_slots_allow_only_their_taiwan_day(self):
        for workflow, hours, minute, refresh in (
            ("daily-discovery", (0, 6, 12, 18), 0, True),
            ("public-growth", (1, 7, 13, 19), 15, False),
        ):
            for hour in hours:
                local = datetime.fromisoformat(f"2026-10-03T{hour:02d}:{minute:02d}:00+08:00")
                slot = local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                with self.subTest(workflow=workflow, hour=hour):
                    self.assertTrue(self.decision(workflow, slot, "2026-10-03T15:59:59Z", refresh))
                    self.assertFalse(self.decision(workflow, slot, "2026-10-03T16:00:00Z", refresh))
                    self.assertFalse(self.decision(workflow, slot, "2026-10-02T15:59:59Z", refresh))
            for local_time in ("05:00", "06:15", "07:00", "13:00", "18:15", "23:15"):
                local = datetime.fromisoformat(f"2026-10-03T{local_time}:00+08:00")
                slot = local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                with self.subTest(workflow=workflow, invalid_time=local_time), self.assertRaises(ValueError):
                    self.decision(workflow, slot, "2026-10-03T15:59:59Z", refresh)

    def test_growth_incomplete_progress_does_not_count_as_daily_success(self):
        now = datetime.fromisoformat("2026-10-03T12:00:00+08:00")
        measurement = {"appid": 10, "followers": 5000, "at": "2026-10-03T01:16:00+08:00"}
        result = {"reason": "completed", "eligible": 1, "errors": [], "measurements": [measurement]}
        self.assertTrue(growth_collection_complete(result, now))
        self.assertTrue(growth_collection_complete({**result, "eligible": 0, "measurements": []}, now))
        for updates in (
            {"reason": "rate_limited"}, {"reason": "bounded_run"}, {"reason": "source_unavailable"},
            {"errors": [20]}, {"eligible": 2}, {"measurements": []},
            {"eligible": 2, "measurements": [measurement, measurement]},
            {"eligible": True}, {"measurements": [{**measurement, "followers": -1}]},
            {"measurements": [{**measurement, "at": "2026-10-02T23:59:59+08:00"}]},
            {"measurements": [{**measurement, "at": "2026-10-03T12:00:01+08:00"}]},
            {"measurements": [{**measurement, "at": "2026-10-03T01:16:00"}]},
        ):
            with self.subTest(updates=updates):
                self.assertFalse(growth_collection_complete({**result, **updates}, now))
        self.assertFalse(growth_collection_complete({"measurements": [measurement]}, now))

    def test_verified_unavailability_can_finish_daily_work_without_inventing_a_measurement(self):
        report = accounted_growth_report()
        now = datetime.fromisoformat("2026-10-11T07:15:00+08:00")
        finished = stamp_growth_publication(report, now, state_persisted=True, published=True)
        self.assertTrue(growth_collection_complete(finished, now))
        self.assertEqual(len(finished["measurements"]), 1)
        self.assertFalse(finished["measurement_complete"])
        self.assertNotIn("followers", finished["unavailable"][0])
        self.assertEqual(finished["measurements"][0]["followers"], 5001)
        for persisted, published in ((False, True), (True, False), (False, False)):
            with self.subTest(persisted=persisted, published=published):
                failed = stamp_growth_publication(
                    finished, now, state_persisted=persisted, published=published,
                )
                self.assertFalse(growth_collection_complete(failed, now))
                self.assertEqual(failed["job_result"]["status"], "failed")

    def test_unresolved_retry_and_new_public_games_keep_six_hour_recovery_available(self):
        now = datetime.fromisoformat("2026-10-11T07:15:00+08:00")
        for updates in (
            {"reason": "awaiting_group_resolution", "collection_complete": False,
             "unavailable": [], "pending": [{"appid": 20, "reason": "group_missing_api_key"}]},
            {"reason": "source_unavailable", "collection_complete": False, "errors": [20]},
            {"published_eligible_appids": [10, 20, 30]},
            {"published_resolvable_appids": [10, 20]},
        ):
            report = {**accounted_growth_report(), **updates}
            delivered = stamp_growth_publication(report, now, state_persisted=True, published=True)
            with self.subTest(updates=updates):
                self.assertFalse(growth_collection_complete(delivered, now))
                for hour in (7, 13, 19):
                    local = datetime.fromisoformat(f"2026-10-11T{hour:02d}:15:00+08:00")
                    slot = local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    self.assertTrue(schedule_decision(
                        "public-growth", "workflow_dispatch", "cloudflare", slot, False, local,
                    )[0])

    def test_empty_new_coverage_cannot_be_reused_across_taiwan_midnight(self):
        report = {**accounted_growth_report(), "eligible": 0, "eligible_appids": [],
                  "measurements": [], "unavailable": [], "measurement_complete": True,
                  "published_eligible_appids": [], "published_resolvable_appids": []}
        same_day = datetime.fromisoformat("2026-10-11T23:59:59+08:00")
        finished = stamp_growth_publication(report, same_day, state_persisted=True, published=True)
        self.assertTrue(growth_collection_complete(finished, same_day))
        self.assertFalse(growth_collection_complete(
            finished, datetime.fromisoformat("2026-10-12T00:00:00+08:00"),
        ))

    def test_persisted_same_slot_resumes_progress_and_manual_can_reset(self):
        day = date(2026, 10, 3)
        state = {"mode": "two_phase_steam_year", "anchor_date": day.isoformat(),
                 "last_reset_date_taipei": day.isoformat(), "daily_refresh_slot": daily_slot(day),
                 "phase": "prefilter", "days_scanned": 365, "prefilter_next_index": 1200}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            resumed = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(daily_reset_required(resumed, day))
            self.assertEqual(resumed, state)
            self.assertTrue(daily_reset_required(resumed, day, force=True))
            self.assertTrue(daily_reset_required(resumed, date(2026, 10, 4)))
            legacy_today = {key: value for key, value in resumed.items()
                            if key not in {"daily_refresh_slot", "last_reset_date_taipei"}}
            self.assertFalse(daily_reset_required(legacy_today, day))
            self.assertEqual(legacy_today["prefilter_next_index"], 1200)
            self.assertTrue(daily_reset_required(legacy_today, date(2026, 10, 4)))
            resumed["anchor_date"] = "2026-10-02"
            self.assertTrue(daily_reset_required(resumed, day))


if __name__ == "__main__":
    unittest.main()
