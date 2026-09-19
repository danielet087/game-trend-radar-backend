"""One Taiwanese release-calendar day per GitHub run, led by ChatGPT's hourly task.

This is a separate state machine from the old two-month initializer. It uses the
same private master, follower cache, and remote steam-state checkpoint; it does
not reinitialize those resources or claim Steam search is an exhaustive index.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from collectors.steam_upcoming import SteamUpcomingCollector, taiwan_today, write_json
from scripts.steam_release_dates import corrected_games, fetch_store_browse_releases
from scripts.update_steam_daily import (
    build_public_payload, load_json, merge_partial_segment, save_json,
)

LOGGER = logging.getLogger(__name__)


def fresh_day_state(today: date, horizon_days: int) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "daily_release_date",
        "anchor_date": today.isoformat(),
        "end_date": (today + timedelta(days=horizon_days - 1)).isoformat(),
        "next_date": today.isoformat(),
        "scanned_dates_count": 0,
        "dates_without_candidates": [],
        "initial_complete": False,
        "coverage_exhaustive": False,
        "last_attempt": None,
    }


def advance_date(state: dict[str, Any], current: date) -> None:
    next_date = current + timedelta(days=1)
    state["next_date"] = next_date.isoformat()
    state["scanned_dates_count"] = int(state.get("scanned_dates_count") or 0) + 1
    state["initial_complete"] = next_date > date.fromisoformat(state["end_date"])


def run_day(args: argparse.Namespace) -> dict[str, Any]:
    today = taiwan_today()
    path = Path(args.state)
    state = load_json(path, fresh_day_state(today, args.days))
    if state.get("mode") != "daily_release_date":
        raise ValueError("Expected the separate daily-release-date state; legacy state was not changed")

    current = date.fromisoformat(state["next_date"])
    end = date.fromisoformat(state["end_date"])
    master_path = Path(args.master)
    master = load_json(master_path, {"games": []})
    existing = corrected_games(
        master["games"] if isinstance(master.get("games"), list) else []
    )

    if current > end:
        state["initial_complete"] = True
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        result = {
            "generated_at": now,
            "source": {"catalog": "Steam Store searched in Taiwan release-day windows"},
            "filter": {"country": args.country, "release_horizon_days": args.days,
                       "min_followers": args.min_followers,
                       "release_date_timezone": "Asia/Taipei"},
            "initialization": state,
            "latest_collection": {},
            "count": len(existing),
            "games": existing,
        }
        save_json(path, state)
        write_json(result, args.output)
        return result

    LOGGER.info("Steam Taiwan release day %s / through %s", current, end)
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
        reuse_all_cached_during_initialization=True,
        max_fresh_requests_per_run=args.max_fresh_requests,
    )
    latest = collector.collect(
        today=today, window_start=current, window_end=current,
    )
    collection = latest.get("collection") or {}
    failures = int(collection.get("follower_failures") or 0)
    paused = bool(collection.get("paused_due_to_fresh_request_budget"))
    candidates = int(latest.get("candidate_count") or 0)
    combined = merge_partial_segment(
        existing, latest.get("games", []), today=today,
    )
    if candidates == 0:
        status = "no_candidates_discovered"
        empty = state.setdefault("dates_without_candidates", [])
        if current.isoformat() not in empty:
            empty.append(current.isoformat())
        # Search with multiple sorts is still not a completeness guarantee.
        # Advance a day, but surface this uncertainty in both state and output.
    elif failures:
        status = "follower_incomplete"
    elif paused:
        status = "paused_after_50"
    else:
        status = "date_scanned"

    if status in {"no_candidates_discovered", "date_scanned"}:
        advance_date(state, current)

    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    state["last_attempt"] = {
        "release_date": current.isoformat(),
        "status": status,
        "attempted_at": stamp,
        "candidate_count": candidates,
        "fresh_follower_requests": int(collection.get("fresh_follower_requests") or 0),
        "cached_follower_reuses": int(collection.get("cached_follower_reuses") or 0),
        "qualified_count": int(latest.get("count") or 0),
        "follower_failures": failures,
        "remaining_unchecked_candidates": int(collection.get("remaining_unchecked_candidates") or 0),
    }
    # Public dates already parsed from search rows are enriched with
    # Steam's actual scheduled release timestamp when it is available.
    browse = fetch_store_browse_releases(
        requests.Session(), [int(row["appid"]) for row in combined],
        country=args.country,
    )
    combined = corrected_games(combined, browse)
    save_json(master_path, {"version": 1, "updated_at": stamp, "games": combined})
    save_json(path, state)
    public = build_public_payload(
        games=combined, mode="daily_release_date", state=state,
        latest_run=latest,
    )
    public["initialization"] = {
        "mode": "daily_release_date",
        "complete": state["initial_complete"],
        "anchor_date": state["anchor_date"],
        "end_date": state["end_date"],
        "next_date": state["next_date"],
        "scanned_dates_count": state["scanned_dates_count"],
        "dates_without_candidates": state["dates_without_candidates"],
        "coverage_exhaustive": False,
        "last_attempt": state["last_attempt"],
    }
    public["release_time_lookup"] = {
        "provider": "Steam IStoreBrowseService/GetItems",
        "country": args.country,
        "timestamps_found": len(browse),
        "timezone": "Asia/Taipei",
    }
    write_json(public, args.output)
    LOGGER.info(
        "Steam daily-date progress: %s; next=%s; %d release dates scanned; "
        "%d newly checked Followers; %d qualified games total",
        status, state["next_date"], state["scanned_dates_count"],
        state["last_attempt"]["fresh_follower_requests"], len(combined),
    )
    return public


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="One release day per 50-Followers Steam batch.")
    p.add_argument("--state", default="data/steam_day_state.json")
    p.add_argument("--master", default="data/steam_upcoming_master.json")
    p.add_argument("--output", default="output/steam_upcoming.json")
    p.add_argument("--follower-cache", default="data/steam_followers_cache.json")
    p.add_argument("--checkpoint", default="data/steam_followers_checkpoint.json")
    p.add_argument("--checkpoint-branch", default="steam-state")
    p.add_argument("--checkpoint-every", type=int, default=5)
    p.add_argument("--max-fresh-requests", type=int, default=50)
    p.add_argument("--country", default="TW")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--min-followers", type=int, default=5000)
    p.add_argument("--request-interval", type=float, default=30.0)
    p.add_argument("--search-interval", type=float, default=10.0)
    p.add_argument("--max-pages", type=int, default=100)
    return p


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    run_day(build_parser().parse_args())
