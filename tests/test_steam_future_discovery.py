"""Distant upcoming games must not disappear behind Steam's TBA sorting."""
import argparse
import json
from datetime import date
from unittest.mock import patch

from collectors.steam_upcoming import SteamUpcomingCollector
from scripts.update_steam_daily import run


def steam_row(appid: int, raw_date: str) -> str:
    return (
        f'<a href="https://store.steampowered.com/app/{appid}/" '
        f'data-ds-appid="{appid}" class="search_result_row">'
        f'<span class="title">Game {appid}</span>'
        f'<div class="search_released">{raw_date}</div></a>'
    )


def test_price_sort_discovers_2027_game_hidden_from_release_ascending(tmp_path):
    collector = SteamUpcomingCollector(
        max_pages=1,
        follower_cache_path=tmp_path / "cache.json",
        checkpoint_path=tmp_path / "checkpoint.json",
        follower_request_interval=0,
        search_request_interval=0,
    )
    sorts = []
    class FakeResponse:
        def __init__(self, html):
            self.html = html
        def json(self):
            return {"results_html": self.html, "total_count": 55000}

    def fake_request(url, *, params, limiter):
        sorts.append(params["sort_by"])
        if params["sort_by"] == "Released_ASC":
            return FakeResponse(steam_row(101, "19 Sep, 2026"))
        if params["sort_by"] == "Price_DESC":
            return FakeResponse(
                steam_row(202, "25 Feb, 2027")
                + steam_row(303, "Coming soon")
            )
        return FakeResponse("")

    collector._request = fake_request
    result = collector.fetch_candidates(
        today=date(2026, 9, 19),
        window_start=date(2027, 1, 19),
        window_end=date(2027, 3, 18),
        segment_index=2,
        segment_anchor=date(2026, 9, 19),
        segment_months=2,
        total_segments=6,
    )
    assert {g.appid for g in result} == {202}
    assert result[0].release_start == "2027-02-25"
    assert "Price_DESC" in sorts
    assert all(g.release_precision == "day" for g in result)


def test_empty_distant_segment_is_not_declared_completed(tmp_path):
    state = tmp_path / "state.json"
    master = tmp_path / "master.json"
    output = tmp_path / "public.json"
    cached = {
        "appid": 333, "name": "Already saved", "followers": 9000,
        "release_raw": "15 Dec, 2026",
        "release_start": "2026-12-15", "release_end": "2026-12-15",
    }
    state.write_text(json.dumps({
        "version": 1,
        "anchor_date": "2026-09-19",
        "segment_months": 2,
        "total_segments": 6,
        "next_segment": 1,
        "completed_segments": [{"segment": 0, "candidate_count": 892,
                                "alternative_search_checked": True}],
        "initial_complete": False,
    }), encoding="utf-8")
    master.write_text(json.dumps({"games": [cached]}), encoding="utf-8")
    args = argparse.Namespace(
        state=str(state), master=str(master), output=str(output),
        follower_cache=str(tmp_path / "followers.json"),
        checkpoint=str(tmp_path / "checkpoint.json"),
        country="TW", days=365, min_followers=5000,
        request_interval=0, search_interval=0, max_pages=1,
        segment_months=2, total_segments=6, checkpoint_branch="steam-state",
        checkpoint_every=5, max_fresh_requests=50,
    )
    empty = {
        "candidate_count": 0, "games": [],
        "source": {}, "collection": {
            "follower_failures": 0,
            "paused_due_to_fresh_request_budget": False,
            "fresh_follower_requests": 0,
        }
    }
    with (
        patch("scripts.update_steam_daily.SteamUpcomingCollector") as cls,
        patch("scripts.update_steam_daily.taiwan_today", return_value=date(2026, 9, 19)),
        patch("scripts.update_steam_daily.fetch_store_browse_releases", return_value={}),
    ):
        cls.return_value.collect.return_value = empty
        payload = run(args)

    init = payload["initialization"]
    assert init["complete"] is False
    assert init["next_segment"] == 1
    assert len(init["completed_segments"]) == 1
    assert init["last_attempt"]["status"] == "discovery_incomplete"
    assert {g["appid"] for g in payload["games"]} == {333}
