"""Taiwan's day of the release time, and the Store-day authority kept for wider gaps.

A Store day one day off the release time is the announced day's time zone, so
the admission takes Taiwan's day. Only a wider gap keeps a verified TW Store
day (without changing the instant), and earlier one-day authority rows move to
Taiwan's day without announcing a new release date.
"""
from copy import deepcopy
from datetime import date, datetime
import json

import pytest

from scripts.build_public_steam_shards import build, merge_game, valid_record
from scripts.import_twitch_steam_discoveries import build_candidate, follower_candidate
from scripts.steam_master_date_gate import (
    apply_store_release_detail, filter_confirmed_master_games, parse_store_release_detail,
)
from scripts.twitch_official_queue import is_twitch_queue_candidate
from radar_backend.publication.activity_origins import catalog_activity_origins
from radar_core.domain.twitch_admission import STEAM_RELEASE_TIME_TAIPEI, taipei_release_day
from scripts.twitch_steam_admission import (
    has_taiwan_store_date_authority, is_twitch_qualified, resolve_store_release_day,
)
from tests.test_twitch_steam_admission import NOW, proof, row, steam


# Two days before Taiwan's 9/4: wider than a time zone, so the Store day keeps authority.
STORE_DAY = "2026-09-02"
REAL_INSTANT = "2026-09-04T04:02:14Z"
PROVIDER = "Steam Store appdetails cc=TW l=tchinese"


def authoritative_record(followers=0, store_text="2026 年 9 月 2 日"):
    item, details = steam()
    item["release"]["steam_release_date"] = int(datetime.fromisoformat(
        REAL_INSTANT.replace("Z", "+00:00")).timestamp())
    details["release_date"] = {"date": store_text, "coming_soon": False}
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
    ("release_store_date", "2026-09-01"),
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


def test_a_one_day_gap_is_a_time_zone_and_admits_taiwans_day():
    # The Store's 9/3 is an evening launch in the Americas: Taiwan has 9/4 12:02.
    game = authoritative_record(store_text="2026 年 9 月 3 日")
    assert game["release_start"] == game["release_end"] == game["release_timestamp_taipei_date"] == "2026-09-04"
    assert game["release_store_date"] == "2026-09-03"
    assert game["release_date_normalization"] == STEAM_RELEASE_TIME_TAIPEI
    assert game["release_date_conflict"] is False
    assert game["release_display_provider"] == "Steam IStoreBrowseService/GetItems"
    assert not has_taiwan_store_date_authority(game)
    assert is_twitch_qualified(game)
    assert is_twitch_queue_candidate(follower_candidate(game), NOW)


def legacy_one_day_authority():
    """A row admitted before the time-zone rule: the Store's 9/3 kept as the day."""
    game = authoritative_record(store_text="2026 年 9 月 3 日")
    game.update({"release_raw": "2026-09-03", "release_start": "2026-09-03", "release_end": "2026-09-03",
                 "release_date_normalization": "steam_taiwan_store_date_authoritative",
                 "release_display_provider": PROVIDER, "release_date_conflict": True})
    assert has_taiwan_store_date_authority(game) and is_twitch_qualified(game)
    return game


def test_an_earlier_one_day_authority_row_moves_to_taiwans_day_in_the_master_and_calendar(tmp_path):
    legacy = legacy_one_day_authority()
    retained = filter_confirmed_master_games([legacy], [], today=date(2026, 10, 2))
    assert [row["release_start"] for row in retained] == ["2026-09-04"]
    moved = retained[0]
    assert moved == taipei_release_day(legacy)
    assert moved["release_store_date"] == "2026-09-03" and moved["release_date_conflict"] is False
    assert is_twitch_qualified(moved)
    source = tmp_path / "master.json"
    frontend = tmp_path / "frontend"
    source.write_text(json.dumps({"games": retained}), encoding="utf-8")
    build(source, frontend, authoritative_future=True)
    published = json.loads((frontend / "data/catalog.json").read_text())["games"][0]
    assert published["release_start"] == "2026-09-04"
    assert published["release_store_date"] == "2026-09-03"
    assert is_twitch_qualified(published)


def test_the_daily_refresh_saves_earlier_one_day_rows_with_its_batch_output():
    """The daily refresh stops before the Followers phase that saved the
    filtered master, so loading the master moves these rows in any phase; the
    move is saved with the batch output, so a run that fails or makes none
    leaves the master file alone and can still save its progress."""
    from radar_backend.application import candidates as application
    from radar_backend.jobs.candidates import build_parser
    from tests.test_candidate_layers import MemoryState, eligible_catalog, prepared_state, runtime

    legacy = legacy_one_day_authority()
    other = dict(legacy_one_day_authority(), appid=456)
    other.pop("twitch_admission")  # Not an authority row: left exactly as it is.
    master = {"games": [legacy, other], "updated_at": "2026-10-01T00:00:00Z"}
    args = build_parser().parse_args([
        "--state", "state.json", "--catalog", "catalog.json", "--master", "master.json",
        "--prefilter-state", "pre.json", "--prefilter-batch-size", "1",
        "--prefilter-request-interval", "0", "--output", "public.json",
    ])
    # No batch output: the master file stays as it is.
    store = MemoryState({"state.json": prepared_state("complete"), "catalog.json": {"games": []},
                         "master.json": master})
    application.execute_pipeline(args, runtime(store))
    assert "master.json" not in store.saved and store.values["master.json"] == master

    pre = {"version": 1, "next_index": 0, "complete": False, "games": {}}
    store = MemoryState({"state.json": prepared_state(), "catalog.json": eligible_catalog(),
                         "pre.json": pre, "master.json": master})

    def failed_scan(rows, pre, **kwargs):
        raise RuntimeError("Third-party response incomplete")

    with pytest.raises(RuntimeError, match="incomplete"):
        application.execute_pipeline(args, runtime(store, scan_batch=failed_scan))
    assert store.saved == [] and store.values["master.json"] == master

    def successful_scan(rows, pre, **kwargs):
        pre["next_index"] = 1
        pre["games"]["101"] = {"third_party_followers": 4500}
        return {"start_index": 0, "next_index": 1, "screened": 1, "priority": 1, "missing": 0}

    application.execute_pipeline(args, runtime(
        store, scan_batch=successful_scan, filter_confirmed_master_games=lambda games, rows, **kw: games,
        is_twitch_qualified=lambda row: False))
    saved = store.values["master.json"]
    assert "master.json" in store.saved and "public.json" in store.outputs
    assert saved["games"] == [taipei_release_day(legacy), other]
    assert saved["games"][0]["release_start"] == "2026-09-04"
    assert saved["games"][0]["release_store_date"] == "2026-09-03"
    assert saved["updated_at"] == "2026-10-01T00:00:00Z"
    # Nothing left to move: loading the saved master changes no row again.
    assert all(taipei_release_day(row) is row for row in saved["games"])


def test_moving_to_taiwans_day_is_not_announced_as_a_new_release_date():
    legacy, moved = legacy_one_day_authority(), taipei_release_day(legacy_one_day_authority())
    stamp = "2026-10-02T09:00:00Z"
    origins = catalog_activity_origins(None, {"games": [legacy]}, {"games": [moved]},
                                       update_reason="steam_daily_search", at=stamp)
    assert origins["events"] == []
    # A release time that really moves to another Taiwan day is still news.
    later = dict(moved, release_raw="2026-09-05", release_start="2026-09-05", release_end="2026-09-05",
                 release_timestamp_taipei_date="2026-09-05", release_time_utc="2026-09-05T04:00:00Z")
    origins = catalog_activity_origins(None, {"games": [moved]}, {"games": [later]},
                                       update_reason="steam_daily_search", at=stamp)
    assert [(event["type"], event["previous_date"], event["date"]) for event in origins["events"]] == [
        ("release_date", "2026-09-04", "2026-09-05")]
