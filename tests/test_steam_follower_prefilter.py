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


def test_failed_mapping_must_not_advance_or_write_partial_window(monkeypatch):
    monkeypatch.setattr(priority.time, "sleep", lambda _: None)
    saved = {"version": 1, "next_index": 0, "games": {}}
    with pytest.raises(RuntimeError, match="HTTP 429"):
        priority.scan_batch(
            games(1, 2), saved,
            steam_api_key="test-key", initial_index=0, limit=2,
            request_interval=0,
            session=FakeSession({1: 9000, 2: 8000}, bad_appid=2),
        )
    assert saved == {"version": 1, "next_index": 0, "games": {}}


def test_incomplete_screen_cannot_run_steam_xml(tmp_path):
    catalog = games(101, 102, 103)
    pre_file = tmp_path / "pre.json"
    pre_file.write_text(json.dumps({
        "version": 1, "next_index": 2, "complete": False,
        "games": {
            "101": {"third_party_followers": 9000, "priority": True},
            "102": {"third_party_followers": 10, "priority": False},
        },
    }), encoding="utf-8")
    state = pipeline.fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    args = argparse.Namespace(prefilter_state=str(pre_file),
                              follower_cache=str(tmp_path / "cache.json"))
    with patch.object(pipeline, "SteamUpcomingCollector") as collector:
        with pytest.raises(RuntimeError, match="Step 2 has not screened"):
            pipeline.run_follower_batch(args, state, {"games": catalog}, {"games": []})
    collector.assert_not_called()


def test_completed_screen_checks_only_measured_4000_or_more(tmp_path):
    catalog = games(101, 102, 103, 104)
    pre_file = tmp_path / "pre.json"
    pre_file.write_text(json.dumps({
        "version": 1, "next_index": 4, "complete": True,
        "games": {
            "101": {"priority": True, "third_party_followers": 4000},
            "102": {"priority": False, "third_party_followers": 3999},
            "103": {"priority": False, "third_party_followers": None},
            "104": {"priority": True, "third_party_followers": 8000},
        },
    }), encoding="utf-8")
    cache_file = tmp_path / "official.json"
    cache_file.write_text(json.dumps({
        "games": {"101": {"followers": 5100, "checked_at": "2026-09-19T00:00:00Z"}}
    }), encoding="utf-8")
    state = pipeline.fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "followers"
    args = argparse.Namespace(
        follower_cache=str(cache_file),
        checkpoint=str(tmp_path / "checkpoint.json"),
        checkpoint_branch="steam-state", request_interval=0, search_interval=0,
        prefilter_state=str(pre_file), max_fresh_requests_per_run=5,
    )
    xml_calls = []

    class Collector:
        fresh_follower_requests = 0
        cached_follower_reuses = 0
        failed_follower_appids = []
        follower_failures = 0
        follower_rate_limit_events = 0
        processed_candidate_count = 0

        def qualify(self, selection):
            selected = list(selection)
            xml_calls.append([game.appid for game in selected])
            self.fresh_follower_requests += len(selected)
            rows = json.loads(cache_file.read_text(encoding="utf-8"))
            for game in selected:
                rows["games"][str(game.appid)] = {
                    "followers": 5100, "checked_at": "2026-09-19T00:00:00Z",
                }
            cache_file.write_text(json.dumps(rows), encoding="utf-8")
            return []

    with patch.object(pipeline, "SteamUpcomingCollector", return_value=Collector()):
        result = pipeline.run_follower_batch(
            args, state, {"games": catalog}, {"games": []},
        )
    assert xml_calls == [[104]]
    assert result["fresh_follower_requests"] == 1
    assert result["verified_priority_count"] == 2
    assert result["priority_total"] == 2
    assert state["phase"] == "complete"
    assert state["initial_complete"] is True
    assert state["coverage_exhaustive"] is False
    assert state["next_follower_index"] == 0
    # 3,999 and missing are NOT sent for official verification.
    assert "102" not in json.loads(cache_file.read_text())["games"]
    assert "103" not in json.loads(cache_file.read_text())["games"]


def test_screen_two_batches_before_any_steam_xml(tmp_path, monkeypatch):
    catalog = games(101, 102, 103)
    state = pipeline.fresh_state(date(2026, 9, 19), 1)
    state["phase"] = "prefilter"
    state["days_scanned"] = 1
    args = argparse.Namespace(
        prefilter_state=str(tmp_path / "pre.json"),
        follower_cache=str(tmp_path / "official.json"),
        prefilter_batch_size=2, prefilter_request_interval=0,
    )
    monkeypatch.setenv("STEAM_WEB_API_KEY", "test-only")
    counts = {101: 9000, 102: 3999, 103: 4000}

    def fake_scan(rows, saved, **kwargs):
        return priority.scan_batch(
            rows, saved, session=FakeSession(counts), request_interval=0,
            **{k: v for k, v in kwargs.items() if k != "request_interval"},
        )

    with (
        patch.object(pipeline, "scan_batch", side_effect=fake_scan),
        patch.object(pipeline, "SteamUpcomingCollector") as collector,
    ):
        first = pipeline.run_prefilter_phase(args, state, {"games": catalog})
        assert first["fresh_follower_requests"] == 0
        assert state["phase"] == "prefilter"
        assert state["prefilter_next_index"] == 2
        second = pipeline.run_prefilter_phase(args, state, {"games": catalog})
    collector.assert_not_called()
    assert second["fresh_follower_requests"] == 0
    assert state["phase"] == "followers"
    assert state["prefilter_complete"] is True
    assert state["prefilter_next_index"] == 3
    stored = json.loads((tmp_path / "pre.json").read_text(encoding="utf-8"))
    assert len(stored["games"]) == 3
    assert {appid for appid, data in stored["games"].items()
            if data["priority"]} == {"101", "103"}



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


def test_transient_steam_connection_error_is_retried_and_window_committed(monkeypatch):
    import requests

    class FlakySession(FakeSession):
        attempts = 0
        def get(self, url, *, params, timeout):
            self.attempts += 1
            if self.attempts == 1:
                raise requests.ConnectionError("transient network error")
            return super().get(url, params=params, timeout=timeout)

    intervals = []
    monkeypatch.setattr(priority.time, "sleep", intervals.append)
    saved = {"version": 1, "next_index": 0, "games": {}}
    fake = FlakySession({101: 4200})
    result = priority.scan_batch(
        games(101), saved, steam_api_key="private-test-only",
        initial_index=0, limit=1, request_interval=0, session=fake,
    )
    assert fake.attempts == 2
    assert intervals == [priority.RETRY_DELAYS[0]]
    assert result["next_index"] == 1
    assert saved["games"]["101"]["third_party_followers"] == 4200


def test_temporary_429_uses_backoff_then_recovers(monkeypatch):
    class ThrottledSession(FakeSession):
        attempts = 0
        def get(self, url, *, params, timeout):
            self.attempts += 1
            if self.attempts <= 2:
                return FakeResponse(429, {})
            return super().get(url, params=params, timeout=timeout)

    delays = []
    monkeypatch.setattr(priority.time, "sleep", delays.append)
    saved = {"version": 1, "next_index": 0, "games": {}}
    session = ThrottledSession({101: 4000})
    result = priority.scan_batch(
        games(101), saved, steam_api_key="private-test-only",
        initial_index=0, limit=1, request_interval=0, session=session,
    )
    assert session.attempts == 3
    assert delays == list(priority.RATE_LIMIT_DELAYS[:2])
    assert result["priority"] == 1
    assert saved["games"]["101"]["priority"]


def test_permanent_api_403_not_retried_and_cursor_preserved(monkeypatch):
    class Forbidden(FakeSession):
        attempts = 0
        def get(self, url, *, params, timeout):
            self.attempts += 1
            return FakeResponse(403, {})

    delays = []
    monkeypatch.setattr(priority.time, "sleep", delays.append)
    saved = {"version": 1, "next_index": 0, "games": {}}
    session = Forbidden({})
    with pytest.raises(RuntimeError, match="HTTP 403"):
        priority.scan_batch(
            games(101), saved, steam_api_key="private-test-only",
            initial_index=0, limit=1, request_interval=0, session=session,
        )
    assert session.attempts == 1
    assert delays == []
    assert saved == {"version": 1, "next_index": 0, "games": {}}
