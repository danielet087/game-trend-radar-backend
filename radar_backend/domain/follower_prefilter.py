"""Pure third-party follower parsing and official-screen queue selection."""
from __future__ import annotations

from typing import Any


GROUP_BASE = 103582791429521408
PRIORITY_THRESHOLD = 4000
STEAM_PUBLIC_THRESHOLD = 5000
DEFAULT_BATCH_SIZE = 200
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
RETRY_DELAYS = (4, 12, 30, 60)
RATE_LIMIT_DELAYS = (15, 30, 60, 120)


def parse_group_id(payload: Any, *, group_base: int = GROUP_BASE) -> int | None:
    """Resolve the short group ID without classifying an absent group as measured."""
    try:
        item = payload["response"]
        if item.get("success") != 1:
            return None
        gid = int(item["steamid"])
        return gid - group_base if gid > group_base else None
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Unexpected Steam vanity response; priority cursor unchanged") from None


def parse_bulk_counts(payload: Any, group_ids: list[int]) -> dict[int, int]:
    """Accept provider integer/string IDs and measured nonnegative integer counts."""
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("unexpected group data")
        counts: dict[int, int] = {}
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            gid, members = item.get("id"), item.get("members")
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
        if payload["data"] and not counts:
            raise ValueError("bulk data returned, but none of the IDs/member counts parsed")
        return counts
    except (ValueError, TypeError):
        raise RuntimeError(
            "Third-party bulk response malformed; priority cursor unchanged"
        ) from None


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
