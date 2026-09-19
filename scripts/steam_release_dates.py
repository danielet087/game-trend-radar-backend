"""Resolve Steam release dates to the calendar day in Taiwan.

A Store release_date.date string may differ from the local unlock day.
Use precise timestamps when known. Do not shift every date-only date.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from collectors.steam_upcoming import parse_release_window

TAIWAN_TZ = timezone(timedelta(hours=8))

# User-reported TW Steam storefront calendar dates. These are date-only
# corrections, NOT verified UTC unlock timestamps. Reconfirm if Steam's
# underlying Store API date changes.
TAIWAN_STOREFRONT_DATES: dict[int, dict[str, str]] = {
    4019220: {
        "store_date": "2026-09-21",
        "taiwan_date": "2026-09-22",
        "basis": "steam_tw_storefront_date_user_reported",
    },
}

def resolve_release_date(
    appid: int, announced: Any, *, detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return Taiwan release date, preserving original Store date and source."""
    release = parse_release_window(announced)
    result: dict[str, Any] = {
        "release_raw": release.raw,
        "release_start": release.start.isoformat() if release.start else None,
        "release_end": release.end.isoformat() if release.end else None,
        "release_precision": release.precision,
        "release_date_timezone": "Asia/Taipei",
        "release_date_basis": "steam_store_announced_date",
        "release_time_utc": None,
        "release_time_source": None,
    }

    details = detail or {}
    candidate = None
    for key in ("timestamp", "release_timestamp", "release_time_utc"):
        value = details.get(key)
        if value is not None and not isinstance(value, bool):
            candidate = value
            break

    if candidate is None:
        tw_display = TAIWAN_STOREFRONT_DATES.get(int(appid))
        if tw_display and result["release_start"] == tw_display["store_date"]:
            result["release_start"] = tw_display["taiwan_date"]
            result["release_end"] = tw_display["taiwan_date"]
            result["release_date_basis"] = tw_display["basis"]
        return result

    precise = parse_release_window(candidate)
    if precise.precision != "day" or not precise.start:
        result["release_time_source"] = None
        return result

    timestamp: datetime | None = None
    try:
        if isinstance(candidate, (int, float)) or (
            isinstance(candidate, str) and candidate.isdigit()
        ):
            seconds = float(candidate)
            if abs(seconds) >= 100_000_000_000:
                seconds /= 1000
            timestamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
        elif isinstance(candidate, str) and "T" in candidate:
            timestamp = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        timestamp = None

    if timestamp is None or timestamp.tzinfo is None:
        result["release_time_source"] = None
        return result

    utc = timestamp.astimezone(timezone.utc).replace(microsecond=0)
    result["release_start"] = precise.start.isoformat()
    result["release_end"] = precise.end.isoformat() if precise.end else None
    result["release_precision"] = precise.precision
    result["release_date_basis"] = "steam_structured_release_time"
    result["release_time_utc"] = utc.isoformat().replace("+00:00", "Z")
    return result


def corrected_games(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Correct verified release times in both cached and freshly fetched games."""
    result: list[dict[str, Any]] = []
    for record in games:
        game = dict(record)
        release = resolve_release_date(
            int(game["appid"]), game.get("release_raw"),
            detail={"release_time_utc": game["release_time_utc"]}
            if game.get("release_time_utc") and game.get("release_date_basis") == "steam_structured_release_time"
            else None,
        )
        if release.get("release_date_basis") != "steam_store_announced_date":
            game.update(release)
        result.append(game)
    return result
