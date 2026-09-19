"""Two-stage Steam candidate discovery must not query Followers until day 365."""
import argparse
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from scripts.steam_candidate_pipeline import (
    QUERY_URL, candidate_record, fresh_state, query_one_day,
    run_discovery, run_follower_batch,
)

PHANTOM = {
    "appid": 4115450, "name": "Phantom Blade Zero",
    "release": {"steam_release_date": 1793239200, "is_coming_soon": True},
}


def test_query_candidate_uses_taiwan_day_and_retains_exact_time():
    row = candidate_record(PHANTOM, date(2026, 10, 29))
    assert row["appid"] == 4115450
    assert row["release_start"] == "2026-10-29"
    assert row["release_time_utc"] == "2026-10-29T02:00:00Z"
    assert row["release_date_basis"] == "steam_store_query_release_time"
    assert candidate_record(PHANTOM, date(2026, 10, 28)) is None
    assert candidate_record({"appid": 12, "release": {"is_coming_soon": True}},
                            date(2026, 10, 29)) is None


def test_one_day_query_uses_real_date_filter_and_metadata_pagination():
    class Reply:
        def __init__(self, result):
            self.result = result
        def raise_for_status(self):
            return None
        def json(self):
            return {"response": self.result}
    class Session:
        def __init__(self):
            self.calls = []
        def get(self, url, *, params, timeout):
            import json
            assert url == QUERY_URL
            assert timeout == 35
            assert params["key"] == "secret-is-not-logged"
            query = json.loads(params["input_json"])["query"]
            self.calls.append(query)
            start = query["start"]
            return Reply({
                "metadata": {"total_matching_records": 2,
                             "start": start, "count": 1},
                "store_items": [
                    PHANTOM if start == 0 else
                    {"appid": 234567, "name": "Other Game",
                     "release": {"steam_release_date": 1793239800,
                                 "is_coming_soon": True}}
                ],
            })
    session = Session()
    games, summary = query_one_day(
        session, "secret-is-not-logged", date(2026, 10, 29),
        request_interval=0, page_size=1,
    )
    assert {row["appid"] for row in games} == {4115450, 234567}
    assert summary["api_matches"] == 2
    assert summary["pages"] == 2
    query = session.calls[0]
    filt = query["filters"]["release_date_filter"]
    assert filt["release_date_type"] == 1
    assert datetime.fromtimestamp(filt["start_date"], timezone.utc).isoformat() == \
           "2026-10-28T16:00:00+00:00"
    assert datetime.fromtimestamp(filt["end_date"], timezone.utc).isoformat() == \
           "2026-10-29T16:00:00+00:00"


def test_incomplete_pagination_does_not_mark_a_day_as_scanned():
    class Session:
        def get(self, *args, **kwargs):
            class Reply:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"response": {
                        "metadata": {"total_matching_records": 2},
                        "store_items": []}}
            return Reply()
    with pytest.raises(RuntimeError, match="incomplete page"):
        query_one_day(Session(), "test", date(2026, 10, 29),
                      request_interval=0)


def test_stage_one_advances_calendar_days_without_followers(monkeypatch):
    from scripts import steam_candidate_pipeline as pipeline
    state = fresh_state(date(2026, 9, 19), 2)
    catalog = {"games": []}
    row = candidate_record(PHANTOM, date(2026, 10, 29))
    # The unit test spans two days; re-label record to the mocked days.
    def fake_query(session, api_key, day, **kwargs):
        game = dict(row, release_start=day.isoformat(),
                    release_end=day.isoformat())
        return [game], {"day": day.isoformat(), "api_matches": 1,
                         "candidates": 1, "unverified_rows": 0, "pages": 1}
    monkeypatch.setenv("STEAM_WEB_API_KEY", "test-only")
    args = argparse.Namespace(batch_days=1, search_interval=0)
    with patch.object(pipeline, "query_one_day", side_effect=fake_query):
        first = run_discovery(args, state, catalog)
        assert first["phase"] == "discovery"
        assert first["followers_queried"] == 0
        assert state["days_scanned"] == 1
        second = run_discovery(args, state, catalog)
    assert second["phase"] == "followers"
    assert second["followers_queried"] == 0
    assert state["days_scanned"] == 2


def test_follower_stage_reuses_cache_and_50_request_limit(tmp_path):
    from scripts import steam_candidate_pipeline as pipeline
    state = fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    state["days_scanned"] = 1
    catalog = {"games": [
        {"appid": appid, "name": f"Game {appid}", "release_raw": "2026-10-01",
         "release_start": "2026-10-01", "release_end": "2026-10-01",
         "capsule_image": None, "store_url": f"https://store.steampowered.com/app/{appid}/"}
        for appid in (101, 102, 103)]}
    master = {"games": []}
    args = argparse.Namespace(
        follower_cache=str(tmp_path / "cache.json"),
        checkpoint=str(tmp_path / "checkpoint.json"),
        checkpoint_branch="steam-state", request_interval=0,
        search_interval=0,
    )
    class Collector:
        fresh_follower_requests = 2
        cached_follower_reuses = 0
        failed_follower_appids = []
        processed_candidate_count = 2
        follower_failures = 0
        follower_rate_limit_events = 0
        def qualify(self, items):
            assert len(items) == 3
            return []
    with (
        patch.object(pipeline, "SteamUpcomingCollector", return_value=Collector()) as ctor,
        patch.object(pipeline, "corrected_games", side_effect=lambda games: games),
    ):
        result = run_follower_batch(args, state, catalog, master)
    assert ctor.call_args.kwargs["max_fresh_requests_per_run"] == 50
    assert ctor.call_args.kwargs["reuse_all_cached_during_initialization"] is True
    assert result["fresh_follower_requests"] == 2
    assert state["next_follower_index"] == 2
    assert state["phase"] == "followers"
