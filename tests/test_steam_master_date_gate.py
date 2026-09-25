"""Regression tests: an API timestamp must not qualify an unannounced title."""
from datetime import date
from unittest.mock import patch

import pytest

from scripts.steam_master_date_gate import filter_confirmed_master_games


TODAY = date(2026, 9, 25)


def game(appid, day, *, verified=True, followers=6000):
    return {
        "appid": appid,
        "release_start": day,
        "followers": followers,
        "release_display_precision": "date_full" if verified else None,
    }


def eligible(appid, day):
    return {
        "appid": appid,
        "release_start": day,
        "release_display_precision": "date_full",
        "sexual_content_screened": True,
    }


def test_timestamp_only_future_game_is_not_qualified():
    # Date-shaped Query API timestamp is not proof of a full Store announcement.
    rows = [game(1, "2026-12-31", verified=False)]
    assert filter_confirmed_master_games(rows, [eligible(1, "2026-12-31")], today=TODAY) == []


def test_future_game_needs_current_store_confirmation_of_same_date():
    rows = [game(1, "2026-12-31"), game(2, "2026-11-01")]
    assert filter_confirmed_master_games(
        rows, [eligible(1, "2027-01-01")], today=TODAY,
    ) == []


def test_currently_confirmed_future_title_is_preserved():
    rows = [game(1, "2026-10-14")]
    assert filter_confirmed_master_games(
        rows, [eligible(1, "2026-10-14")], today=TODAY,
    ) == rows


def test_released_store_confirmed_history_stays_without_rolling_candidate():
    rows = [game(1, "2026-09-20"), game(2, "2026-09-25")]
    assert filter_confirmed_master_games(rows, [], today=TODAY) == rows


def test_unverified_history_is_not_rescued_merely_by_past_timestamp():
    rows = [game(1, "2026-09-20", verified=False)]
    assert filter_confirmed_master_games(rows, [], today=TODAY) == []


def test_blocked_adult_and_subthreshold_titles_are_not_qualified():
    rows = [game(1, "2026-10-14"), game(2, "2026-10-15", followers=4000)]
    with patch("scripts.steam_master_date_gate.excluded_appids", return_value={1}):
        assert filter_confirmed_master_games(
            rows, [eligible(1, "2026-10-14"), eligible(2, "2026-10-15")],
            today=TODAY,
        ) == []


def test_incomplete_store_eligibility_snapshot_fails_closed():
    with pytest.raises(RuntimeError, match="not fully verified"):
        filter_confirmed_master_games(
            [game(1, "2026-10-14")],
            [{"appid": 1, "release_start": "2026-10-14",
              "release_display_precision": "date_year",
              "sexual_content_screened": True}],
            today=TODAY,
        )


def test_duplicate_store_snapshot_appid_is_rejected():
    with pytest.raises(RuntimeError, match="Duplicate AppID"):
        filter_confirmed_master_games(
            [], [eligible(1, "2026-10-14"), eligible(1, "2026-10-14")],
            today=TODAY,
        )


def test_legacy_two_month_updater_cannot_reintroduce_unverified_dates():
    from argparse import Namespace
    from scripts.update_steam_daily import run

    with pytest.raises(RuntimeError, match="cannot verify the displayed full Store release date"):
        run(Namespace())
