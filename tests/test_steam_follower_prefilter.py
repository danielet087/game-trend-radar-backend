"""Safety regression tests for the 4000 third-party / 5000 official two-pass path."""
from __future__ import annotations

import argparse
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import steam_candidate_pipeline as pipeline
from scripts import steam_follower_prefilter as priority


def games(*ids):
    return [
        {"appid": appid, "name": f"Game {appid}", "release_raw": "2026-10-01",
         "release_start": "2026-10-01", "release_end": "2026-10-01",
         "release_precision": "day", "capsule_image": None,
         "store_url": f"https://store.steampowered.com/app/{appid}/"}
        for appid in ids
    ]


class FakeResponse:
    def __init__(self, code, data):
        self.status_code = code
        self._data = data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, amounts, *, bad_appid=None):
        self.amounts = amounts
        self.bad_appid = bad_appid
        self.headers = {}
        self.called = []

    def get(self, url, *, params, timeout):
        appid = int(params["vanityurl"])
        self.called.append(appid)
        if appid == self.bad_appid:
            return FakeResponse(429, {})
        return FakeResponse(200, {
            "response": {
                "success": 1,
                "steamid": str(priority.GROUP_BASE + appid),
            },
        })

    def post(self, url, *, json, timeout):
        return FakeResponse(200, {
            "data": [
                {"id": appid, "members": count}
                for appid, count in self.amounts.items()
                if appid in json["ids"] and count is not None
            ],
            "notFound": [
                appid for appid in json["ids"]
                if appid not in self.amounts or self.amounts[appid] is None
            ],
        })


def test_priority_threshold_and_unknown_fallback_are_not_official_counts():
    catalog = games(101, 102, 103, 104)
    state = {"version": 1, "next_index": 0, "games": {}}
    fake = FakeSession({101: 3999, 102: 4000, 103: 9300, 104: None})
    report = priority.scan_batch(
        catalog, state, steam_api_key="test-key", initial_index=0,
        limit=4, request_interval=0, session=fake,
    )
    assert report == {
        "start_index": 0, "next_index": 4, "screened": 4,
        "priority": 3, "missing": 1, "complete": True,
    }
    assert state["games"]["101"]["priority"] is False
    assert state["games"]["102"]["priority"] is True
    assert state["games"]["103"]["third_party_followers"] == 9300
    assert state["games"]["104"]["third_party_followers"] is None
    assert [g["appid"] for g in priority.pending_priorities(
        catalog, state, {}, min_start_index=0,
    )] == [102, 103, 104]
    assert [g["appid"] for g in priority.pending_priorities(
        catalog, state, {"102": {"followers": 4300}}, min_start_index=0,
    )] == [103, 104]


def test_failed_mapping_must_not_advance_or_write_partial_window():
    saved = {"version": 1, "next_index": 0, "games": {}}
    with pytest.raises(RuntimeError, match="HTTP 429"):
        priority.scan_batch(
            games(1, 2), saved,
            steam_api_key="test-key", initial_index=0, limit=2,
            request_interval=0,
            session=FakeSession({1: 9000, 2: 8000}, bad_appid=2),
        )
    assert saved == {"version": 1, "next_index": 0, "games": {}}


def test_batch_first_verifies_hot_and_missing_without_moving_official_cursor(tmp_path, monkeypatch):
    catalog = games(101, 102, 103, 104)
    # Existing private cursor has already officially checked app 101.
    official = {
        "101": {"followers": 40, "checked_at": "2026-09-19T00:00:00Z"},
    }
    state = pipeline.fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    state["next_follower_index"] = 1
    prefilter_path = tmp_path / "pre.json"
    arguments = argparse.Namespace(
        follower_cache=str(tmp_path / "official.json"),
        checkpoint=str(tmp_path / "checkpoint.json"),
        checkpoint_branch="steam-state", request_interval=0, search_interval=0,
        prefilter_state=str(prefilter_path), prefilter_batch_size=2,
        prefilter_request_interval=0,
    )
    results = []

    class Collector:
        fresh_follower_requests = 0
        cached_follower_reuses = 0
        failed_follower_appids = []
        follower_failures = 0
        follower_rate_limit_events = 0
        processed_candidate_count = 0
        follower_cache = dict(official)

        def qualify(self, selection):
            selection = list(selection)
            results.append([g.appid for g in selection])
            self.fresh_follower_requests += len(selection)
            self.follower_cache.update({
                str(g.appid): {"followers": 5100, "checked_at": "2026-09-19T00:00:00Z"}
                for g in selection
            })
            return [
                SimpleNamespace(**vars(g), followers=5100,
                                follower_checked_at="2026-09-19T00:00:00Z",
                                community_url=f"https://steamcommunity.com/app/{g.appid}/")
                for g in selection
            ]

    def fake_scan(rows, saved, **kwargs):
        assert kwargs["initial_index"] == 1
        saved.update({
            "next_index": 3, "complete": False,
            "games": {
                "102": {"priority": False, "third_party_followers": 3999},
                "103": {"priority": True, "third_party_followers": 4000},
            },
        })
        return {"start_index": 1, "next_index": 3, "screened": 2,
                "priority": 1, "missing": 0, "complete": False}

    monkeypatch.setenv("STEAM_WEB_API_KEY", "test-only")
    with (
        patch.object(pipeline, "SteamUpcomingCollector", return_value=Collector()),
        patch.object(pipeline, "scan_batch", side_effect=fake_scan),
    ):
        outcome = pipeline.run_follower_batch(
            arguments, state, {"games": catalog}, {"games": []},
        )
    assert results == [[103]]
    assert outcome["priority_fresh_requests"] == 1
    assert outcome["next_index"] == 1
    assert state["prefilter_next_index"] == 3
    assert prefilter_path.exists()
    assert json.loads(prefilter_path.read_text())["games"]["102"]["priority"] is False


def test_after_priority_complete_original_xml_cursor_resumes(tmp_path):
    catalog = games(101, 102, 103)
    prefilter_path = tmp_path / "pre.json"
    prefilter_path.write_text(json.dumps({
        "version": 1, "next_index": 3, "complete": True,
        "games": {
            "101": {"priority": True, "third_party_followers": 4000},
            "102": {"priority": False, "third_party_followers": 500},
            "103": {"priority": False, "third_party_followers": 1},
        },
    }), encoding="utf-8")
    state = pipeline.fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    arguments = argparse.Namespace(
        follower_cache=str(tmp_path / "official.json"),
        checkpoint=str(tmp_path / "checkpoint.json"),
        checkpoint_branch="steam-state", request_interval=0, search_interval=0,
        prefilter_state=str(prefilter_path), prefilter_batch_size=200,
        prefilter_request_interval=0,
    )
    calls = []

    class Collector:
        fresh_follower_requests = 0
        cached_follower_reuses = 0
        failed_follower_appids = []
        follower_failures = 0
        follower_rate_limit_events = 0
        processed_candidate_count = 0
        follower_cache = {"101": {"followers": 5100}}

        def qualify(self, selection):
            selection = list(selection)
            calls.append([g.appid for g in selection])
            self.fresh_follower_requests += 2
            self.cached_follower_reuses += 1
            self.processed_candidate_count = len(selection)
            return []

    with patch.object(pipeline, "SteamUpcomingCollector", return_value=Collector()):
        result = pipeline.run_follower_batch(
            arguments, state, {"games": catalog}, {"games": []},
        )
    assert calls == [[101, 102, 103]]
    assert result["backfill_started"] is True
    assert result["next_index"] == 3
    assert state["phase"] == "complete"
    assert state["initial_complete"] is True
