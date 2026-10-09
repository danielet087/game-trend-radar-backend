"""Compatible Steam Store exact-date gate for officially qualified games.

A release timestamp from discovery is only a search hint. After official
Followers >= 5,000, the backend must query current Steam Store metadata again.
Future titles qualify only when Store Browse says coming_soon_display=date_full.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from radar_backend.application import store_release as _application
from radar_backend.domain import store_release as _rules
from radar_backend.domain.store_release import TAIPEI, STORE_DATE_PROVIDER
from scripts.screen_steam_candidates_before_followers import fetch_metadata
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.twitch_steam_admission import is_twitch_qualified, has_taiwan_store_date_authority


def parse_store_release_detail(item: dict[str, Any] | None, *, today: date) -> dict[str, Any]:
    """Compatible entry point for the pure fail-closed Store date decision."""
    return _rules.parse_store_release_detail(
        item, today=today, provider=STORE_DATE_PROVIDER, taipei=TAIPEI,
        fromtimestamp=datetime.fromtimestamp,
    )


def fetch_store_release_details(
    session: requests.Session,
    appids: list[int],
    *,
    today: date,
    interval: float = 1.5,
) -> dict[int, dict[str, Any]]:
    """Batch-read Steam Store release detail without touching Followers state."""
    return _application.fetch_store_release_details(
        session, appids, today=today, interval=interval,
        fetch_metadata=fetch_metadata,
        parse_store_release_detail=parse_store_release_detail,
    )


def apply_store_release_detail(row: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Copy a verified Store date onto an already officially-qualified row."""
    return _application.apply_store_release_detail(
        row, detail, clock=lambda: datetime.now(timezone.utc),
        has_taiwan_store_date_authority=has_taiwan_store_date_authority,
    )


def filter_confirmed_master_games(
    games: list[dict[str, Any]],
    eligible_rows: list[dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, Any]]:
    """Keep only >=5000 titles carrying post-Followers Store verification."""
    return _application.filter_confirmed_master_games(
        games, eligible_rows, today=today, excluded_appids=excluded_appids,
        is_disallowed=is_disallowed, is_twitch_qualified=is_twitch_qualified,
    )
