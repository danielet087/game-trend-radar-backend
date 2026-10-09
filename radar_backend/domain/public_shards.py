"""Pure eligibility, merge, and list rules for the AppID public shards."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Callable

from radar_backend.domain.catalog_metadata import PLAYER_CATEGORY_FIELDS


CORE_FIELDS = {
    "appid", "followers", "follower_checked_at",
    "release_raw", "release_start", "release_end", "release_precision",
    "release_date_timezone", "release_date_basis", "release_time_utc",
    "release_time_source", "store_url", "community_url", "discovered_by",
    "sexual_content_screened", "release_display_precision", "release_display_provider",
    "release_date_verified_at", "post_followers_store_verified_at", "post_followers_store_verified",
    "release_date_conflict", "release_timestamp_taipei_date",
    "release_store_date", "release_date_normalization",
    "twitch_admission", "steam_type", "content_descriptorids",
}


def valid_record(
    row: Any, *, date_type=date, is_twitch_qualified: Callable,
) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        appid = int(row.get("appid"))
        followers = int(row.get("followers"))
    except (TypeError, ValueError):
        return False
    day = row.get("release_start") or row.get("release_date")
    try:
        if date_type.fromisoformat(day).isoformat() != day:
            return False
    except (ValueError, TypeError):
        return False
    return (
        appid > 0
        and (followers >= 3000 or is_twitch_qualified(row))
        and isinstance(day, str)
        and len(day) == 10
        and row.get("release_precision", "day") == "day"
    )


def merge_game(
    existing: dict[str, Any], incoming: dict[str, Any], *,
    keep_newer_release: Callable, preserve_twitch_admission: Callable,
    preserve_player_categories: Callable, add_traditional_display_names: Callable,
    core_fields=CORE_FIELDS, player_category_fields=PLAYER_CATEGORY_FIELDS,
) -> dict[str, Any]:
    """Preserve rich presentation metadata but trust backend core fields."""
    incoming = keep_newer_release(existing, incoming)
    incoming = preserve_twitch_admission(existing, incoming)
    incoming = preserve_player_categories(existing, incoming)
    merged = dict(existing)
    for key, value in incoming.items():
        if key in player_category_fields:
            continue
        if value is None or value == "":
            continue
        if isinstance(value, list) and not value:
            continue
        if key in core_fields or key not in merged or merged.get(key) in (None, "", []):
            merged[key] = value
    for key in ("name", "name_en", "name_zh_tw", "name_zh_cn", "capsule_image"):
        value = incoming.get(key)
        if value not in (None, ""):
            merged[key] = value
    for key in player_category_fields:
        if key in incoming:
            merged[key] = incoming[key]
        else:
            merged.pop(key, None)
    merged["appid"] = int(incoming.get("appid", merged.get("appid")))
    add_traditional_display_names(merged)
    merged["storage_version"] = 2
    return merged


def publishable(
    game: dict[str, Any], *, today_s: str, blocked, unconfirmed_ids,
    audit_active: bool, valid_record: Callable, is_disallowed: Callable,
    is_twitch_qualified: Callable,
) -> bool:
    if not valid_record(game) or is_disallowed(game, blocked):
        return False
    if (
        int(game["appid"]) in unconfirmed_ids
        and game.get("release_display_precision") != "date_full"
    ):
        return False
    if str(game["release_start"]) >= today_s:
        if int(game["followers"]) < 5000 and not is_twitch_qualified(game):
            return False
        if audit_active:
            return game.get("release_display_precision") == "date_full"
    return True


def stale_future(
    row: dict[str, Any], appid: int, *, authoritative_future: bool,
    today_s: str, incoming_ids, is_twitch_qualified: Callable,
) -> bool:
    return (
        authoritative_future
        and str(row.get("release_start") or "") >= today_s
        and appid not in incoming_ids
        and not is_twitch_qualified(row)
    )


def preserve_verified_release(
    prior: dict[str, Any], row: dict[str, Any], merged: dict[str, Any],
) -> None:
    """Retain prior verified display-date proof after a Query-only refresh."""
    if prior.get("release_date_verified_at") and row.get("release_display_precision") != "date_full":
        if row.get("release_start") != prior.get("release_start"):
            merged["release_start"] = prior["release_start"]
            merged["release_end"] = prior.get("release_end", prior["release_start"])
            merged["release_raw"] = prior.get("release_raw", prior["release_start"])
        merged["release_display_precision"] = prior.get("release_display_precision")
        merged["release_date_verified_at"] = prior["release_date_verified_at"]


def sorted_rows(existing: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        existing.values(),
        key=lambda g: (
            str(g.get("release_start") or g.get("release_date") or "9999-12-31"),
            -int(g.get("followers") or 0),
            int(g["appid"]),
        ),
    )


def rows_by_month(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = str(row.get("release_start") or row.get("release_date"))
        by_month[day[:7]].append(row)
    return by_month


def upcoming_appids(
    rows: list[dict[str, Any]], *, today_s: str, is_twitch_qualified: Callable,
) -> list[int]:
    return [
        int(row["appid"]) for row in rows
        if str(row.get("release_start") or row.get("release_date")) >= today_s
        and (int(row.get("followers") or 0) >= 5000 or is_twitch_qualified(row))
    ]


def released_appids(
    rows: list[dict[str, Any]], *, today_s: str, released_from: str,
    is_twitch_qualified: Callable,
) -> list[int]:
    return [
        int(row["appid"]) for row in rows
        if released_from <= str(row.get("release_start") or row.get("release_date")) < today_s
        and (
            int(row.get("followers") or 0) >= 5000
            or is_twitch_qualified(row)
            or (
                int(row.get("followers") or 0) > 3000
                and row.get("recent_source") in {"tracked_release", "direct_release"}
            )
        )
    ]
