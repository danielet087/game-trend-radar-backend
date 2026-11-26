"""Master rows take Steam's current release time from the content backend's store facts."""
from __future__ import annotations

import copy
import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from radar_backend.domain.store_fact_releases import (
    EXACT_BASIS, NOT_FULL_BASIS, apply_store_fact_releases, corrected_row, fact_release,
)

NOW = datetime(2026, 11, 25, 16, tzinfo=timezone.utc)  # 2026-11-26 00:00 in Taiwan
CHECKED = "2026-11-25T11:30:00Z"


def row(appid=2988120, day="2026-12-08", time="2026-12-08T09:00:00Z", **changes):
    """A verified master row, as the Followers phase leaves it."""
    return {
        "appid": appid, "name": f"Game {appid}", "followers": 11165,
        "follower_checked_at": "2026-09-19T19:35:09Z",
        "release_raw": day, "release_start": day, "release_end": day, "release_precision": "day",
        "release_display_precision": "date_full", "release_display_provider": "Steam IStoreBrowseService/GetItems",
        "release_date_basis": "steam_store_browse_verified_full_date", "release_date_timezone": "Asia/Taipei",
        "release_time_utc": time, "release_time_source": "Steam IStoreBrowseService/GetItems",
        "release_timestamp_taipei_date": day, "release_store_date": day,
        "release_date_normalization": "steam_store_date_matches_taipei", "release_date_conflict": False,
        "release_date_verified_at": "2026-09-25T14:06:40Z",
        "post_followers_store_verified": True, "post_followers_store_verified_at": "2026-09-25T14:06:40Z",
        **changes,
    }


def facts(**releases):
    """store_facts.json with one release record per AppID."""
    return {"schema_version": 1, "checked_at": CHECKED, "games": {
        appid: {"price": None, "rating": None, "steam_deck": None, "advance_access": None, "early_access": None,
                "release": release, "checked_at": CHECKED}
        for appid, release in releases.items()}}


def release(at, coming_soon=True, display="date_full"):
    return {"at": at, "coming_soon": coming_soon, "display": display}


def test_a_game_moved_to_a_vaguer_date_leaves_the_calendar_until_it_is_verified_again():
    games, changes = apply_store_fact_releases(
        [row()], facts(**{"2988120": release("2027-03-31T07:00:00Z", display="date_quarter")}), NOW)
    moved = games[0]
    assert changes == [{"appid": 2988120, "from": "2026-12-08", "from_time": "2026-12-08T09:00:00Z",
                        "to": "2027-03-31", "to_time": "2027-03-31T07:00:00Z", "exact": False}]
    assert (moved["release_start"], moved["release_time_utc"], moved["release_display_precision"]) == (
        "2027-03-31", "2027-03-31T07:00:00Z", None)
    assert moved["release_date_basis"] == NOT_FULL_BASIS and moved["post_followers_store_verified"] is False
    assert moved["release_date_verified_at"] == CHECKED and moved["followers"] == 11165
    # The old Store-day evidence is gone; the rest of the row is untouched.
    assert "release_store_date" not in moved and "release_date_normalization" not in moved
    assert apply_store_fact_releases(games, facts(**{"2988120": release("2027-03-31T07:00:00Z", display="date_quarter")}),
                                     NOW)[1] == []


def test_a_new_full_date_and_a_late_launch_are_exact_on_taiwan_days():
    later = corrected_row(row(), fact_release(facts(**{"2988120": release("2027-02-10T17:00:00Z")})["games"]["2988120"],
                                              NOW), NOW)
    assert (later["release_start"], later["release_timestamp_taipei_date"], later["release_display_precision"]) == (
        "2027-02-11", "2027-02-11", "date_full")
    assert later["release_date_basis"] == EXACT_BASIS and later["post_followers_store_verified"] is True
    assert later["post_followers_store_verified_at"] == CHECKED
    assert later["release_time_source"] == "Steam IStoreBrowseService/GetItems"
    # Nomad Drive: verified for 10/20 but launched on 10/28.
    nomad = row(2223980, "2026-10-20", "2026-10-20T15:00:00Z")
    games, changes = apply_store_fact_releases(
        [nomad], facts(**{"2223980": release("2026-10-27T17:00:44Z", coming_soon=False, display=None)}), NOW)
    assert changes[0]["exact"] is True
    assert (games[0]["release_start"], games[0]["release_time_utc"]) == ("2026-10-28", "2026-10-27T17:00:44Z")


def test_small_differences_authority_rows_old_history_and_stale_or_missing_facts_stay():
    early = facts(**{"2988120": release("2026-12-08T06:45:00Z")})  # released 2 h 15 min early
    assert apply_store_fact_releases([row()], early, NOW)[1] == []
    authority = row(release_date_normalization="steam_taiwan_store_date_authoritative",
                    release_display_provider="Steam Store appdetails cc=TW l=tchinese")
    with patch("radar_backend.domain.store_fact_releases.has_taiwan_store_date_authority", return_value=True):
        assert apply_store_fact_releases([authority], facts(**{"2988120": release("2027-03-31T07:00:00Z")}), NOW)[1] == []
    old = row(day="2026-08-01", time="2026-08-01T01:00:00Z")
    assert apply_store_fact_releases([old], facts(**{"2988120": release("2026-08-20T01:00:00Z", False)}), NOW)[1] == []
    stale = facts(**{"2988120": release("2027-03-31T07:00:00Z")})
    stale["games"]["2988120"]["checked_at"] = "2026-11-24T10:00:00Z"
    assert apply_store_fact_releases([row()], stale, NOW)[1] == []
    for broken in ({"at": "soon", "coming_soon": True, "display": "date_full"},
                   {"at": "2027-03-31T07:00:00Z", "coming_soon": "yes", "display": "date_full"}, None):
        assert apply_store_fact_releases([row()], facts(**{"2988120": broken}), NOW)[1] == []
    assert apply_store_fact_releases([row()], None, NOW) == ([row()], [])
    assert apply_store_fact_releases([row()], {"games": []}, NOW)[1] == []


def test_a_coming_soon_date_steam_no_longer_calls_full_is_not_exact_even_when_it_has_passed():
    passed = facts(**{"2988120": release("2026-11-20T09:00:00Z", coming_soon=True, display="date_month")})
    games, changes = apply_store_fact_releases([row()], passed, NOW)
    assert changes[0]["exact"] is False and games[0]["release_display_precision"] is None
    # Without the flag a future time is not exact either.
    future = facts(**{"2988120": release("2027-03-31T07:00:00Z", coming_soon=None, display=None)})
    assert apply_store_fact_releases([row()], future, NOW)[1][0]["exact"] is False


def test_a_launch_store_browse_reports_without_a_coming_soon_flag_is_exact():
    # Store Browse leaves the flag out for released games. On 2026-11-25 this
    # took Online: 404, Motocross The Game and Burning Sword off the calendar.
    online = row(4094350, "2026-10-17", "2026-10-16T17:00:00Z")
    games, changes = apply_store_fact_releases(
        [online], facts(**{"4094350": release("2026-10-21T20:16:02Z", coming_soon=None, display=None)}), NOW)
    assert changes == [{"appid": 4094350, "from": "2026-10-17", "from_time": "2026-10-16T17:00:00Z",
                        "to": "2026-10-22", "to_time": "2026-10-21T20:16:02Z", "exact": True}]
    moved = games[0]
    assert (moved["release_start"], moved["release_timestamp_taipei_date"], moved["release_display_precision"]) == (
        "2026-10-22", "2026-10-22", "date_full")
    assert moved["release_date_basis"] == EXACT_BASIS and moved["post_followers_store_verified"] is True
    assert moved["release_date_verified_at"] == CHECKED


def test_a_row_taken_off_the_calendar_comes_back_once_steam_s_date_is_exact():
    # The 2026-11-26 00:00 run left these rows on Steam's time but not exact.
    gone = corrected_row(row(4094350, "2026-10-17", "2026-10-16T17:00:00Z"), fact_release(
        facts(**{"4094350": release("2026-10-21T20:16:02Z", coming_soon=None, display=None)})["games"]["4094350"],
        NOW - timedelta(hours=1)), NOW)
    gone.update(release_display_precision=None, release_date_basis=NOT_FULL_BASIS, post_followers_store_verified=False)
    games, changes = apply_store_fact_releases(
        [gone], facts(**{"4094350": release("2026-10-21T20:16:02Z", coming_soon=None, display=None)}), NOW)
    assert changes == [{"appid": 4094350, "from": "2026-10-22", "from_time": "2026-10-21T20:16:02Z",
                        "to": "2026-10-22", "to_time": "2026-10-21T20:16:02Z", "exact": True}]
    assert games[0]["release_display_precision"] == "date_full" and games[0]["release_date_basis"] == EXACT_BASIS
    assert games[0]["post_followers_store_verified"] is True
    # Never Be Alone stays off while Steam shows only a quarter, and an exact row is left alone.
    quarter = facts(**{"2988120": release("2027-03-31T07:00:00Z", display="date_quarter")})
    off = apply_store_fact_releases([row()], quarter, NOW)[0]
    assert apply_store_fact_releases(off, quarter, NOW)[1] == []
    assert apply_store_fact_releases(games, facts(**{"4094350": release("2026-10-21T20:16:02Z", None, None)}), NOW)[1] == []


def test_the_daily_refresh_saves_moved_rows_with_its_batch_output_and_drops_them_from_the_calendar():
    from radar_backend.application import candidates as application
    from radar_backend.jobs.candidates import build_parser
    from tests.test_candidate_layers import MemoryState, eligible_catalog, prepared_state, runtime

    master = {"games": [row()], "updated_at": "2026-11-01T00:00:00Z"}
    moved = facts(**{"2988120": release("2027-03-31T07:00:00Z", display="date_quarter")})
    args = build_parser().parse_args([
        "--state", "state.json", "--catalog", "catalog.json", "--master", "master.json",
        "--prefilter-state", "pre.json", "--prefilter-batch-size", "1",
        "--prefilter-request-interval", "0", "--output", "public.json",
    ])
    # No batch output: the master file stays as it is.
    def at_now(store, **operations):
        return dataclasses.replace(runtime(store, store_facts=lambda: moved, **operations), utcnow=lambda: NOW)

    store = MemoryState({"state.json": prepared_state("complete"), "catalog.json": {"games": []},
                         "master.json": master})
    application.execute_pipeline(args, at_now(store))
    assert "master.json" not in store.saved and store.values["master.json"] == master

    pre = {"version": 1, "next_index": 0, "complete": False, "games": {}}
    store = MemoryState({"state.json": prepared_state(), "catalog.json": eligible_catalog(),
                         "pre.json": pre, "master.json": copy.deepcopy(master)})

    def scan(rows, pre, **kwargs):
        pre["next_index"] = 1
        pre["games"]["101"] = {"third_party_followers": 4500}
        return {"start_index": 0, "next_index": 1, "screened": 1, "priority": 1, "missing": 0}

    application.execute_pipeline(args, at_now(
        store, scan_batch=scan, filter_confirmed_master_games=lambda games, rows, **kw: games,
        is_twitch_qualified=lambda row: False))
    assert "master.json" in store.saved and "public.json" in store.outputs
    saved = store.values["master.json"]["games"][0]
    assert (saved["release_start"], saved["release_display_precision"]) == ("2027-03-31", None)
    # Loading the saved master again changes nothing.
    assert apply_store_fact_releases(store.values["master.json"]["games"], moved, NOW)[1] == []


def test_the_calendar_filter_drops_a_future_date_that_is_no_longer_full_and_keeps_an_exact_one():
    from datetime import date
    from radar_backend.domain.store_release import filter_confirmed_master_games

    vague = apply_store_fact_releases([row()], facts(**{"2988120": release("2027-03-31T07:00:00Z", display="date_quarter")}),
                                      NOW)[0][0]
    exact = apply_store_fact_releases([row(5)], facts(**{"5": release("2027-02-10T17:00:00Z")}), NOW)[0][0]
    eligible = [{"appid": appid, "release_display_precision": "date_full", "sexual_content_screened": True}
                for appid in (2988120, 5)]
    kept = filter_confirmed_master_games([vague, exact], eligible, today=date(2026, 11, 26), blocked=set(),
                                         is_disallowed=lambda game, blocked: False,
                                         is_twitch_qualified=lambda game: False)
    assert [game["appid"] for game in kept] == [5]


def test_store_facts_come_from_the_primary_database_only():
    from radar_backend.adapters import candidate_sources as sources
    from radar_core.storage.sql import StorageError

    with patch("radar_core.storage.primary.enabled", return_value=False):
        assert sources.read_store_facts() is None
    document = facts(**{"1": release("2027-01-01T00:00:00Z")})
    with patch("radar_core.storage.primary.enabled", return_value=True), \
            patch("radar_core.storage.primary.read_json", return_value=document) as read:
        assert sources.read_store_facts() == document
    read.assert_called_once_with("danielet087/game-trend-radar", "data/store_facts.json")
    with patch("radar_core.storage.primary.enabled", return_value=True), \
            patch("radar_core.storage.primary.read_json", side_effect=StorageError("SQL_UNAVAILABLE", "down")):
        assert sources.read_store_facts() is None
