"""Post-Followers Store date decisions and qualified master promotion.

The Store announcement is the calendar authority; its API timestamp is kept
as diagnostic evidence. All clocks and adult decisions are explicit inputs.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable

from radar_backend.domain.official_queue import (
    TAIPEI, queue_checked_numeric as checked_numeric, valid_date,
)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Store verification clock must include an offset")
    return value


def qualifies_for_master(result: dict) -> bool:
    """Use the existing ordinary official qualification threshold."""
    return (
        checked_numeric(result.get("official_followers"))
        and result["official_followers"] >= 5000
        and result.get("store_date_exact") is True
        and result.get("release_display_precision") == "date_full"
        and valid_date(result.get("release_date"))
    )


def apply_store_detail_to_result(result: dict, detail: dict, *, checked_at: datetime) -> bool:
    """Update one result in place without shifting an announced Taiwan day."""
    result["store_date_checked_at_taipei"] = _aware(checked_at).astimezone(TAIPEI).isoformat()
    result["store_date_exact"] = detail.get("exact") is True
    result["store_date_status"] = detail.get("status")
    if not result["store_date_exact"]:
        return False
    announced_day = result.get("release_date")
    timestamp_day = detail["release_start"]
    result["release_date"] = announced_day if valid_date(announced_day) else timestamp_day
    result["release_timestamp_taipei_date"] = detail.get(
        "release_timestamp_taipei_date", timestamp_day,
    )
    result["release_date_conflict"] = result["release_date"] != timestamp_day
    result["release_display_precision"] = "date_full"
    for key in (
        "release_display_provider", "release_date_basis",
        "release_date_timezone", "release_time_utc",
    ):
        result[key] = detail.get(key)
    return True


def select_pending_store_results(checkpoint: dict, *, today: date, limit: int = 25) -> list[dict]:
    """Return original result objects eligible for a later Store-only recheck."""
    selected = []
    today_prefix = today.isoformat()
    for result in sorted(
        checkpoint.get("official_results", {}).values(),
        key=lambda row: (row.get("release_date", "9999-12-31"), int(row.get("appid", 0))),
    ):
        if len(selected) >= limit:
            break
        if result.get("queue_source") == "twitch_steam_discovery":
            continue
        followers = result.get("official_followers")
        if not checked_numeric(followers) or followers < 5000:
            continue
        checked = str(result.get("store_date_checked_at_taipei") or "")
        if result.get("store_date_exact") is True or checked.startswith(today_prefix):
            continue
        selected.append(result)
    return selected


def upsert_qualified_master(
    master: dict, result: dict, *, now: datetime, blocked: set[int], is_disallowed: Callable,
) -> bool:
    """Merge verified ordinary official evidence into the established master."""
    if not qualifies_for_master(result):
        return False
    stamp = _aware(now).astimezone(timezone.utc).isoformat()
    appid = int(result["appid"])
    name = result.get("name") or f"Steam App {appid}"
    candidate = {
        "appid": appid,
        "name": name,
        "name_en": name,
        "release_raw": result["release_date"],
        "release_start": result["release_date"],
        "release_end": result["release_date"],
        "release_precision": "day",
        "release_display_precision": "date_full",
        "release_display_provider": result.get("release_display_provider"),
        "release_date_basis": result.get("release_date_basis"),
        "release_date_timezone": result.get("release_date_timezone") or "Asia/Taipei",
        "release_time_utc": result.get("release_time_utc"),
        "release_time_source": result.get("release_display_provider"),
        "release_timestamp_taipei_date": result.get("release_timestamp_taipei_date"),
        "release_date_conflict": result.get("release_date_conflict") is True,
        "release_date_verified_at": stamp,
        "post_followers_store_verified": True,
        "post_followers_store_verified_at": stamp,
        "followers": int(result["official_followers"]),
        "follower_checked_at": result.get("official_checked_at_taipei"),
        "follower_source": result.get("official_source") or "Steam Community XML memberCount",
        "official_ge5000": True,
        "store_url": result.get("steam_url") or f"https://store.steampowered.com/app/{appid}/",
    }
    if is_disallowed(candidate, blocked):
        return False
    games = master.get("games")
    if not isinstance(games, list):
        games = []
    by_id = {
        int(row["appid"]): dict(row) for row in games
        if isinstance(row, dict) and row.get("appid") is not None
    }
    prior = by_id.get(appid, {})
    merged = dict(prior)
    merged.update({key: value for key, value in candidate.items() if value is not None})
    merged.pop("follower_status", None)
    merged.pop("follower_unavailable_at", None)
    changed = merged != prior
    by_id[appid] = merged
    master["games"] = sorted(
        by_id.values(),
        key=lambda row: (
            str(row.get("release_start") or "9999-12-31"),
            -int(row.get("followers") or 0), int(row.get("appid") or 0),
        ),
    )
    if changed:
        master["updated_at"] = stamp
        master["post_followers_store_gate_version"] = 1
        master["post_followers_store_gate_checked_at"] = stamp
    return changed
