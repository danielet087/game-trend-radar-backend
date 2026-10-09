"""Resolve the public timestamp calendar without changing official Store gates.

This older public-calendar policy prefers a corroborated supplied timestamp.
The official catalog's announced-date authority remains a separate rule.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from radar_backend.domain.release_window import ReleaseWindow, parse_release_window

STORE_BROWSE_RELEASE_SOURCE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"

# Date-only corrections reported from the TW storefront, not verified unlock
# instants. A changed underlying Store date disables the matching correction.
TAIWAN_STOREFRONT_DATES: dict[int, dict[str, str]] = {
    4019220: {
        "store_date": "2026-09-21",
        "taiwan_date": "2026-09-22",
        "basis": "steam_tw_storefront_date_user_reported",
    },
}


def resolve_release_date(
    appid: int,
    announced: Any,
    *,
    detail: dict[str, Any] | None = None,
    parse_window: Callable[[Any], ReleaseWindow] | None = None,
    storefront_dates: dict[int, dict[str, str]] | None = None,
    datetime_type: Any = datetime,
) -> dict[str, Any]:
    """Return Taiwan dates and provenance while preserving source precedence."""
    parser = parse_release_window if parse_window is None else parse_window
    corrections = TAIWAN_STOREFRONT_DATES if storefront_dates is None else storefront_dates
    release = parser(announced)
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
    for key in ("steam_release_date", "timestamp", "release_timestamp", "release_time_utc"):
        value = details.get(key)
        if value is not None and not isinstance(value, bool):
            candidate = value
            break

    if candidate is None:
        tw_display = corrections.get(int(appid))
        if tw_display and result["release_start"] == tw_display["store_date"]:
            result["release_start"] = tw_display["taiwan_date"]
            result["release_end"] = tw_display["taiwan_date"]
            result["release_date_basis"] = tw_display["basis"]
        return result

    precise = parser(candidate)
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
            timestamp = datetime_type.fromtimestamp(seconds, tz=timezone.utc)
        elif isinstance(candidate, str) and "T" in candidate:
            timestamp = datetime_type.fromisoformat(candidate.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        timestamp = None

    if timestamp is None or timestamp.tzinfo is None:
        result["release_time_source"] = None
        return result

    utc = timestamp.astimezone(timezone.utc).replace(microsecond=0)
    if release.start and abs((precise.start - release.start).days) > 2:
        return result
    result["release_start"] = precise.start.isoformat()
    result["release_end"] = precise.end.isoformat() if precise.end else None
    result["release_precision"] = precise.precision
    result["release_date_basis"] = (
        "steam_store_browse_release_time"
        if details.get("steam_release_date") is not None
        else "steam_structured_release_time"
    )
    result["release_time_source"] = (
        details.get("release_time_source") if details.get("steam_release_date") is not None
        else None
    )
    result["release_time_utc"] = utc.isoformat().replace("+00:00", "Z")
    return result


def resolved_store_date(
    appid: int,
    announced: Any,
    browse_release: dict[str, Any] | None = None,
    *,
    fallback_detail: dict[str, Any] | None = None,
    resolve: Callable[..., dict[str, Any]] | None = None,
    browse_source: str = STORE_BROWSE_RELEASE_SOURCE,
) -> dict[str, Any]:
    """Prefer present Browse metadata over cached structured-time fallback."""
    resolver = resolve_release_date if resolve is None else resolve
    browse = browse_release or {}
    detail = (
        {"steam_release_date": browse["steam_release_date"],
         "release_time_source": browse.get("release_time_source", browse_source)}
        if browse.get("steam_release_date") is not None
        else fallback_detail
    )
    return resolver(appid, announced, detail=detail)
