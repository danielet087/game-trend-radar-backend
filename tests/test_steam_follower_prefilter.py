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


def test_priority_threshold_and_unknown_do_not_enter_steam_xml():
    catalog = games(101, 102, 103, 104)
    state = {"version": 1, "next_index": 0, "games": {}}
    fake = FakeSession({101: 3999, 102: 4000, 103: 9300, 104: None})
    report = priority.scan_batch(
        catalog, state, steam_api_key="test-key", initial_index=0,
        limit=4, request_interval=0, session=fake,
    )
    assert report == {
        "start_index": 0, "next_index": 4, "screened": 4,
        "priority": 2, "missing": 1, "complete": True,
    }
    assert state["games"]["101"]["priority"] is False
    assert state["games"]["102"]["priority"] is True
    assert state["games"]["103"]["third_party_followers"] == 9300
    assert state["games"]["104"]["third_party_followers"] is None
    assert state["games"]["104"]["priority"] is False
    assert state["games"]["104"]["scheduling_band"] == "unresolved"
    assert [g["appid"] for g in priority.pending_priorities(
        catalog, state, {}, min_start_index=0,
    )] == [102, 103]
    assert [g["appid"] for g in priority.pending_priorities(
        catalog, state, {"102": {"followers": 4300}}, min_start_index=0,
    )] == [103]


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


def test_screen_first_does_not_call_steam_xml_before_catalog_screen_complete(tmp_path, monkeypatch):
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
    assert results == []
    assert outcome["priority_fresh_requests"] == 0
    assert outcome["next_index"] == 1
    assert state["prefilter_next_index"] == 3
    assert state["phase"] == "followers"
    assert state["prefilter_matched_count"] == 1
    assert prefilter_path.exists()
    assert json.loads(prefilter_path.read_text())["games"]["102"]["priority"] is False


def test_after_screen_complete_only_hot_games_get_xml_not_full_backfill(tmp_path):
    catalog = games(101, 102, 103, 104)
    prefilter_path = tmp_path / "pre.json"
    prefilter_path.write_text(json.dumps({
        "version": 1, "next_index": 4, "complete": True,
        "games": {
            "101": {"priority": True, "third_party_followers": 4000},
            "102": {"priority": False, "third_party_followers": 3999},
            "103": {"priority": False, "third_party_followers": None},
            "104": {"priority": True, "third_party_followers": 8000},
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
            self.fresh_follower_requests += len(selection)
            for g in selection:
                self.follower_cache[str(g.appid)] = {"followers": 5100}
            self.processed_candidate_count = len(selection)
            return []

    with patch.object(pipeline, "SteamUpcomingCollector", return_value=Collector()):
        result = pipeline.run_follower_batch(
            arguments, state, {"games": catalog}, {"games": []},
        )
    assert calls == [[104]]
    assert result["backfill_started"] is False
    assert result["next_index"] == 0
    assert result["official_verified_total"] == 2
    assert state["phase"] == "complete"
    assert state["initial_complete"] is True
    assert state["coverage_exhaustive"] is False
    assert state["prefilter_missing_count"] == 1


def test_last_screen_window_defers_steam_until_screen_ends(tmp_path, monkeypatch):
    catalog = games(101, 102, 103)
    state = pipeline.fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    args = argparse.Namespace(
        follower_cache=str(tmp_path / "official.json"),
        checkpoint=str(tmp_path / "checkpoint.json"),
        checkpoint_branch="steam-state", request_interval=0, search_interval=0,
        prefilter_state=str(tmp_path / "pre.json"), prefilter_batch_size=2,
        prefilter_request_interval=0, max_fresh_requests_per_run=5,
    )
    counts = {101: 9000, 102: 3999, 103: 4000}
    monkeypatch.setenv("STEAM_WEB_API_KEY", "test-only")
    with patch.object(pipeline, "scan_batch", wraps=lambda rows, saved, **kw: priority.scan_batch(
            rows, saved, session=FakeSession(counts),
            request_interval=0,
            **{k:v for k,v in kw.items() if k!="request_interval"},
        )):
        class Collector:
            fresh_follower_requests = 0
            cached_follower_reuses = 0
            failed_follower_appids = []
            follower_failures = 0
            follower_rate_limit_events = 0
            processed_candidate_count = 0
            follower_cache = {}
            def qualify(self, selection):
                selected = list(selection)
                self.fresh_follower_requests += len(selected)
                self.follower_cache.update({
                    str(x.appid): {"followers": 5100} for x in selected
                })
                return []
        with patch.object(pipeline, "SteamUpcomingCollector", side_effect=Collector):
            first = pipeline.run_follower_batch(args, state, {"games": catalog}, {"games": []})
            assert first["fresh_follower_requests"] == 0
            assert state["prefilter_next_index"] == 2
            assert state["phase"] == "followers"
            second = pipeline.run_follower_batch(args, state, {"games": catalog}, {"games": []})
    assert state["prefilter_next_index"] == 3
    assert second["fresh_follower_requests"] == 2
    assert state["phase"] == "complete"


def test_string_ids_are_parsed_and_previous_false_missing_window_repaired():
    class StringIdSession(FakeSession):
        def post(self, url, *, json, timeout):
            response = super().post(url, json=json, timeout=timeout)
            payload = response.json()
            for item in payload["data"]:
                item["id"] = str(item["id"])
            return FakeResponse(200, payload)

    saved = {
        "version": 1, "next_index": 2, "games": {
            "101": {"third_party_followers": None,
                    "group_short_id": 101, "priority": True},
            "102": {"third_party_followers": None,
                    "group_short_id": 102, "priority": True},
        },
    }
    client = StringIdSession({101: 3999, 102: 8000, 103: 4000})
    summary = priority.scan_batch(
        games(101, 102, 103), saved, steam_api_key="test-key",
        initial_index=0, limit=1, request_interval=0, session=client,
    )
    assert summary["start_index"] == 2
    assert summary["next_index"] == 3
    assert saved["bulk_parser_version"] == 2
    assert saved["games"]["101"]["third_party_followers"] == 3999
    assert saved["games"]["101"]["priority"] is False
    assert saved["games"]["102"]["third_party_followers"] == 8000
    assert saved["games"]["102"]["priority"] is True
    assert saved["games"]["103"]["third_party_followers"] == 4000
    assert saved["games"]["103"]["priority"] is True
    # An older unknown entry must be demoted, not kept in the priority queue.
    assert saved["games"]["101"]["scheduling_band"] == "measured"

    assert client.called == [103]
