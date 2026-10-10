"""A missing GroupID is explicit unknown evidence, never a zero-count shortcut."""

from copy import deepcopy
from itertools import combinations
import unittest

from test_admission import row
from radar_core.domain.twitch_admission import (
    TW_STORE_DATE_AUTHORITY, TW_STORE_DATE_PROVIDER, UNKNOWN_FOLLOWER_FIELDS,
    has_unavailable_group_followers, is_twitch_qualified, preserve_follower_measurement,
    preserve_twitch_admission,
)


def unknown_row():
    game = row()
    game.update({
        "followers": None, "follower_checked_at": None, "follower_source": None,
        "official_ge5000": False, "group_id64": None,
        "follower_status": "unavailable_group_id",
        "follower_unavailable_at": "2026-10-02T09:00:00Z",
    })
    return game


class UnknownFollowerTests(unittest.TestCase):
    def test_complete_unknown_row_is_qualified_without_claiming_zero(self):
        game = unknown_row()
        self.assertTrue(has_unavailable_group_followers(game))
        self.assertTrue(is_twitch_qualified(game))
        self.assertIsNone(game["followers"])
        self.assertIsNone(game["follower_checked_at"])
        self.assertIsNone(game["follower_source"])
        self.assertIs(game["official_ge5000"], False)
        game.pop("group_id64")
        self.assertTrue(is_twitch_qualified(game))

    def test_every_incomplete_unknown_marker_subset_is_rejected(self):
        for size in range(1, len(UNKNOWN_FOLLOWER_FIELDS) + 1):
            for missing in combinations(UNKNOWN_FOLLOWER_FIELDS, size):
                game = unknown_row()
                for field in missing:
                    game.pop(field)
                with self.subTest(missing=missing):
                    self.assertFalse(has_unavailable_group_followers(game))
                    self.assertFalse(is_twitch_qualified(game))

    def test_unknown_requires_exact_nulls_false_status_and_no_group(self):
        mutations = (
            ("followers", 0), ("followers", True), ("followers", "0"),
            ("followers", -1), ("followers", 0.0),
            ("follower_checked_at", "2026-10-02T09:00:00Z"),
            ("follower_source", "Steam Community XML memberCount"),
            ("follower_source", ""), ("official_ge5000", 0),
            ("official_ge5000", True), ("official_ge5000", "false"),
            ("follower_status", "unavailable"), ("follower_status", None),
            ("group_id64", "103582791429521412"), ("group_id64", ""),
            ("group_id64", 0), ("group_id64", False),
            ("official_group_id64", "103582791429521412"),
            ("group_short_id", 123),
        )
        for field, value in mutations:
            game = unknown_row(); game[field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(has_unavailable_group_followers(game))
                self.assertFalse(is_twitch_qualified(game))

    def test_unknown_timestamp_is_aware_and_cannot_predate_identity_check(self):
        for stamp in (None, True, 123, "bad", "2026-10-02T09:00:00",
                      "2026-10-02T07:59:59Z", "2026-10-02T15:59:59+08:00"):
            game = unknown_row(); game["follower_unavailable_at"] = stamp
            with self.subTest(stamp=stamp):
                self.assertFalse(is_twitch_qualified(game))
        for stamp in ("2026-10-02T08:00:00Z", "2026-10-02T16:00:00+08:00",
                      "2026-10-02T08:00:01Z"):
            game = unknown_row(); game["follower_unavailable_at"] = stamp
            with self.subTest(stamp=stamp):
                self.assertTrue(is_twitch_qualified(game))

    def test_unknown_has_the_same_identity_enrollment_date_and_content_gates(self):
        mutations = (
            (("appid",), 456), (("steam_type",), "dlc"),
            (("sexual_content_screened",), False),
            (("sexual_content_screened",), 1), (("release_precision",), "month"),
            (("release_display_precision",), "date_month"),
            (("release_date_timezone",), "UTC"),
            (("release_time_utc",), "2026-10-02T07:00:00"),
            (("release_time_utc",), None), (("release_start",), "2026-10-03"),
            (("release_end",), "2026-10-03"), (("release_date_conflict",), True),
            (("release_timestamp_taipei_date",), "2026-10-01"),
            (("twitch_admission",), None),
            (("twitch_admission", "schema_version"), True),
            (("twitch_admission", "method"), "name_search"),
            (("twitch_admission", "appid"), 456),
            (("twitch_admission", "twitch_game_id"), "0"),
            (("twitch_admission", "igdb_id"), "0"),
            (("twitch_admission", "checked_at"), "not-a-time"),
            (("twitch_admission", "source_frontend_commit"), "main"),
            (("twitch_admission", "source_enrollment", "viewer_count"), 6999),
            (("twitch_admission", "source_enrollment", "viewer_count"), True),
            (("twitch_admission", "source_enrollment", "min_viewers"), 5000),
            (("twitch_admission", "source_enrollment", "qualification"), "unverified"),
            (("twitch_admission", "source_enrollment", "source"), "steam_recent_release"),
            (("twitch_admission", "source_enrollment", "observed_at"), "2026-10-02T08:01:00Z"),
        )
        for path, value in mutations:
            for fixture in (row, unknown_row):
                game = fixture()
                target = game
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.subTest(path=path, value=value, fixture=fixture.__name__):
                    self.assertFalse(is_twitch_qualified(game))

    def test_verified_taiwan_store_date_authority_applies_to_unknown_count(self):
        game = unknown_row()
        game.update({
            "release_start": "2026-10-01", "release_end": "2026-10-01",
            "release_store_date": "2026-10-01",
            "release_date_normalization": TW_STORE_DATE_AUTHORITY,
            "release_display_provider": TW_STORE_DATE_PROVIDER,
            "release_date_verified_at": "2026-10-02T08:00:00Z",
            "release_date_conflict": True,
        })
        self.assertTrue(is_twitch_qualified(game))
        game["release_display_provider"] = "unverified"
        self.assertFalse(is_twitch_qualified(game))

    def test_real_zero_and_positive_counts_keep_existing_measurement_contract(self):
        for count in (0, 1, 4999, 5000, 123456):
            game = row(); game["followers"] = count
            with self.subTest(count=count):
                self.assertTrue(is_twitch_qualified(game))
                self.assertFalse(has_unavailable_group_followers(game))
        for count in (True, False, "0", "5000", -1, 5000.0, None):
            game = row(); game["followers"] = count
            with self.subTest(count=count):
                self.assertFalse(is_twitch_qualified(game))

    def test_real_count_with_stale_unknown_marker_needs_refresh_normalization(self):
        game = unknown_row()
        game.update({"followers": 0, "follower_checked_at": "2026-10-03T08:00:00Z",
                     "follower_source": "Steam Community XML memberCount"})
        self.assertFalse(is_twitch_qualified(game))
        incoming = deepcopy(game)
        result = preserve_twitch_admission(unknown_row(), incoming)
        self.assertTrue(is_twitch_qualified(result))
        self.assertEqual(result["followers"], 0)
        self.assertNotIn("follower_status", result)
        self.assertNotIn("follower_unavailable_at", result)
        self.assertIn("follower_status", incoming)

    def test_title_only_refresh_retains_existing_complete_unknown_observation(self):
        existing = unknown_row()
        incoming = {"appid": 123, "name": "A translated title"}
        result = preserve_twitch_admission(existing, incoming)
        for field in UNKNOWN_FOLLOWER_FIELDS:
            self.assertEqual(result[field], existing[field])
        self.assertEqual(result["name"], incoming["name"])
        self.assertTrue(has_unavailable_group_followers(result))
        self.assertNotIn("followers", incoming)

    def test_refresh_cannot_invent_unknown_evidence_for_explicit_null_or_other_game(self):
        existing = unknown_row()
        for incoming in ({"appid": 123, "followers": None},
                         {"appid": 123, "follower_status": "unavailable_group_id"},
                         {"appid": 123, "group_id64": "103582791429521412"},
                         {"appid": 456, "name": "Other game"}):
            result = preserve_twitch_admission(existing, incoming)
            with self.subTest(incoming=incoming):
                self.assertFalse(has_unavailable_group_followers(result))
                self.assertNotIn("follower_unavailable_at", result)
        existing["sexual_content_screened"] = False
        result = preserve_twitch_admission(existing, {"appid": 123, "name": "Updated"})
        self.assertNotIn("followers", result)

    def test_newer_missing_group_cannot_erase_any_previous_real_measurement(self):
        for count in (0, 17, 5000):
            for preserve in (preserve_follower_measurement, preserve_twitch_admission):
                existing = row()
                existing.update({
                    "followers": count, "follower_source": "Steam Community XML memberCount",
                    "official_ge5000": count >= 5000,
                    "group_id64": "103582791429521412", "group_short_id": 123,
                })
                incoming = unknown_row()
                incoming["follower_unavailable_at"] = "2026-10-04T09:00:00Z"
                before = deepcopy((existing, incoming))
                result = preserve(existing, incoming)
                with self.subTest(count=count, preserve=preserve.__name__):
                    for field in ("followers", "follower_checked_at", "follower_source",
                                  "official_ge5000", "group_id64", "group_short_id"):
                        self.assertEqual(result[field], existing[field])
                    self.assertNotIn("follower_status", result)
                    self.assertNotIn("follower_unavailable_at", result)
                    self.assertTrue(is_twitch_qualified(result))
                    self.assertEqual((existing, incoming), before)

    def test_null_refresh_cannot_preserve_invalid_or_unrelated_measurement(self):
        for field, value in (("followers", True), ("followers", -1),
                             ("followers", "5000"), ("follower_checked_at", None),
                             ("follower_checked_at", "2026-10-02T06:00:00"),
                             ("appid", 456), ("appid", None)):
            existing = row(); existing[field] = value
            result = preserve_follower_measurement(existing, unknown_row())
            with self.subTest(field=field, value=value):
                self.assertIsNone(result["followers"])
                self.assertEqual(result["follower_status"], "unavailable_group_id")

    def test_measurement_helper_leaves_numeric_freshness_to_existing_consumer(self):
        existing = row()
        incoming = row()
        incoming.update({"followers": 42, "follower_checked_at": "2026-10-01T06:00:00Z"})
        self.assertEqual(preserve_follower_measurement(existing, incoming), incoming)
        existing.update({"followers": 5000, "follower_source": "old source"})
        for incoming in ({"appid": 123, "name": "New name"},
                         {"appid": 123, "followers": None}):
            result = preserve_follower_measurement(existing, incoming)
            self.assertEqual(result["followers"], 5000)
            self.assertEqual(result["follower_checked_at"], existing["follower_checked_at"])
            self.assertEqual(result["follower_source"], "old source")


if __name__ == "__main__":
    unittest.main()
