"""Pure Steam Store release decisions and post-Followers catalogue rules."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
STORE_DATE_PROVIDER = "Steam IStoreBrowseService/GetItems"


def parse_store_release_detail(
    item: dict[str, Any] | None, *, today: date,
    provider: str = STORE_DATE_PROVIDER, taipei=TAIPEI,
    fromtimestamp: Callable = datetime.fromtimestamp,
) -> dict[str, Any]:
    """Accept full dates or an actual release, preserving the Taiwan instant."""
    if not isinstance(item, dict) or not isinstance(item.get("release"), dict):
        return {"exact": False, "status": "unavailable"}
    release = item["release"]
    label = release.get("coming_soon_display")
    stamp = release.get("steam_release_date")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
        return {"exact": False, "status": str(label or "missing_release_time")}
    try:
        instant = fromtimestamp(int(stamp), tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return {"exact": False, "status": str(label or "invalid_release_time")}
    tw_day = instant.astimezone(taipei).date()
    exact = label == "date_full" or (
        release.get("is_coming_soon") is False and tw_day <= today
    )
    return {
        "exact": exact,
        "status": "date_full" if label == "date_full" else (
            "released_exact" if exact else str(label or "unknown")
        ),
        "release_start": tw_day.isoformat() if exact else None,
        "release_timestamp_taipei_date": tw_day.isoformat() if exact else None,
        "release_time_utc": instant.isoformat().replace("+00:00", "Z") if exact else None,
        "release_display_precision": "date_full" if exact else None,
        "release_display_provider": provider if exact else None,
        "release_date_basis": "steam_store_browse_verified_full_date" if exact else None,
        "release_date_timezone": "Asia/Taipei" if exact else None,
    }


def prepare_store_release_detail(
    row: dict[str, Any], detail: dict[str, Any], *,
    has_taiwan_store_date_authority: Callable,
) -> tuple[dict[str, Any], bool]:
    """Copy Store evidence before the application records its verification time."""
    if detail.get("exact") is not True:
        raise RuntimeError("Cannot apply a non-exact Steam Store release date")
    result = dict(row)
    store_authority = has_taiwan_store_date_authority(row)
    timestamp_day = detail["release_start"]
    prior_day = str(row.get("release_start") or "")
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
        "release_timestamp_taipei_date", timestamp_day,
    )
    if day != timestamp_day:
        result["release_date_conflict"] = True
        result["release_date_conflict_note"] = (
            "TW Store announced full date preserved; API timestamp maps to a different Taiwan day"
        )
    elif store_authority:
        result["release_date_conflict"] = False
        result.pop("release_date_conflict_note", None)
    else:
        result.pop("release_date_conflict", None)
        result.pop("release_date_conflict_note", None)
    for key in (
        "release_display_precision", "release_display_provider",
        "release_date_basis", "release_date_timezone", "release_time_utc",
    ):
        if store_authority and key == "release_display_provider":
            continue
        result[key] = detail.get(key)
    return result, store_authority


def filter_confirmed_master_games(
    games: list[dict[str, Any]], eligible_rows: list[dict[str, Any]], *,
    today: date, blocked: set[int], is_disallowed: Callable,
    is_twitch_qualified: Callable,
) -> list[dict[str, Any]]:
    """Retain qualified future games and released history in their source order."""
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
            followers = row["followers"]
            if followers is not None:
                followers = int(followers)
        except (KeyError, ValueError, TypeError):
            continue
        twitch_qualified = is_twitch_qualified(row)
        if appid in seen or ((followers is None or followers < 5000) and not twitch_qualified):
            continue
        if day > today:
            if row.get("release_display_precision") != "date_full":
                continue
            if row.get("post_followers_store_verified") is not True:
                continue
            if appid not in eligible_ids and not twitch_qualified:
                continue
        seen.add(appid)
        retained.append(row)
    return retained
