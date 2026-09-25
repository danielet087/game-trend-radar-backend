"""Distant upcoming games must not disappear behind Steam's TBA sorting."""
import argparse
from datetime import date

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


def test_legacy_updater_refuses_to_overwrite_master_with_unverified_dates(tmp_path):
    # The historical two-month collector cannot prove Store Browse date_full.
    # Keep its pure discovery tests, but its master-writing entry point is retired.
    state = tmp_path / "state.json"
    master = tmp_path / "master.json"
    output = tmp_path / "public.json"
    state.write_text('{"next_segment": 1}', encoding="utf-8")
    master.write_text('{"games": [{"appid": 333, "release_start": "2026-12-15"}]}',
                      encoding="utf-8")
    before_state = state.read_bytes()
    before_master = master.read_bytes()
    args = argparse.Namespace(state=str(state), master=str(master), output=str(output))

    import pytest
    with pytest.raises(RuntimeError, match="cannot verify the displayed full Store release date"):
        run(args)

    assert state.read_bytes() == before_state
    assert master.read_bytes() == before_master
    assert not output.exists()
