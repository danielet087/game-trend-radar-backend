"""Resolve Steam release dates to the calendar day in Taiwan.

A Store release_date.date string may differ from the local unlock day.
Use precise timestamps when known. Do not shift every date-only date.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any
import json
import logging
import time
import requests

from collectors.steam_upcoming import parse_release_window

TAIWAN_TZ = timezone(timedelta(hours=8))
LOGGER = logging.getLogger(__name__)
STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STORE_BROWSE_BATCH_SIZE = 35

def fetch_store_browse_releases(
    session: requests.Session,
    appids: list[int],
    *,
    country: str = "TW",
    batch_size: int = STORE_BROWSE_BATCH_SIZE,
    request_interval: float = 2.0,
) -> dict[int, dict[str, Any]]:
    """Batch-read Steam's public scheduled release timestamps.

    The endpoint does not supply a guaranteed actual unlock event. Treat
    steam_release_date as a scheduled, changeable timestamp. On errors,
    return partial data and let the Store's announced date take precedence
    as the fallback. This function never requests Community Followers.
    """
    ids = sorted({int(appid) for appid in appids if int(appid) > 0})
    releases: dict[int, dict[str, Any]] = {}
    last_request: float | None = None
    for start in range(0, len(ids), max(1, batch_size)):
        batch = ids[start:start + max(1, batch_size)]
        payload = {
            "ids": [{"appid": appid} for appid in batch],
            "context": {"country_code": country, "language": "english", "steam_realm": 1},
            "data_request": {"include_release": True},
        }
        for attempt in range(3):
            if last_request is not None:
                time.sleep(max(0.0, request_interval - (time.monotonic() - last_request)))
            last_request = time.monotonic()
            try:
                response = session.get(
                    STORE_BROWSE_URL,
                    params={"input_json": json.dumps(payload, separators=(",", ":"))},
                    timeout=25,
                )
                if response.status_code == 429:
                    time.sleep(15 * (attempt + 1))
                    continue
                response.raise_for_status()
                result = response.json()
                rows = (result.get("response") or {}).get("store_items") or []
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    appid = item.get("appid")
                    release = item.get("release")
                    if not isinstance(appid, int) or appid not in batch or not isinstance(release, dict):
                        continue
                    stamp = release.get("steam_release_date")
                    # Skip absent/placeholder timestamps. Do not infer a time
                    # from original_release_date or private/internal fields.
                    if isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
                        continue
                    try:
                        seconds = int(stamp)
                        instant = datetime.fromtimestamp(seconds, tz=timezone.utc)
                    except (OverflowError, OSError, ValueError, TypeError):
                        continue
                    if not 2010 <= instant.year <= 2100:
                        continue
                    releases[appid] = {
                        "steam_release_date": seconds,
                        "release_time_source": STORE_BROWSE_URL,
                        "is_coming_soon": release.get("is_coming_soon"),
                    }
                break
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                LOGGER.warning("Store Browse date lookup failed: %s", exc)
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
        LOGGER.info("Steam Store Browse timestamps: %d/%d apps processed", min(start + len(batch),len(ids)),len(ids))
    return releases

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
    for key in ("steam_release_date", "timestamp", "release_timestamp", "release_time_utc"):
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
    # A timestamp wildly divergent from the Store's public exact day may be
    # a stale/internal date; do not publish it as the local release date.
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
) -> dict[str, Any]:
    """Prefer validated Browse timestamp, then verified TW display, then Store day."""
    browse = browse_release or {}
    detail = (
        {"steam_release_date": browse["steam_release_date"],
         "release_time_source": browse.get("release_time_source", STORE_BROWSE_URL)}
        if browse.get("steam_release_date") is not None
        else fallback_detail
    )
    return resolve_release_date(appid, announced, detail=detail)


def corrected_games(
    games: list[dict[str, Any]],
    browse_releases: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Apply public Steam timestamps to saved games without rechecking Followers."""
    releases = browse_releases or {}
    result: list[dict[str, Any]] = []
    for record in games:
        game = dict(record)
        appid = int(game["appid"])
        fallback = None
        if (game.get("release_time_utc") and game.get("release_date_basis")
                in {"steam_structured_release_time", "steam_store_browse_release_time"}):
            key = ("steam_release_date" if game["release_date_basis"] == "steam_store_browse_release_time"
                   else "release_time_utc")
            fallback = {
                key: game["release_time_utc"],
                "release_time_source": game.get("release_time_source"),
            }
        release = resolved_store_date(
            appid, game.get("release_raw"), releases.get(appid),
            fallback_detail=fallback,
        )
        # Replace dates and provenance even when a precise time disappeared:
        # fallback dates come from the latest Store announcement.
        game.update(release)
        result.append(game)
    return result
