"""Offline fixtures exercise authoritative identities, dates and enrollment."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from radar_core.domain.twitch_admission import (
    METHOD, TW_STORE_DATE_AUTHORITY, TW_STORE_DATE_PROVIDER, aware_time,
    decimal_id, has_taiwan_store_date_authority, has_twitch_admission,
    is_twitch_qualified, normalize_twitch_admission, preserve_twitch_admission,
    resolve_store_release_day, validate_twitch_snapshot,
)


def proof():
    return {
        "schema_version": 1, "method": METHOD, "appid": 123,
        "twitch_game_id": "22", "igdb_id": "33",
        "checked_at": "2026-10-02T08:00:00Z",
        "source_frontend_commit": "a" * 40,
        "source_enrollment": {
            "source": "igdb_first_release_date",
            "observed_at": "2026-10-01T08:00:00Z",
            "viewer_count": 8000, "min_viewers": 7000,
        },
    }


def row():
    return {
        "appid": 123, "twitch_admission": proof(), "steam_type": "game",
        "sexual_content_screened": True, "release_start": "2026-10-02",
        "release_end": "2026-10-02", "release_precision": "day",
        "release_display_precision": "date_full",
        "release_date_timezone": "Asia/Taipei",
        "release_time_utc": "2026-10-02T07:00:00Z",
        "release_timestamp_taipei_date": "2026-10-02",
        "followers": 0, "follower_checked_at": "2026-10-02T06:00:00Z",
    }


def snapshot():
    evidence = proof()["source_enrollment"]
    registry = {"schema_version": 1, "games": {"22": {
        "game_id": "22", "igdb_id": "33", "tracking_sources": {"twitch_new": {
            "source": "twitch_new", "status": "active",
            "expires_at": "2026-10-20T00:00:00Z", "enrollment": evidence,
        }},
    }}}
    discovery = {"schema_version": 1, "steam_source_id": "777", "games": {"22": {
        "twitch_game_id": "22", "igdb_id": "33", "active": True,
        "status": "matched", "method": METHOD,
        "checked_at": "2026-10-02T08:00:00Z", "twitch_enrollment": evidence,
        "steam_appids": ["123"], "links": [{
            "external_game_id": "444", "external_game_source": "777",
            "uid": "123", "steam_appid": "123", "game": "33",
        }],
    }}}
    return registry, discovery


class AdmissionTests(unittest.TestCase):
    def test_identifiers_reject_booleans_zero_and_non_ascii_digits(self):
        self.assertEqual(decimal_id(123), "123")
        self.assertEqual(decimal_id("123"), "123")
        for value in (True, False, "0", "0123", "１２３", " 123", 1.0, None):
            with self.subTest(value=value):
                self.assertIsNone(decimal_id(value))

    def test_timestamp_must_be_timezone_aware(self):
        self.assertEqual(aware_time("2026-10-02T08:00:00Z").utcoffset().total_seconds(), 0)
        for value in ("2026-10-02T08:00:00", "not-a-time", True, None):
            self.assertIsNone(aware_time(value))

    def test_proof_requires_original_threshold_and_immutable_source(self):
        self.assertIsNotNone(normalize_twitch_admission(proof(), "123"))
        for field, value in (("schema_version", True), ("appid", 456),
                             ("source_frontend_commit", "main"),
                             ("method", "name_search"), ("igdb_id", "0")):
            bad = proof(); bad[field] = value
            with self.subTest(field=field):
                self.assertIsNone(normalize_twitch_admission(bad, 123))
        for field, value in (("source", "steam_recent_release"),
                             ("viewer_count", 6999), ("viewer_count", 8000.0),
                             ("min_viewers", True), ("qualification", "unverified")):
            bad = proof(); bad["source_enrollment"][field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(normalize_twitch_admission(bad, 123))

    def test_enrollment_cannot_postdate_check_and_normalization_copies_evidence(self):
        original = proof()
        normalized = normalize_twitch_admission(original, 123)
        normalized["source_enrollment"]["viewer_count"] = 9999
        self.assertEqual(original["source_enrollment"]["viewer_count"], 8000)
        original["source_enrollment"]["observed_at"] = "2026-10-03T08:00:00Z"
        self.assertIsNone(normalize_twitch_admission(original, 123))

    def test_registry_requires_same_active_identity_enrollment_and_external_source(self):
        registry, discovery = snapshot()
        clock = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        self.assertTrue(validate_twitch_snapshot(proof(), registry, discovery, clock))
        for mutation in ("expired", "igdb", "enrollment", "pending", "external_source",
                         "missing_link", "feedback", "malformed_expiry"):
            r, d = deepcopy(registry), deepcopy(discovery)
            source = r["games"]["22"]["tracking_sources"]["twitch_new"]
            discovered = d["games"]["22"]
            if mutation == "expired": source["expires_at"] = clock.isoformat()
            elif mutation == "igdb": r["games"]["22"]["igdb_id"] = "44"
            elif mutation == "enrollment": source["enrollment"]["viewer_count"] = 9000
            elif mutation == "pending": discovered["status"] = "pending"
            elif mutation == "external_source": discovered["links"][0]["external_game_source"] = "1"
            elif mutation == "missing_link": discovered["links"] = []
            elif mutation == "feedback": source["source"] = "steam_recent_release"
            else: source["expires_at"] = "not-a-time"
            with self.subTest(mutation=mutation):
                self.assertFalse(validate_twitch_snapshot(proof(), r, d, clock))

    def test_utc_midnight_crossing_normalizes_to_taipei_day(self):
        result = resolve_store_release_day("2026-09-08", "2026-09-08T16:02:05Z")
        self.assertEqual(result, {
            "release_start": "2026-09-09", "release_store_date": "2026-09-08",
            "release_date_normalization": "steam_utc_date_normalized_to_taipei",
        })
        matching = resolve_store_release_day("2026-09-09", "2026-09-08T16:02:05Z")
        self.assertEqual(matching["release_date_normalization"], "steam_store_date_matches_taipei")

    def test_unverified_date_conflict_and_imprecise_dates_are_rejected(self):
        self.assertIsNone(resolve_store_release_day("2026-09-03", "2026-09-04T04:02:14Z"))
        for day in ("September 2026", "2026", "20260903", "2026-02-30"):
            self.assertIsNone(resolve_store_release_day(day, "2026-09-04T04:02:14Z",
                                                       allow_taiwan_store_authority=True))

    def test_verified_taiwan_store_authority_requires_complete_audit(self):
        game = row()
        game.update({
            "release_start": "2026-10-01", "release_end": "2026-10-01",
            "release_store_date": "2026-10-01",
            "release_date_normalization": TW_STORE_DATE_AUTHORITY,
            "release_display_provider": TW_STORE_DATE_PROVIDER,
            "release_date_verified_at": "2026-10-02T08:00:00Z",
            "release_date_conflict": True,
        })
        self.assertTrue(has_taiwan_store_date_authority(game))
        self.assertTrue(is_twitch_qualified(game))
        for field, value in (("release_display_provider", "another_source"),
                             ("release_date_verified_at", None),
                             ("release_date_conflict", 1),
                             ("release_timestamp_taipei_date", "2026-10-01")):
            invalid = deepcopy(game); invalid[field] = value
            with self.subTest(field=field):
                self.assertFalse(has_taiwan_store_date_authority(invalid))
                self.assertFalse(is_twitch_qualified(invalid))

    def test_twitch_admission_is_independent_of_ordinary_follower_threshold(self):
        game = row()
        self.assertTrue(has_twitch_admission(game))
        self.assertTrue(is_twitch_qualified(game))
        for field, value in (("followers", True), ("followers", -1),
                             ("follower_checked_at", None), ("steam_type", "dlc"),
                             ("sexual_content_screened", False),
                             ("release_date_conflict", True),
                             ("release_start", "2026-10-03")):
            invalid = deepcopy(game); invalid[field] = value
            with self.subTest(field=field):
                self.assertFalse(is_twitch_qualified(invalid))

    def test_refresh_keeps_newer_independent_admission_without_mutating_inputs(self):
        existing = row()
        refreshed = {"appid": 123, "followers": 100}
        result = preserve_twitch_admission(existing, refreshed)
        self.assertEqual(result["twitch_admission"], proof())
        self.assertEqual(result["followers"], 100)
        self.assertTrue(result["sexual_content_screened"])
        self.assertNotIn("twitch_admission", refreshed)
        unrelated = preserve_twitch_admission(existing, {"appid": 456, "followers": 100})
        self.assertNotIn("twitch_admission", unrelated)
        newer = deepcopy(refreshed); newer["twitch_admission"] = proof()
        newer["twitch_admission"]["checked_at"] = "2026-10-03T08:00:00Z"
        self.assertEqual(preserve_twitch_admission(existing, newer)["twitch_admission"],
                         newer["twitch_admission"])


if __name__ == "__main__":
    unittest.main()
