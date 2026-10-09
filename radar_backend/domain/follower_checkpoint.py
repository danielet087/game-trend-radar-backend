"""Timestamp rules for the legacy follower checkpoint, without I/O."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable


class CheckpointConflict(RuntimeError):
    """The checkpoint's expected revision no longer matches the saved one."""
    status = 409

    def __init__(self):
        super().__init__("HTTP 409")


def parse_checked_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def merge_follower_records(
    *sources: dict[str, dict[str, Any]],
    parse_timestamp: Callable = parse_checked_at,
) -> dict[str, dict[str, Any]]:
    """Keep the newest observation; equal/unknown times keep the first source.

    Remote publication passes the latest remote records first, so an uncertain
    tie cannot replace evidence another writer already saved. Unknown record
    fields travel with their observation rather than being reconstructed.
    """
    merged: dict[str, dict[str, Any]] = {}
    for source in sources:
        for appid, entry in source.items():
            if not isinstance(entry, dict):
                continue
            key = str(appid)
            current = merged.get(key)
            if current is None:
                merged[key] = deepcopy(entry)
                continue
            incoming_time = parse_timestamp(str(entry.get("checked_at") or ""))
            current_time = parse_timestamp(str(current.get("checked_at") or ""))
            if incoming_time is not None and (
                current_time is None or incoming_time > current_time
            ):
                merged[key] = deepcopy(entry)
    return merged
