"""Regression tests for the post-Followers Steam Store exact-date gate."""
from datetime import date
from unittest.mock import patch

import pytest

from scripts.steam_master_date_gate import (
    apply_store_release_detail,
    filter_confirmed_master_games,
    parse_store_release_detail,
)

TODAY = date(2026, 9, 25)


def game(appid, day, *, post_verified=True, followers=6000):
    return {
        "appid": appid,
        "release_start": day,
        "followers": followers,
        "release_display_precision": "date_full" if post_verified else None,
        "post_followers_store_verified": post_verified,
    }


def eligible(appid, day):
    return {
        "appid": appid,
        "release_start": day,
        "release_display_precision": "date_full",
        "sexual_content_screened": True,
    }


def store_item(stamp, label="date_full", coming=True):
    return {
        "appid": 1,
        "release": {
            "steam_release_date": stamp,
            "coming_soon_display": label,
            "is_coming_soon": coming,
        },
    }


def test_future_timestamp_without_post_followers_store_gate_is_rejected():
    rows = [game(1, "2026-12-31", post_verified=False)]
    assert filter_confirmed_master_games(
        rows, [eligible(1, "2026-12-31")], today=TODAY,
    ) == []


def test_post_followers_store_gate_is_final_date_authority():
    # The pre-Followers snapshot may have changed between the two checks.
    rows = [game(1, "2027-01-01")]
    assert filter_confirmed_master_games(
        rows, [eligible(1, "2026-12-31")], today=TODAY,
    ) == rows


def test_future_title_must_still_be_in_current_exact_date_universe():
    rows = [game(1, "2026-10-14")]
    assert filter_confirmed_master_games(rows, [], today=TODAY) == []


def test_released_store_confirmed_history_stays_without_rolling_candidate():
    rows = [game(1, "2026-09-20"), game(2, "2026-09-25")]
    assert filter_confirmed_master_games(rows, [], today=TODAY) == rows


def test_blocked_adult_and_subthreshold_titles_are_not_qualified():
    rows = [game(1, "2026-10-14"), game(2, "2026-10-15", followers=4000)]
    with patch("scripts.steam_master_date_gate.excluded_appids", return_value={1}):
        assert filter_confirmed_master_games(
            rows, [eligible(1, "2026-10-14"), eligible(2, "2026-10-15")],
            today=TODAY,
        ) == []


def test_store_date_full_converts_timestamp_to_taipei_day():
    detail = parse_store_release_detail(
        store_item(1796054400, "date_full", True), today=TODAY,
    )
    assert detail["exact"] is True
    assert detail["release_start"] == "2026-12-01"
    row = apply_store_release_detail(
        {"appid": 1, "followers": 6000, "release_start": "2026-12-31"}, detail,
    )
    assert row["release_start"] == "2026-12-01"
    assert row["post_followers_store_verified"] is True


def test_month_quarter_year_are_rejected_after_followers():
    for label in ("date_month", "date_quarter", "date_year", "coming_soon"):
        detail = parse_store_release_detail(
            store_item(1798675200, label, True), today=TODAY,
        )
        assert detail["exact"] is False
        assert detail["status"] == label


def test_actual_released_title_is_exact_history():
    detail = parse_store_release_detail(
        store_item(1789603200, None, False), today=TODAY,
    )
    assert detail["exact"] is True
    assert detail["status"] == "released_exact"


def test_apply_non_exact_detail_fails_closed():
    with pytest.raises(RuntimeError, match="non-exact"):
        apply_store_release_detail(
            {"appid": 1, "followers": 6000},
            {"exact": False, "status": "date_year"},
        )


def test_legacy_two_month_updater_cannot_reintroduce_unverified_dates():
    from argparse import Namespace
    from scripts.update_steam_daily import run

    with pytest.raises(RuntimeError, match="cannot verify the displayed full Store release date"):
        run(Namespace())
