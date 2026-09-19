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
MISSING_ASSUMED_BELOW = 3000  # scheduling label, never a fabricated follower number
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
            # This third-party API can represent its numeric group ID as a
            # JSON string. Do not classify these *returned* groups as missing.
            if isinstance(gid, bool) or not isinstance(gid, (int, str)):
                continue
            try:
                parsed_gid = int(gid)
            except ValueError:
                continue
            if (
                isinstance(members, int) and not isinstance(members, bool)
                and members >= 0 and parsed_gid in group_ids
            ):
                counts[parsed_gid] = members
        if data["data"] and not counts:
            raise ValueError("bulk data returned, but none of the IDs/member counts parsed")
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
    # Repair a prior parser-version-1 window that mistakenly classified
    # returned JSON-string group IDs as unknown. The original GroupID mapping
    # is already private and cached: repair with ONE bulk call, no 200 new
    # Steam ResolveVanityURL requests. Stage until the new window succeeds.
    repaired = {}
    if int(prefilter.get("bulk_parser_version", 1)) < 2:
        old = prefilter.get("games") or {}
        unknown = {
            str(appid): record for appid, record in old.items()
            if record.get("third_party_followers") is None
            and isinstance(record.get("group_short_id"), int)
        }
        if unknown:
            lookup = list(dict.fromkeys(
                record["group_short_id"] for record in unknown.values()
            ))
            if len(lookup) > 2000:
                raise RuntimeError("Unexpected old prescreen size; manual repair required")
            for offset in range(0, len(lookup), 200):
                chunk = _bulk_counts(client, lookup[offset:offset + 200])
                for appid, record in unknown.items():
                    member_count = chunk.get(record["group_short_id"])
                    if member_count is not None:
                        repaired[appid] = {
                            **record,
                            "third_party_followers": member_count,
                            "priority": member_count >= PRIORITY_THRESHOLD,
                            "scheduling_band": "measured",
                            "checked_at": _now(),
                        }
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
        # A missing third-party record is scheduled in the below-3000
        # BACKGROUND band, per product policy. This is NOT an observed
        # member count and can never be used for official publication.
        priority = members is not None and members >= PRIORITY_THRESHOLD
        priority_count += priority
        missing_count += unknown
        staged[str(appid)] = {
            "third_party_followers": members,
            "group_short_id": group,
            "priority": priority,
            "scheduling_band": (
                "missing_assumed_under_3000" if unknown else "measured"
            ),
            "checked_at": stamp,
        }
    # Commit the window atomically in memory. Reclassify all historical
    # null entries, including older data that falsely marked them priority.
    saved = prefilter.setdefault("games", {})
    for previous in saved.values():
        if previous.get("third_party_followers") is None:
            previous["priority"] = False
            previous["scheduling_band"] = "missing_assumed_under_3000"
    saved.update(repaired)
    saved.update(staged)
    prefilter["bulk_parser_version"] = 2
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
