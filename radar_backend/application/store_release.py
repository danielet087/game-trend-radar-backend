"""Store release use cases with explicit metadata, ledger, and clock ports."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from radar_backend.domain import store_release as rules


def fetch_store_release_details(
    session, appids: list[int], *, today: date, interval: float = 1.5,
    fetch_metadata: Callable,
    parse_store_release_detail: Callable = rules.parse_store_release_detail,
) -> dict[int, dict[str, Any]]:
    """Read one complete Store batch and decide dates without Followers I/O."""
    ids = sorted({int(value) for value in appids if int(value) > 0})
    if not ids:
        return {}
    items = fetch_metadata(session, ids, interval=interval)
    return {
        appid: parse_store_release_detail(items.get(appid), today=today)
        for appid in ids
    }


def apply_store_release_detail(
    row: dict[str, Any], detail: dict[str, Any], *, clock: Callable,
    has_taiwan_store_date_authority: Callable,
) -> dict[str, Any]:
    """Record verification only after the pure Store update has succeeded."""
    result, store_authority = rules.prepare_store_release_detail(
        row, detail,
        has_taiwan_store_date_authority=has_taiwan_store_date_authority,
    )
    verified_at = clock().isoformat()
    if not store_authority:
        result["release_date_verified_at"] = verified_at
    result["post_followers_store_verified_at"] = verified_at
    result["post_followers_store_verified"] = True
    return result


def filter_confirmed_master_games(
    games: list[dict[str, Any]], eligible_rows: list[dict[str, Any]], *,
    today: date, excluded_appids: Callable, is_disallowed: Callable,
    is_twitch_qualified: Callable,
) -> list[dict[str, Any]]:
    """Read the adult ledger before checking the current candidate universe."""
    blocked = excluded_appids()
    return rules.filter_confirmed_master_games(
        games, eligible_rows, today=today, blocked=blocked,
        is_disallowed=is_disallowed, is_twitch_qualified=is_twitch_qualified,
    )
