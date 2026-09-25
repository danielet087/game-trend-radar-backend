"""Steam Store exact-date gate for officially qualified games.

A release timestamp from discovery is only a search hint. After official
Followers >= 5,000, the backend must query current Steam Store metadata again.
Future titles qualify only when Store Browse says coming_soon_display=date_full.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from scripts.screen_steam_candidates_before_followers import fetch_metadata
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed

TAIPEI = ZoneInfo("Asia/Taipei")
STORE_DATE_PROVIDER = "Steam IStoreBrowseService/GetItems"


def parse_store_release_detail(item: dict[str, Any] | None, *, today: date) -> dict[str, Any]:
    """Return a fail-closed Store release-date decision for one AppID."""
    if not isinstance(item, dict) or not isinstance(item.get("release"), dict):
        return {"exact": False, "status": "unavailable"}
    release = item["release"]
    label = release.get("coming_soon_display")
    stamp = release.get("steam_release_date")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
        return {"exact": False, "status": str(label or "missing_release_time")}
    try:
        instant = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return {"exact": False, "status": str(label or "invalid_release_time")}
    tw_day = instant.astimezone(TAIPEI).date()
    # Before release, only Steam's explicit full-date display is acceptable.
    # After release, is_coming_soon=false plus the actual Store timestamp is an
    # exact historical release, so previously qualified history can be kept.
    is_coming_soon = release.get("is_coming_soon")
    exact = label == "date_full" or (is_coming_soon is False and tw_day <= today)
    return {
        "exact": exact,
        "status": "date_full" if label == "date_full" else (
            "released_exact" if exact else str(label or "unknown")
        ),
        # This timestamp-derived Taiwan day is diagnostic metadata only.
        # Steam's visible TW Store full-date announcement remains the display-date authority.
        "release_start": tw_day.isoformat() if exact else None,
        "release_timestamp_taipei_date": tw_day.isoformat() if exact else None,
        "release_time_utc": instant.isoformat().replace("+00:00", "Z") if exact else None,
        "release_display_precision": "date_full" if exact else None,
        "release_display_provider": STORE_DATE_PROVIDER if exact else None,
        "release_date_basis": "steam_store_browse_verified_full_date" if exact else None,
        "release_date_timezone": "Asia/Taipei" if exact else None,
    }


def fetch_store_release_details(
    session: requests.Session,
    appids: list[int],
    *,
    today: date,
    interval: float = 1.5,
) -> dict[int, dict[str, Any]]:
    """Batch-read Steam Store release detail without touching Followers state."""
    ids = sorted({int(x) for x in appids if int(x) > 0})
    if not ids:
        return {}
    items = fetch_metadata(session, ids, interval=interval)
    return {
        appid: parse_store_release_detail(items.get(appid), today=today)
        for appid in ids
    }


def apply_store_release_detail(row: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Copy a verified Store date onto an already officially-qualified row."""
    if detail.get("exact") is not True:
        raise RuntimeError("Cannot apply a non-exact Steam Store release date")
    result = dict(row)
    timestamp_day = detail["release_start"]
    prior_day = str(row.get("release_start") or "")
    # When the upstream TW Store candidate was already verified as date_full,
    # keep that announced calendar day. The Store API timestamp can cross
    # midnight in Taiwan and must not silently shift the user-visible date.
    try:
        date.fromisoformat(prior_day)
        prior_exact = row.get("release_display_precision") == "date_full"
    except (TypeError, ValueError):
        prior_exact = False
    day = prior_day if prior_exact else timestamp_day
    result["release_raw"] = day
    result["release_start"] = day
    result["release_end"] = day
    result["release_precision"] = "day"
    result["release_timestamp_taipei_date"] = detail.get(
        "release_timestamp_taipei_date", timestamp_day
    )
    if day != timestamp_day:
        result["release_date_conflict"] = True
        result["release_date_conflict_note"] = (
            "TW Store announced full date preserved; API timestamp maps to a different Taiwan day"
        )
    else:
        result.pop("release_date_conflict", None)
        result.pop("release_date_conflict_note", None)
    for key in (
        "release_display_precision", "release_display_provider",
        "release_date_basis", "release_date_timezone", "release_time_utc",
    ):
        result[key] = detail.get(key)
    verified_at = datetime.now(timezone.utc).isoformat()
    result["release_date_verified_at"] = verified_at
    result["post_followers_store_verified_at"] = verified_at
    result["post_followers_store_verified"] = True
    return result


def filter_confirmed_master_games(
    games: list[dict[str, Any]],
    eligible_rows: list[dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, Any]]:
    """Keep only >=5000 titles carrying post-Followers Store verification."""
    blocked = excluded_appids()
    eligible_ids = {
        int(row["appid"])
        for row in eligible_rows
        if isinstance(row, dict)
        and row.get("release_display_precision") == "date_full"
        and row.get("sexual_content_screened") is True
    }
    retained: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in games:
        if not isinstance(row, dict) or is_disallowed(row, blocked):
            continue
        try:
            appid = int(row["appid"])
            day = date.fromisoformat(row["release_start"])
            followers = int(row["followers"])
        except (KeyError, ValueError, TypeError):
            continue
        if appid in seen or followers < 5000:
            continue
        if day > today:
            if row.get("release_display_precision") != "date_full":
                continue
            if row.get("post_followers_store_verified") is not True:
                continue
            # Future records must still belong to today's exact-date candidate
            # universe. The post-Followers Store check is the final date source.
            if appid not in eligible_ids:
                continue
        # Released history is retained. Once the game has actually released,
        # it must not disappear merely because Store Browse stops exposing the
        # old coming-soon display metadata.
        seen.add(appid)
        retained.append(row)
    return retained
