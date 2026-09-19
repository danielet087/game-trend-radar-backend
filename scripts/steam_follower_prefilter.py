"""Resume-safe third-party *priority* screen for exact Steam Followers verification.

Third-party numbers are NEVER eligible for publication or persisted to the
official Steam XML follower cache. This screen only changes verification order.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import requests

VANITY_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
BULK_URL = "https://api.steam-groups.com/api/groups/bulk"
GROUP_BASE = 103582791429521408
PRIORITY_THRESHOLD = 4000
STEAM_PUBLIC_THRESHOLD = 5000
DEFAULT_BATCH_SIZE = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _group_id(session: requests.Session, key: str, appid: int) -> int | None:
    try:
        response = session.get(
            VANITY_URL, params={"key": key, "vanityurl": str(appid), "url_type": 3},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            "Steam vanity lookup temporarily unavailable; priority cursor unchanged"
        ) from None
    if response.status_code != 200:
        raise RuntimeError(
            f"Steam vanity returned HTTP {response.status_code}; priority cursor unchanged"
        )
    try:
        item = response.json()["response"]
        if item.get("success") != 1:
            return None
        gid = int(item["steamid"])
        return gid - GROUP_BASE if gid > GROUP_BASE else None
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Unexpected Steam vanity response; priority cursor unchanged") from None


def _bulk_counts(
    session: requests.Session, group_ids: list[int],
) -> dict[int, int]:
    if not group_ids:
        return {}
    try:
        response = session.post(
            BULK_URL,
            json={"ids": group_ids, "limit": len(group_ids)},
            timeout=30,
        )
    except requests.RequestException:
        raise RuntimeError("Third-party bulk lookup unavailable; priority cursor unchanged") from None
    if response.status_code != 200:
        raise RuntimeError(
            f"Third-party bulk returned HTTP {response.status_code}; priority cursor unchanged"
        )
    try:
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("unexpected group data")
        counts: dict[int, int] = {}
        for item in data["data"]:
            if not isinstance(item, dict):
                continue
            gid, members = item.get("id"), item.get("members")
            if (
                isinstance(gid, int) and not isinstance(gid, bool)
                and isinstance(members, int) and not isinstance(members, bool)
                and members >= 0 and gid in group_ids
            ):
                counts[gid] = members
        return counts
    except (ValueError, TypeError):
        raise RuntimeError(
            "Third-party bulk response malformed; priority cursor unchanged"
        ) from None


def scan_batch(
    catalog: list[dict[str, Any]],
    prefilter: dict[str, Any],
    *,
    steam_api_key: str,
    initial_index: int,
    limit: int = DEFAULT_BATCH_SIZE,
    request_interval: float = 0.5,
    session: requests.Session | None = None,
) -> dict[str, int | bool]:
    """Record one *entire* new window only after mapping and bulk succeed.

    If a request fails the saved window is unchanged; the caller saves its
    existing JSON and retries rather than silently classifying unknown titles.
    """
    if not steam_api_key:
        raise RuntimeError("STEAM_WEB_API_KEY required for third-party priority screen")
    if limit < 1:
        raise ValueError("batch size must be positive")
    if prefilter.get("version", 1) != 1:
        raise RuntimeError("Unknown prefilter version")
    start = int(prefilter.get("next_index", initial_index))
    if not initial_index <= start <= len(catalog):
        raise RuntimeError("Priority prefilter cursor out of range")
    stop = min(start + limit, len(catalog))
    window = catalog[start:stop]
    if not window:
        prefilter["next_index"] = start
        prefilter["complete"] = True
        return {"start_index": start, "next_index": start,
                "screened": 0, "priority": 0, "missing": 0, "complete": True}

    client = session or requests.Session()
    client.headers.setdefault("User-Agent", "GameTrendRadarThirdPartyPriority/1.0")
    groups: dict[int, int | None] = {}
    for idx, row in enumerate(window):
        if idx:
            time.sleep(request_interval)
        groups[int(row["appid"])] = _group_id(client, steam_api_key, int(row["appid"]))
    short_ids = list(dict.fromkeys(g for g in groups.values() if g is not None))
    counts = _bulk_counts(client, short_ids)
    stamp = _now()
    staged = {}
    priority_count = 0
    missing_count = 0
    for appid, group in groups.items():
        members = counts.get(group) if group is not None else None
        unknown = members is None
        priority = unknown or members >= PRIORITY_THRESHOLD
        priority_count += priority
        missing_count += unknown
        staged[str(appid)] = {
            "third_party_followers": members,
            "group_short_id": group,
            "priority": priority,
            "checked_at": stamp,
        }
    # Commit the window atomically in the in-memory JSON object.
    prefilter.setdefault("games", {}).update(staged)
    prefilter["version"] = 1
    prefilter["threshold"] = PRIORITY_THRESHOLD
    prefilter["next_index"] = stop
    prefilter["complete"] = stop >= len(catalog)
    prefilter["updated_at"] = stamp
    return {
        "start_index": start, "next_index": stop,
        "screened": len(window), "priority": priority_count,
        "missing": missing_count, "complete": stop >= len(catalog),
    }


def pending_priorities(
    catalog: list[dict[str, Any]],
    prefilter: dict[str, Any],
    official_cache: dict[str, Any],
    *,
    min_start_index: int,
    max_candidates: int = 50,
) -> list[dict[str, Any]]:
    """Priority candidates not already confirmed by official Steam XML."""
    saved = prefilter.get("games") or {}
    pending = []
    for row in catalog[min_start_index:]:
        appid = str(row["appid"])
        entry = saved.get(appid)
        if not entry or not entry.get("priority") or appid in official_cache:
            continue
        pending.append(row)
        if len(pending) >= max_candidates:
            break
    return pending
