"""Regression: Steam adult-only titles must never reach the calendar again."""
from datetime import date

from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.screen_steam_candidates_before_followers import build_snapshot
from scripts.update_steam_daily import merge_partial_segment


def test_audited_exclusion_contains_freshwomen_and_other_known_titles():
    ids = excluded_appids()
    assert {4005870, 1032980, 3850550, 4450390, 3464000} <= ids
    assert is_disallowed({"appid": 4005870}, ids)
    assert is_disallowed({"appid": 999, "content_descriptorids": [3]}, ids)
    assert not is_disallowed({"appid": 999, "content_descriptorids": [1]}, ids)


def test_candidate_gate_blocks_audited_adult_even_with_incomplete_store_metadata():
    game = {
        "appid": 4005870,
        "name": "FreshWomen - Season 3",
        "release_precision": "day",
        "release_start": "2026-10-16",
    }
    snapshot = build_snapshot(
        {"games": [game]},
        {4005870: {"release": {"coming_soon_display": "date_full"}}},
    )
    assert snapshot["count"] == 0
    assert snapshot["reasons"]["audited_adult_exclusion"] == 1


def test_follower_master_merge_cannot_reinsert_audited_adult():
    previous = [{
        "appid": 4005870,
        "name": "FreshWomen - Season 3",
        "release_start": "2026-10-16",
        "release_end": "2026-10-16",
        "followers": 7268,
    }]
    clean = [{
        "appid": 4019220,
        "name": "Dressmaker",
        "release_start": "2026-09-22",
        "release_end": "2026-09-22",
        "followers": 12629,
    }]
    merged = merge_partial_segment(previous, clean, today=date(2026, 9, 24))
    assert [game["appid"] for game in merged] == [4019220]
