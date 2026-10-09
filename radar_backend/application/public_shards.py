"""AppID shard publication coordinated through explicit external ports."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from radar_backend.domain import public_shards as rules


def build(
    input_path: Path,
    frontend: Path,
    *,
    authoritative_future: bool = False,
    now: datetime | None = None,
    clock: Callable[[], datetime],
    load_json: Callable[[Path, Any], Any],
    write_if_changed: Callable[[Path, Any], bool],
    exists: Callable[[Path], bool],
    glob: Callable[[Path, str], Any],
    unlink: Callable[[Path], Any],
    excluded_appids: Callable[[], set[int]],
    is_disallowed: Callable[[Any, set[int]], bool],
    is_twitch_qualified: Callable[[Any], bool],
    valid_record: Callable[[Any], bool],
    merge_game: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    add_traditional_display_names: Callable[[dict[str, Any]], Any],
    write_catalog_projection: Callable[[Path, list[dict[str, Any]], Any], dict[str, Any]],
    timezone_type: Any = timezone,
    timedelta_type: Any = timedelta,
) -> dict[str, Any]:
    """Publish shards using explicit state, qualification, and presentation ports.

    The ports retain the existing publication order and failure behavior. This
    use case performs no filesystem operations itself.
    """
    observed = now or clock()
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Timezone-aware publication time required")
    incoming_payload = load_json(input_path, None)
    if not isinstance(incoming_payload, dict) or not isinstance(incoming_payload.get("games"), list):
        raise ValueError("Invalid source catalog; refusing to replace published records")
    incoming_games = incoming_payload["games"]
    if authoritative_future and not incoming_games:
        raise ValueError("Empty authoritative catalog; refusing mass removal")
    incoming_ids = {
        int(row["appid"]) for row in incoming_games
        if isinstance(row, dict) and row.get("appid") is not None
    }
    games_dir = frontend / "data" / "games"
    calendar_dir = frontend / "data" / "calendar"
    lists_dir = frontend / "data" / "lists"

    blocked = excluded_appids()
    index = load_json(frontend / "data" / "index.json", {})
    audit_active = index.get("release_date_audited") is True or authoritative_future
    precision_exclusions = load_json(frontend / "data" / "excluded_date_appids.json", {})
    unconfirmed_ids = {int(x) for x in precision_exclusions.get("appids", [])}
    today_s = observed.astimezone(timezone_type(timedelta_type(hours=8))).date().isoformat()

    def publishable(game: dict[str, Any]) -> bool:
        return rules.publishable(
            game,
            today_s=today_s,
            blocked=blocked,
            unconfirmed_ids=unconfirmed_ids,
            audit_active=audit_active,
            valid_record=valid_record,
            is_disallowed=is_disallowed,
            is_twitch_qualified=is_twitch_qualified,
        )

    existing: dict[int, dict[str, Any]] = {}
    if exists(games_dir):
        for path in glob(games_dir, "*.json"):
            row = load_json(path, None)
            if isinstance(row, dict) and publishable(row):
                existing[int(row["appid"])] = row

    # Removed/unconfirmed titles must not survive as stale independent
    # AppID files after an audit. Historical released records remain.
    removed_stale_future = 0
    for path in glob(games_dir, "*.json"):
        try:
            appid = int(path.stem)
            row = load_json(path, {})
            stale_future = rules.stale_future(
                row,
                appid,
                authoritative_future=authoritative_future,
                today_s=today_s,
                incoming_ids=incoming_ids,
                is_twitch_qualified=is_twitch_qualified,
            )
            if appid in blocked or (
                appid in unconfirmed_ids
                and row.get("release_display_precision") != "date_full"
            ) or stale_future:
                existing.pop(appid, None)
                unlink(path)
                if stale_future:
                    removed_stale_future += 1
        except ValueError:
            continue

    changed_games = 0
    for row in incoming_games:
        if not valid_record(row) or is_disallowed(row, blocked):
            continue
        appid = int(row["appid"])
        if appid in unconfirmed_ids and row.get("release_display_precision") != "date_full":
            continue
        prior = existing.get(appid, {})
        merged = merge_game(prior, row)
        # An outdated master Query timestamp must not overwrite a verified
        # date (or silently borrow proof from a different release date).
        rules.preserve_verified_release(prior, row, merged)
        if not publishable(merged):
            continue
        existing[appid] = merged
        if write_if_changed(games_dir / f"{appid}.json", merged):
            changed_games += 1

    # Also backfill records that were not present in today's rolling
    # Followers output. Released games must retain their localized names.
    for appid, game in existing.items():
        previous = dict(game)
        add_traditional_display_names(game)
        game["storage_version"] = 2
        if game != previous:
            if write_if_changed(games_dir / f"{appid}.json", game):
                changed_games += 1

    rows = rules.sorted_rows(existing)
    by_month = rules.rows_by_month(rows)

    generated_at = incoming_payload.get("generated_at") or observed.isoformat()
    changed_months = 0
    for month, month_rows in sorted(by_month.items()):
        payload = {
            "version": 2,
            "generated_at": generated_at,
            "month": month,
            "count": len(month_rows),
            "games": month_rows,
        }
        if write_if_changed(calendar_dir / f"{month}.json", payload):
            changed_months += 1

    # Remove obsolete month files only when no retained game belongs to that month.
    if exists(calendar_dir):
        active = set(by_month)
        for path in glob(calendar_dir, "????-??.json"):
            if path.stem not in active:
                unlink(path)

    # Keep one Taiwan eligibility date across publication retries.
    today = observed.astimezone(timezone_type(timedelta_type(hours=8))).date()
    today_s = today.isoformat()
    released_from = (today - timedelta_type(days=30)).isoformat()

    upcoming = rules.upcoming_appids(
        rows, today_s=today_s, is_twitch_qualified=is_twitch_qualified,
    )
    released = rules.released_appids(
        rows,
        today_s=today_s,
        released_from=released_from,
        is_twitch_qualified=is_twitch_qualified,
    )

    write_if_changed(
        lists_dir / "upcoming.json",
        {"version": 2, "generated_at": generated_at, "count": len(upcoming), "appids": upcoming},
    )
    write_if_changed(
        lists_dir / "released.json",
        {"version": 2, "generated_at": generated_at, "count": len(released), "appids": released},
    )
    # Keep the legacy fallback authoritative too; otherwise a stale fallback
    # can re-expose games that were removed from the sharded catalog.
    write_if_changed(
        frontend / "data" / "steam_upcoming.json",
        {
            "version": 2,
            "generated_at": generated_at,
            "count": len(rows),
            "games": rows,
        },
    )
    projection = write_catalog_projection(frontend / "data", rows, generated_at)
    write_if_changed(
        frontend / "data" / "index.json",
        {
            "version": 2,
            "generated_at": generated_at,
            "source": "Steam AppID-sharded public catalog",
            "game_count": len(rows),
            "months": sorted(by_month),
            "calendar_path": "calendar/{YYYY-MM}.json",
            "game_path": "games/{appid}.json",
            "lists": {
                "upcoming": "lists/upcoming.json",
                "released": "lists/released.json",
            },
            "legacy_fallback": "steam_upcoming.json",
            "release_date_audited": audit_active,
            **projection,
        },
    )
    return {
        "games": len(rows),
        "incoming": len(incoming_games),
        "changed_games": changed_games,
        "months": len(by_month),
        "changed_months": changed_months,
        "upcoming": len(upcoming),
        "released": len(released),
        "removed_stale_future": removed_stale_future,
    }
