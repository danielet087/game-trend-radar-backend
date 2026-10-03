"""Preserve a verified Taiwan Store calendar date without changing its instant."""
from copy import deepcopy
from datetime import date, datetime
import json

import pytest

from scripts.build_public_steam_shards import build, merge_game, valid_record
from scripts.import_twitch_steam_discoveries import build_candidate, follower_candidate
from scripts.steam_master_date_gate import apply_store_release_detail, parse_store_release_detail
from scripts.twitch_official_queue import is_twitch_queue_candidate
from scripts.twitch_steam_admission import (
    has_taiwan_store_date_authority, is_twitch_qualified, resolve_store_release_day,
)
from tests.test_twitch_steam_admission import NOW, proof, row, steam


STORE_DAY = "2026-09-03"
REAL_INSTANT = "2026-09-04T04:02:14Z"
PROVIDER = "Steam Store appdetails cc=TW l=tchinese"


def authoritative_record(followers=0):
    item, details = steam()
    item["release"]["steam_release_date"] = int(datetime.fromisoformat(
        REAL_INSTANT.replace("Z", "+00:00")).timestamp())
    details["release_date"] = {"date": "2026 年 9 月 3 日", "coming_soon": False}
    candidate, reason = build_candidate(
        123, proof(), item, details, followers, "2026-10-02T06:00:00Z", NOW, set(),
    )
    assert reason == "accepted"
    return candidate


def test_taiwan_store_authority_is_opt_in_and_keeps_the_real_release_instant():
    assert resolve_store_release_day(STORE_DAY, REAL_INSTANT) is None
    normalized = resolve_store_release_day(STORE_DAY, REAL_INSTANT,
                                           allow_taiwan_store_authority=True)
    assert normalized["release_start"] == STORE_DAY
    assert normalized["release_date_normalization"] == "steam_taiwan_store_date_authoritative"
    game = authoritative_record()
    assert game["release_start"] == game["release_end"] == game["release_store_date"] == STORE_DAY
    assert game["release_time_utc"] == REAL_INSTANT
    assert game["release_timestamp_taipei_date"] == "2026-09-04"
    assert game["release_date_conflict"] is True
    assert game["release_display_provider"] == PROVIDER
    assert game["release_date_verified_at"] == "2026-10-02T09:00:00Z"
    assert has_taiwan_store_date_authority(game)
    assert is_twitch_qualified(game)


@pytest.mark.parametrize("field,value", [
    ("release_display_provider", "Steam IStoreBrowseService/GetItems"),
    ("release_date_verified_at", None),
    ("release_date_verified_at", "2026-10-02T09:00:00"),
    ("release_date_normalization", "steam_store_date_matches_taipei"),
    ("release_timestamp_taipei_date", STORE_DAY),
    ("release_date_conflict", False),
    ("release_store_date", "2026-09-02"),
    ("release_time_utc", None),
])
def test_incomplete_or_inconsistent_authority_cannot_bypass_date_validation(field, value):
    game = authoritative_record()
    game[field] = value

    assert not has_taiwan_store_date_authority(game)
    assert not is_twitch_qualified(game)


@pytest.mark.parametrize("field,value", [
    ("steam_type", "dlc"), ("sexual_content_screened", False),
    ("followers", True), ("follower_checked_at", None),
])
def test_date_authority_keeps_the_existing_type_adult_and_true_follower_gates(field, value):
    game = authoritative_record()
    game[field] = value

    assert not is_twitch_qualified(game)


def test_pending_authoritative_candidate_keeps_priority_and_rejects_forged_proof():
    game = authoritative_record()
    queued = follower_candidate(game)
    assert queued["release_date"] == STORE_DAY
    assert is_twitch_queue_candidate(queued, NOW)
    bad = deepcopy(queued)
    bad["twitch_admission"]["appid"] = 999
    assert not is_twitch_queue_candidate(bad, NOW)


def test_authority_does_not_create_an_ordinary_low_follower_exception():
    game = authoritative_record()
    assert valid_record(game)
    ordinary = deepcopy(game)
    ordinary.pop("twitch_admission")

    assert not has_taiwan_store_date_authority(ordinary)
    assert not is_twitch_qualified(ordinary)
    assert not valid_record(ordinary)


def test_public_calendar_and_small_projection_keep_complete_date_authority(tmp_path):
    game = authoritative_record()
    source = tmp_path / "master.json"
    frontend = tmp_path / "frontend"
    source.write_text(json.dumps({"games": [game]}), encoding="utf-8")

    build(source, frontend, authoritative_future=True)

    month = json.loads((frontend / "data/calendar/2026-09.json").read_text())
    published = month["games"][0]
    assert published["release_start"] == STORE_DAY
    assert published["release_time_utc"] == REAL_INSTANT
    projection = json.loads((frontend / "data/catalog.json").read_text())["games"][0]
    assert projection["release_display_provider"] == PROVIDER
    assert projection["release_date_verified_at"] == game["release_date_verified_at"]
    assert is_twitch_qualified(projection)


def test_a_new_authority_audit_replaces_old_normalization_fields():
    older = row()
    older["release_date_verified_at"] = "2026-10-01T09:00:00Z"
    newer = authoritative_record()

    merged = merge_game(older, newer)

    assert merged["release_store_date"] == STORE_DAY
    assert merged["release_date_normalization"] == "steam_taiwan_store_date_authoritative"
    assert merged["release_start"] == STORE_DAY
    assert is_twitch_qualified(merged)


def test_browse_refresh_retains_verified_taiwan_date_and_updates_only_the_diagnostic():
    game = authoritative_record()
    item, _ = steam()
    refreshed = parse_store_release_detail(item, today=date(2026, 10, 2))

    updated = apply_store_release_detail(game, refreshed)

    assert updated["release_start"] == updated["release_store_date"] == STORE_DAY
    assert updated["release_display_provider"] == PROVIDER
    assert updated["release_date_verified_at"] == game["release_date_verified_at"]
    assert updated["release_date_normalization"] == game["release_date_normalization"]
    assert updated["release_time_utc"] == refreshed["release_time_utc"]
    assert updated["release_timestamp_taipei_date"] == "2026-10-02"
    assert updated["release_date_conflict"] is True
    assert is_twitch_qualified(updated)
