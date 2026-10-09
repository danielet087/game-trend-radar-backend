"""Preview Store HTTP and composition, retaining its independent retry policy."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from radar_backend.adapters.public_catalog import preserve_player_categories as _preserve_player_categories
from radar_backend.adapters.public_release_dates import resolved_store_date
from radar_backend.adapters.steam_localized_titles import add_traditional_display_names
from radar_backend.application import preview_metadata as application
from radar_backend.domain import preview_metadata as rules
from radar_backend.domain.preview_metadata import localized_names


APP_DETAILS = "https://store.steampowered.com/api/appdetails"
LOGGER = logging.getLogger(__name__)


def steam_get(
    session: requests.Session, url: str, params: dict[str, Any], *,
    sleep: Callable | None = None, logger=None, requests_module=None,
) -> dict[str, Any] | None:
    sleep = sleep if sleep is not None else time.sleep
    logger = logger if logger is not None else LOGGER
    requests_module = requests_module if requests_module is not None else requests
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=25)
            if response.status_code == 429:
                delay = max(60, min(180, 60 * (attempt + 1)))
                logger.warning("Steam Store returned 429; sleeping %ds", delay)
                sleep(delay)
                continue
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else None
        except (requests_module.RequestException, ValueError) as exc:
            logger.warning("Steam Store metadata request failed: %s", exc)
            if attempt < 2:
                sleep(5 * (attempt + 1))
    return None


def app_details(
    session: requests.Session, appid: int, *, language: str = "english",
    fetch: Callable | None = None, url: str = APP_DETAILS,
) -> dict[str, Any] | None:
    fetch = fetch if fetch is not None else steam_get
    data = fetch(session, url, {"appids": appid, "cc": "TW", "l": language})
    return rules.app_details_from_response(appid, data)


def add_traditional_name(
    session: requests.Session, appid: int, game: dict[str, Any], english_details: dict[str, Any], *,
    delay_seconds: float, sleep: Callable | None = None, fetch: Callable | None = None,
    select_names: Callable | None = None, display_names: Callable | None = None,
) -> None:
    application.add_traditional_name(
        session, appid, game, english_details, delay_seconds=delay_seconds,
        sleep=sleep if sleep is not None else time.sleep,
        app_details=fetch if fetch is not None else app_details,
        localized_names=select_names if select_names is not None else localized_names,
        add_traditional_display_names=(
            display_names if display_names is not None else add_traditional_display_names
        ),
    )


def normalized_metadata(
    appid: int, details: dict[str, Any], followers: int | None,
    checked_at: str | None, *, browse_release: dict[str, Any] | None = None,
    preserve_player_categories: Callable | None = None, resolve: Callable | None = None,
    clock: Callable | None = None,
) -> dict[str, Any] | None:
    """Use the shared catalog owner, retaining an explicit replacement port."""
    return application.normalized_metadata(
        appid, details, followers, checked_at, browse_release=browse_release,
        resolved_store_date=resolve if resolve is not None else resolved_store_date,
        preserve_player_categories=(
            preserve_player_categories if preserve_player_categories is not None
            else _preserve_player_categories
        ),
        clock=clock if clock is not None else lambda: datetime.now(timezone.utc).isoformat(),
    )
