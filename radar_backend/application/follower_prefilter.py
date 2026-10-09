"""Stage a complete follower-screen window before mutating caller-owned state."""
from __future__ import annotations

from typing import Any, Callable

from radar_backend.domain.follower_prefilter import DEFAULT_BATCH_SIZE, PRIORITY_THRESHOLD


def scan_batch(
    catalog: list[dict[str, Any]],
    prefilter: dict[str, Any],
    *,
    steam_api_key: str,
    initial_index: int,
    limit: int = DEFAULT_BATCH_SIZE,
    request_interval: float = 0.5,
    session: Any = None,
    session_factory: Callable,
    group_id: Callable,
    bulk_counts: Callable,
    clock_stamp: Callable,
    sleep: Callable,
    priority_threshold: int = PRIORITY_THRESHOLD,
) -> dict[str, int | bool]:
    """Record one entire new window only after mapping and bulk succeed."""
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

    client = session or session_factory()
    client.headers.setdefault("User-Agent", "GameTrendRadarThirdPartyPriority/1.0")
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
                chunk = bulk_counts(client, lookup[offset:offset + 200])
                for appid, record in unknown.items():
                    member_count = chunk.get(record["group_short_id"])
                    if member_count is not None:
                        repaired[appid] = {
                            **record,
                            "third_party_followers": member_count,
                            "priority": member_count >= priority_threshold,
                            "scheduling_band": "measured",
                            "checked_at": clock_stamp(),
                        }
    groups: dict[int, int | None] = {}
    for idx, row in enumerate(window):
        if idx:
            sleep(request_interval)
        groups[int(row["appid"])] = group_id(client, steam_api_key, int(row["appid"]))
    short_ids = list(dict.fromkeys(g for g in groups.values() if g is not None))
    counts = bulk_counts(client, short_ids)
    stamp = clock_stamp()
    staged = {}
    priority_count = 0
    missing_count = 0
    for appid, group in groups.items():
        members = counts.get(group) if group is not None else None
        unknown = members is None
        priority = members is not None and members >= priority_threshold
        priority_count += priority
        missing_count += unknown
        staged[str(appid)] = {
            "third_party_followers": members,
            "group_short_id": group,
            "priority": priority,
            "scheduling_band": "unresolved" if unknown else "measured",
            "checked_at": stamp,
        }
    # Requests and repair complete before the first saved-window mutation.
    saved = prefilter.setdefault("games", {})
    for previous in saved.values():
        if previous.get("third_party_followers") is None:
            previous["priority"] = False
            previous["scheduling_band"] = "unresolved"
    saved.update(repaired)
    saved.update(staged)
    prefilter["bulk_parser_version"] = 2
    prefilter["version"] = 1
    prefilter["threshold"] = priority_threshold
    prefilter["next_index"] = stop
    prefilter["complete"] = stop >= len(catalog)
    prefilter["updated_at"] = stamp
    return {
        "start_index": start, "next_index": stop,
        "screened": len(window), "priority": priority_count,
        "missing": missing_count, "complete": stop >= len(catalog),
    }
