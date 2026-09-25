"""Keep only Steam Store-confirmed full-date titles in the qualified master.

IStoreQueryService release timestamps are search hints, not an announcement of
an exact release date. Current/future titles must match the newest completed
Store Browse eligibility snapshot; previously confirmed released titles remain
in the calendar history. Official follower caches/checkpoints are unrelated.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from scripts.steam_adult_exclusions import excluded_appids, is_disallowed


def filter_confirmed_master_games(
    games: list[dict[str, Any]],
    eligible_rows: list[dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, Any]]:
    """Validate qualified records without consulting or resetting Followers.

    Future games need the SAME date in today's Store-confirmed eligibility
    snapshot. A date_full marker copied from a different day cannot qualify.
    Released, previously Store-confirmed games may fall out of rolling search,
    and must remain in historical records.
    """
    blocked = excluded_appids()
    approved: dict[int, dict[str, Any]] = {}
    for source in eligible_rows:
        if not isinstance(source, dict):
            raise RuntimeError("Invalid Store-verified eligibility record")
        if (
            source.get("release_display_precision") != "date_full"
            or source.get("sexual_content_screened") is not True
            or not isinstance(source.get("release_start"), str)
        ):
            raise RuntimeError("Store eligibility snapshot is not fully verified")
        if is_disallowed(source, blocked):
            continue
        appid = int(source["appid"])
        if appid in approved:
            raise RuntimeError(f"Duplicate AppID in Store eligibility snapshot: {appid}")
        approved[appid] = source

    retained: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in games:
        if not isinstance(row, dict) or is_disallowed(row, blocked):
            continue
        try:
            appid = int(row["appid"])
            day = date.fromisoformat(row["release_start"])
            followers = int(row["followers"])
        except (KeyError, ValueError, TypeError):
            continue
        if (
            appid in seen
            or followers < 5000
            or row.get("release_display_precision") != "date_full"
        ):
            continue
        if day > today:
            source = approved.get(appid)
            if source is None or source["release_start"] != day.isoformat():
                continue
        seen.add(appid)
        retained.append(row)
    return retained
