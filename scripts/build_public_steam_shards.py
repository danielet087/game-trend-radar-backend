from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.steam_localized_titles import add_traditional_display_names

CORE_FIELDS = {
    "appid", "followers", "follower_checked_at",
    "release_raw", "release_start", "release_end", "release_precision",
    "release_date_timezone", "release_date_basis", "release_time_utc",
    "release_time_source", "store_url", "community_url", "discovered_by",
    "sexual_content_screened", "release_display_precision",
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_if_changed(path: Path, payload: Any) -> bool:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def valid_record(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        appid = int(row.get("appid"))
        followers = int(row.get("followers"))
    except (TypeError, ValueError):
        return False
    day = row.get("release_start") or row.get("release_date")
    return (
        appid > 0
        and followers >= 3000
        and isinstance(day, str)
        and len(day) == 10
        and row.get("release_precision", "day") == "day"
    )


def merge_game(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Preserve rich presentation metadata but trust backend core fields."""
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None or value == "":
            continue
        if isinstance(value, list) and not value:
            continue
        if key in CORE_FIELDS or key not in merged or merged.get(key) in (None, "", []):
            merged[key] = value
    # Names/assets supplied by the backend are also allowed to refresh when present.
    for key in ("name", "name_en", "name_zh_tw", "name_zh_cn", "capsule_image"):
        value = incoming.get(key)
        if value not in (None, ""):
            merged[key] = value
    merged["appid"] = int(incoming.get("appid", merged.get("appid")))
    # Preserve Steam's original localized names and create separate
    # Traditional-script presentation fields for every future shard update.
    add_traditional_display_names(merged)
    merged["storage_version"] = 2
    return merged


def build(input_path: Path, frontend: Path) -> dict[str, Any]:
    incoming_payload = load_json(input_path, {"games": []})
    incoming_games = incoming_payload.get("games") or []
    games_dir = frontend / "data" / "games"
    calendar_dir = frontend / "data" / "calendar"
    lists_dir = frontend / "data" / "lists"

    existing: dict[int, dict[str, Any]] = {}
    if games_dir.exists():
        for path in games_dir.glob("*.json"):
            row = load_json(path, None)
            if valid_record(row):
                existing[int(row["appid"])] = row

    changed_games = 0
    for row in incoming_games:
        if not valid_record(row):
            continue
        appid = int(row["appid"])
        merged = merge_game(existing.get(appid, {}), row)
        existing[appid] = merged
        if write_if_changed(games_dir / f"{appid}.json", merged):
            changed_games += 1

    rows = sorted(
        existing.values(),
        key=lambda g: (
            str(g.get("release_start") or g.get("release_date") or "9999-12-31"),
            -int(g.get("followers") or 0),
            int(g["appid"]),
        ),
    )
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = str(row.get("release_start") or row.get("release_date"))
        by_month[day[:7]].append(row)

    generated_at = incoming_payload.get("generated_at") or datetime.now(timezone.utc).isoformat()
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
    if calendar_dir.exists():
        active = set(by_month)
        for path in calendar_dir.glob("????-??.json"):
            if path.stem not in active:
                path.unlink()

    today = date.today()
    # GitHub runner is UTC; convert operational "today" explicitly to Taiwan.
    today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    today_s = today.isoformat()
    released_from = (today - timedelta(days=30)).isoformat()

    upcoming = [
        int(row["appid"]) for row in rows
        if str(row.get("release_start") or row.get("release_date")) >= today_s
        and int(row.get("followers") or 0) >= 5000
    ]
    released = [
        int(row["appid"]) for row in rows
        if released_from <= str(row.get("release_start") or row.get("release_date")) < today_s
        and (
            int(row.get("followers") or 0) >= 5000
            or (
                int(row.get("followers") or 0) > 3000
                and row.get("recent_source") in {"tracked_release", "direct_release"}
            )
        )
    ]

    write_if_changed(
        lists_dir / "upcoming.json",
        {"version": 2, "generated_at": generated_at, "count": len(upcoming), "appids": upcoming},
    )
    write_if_changed(
        lists_dir / "released.json",
        {"version": 2, "generated_at": generated_at, "count": len(released), "appids": released},
    )
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
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="output/steam_upcoming.json")
    parser.add_argument("--frontend", default="frontend")
    args = parser.parse_args()
    result = build(Path(args.input), Path(args.frontend))
    print("STEAM_SHARDS", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
