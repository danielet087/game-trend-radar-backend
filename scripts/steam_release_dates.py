"""Compatible scheduled Steam release dates and partial timestamp lookup.

Date-only announcements are never shifted by an inferred unlock hour. The
scheduled-time policy remains separate from the official master Store gate.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any
import logging
import time
import requests

from collectors.steam_upcoming import parse_release_window
from radar_backend.adapters import steam_release_timestamps as _transport
from radar_backend.application import public_release_dates as _application
from radar_backend.domain import public_release_dates as _rules

TAIWAN_TZ = timezone(timedelta(hours=8))
LOGGER = logging.getLogger(__name__)
STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STORE_BROWSE_BATCH_SIZE = 35
TAIWAN_STOREFRONT_DATES = _rules.TAIWAN_STOREFRONT_DATES


def fetch_store_browse_releases(
    session: requests.Session, appids: list[int], *, country: str = "TW",
    batch_size: int = STORE_BROWSE_BATCH_SIZE, request_interval: float = 2.0,
) -> dict[int, dict[str, Any]]:
    """Keep the scheduled timestamp lookup's bounded partial-data policy."""
    return _transport.fetch_release_timestamps(
        session, appids, country=country, batch_size=batch_size,
        request_interval=request_interval, sleep=time.sleep, monotonic=time.monotonic,
        logger=LOGGER, requests_module=requests, url=STORE_BROWSE_URL,
        fromtimestamp=datetime.fromtimestamp,
    )


def resolve_release_date(
    appid: int, announced: Any, *, detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _rules.resolve_release_date(
        appid, announced, detail=detail, parse_window=parse_release_window,
        storefront_dates=TAIWAN_STOREFRONT_DATES, datetime_type=datetime,
    )


def resolved_store_date(
    appid: int, announced: Any, browse_release: dict[str, Any] | None = None, *,
    fallback_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _rules.resolved_store_date(
        appid, announced, browse_release, fallback_detail=fallback_detail,
        resolve=resolve_release_date, browse_source=STORE_BROWSE_URL,
    )


def corrected_games(
    games: list[dict[str, Any]], browse_releases: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return _application.corrected_games(games, browse_releases, resolve=resolved_store_date)
