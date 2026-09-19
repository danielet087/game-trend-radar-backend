from __future__ import annotations

import argparse
import calendar
import json
import logging
import requests
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collectors.steam_upcoming import SteamUpcomingCollector, parse_release_window, taiwan_today, write_json
from scripts.steam_release_dates import corrected_games, fetch_store_browse_releases

LOGGER = logging.getLogger(__name__)


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else default
    except (OSError, ValueError, TypeError):
        LOGGER.warning("Could not read %s; using default state", path)
        return default


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def fresh_initial_state(today: date, *, segment_months: int, total_segments: int) -> dict[str, Any]:
    return {
        "version": 1,
        "anchor_date": today.isoformat(),
        "segment_months": segment_months,
        "total_segments": total_segments,
        "next_segment": 0,
        "completed_segments": [],
        "initial_complete": False,
    }


def release_overlaps(game: dict[str, Any], start: date, end: date) -> bool:
    raw = game.get("release_raw")
    window = parse_release_window(raw)
    return window.overlaps(start, end)


def prune_released(games: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for game in games:
        release_end = game.get("release_end")
        if not release_end:
            continue
        try:
            if date.fromisoformat(str(release_end)) < today:
                continue
        except ValueError:
            continue
        kept.append(game)
    return kept


def merge_segment(
    existing_games: list[dict[str, Any]],
    segment_games: list[dict[str, Any]],
    *,
    window_start: date,
    window_end: date,
    today: date,
) -> list[dict[str, Any]]:
    base = [
        game
        for game in prune_released(existing_games, today)
        if not release_overlaps(game, window_start, window_end)
    ]

    merged: dict[int, dict[str, Any]] = {}
    for game in base + segment_games:
        try:
            appid = int(game["appid"])
        except (KeyError, TypeError, ValueError):
            continue
        merged[appid] = game

    return sorted(
        merged.values(),
        key=lambda game: (
            -int(game.get("followers") or 0),
            str(game.get("release_start") or "9999-12-31"),
            str(game.get("name") or "").casefold(),
        ),
    )


def merge_partial_segment(
    existing_games: list[dict[str, Any]],
    newly_qualified: list[dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, Any]]:
    """Persist each 50-lookup batch without discarding older segment progress.

    An incomplete run cannot replace the entire two-month window: later
    candidates have not yet been checked. Only update an AppID when the run
    actually returned that game's Followers.
    """
    by_appid: dict[int, dict[str, Any]] = {}
    for game in prune_released(existing_games, today) + newly_qualified:
        try:
            appid = int(game["appid"])
        except (KeyError, ValueError, TypeError):
            continue
        by_appid[appid] = game
    return sorted(
        by_appid.values(),
        key=lambda game: (
            -int(game.get("followers") or 0),
            str(game.get("release_start") or "9999-12-31"),
            int(game["appid"]),
        ),
    )


def build_public_payload(
    *,
    games: list[dict[str, Any]],
    mode: str,
    state: dict[str, Any],
    latest_run: dict[str, Any],
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "generated_at": generated_at,
        "source": latest_run.get("source", {}),
        "filter": {
            "country": "TW",
            "release_horizon_days": 365,
            "release_date_timezone": "Asia/Taipei",
            "min_followers": 5000,
            "unknown_release_dates_included": False,
        },
        "initialization": {
            "mode": mode,
            "complete": bool(state.get("initial_complete")),
            "anchor_date": state.get("anchor_date"),
            "segment_months": state.get("segment_months"),
            "total_segments": state.get("total_segments"),
            "next_segment": state.get("next_segment"),
            "completed_segments": state.get("completed_segments", []),
            "last_attempt": state.get("last_attempt"),
        },
        "latest_collection": latest_run.get("collection", {}),
        "count": len(games),
        "games": games,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    today = taiwan_today()
    state_path = Path(args.state)
    master_path = Path(args.master)

    state = load_json(
        state_path,
        fresh_initial_state(
            today,
            segment_months=args.segment_months,
            total_segments=args.total_segments,
        ),
    )

    if not state.get("anchor_date"):
        state = fresh_initial_state(
            today,
            segment_months=args.segment_months,
            total_segments=args.total_segments,
        )

    anchor = date.fromisoformat(str(state["anchor_date"]))
    state["segment_months"] = int(state.get("segment_months") or args.segment_months)
    state["total_segments"] = int(state.get("total_segments") or args.total_segments)

    master = load_json(master_path, {"games": []})
    existing_games = corrected_games(master.get("games") if isinstance(master.get("games"), list) else [])

    collector = SteamUpcomingCollector(
        country=args.country,
        horizon_days=args.days,
        min_followers=args.min_followers,
        follower_request_interval=args.request_interval,
        search_request_interval=args.search_interval,
        max_pages=args.max_pages,
        follower_cache_path=args.follower_cache,
        checkpoint_path=args.checkpoint,
        checkpoint_branch=args.checkpoint_branch,
        checkpoint_every=args.checkpoint_every,
        reuse_all_cached_during_initialization=not bool(state.get("initial_complete")),
        max_fresh_requests_per_run=(
            args.max_fresh_requests if not state.get("initial_complete") else 600
        ),
    )

    if not state.get("initial_complete"):
        segment = int(state.get("next_segment") or 0)
        total_segments = int(state["total_segments"])
        segment_months = int(state["segment_months"])

        if segment >= total_segments:
            state["initial_complete"] = True
            state["next_segment"] = total_segments
            save_json(state_path, state)
            return run(args)

        window_start = add_months(anchor, segment * segment_months)
        window_end = add_months(anchor, (segment + 1) * segment_months) - timedelta(days=1)

        LOGGER.info(
            "Initial Steam segment %d/%d: %s through %s",
            segment + 1,
            total_segments,
            window_start,
            window_end,
        )

        latest = collector.collect(
            today=today,
            window_start=window_start,
            window_end=window_end,
            segment_index=segment,
            segment_anchor=anchor,
            segment_months=segment_months,
            total_segments=total_segments,
        )

        collection = latest.get("collection") or {}
        failures = int(collection.get("follower_failures") or 0)
        paused_for_budget = bool(collection.get("paused_due_to_fresh_request_budget"))
        remaining_unchecked = int(collection.get("remaining_unchecked_candidates") or 0)

        if failures > 0 or paused_for_budget:
            if paused_for_budget:
                LOGGER.info(
                    "Initial segment %d/%d paused cleanly with %d candidates remaining. "
                    "Saved follower checkpoints will be reused on the next run.",
                    segment + 1,
                    total_segments,
                    remaining_unchecked,
                )
            else:
                LOGGER.warning(
                    "Initial segment %d/%d is incomplete: %d follower lookups failed. "
                    "The segment will be retried on the next scheduled run.",
                    segment + 1,
                    total_segments,
                    failures,
                )
            combined_games = merge_partial_segment(
                existing_games, latest.get("games", []), today=today,
            )
            state["last_attempt"] = {
                "segment": segment,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "attempted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "status": "paused_budget" if paused_for_budget else "incomplete",
                "candidate_count": int(latest.get("candidate_count") or 0),
                "qualified_count_partial": int(latest.get("count") or 0),
                "published_games_total": len(combined_games),
                "fresh_requests_this_batch": int(
                    collection.get("fresh_follower_requests") or 0
                ),
                "processed_candidate_count": int(collection.get("processed_candidate_count") or 0),
                "remaining_unchecked_candidates": remaining_unchecked,
                "follower_failures": failures,
            }
        else:
            combined_games = merge_segment(
                existing_games,
                latest.get("games", []),
                window_start=window_start,
                window_end=window_end,
                today=today,
            )

            completed = list(state.get("completed_segments") or [])
            completed = [item for item in completed if int(item.get("segment", -1)) != segment]
            completed.append(
                {
                    "segment": segment,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "candidate_count": int(latest.get("candidate_count") or 0),
                    "qualified_count": int(latest.get("count") or 0),
                }
            )
            completed.sort(key=lambda item: int(item["segment"]))

            state["completed_segments"] = completed
            state["next_segment"] = segment + 1
            state["initial_complete"] = state["next_segment"] >= total_segments
            state["last_attempt"] = {
                "segment": segment,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "attempted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "status": "complete",
                "candidate_count": int(latest.get("candidate_count") or 0),
                "qualified_count": int(latest.get("count") or 0),
                "follower_failures": 0,
            }

        state["initial_batch_count"] = int(state.get("initial_batch_count") or 0) + 1
        state["last_attempt"]["initial_batch_count"] = state["initial_batch_count"]
        mode = "initializing"

    else:
        window_start = today
        window_end = today + timedelta(days=args.days)

        LOGGER.info(
            "Steam maintenance scan: %s through %s",
            window_start,
            window_end,
        )

        latest = collector.collect(
            today=today,
            window_start=window_start,
            window_end=window_end,
        )
        combined_games = merge_partial_segment(
            existing_games, latest.get("games", []), today=today,
        ) if latest.get("collection", {}).get("paused_due_to_fresh_request_budget") else list(
            latest.get("games", [])
        )
        mode = "maintenance"

    # Look up public Store Browse timestamps only for qualified games.
    # The six-hour initialization and private Follower checkpoint are untouched.
    browse_releases = fetch_store_browse_releases(
        requests.Session(),
        [int(game["appid"]) for game in combined_games],
        country=args.country,
    )
    combined_games = corrected_games(combined_games, browse_releases)

    master_payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "games": combined_games,
    }
    save_json(master_path, master_payload)
    save_json(state_path, state)

    public_payload = build_public_payload(
        games=combined_games,
        mode=mode,
        state=state,
        latest_run=latest,
    )
    public_payload["release_time_lookup"] = {
        "provider": "Steam IStoreBrowseService/GetItems",
        "country": args.country,
        "timestamps_found": len(browse_releases),
        "timezone": "Asia/Taipei",
    }
    write_json(public_payload, args.output)
    return public_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run segmented Steam initialization, then daily maintenance.")
    parser.add_argument("--output", default="output/steam_upcoming.json")
    parser.add_argument("--state", default="data/steam_initial_state.json")
    parser.add_argument("--master", default="data/steam_upcoming_master.json")
    parser.add_argument("--follower-cache", default="data/steam_followers_cache.json")
    parser.add_argument("--checkpoint", default="data/steam_followers_checkpoint.json")
    parser.add_argument("--checkpoint-branch", default="steam-state")
    parser.add_argument("--checkpoint-every", type=int, default=5)
    parser.add_argument(
        "--max-fresh-requests",
        type=int,
        default=600,
        help="During initialization, save and publish after this many fresh follower lookups; <=0 disables the cap.",
    )
    parser.add_argument("--country", default="TW")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--min-followers", type=int, default=5000)
    parser.add_argument("--request-interval", type=float, default=30.0)
    parser.add_argument("--search-interval", type=float, default=10.0)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--segment-months", type=int, default=2)
    parser.add_argument("--total-segments", type=int, default=6)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    payload = run(build_parser().parse_args())
    init = payload.get("initialization", {})
    print(
        "Steam update complete: "
        f"mode={init.get('mode')} "
        f"complete={init.get('complete')} "
        f"next_segment={init.get('next_segment')} "
        f"qualified={payload.get('count')}"
    )


if __name__ == "__main__":
    main()
