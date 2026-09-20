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
    assert second["phase"] == "date_precision"
    assert second["followers_queried"] == 0
    assert state["days_scanned"] == 2
    assert state["date_precision_required"] is True


def test_stage_three_refuses_xml_until_third_party_entire_catalog_complete(tmp_path):
    from scripts import steam_candidate_pipeline as pipeline
    state = fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    catalog = {"games": [
        {"appid": 101, "name": "Game 101", "release_raw": "2026-10-01",
         "release_start": "2026-10-01", "release_end": "2026-10-01",
         "capsule_image": None, "store_url": "https://store.steampowered.com/app/101/"}
    ]}
    catalog["date_precision_complete"] = True
    catalog["date_precision_eligible"] = catalog["games"]
    args = argparse.Namespace(
        prefilter_state=str(tmp_path / "pre.json"),
        follower_cache=str(tmp_path / "official.json"),
    )
    with pytest.raises(RuntimeError, match="Step 2 has not screened"):
        pipeline.run_follower_batch(args, state, catalog, {"games": []})


def test_new_prefilter_is_separate_phase_before_official_queries(tmp_path, monkeypatch):
    from scripts import steam_candidate_pipeline as pipeline
    monkeypatch.setenv("STEAM_WEB_API_KEY", "test-key")
    catalog = {"games": [
        {"appid": 101, "name": "Game 101", "release_raw": "2026-10-01",
         "release_start": "2026-10-01", "release_end": "2026-10-01",
         "capsule_image": None, "store_url": "https://store.steampowered.com/app/101/"}
    ]}
    state = fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    state["days_scanned"] = 1
    catalog["date_precision_complete"] = True
    catalog["date_precision_eligible"] = catalog["games"]
    pre_file = tmp_path / "pre.json"
    args = argparse.Namespace(
        prefilter_state=str(pre_file), prefilter_batch_size=200,
        prefilter_request_interval=0, follower_cache=str(tmp_path/"cache.json"),
    )
    def fake_scan(rows, stored, **kwargs):
        stored["next_index"] = 1
        stored["games"]["101"] = {
            "third_party_followers": 4200, "priority": True}
        return {"start_index": 0, "next_index": 1,
                "screened": 1, "priority": 1, "missing": 0}
    with (
        patch.object(pipeline, "scan_batch", side_effect=fake_scan),
        patch.object(pipeline, "SteamUpcomingCollector") as official,
    ):
        result = pipeline.run_prefilter_phase(args, state, catalog)
    official.assert_not_called()
    assert result["phase"] == "prefilter"
    assert result["fresh_follower_requests"] == 0
    assert state["phase"] == "followers"
    assert state["prefilter_complete"] is True
    assert __import__("json").loads(pre_file.read_text())["games"]["101"]["priority"]

def test_future_pipeline_verifies_public_display_before_ever_prefiltering(monkeypatch):
    from scripts import steam_candidate_pipeline as pipeline
    state = fresh_state(date(2026, 9, 19), 365)
    state["phase"] = "date_precision"
    state["days_scanned"] = 365
    catalog = {"games": [
        {"appid": 1, "name": "Full date", "release_precision": "day",
         "release_start": "2026-10-01"},
        {"appid": 2, "name": "Year-only hidden placeholder",
         "release_precision": "day", "release_start": "2026-12-31"},
        {"appid": 3, "name": "Sex-focused", "release_precision": "day",
         "release_start": "2026-11-01"},
    ]}
    from unittest.mock import patch
    meta = {
        1: {"release": {"coming_soon_display": "date_full"}},
        2: {"release": {"coming_soon_display": "date_year"}},
        3: {"release": {"coming_soon_display": "date_full"},
            "content_descriptorids": [3]},
    }
    original = [dict(x) for x in catalog["games"]]
    with (
        patch.object(pipeline, "fetch_metadata", return_value=meta) as browse,
        patch.object(pipeline, "scan_batch") as third_party,
        patch.object(pipeline, "SteamUpcomingCollector") as steam_xml,
    ):
        result = pipeline.run_date_precision_phase(state, catalog)
    browse.assert_called_once()
    third_party.assert_not_called()
    steam_xml.assert_not_called()
    assert result["fresh_follower_requests"] == 0
    assert result["eligible_count"] == 1
    assert state["phase"] == "prefilter"
    assert [g["appid"] for g in pipeline.active_candidate_rows(catalog, state)] == [1]
    assert catalog["games"] == original


def test_fresh_state_cannot_skip_public_release_date_gate():
    from scripts import steam_candidate_pipeline as pipeline
    state = fresh_state(date(2026, 9, 19), 365)
    state["phase"] = "prefilter"
    with pytest.raises(RuntimeError, match="date gate incomplete"):
        pipeline.active_candidate_rows({"games": [{"appid": 1}]}, state)
