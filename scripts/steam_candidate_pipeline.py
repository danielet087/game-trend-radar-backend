"""Steam year initialization: discover exact TW release days BEFORE querying Followers.

Phase 1 visits 365 Taiwan calendar-day intervals through the authenticated Steam
IStoreQueryService (release_date_type=1), saves a private candidate catalog and
resume cursor. Phase 2 queries up to 50 NEW Followers per independent GH run.
Neither phase overwrites the legacy initialization state or Steam checkpoint.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from collectors.steam_upcoming import (
    SteamUpcomingCollector, UpcomingGame, TAIWAN_TZ, taiwan_today, write_json,
)
from scripts.update_steam_daily import load_json, merge_partial_segment, save_json
from scripts.steam_follower_prefilter import (
    PRIORITY_THRESHOLD, pending_priorities, scan_batch,
)

LOG = logging.getLogger(__name__)
QUERY_URL = "https://api.steampowered.com/IStoreQueryService/Query/v1/"
PAGE_SIZE = 1000
MAX_PAGES_PER_DAY = 30
QUERY_INTERVAL_SECONDS = 1.5


def fresh_state(anchor: date, days: int) -> dict[str, Any]:
    return {
        "version": 1, "mode": "two_phase_steam_year",
        "phase": "discovery", "anchor_date": anchor.isoformat(),
        "end_date": (anchor + timedelta(days=days - 1)).isoformat(),
        "next_date": anchor.isoformat(), "days_scanned": 0,
        "next_follower_index": 0, "initial_complete": False,
        "coverage_exhaustive": False, "last_attempt": None,
    }


def candidate_record(item: dict[str, Any], expected_day: date) -> dict[str, Any] | None:
    if not isinstance(item, dict) or not isinstance(item.get("appid"), int):
        return None
    release = item.get("release") or {}
    if not isinstance(release, dict) or release.get("is_coming_soon") is not True:
        return None
    stamp = release.get("steam_release_date")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
        return None
    try:
        instant = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    tw_day = instant.astimezone(TAIWAN_TZ).date()
    if tw_day != expected_day:
        return None
    appid = item["appid"]
    name = str(item.get("name") or f"Steam App {appid}").strip()
    assets = item.get("assets") or {}
    if not isinstance(assets, dict):
        assets = {}
    # Store asset keys change over time. The public Steam capsule URL is a
    # fallback and a broken image is already handled in the frontend.
    capsule = (
        assets.get("small_capsule")
        or assets.get("capsule")
        or assets.get("header")
        or f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/capsule_231x87.jpg"
    )
    return {
        "appid": appid, "name": name, "name_en": name, "name_zh_tw": None,
        "release_raw": tw_day.isoformat(),
        "release_start": tw_day.isoformat(), "release_end": tw_day.isoformat(),
        "release_precision": "day", "release_date_timezone": "Asia/Taipei",
        "release_date_basis": "steam_store_query_release_time",
        "release_time_utc": instant.isoformat().replace("+00:00", "Z"),
        "release_time_source": QUERY_URL,
        "capsule_image": str(capsule), "store_url": f"https://store.steampowered.com/app/{appid}/",
        "discovered_by": "IStoreQueryService/Query",
    }


def query_one_day(
    session: requests.Session, api_key: str, day: date, *,
    request_interval: float = QUERY_INTERVAL_SECONDS,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES_PER_DAY,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read every paginated result for one Taiwan release day.

    Fail closed on unexpected pagination, missing metadata, or a rate limit.
    Never mark a day scanned merely because a page yielded no usable titles.
    """
    utc_start = datetime(day.year, day.month, day.day, tzinfo=TAIWAN_TZ).astimezone(timezone.utc)
    utc_end = utc_start + timedelta(days=1)
    by_id: dict[int, dict[str, Any]] = {}
    skipped_no_precise_time = 0
    reported_total: int | None = None
    offset = 0
    pages = 0
    for _ in range(max_pages):
        query = {
            "query": {
                "start": offset, "count": page_size,
                "filters": {
                    "coming_soon_only": True,
                    "type_filters": {"include_games": True},
                    "release_date_filter": {
                        "release_date_type": 1,
                        "start_date": int(utc_start.timestamp()),
                        "end_date": int(utc_end.timestamp()),
                    },
                },
            },
            "context": {"country_code": "TW", "language": "english"},
            "data_request": {
                "include_basic_info": True, "include_release": True, "include_assets": True,
            },
        }
        # Never print the API key or URL containing it.
        response = session.get(
            QUERY_URL,
            params={"key": api_key, "input_json": json.dumps(query, separators=(",", ":"))},
            timeout=35,
        )
        response.raise_for_status()
        data = response.json()
        result = data.get("response")
        if not isinstance(result, dict):
            raise RuntimeError("Steam Query has no response; day not verified")
        metadata = result.get("metadata") or {}
        if not isinstance(metadata.get("total_matching_records"), int):
            raise RuntimeError("Steam Query has no total_matching_records; cannot verify pagination")
        count = metadata["total_matching_records"]
        if reported_total is None:
            reported_total = count
        elif count != reported_total:
            raise RuntimeError(f"Steam Query changed matching total within {day}; retry later")
        rows = result.get("store_items") or []
        if count > offset and not rows:
            raise RuntimeError(f"Steam Query returned an incomplete page at {day}, offset {offset}")
        for item in rows:
            game = candidate_record(item, day)
            if game is None:
                skipped_no_precise_time += 1
            else:
                by_id[game["appid"]] = game
        offset += len(rows)
        pages += 1
        if offset >= count:
            return (
                sorted(by_id.values(), key=lambda x: x["appid"]),
                {"day": day.isoformat(), "api_matches": count,
                 "candidates": len(by_id),
                 "unverified_rows": skipped_no_precise_time,
                 "pages": pages},
            )
        if len(rows) > page_size or len(rows) == 0:
            raise RuntimeError(f"Invalid Steam Query pagination for {day}")
        if request_interval:
            time.sleep(request_interval)
    raise RuntimeError(f"Steam Query page ceiling reached for {day} ({reported_total} matches)")


def run_discovery(args: argparse.Namespace, state: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("STEAM_WEB_API_KEY is required for verified day-range discovery")
    start = date.fromisoformat(state["next_date"])
    end = date.fromisoformat(state["end_date"])
    session = requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadar/0.8"})
    games_by_id = {str(x["appid"]): x for x in catalog.get("games", [])}
    summaries = []
    attempts = 0
    while start <= end and attempts < args.batch_days:
        games, info = query_one_day(session, key, start, request_interval=args.search_interval)
        for game in games:
            games_by_id[str(game["appid"])] = game
        summaries.append(info)
        state["next_date"] = (start + timedelta(days=1)).isoformat()
        state["days_scanned"] = int(state.get("days_scanned") or 0) + 1
        start += timedelta(days=1)
        attempts += 1
        LOG.info("Discovery TW %s: %s matches, %s candidates; %s/365 days",
                 info["day"], info["api_matches"], info["candidates"], state["days_scanned"])
        if attempts < args.batch_days and start <= end:
            time.sleep(args.search_interval)
    state["last_attempt"] = {
        "phase": "discovery", "days": summaries,
        "new_catalog_total": len(games_by_id), "followers_queried": 0,
    }
    if start > end:
        state["phase"] = "followers"
        state["discovery_finished_at"] = datetime.now(timezone.utc).isoformat()
    catalog["games"] = sorted(
        games_by_id.values(), key=lambda x: (x["release_start"], x["appid"])
    )
    catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    catalog["count"] = len(catalog["games"])
    return {"phase": state["phase"], "days_processed": attempts,
            "days_scanned": state["days_scanned"],
            "candidate_count": catalog["count"], "followers_queried": 0}


def run_follower_batch(
    args: argparse.Namespace,
    state: dict[str, Any],
    catalog: dict[str, Any],
    master: dict[str, Any],
) -> dict[str, Any]:
    rows = catalog.get("games", [])
    cursor = int(state.get("next_follower_index") or 0)
    if cursor >= len(rows):
        state["phase"] = "complete"
        state["initial_complete"] = True
        state["last_attempt"] = {"phase": "complete", "candidate_count": len(rows)}
        return {"phase": "complete", "followers_queried": 0}
    today = taiwan_today()
    collector = SteamUpcomingCollector(
        country="TW", horizon_days=365, min_followers=5000,
        follower_request_interval=args.request_interval,
        search_request_interval=args.search_interval,
        follower_cache_path=args.follower_cache, checkpoint_path=args.checkpoint,
        checkpoint_branch=args.checkpoint_branch, checkpoint_every=5,
        reuse_all_cached_during_initialization=True,
        max_fresh_requests_per_run=int(getattr(args, "max_fresh_requests_per_run", 50)),
    )

    def as_game(raw: dict[str, Any]) -> UpcomingGame:
        return UpcomingGame(
            appid=raw["appid"], name=raw["name"], release_raw=raw["release_raw"],
            release_start=raw["release_start"], release_end=raw["release_end"],
            release_precision="day", capsule_image=raw.get("capsule_image"),
            store_url=raw["store_url"],
        )

    prefilter_enabled = int(getattr(args, "prefilter_batch_size", 0) or 0) > 0
    prefilter: dict[str, Any] = {}
    screen = {"start_index": cursor, "next_index": cursor,
              "screened": 0, "priority": 0, "missing": 0, "complete": True}
    qualified = []
    budget = int(getattr(args, "max_fresh_requests_per_run", 50))
    if not 1 <= budget <= 50:
        raise ValueError("Official XML requests per batch must be within 1..50")
    priority_fresh_requests = 0
    priority_remaining = 0
    sequential_processed = 0
    if prefilter_enabled:
        prefilter = load_json(
            Path(args.prefilter_state),
            {"version": 1, "next_index": cursor, "complete": False, "games": {}},
        )
        if int(prefilter.get("next_index", cursor)) < len(rows):
            screen = scan_batch(
                rows, prefilter, steam_api_key=os.environ.get("STEAM_WEB_API_KEY", "").strip(),
                initial_index=cursor, limit=args.prefilter_batch_size,
                request_interval=getattr(args, "prefilter_request_interval", 0.5),
            )
        else:
            prefilter["complete"] = True
            screen["start_index"] = int(prefilter.get("next_index", cursor))
            screen["next_index"] = screen["start_index"]
        # A separate resumable JSON: never mix third-party counts into the
        # official XML cache, and do not mark a low-score game as verified.
        save_json(Path(args.prefilter_state), prefilter)
        priority_rows = pending_priorities(
            rows, prefilter, collector.follower_cache,
            min_start_index=cursor, max_candidates=budget,
        )
        if priority_rows:
            qualified.extend(collector.qualify(map(as_game, priority_rows)))
            priority_fresh_requests = collector.fresh_follower_requests
        priority_remaining = len(pending_priorities(
            rows, prefilter, collector.follower_cache,
            min_start_index=cursor, max_candidates=len(rows),
        ))
        state["prefilter_next_index"] = int(prefilter.get("next_index", cursor))
        state["prefilter_complete"] = bool(prefilter.get("complete", False))
        state["prefilter_threshold"] = PRIORITY_THRESHOLD

    # When the entire fast screen and priority queue are finished, return to
    # the original complete XML pass starting at the unchanged official cursor.
    # This prevents third-party undercounts from permanently losing real hits.
    backfill = not prefilter_enabled or (
        prefilter.get("complete", False)
        and priority_remaining == 0 and not collector.failed_follower_appids
    )
    if backfill and collector.fresh_follower_requests < budget:
        selection = list(map(as_game, rows[cursor:]))
        qualified.extend(collector.qualify(selection))
        sequential_processed = collector.processed_candidate_count

    catalog_by_id = {row["appid"]: row for row in rows}
    enriched = []
    for row in {row.appid: row for row in qualified}.values():
        record = vars(row).copy()
        source = catalog_by_id.get(row.appid, {})
        for field in ("name_en", "name_zh_tw", "release_date_timezone",
                      "release_date_basis", "release_time_utc",
                      "release_time_source", "discovered_by"):
            record[field] = source.get(field)
        enriched.append(record)
    combined = merge_partial_segment(master.get("games", []), enriched, today=today)
    master["games"] = combined
    master["updated_at"] = datetime.now(timezone.utc).isoformat()
    failed = collector.failed_follower_appids
    if failed and backfill:
        failed_index = next(
            (i for i, row in enumerate(rows[cursor:], start=cursor)
             if row["appid"] in failed), cursor
        )
        next_cursor = min(cursor + sequential_processed, failed_index)
    elif backfill:
        next_cursor = cursor + sequential_processed
    else:
        next_cursor = cursor
    state["next_follower_index"] = next_cursor
    if next_cursor >= len(rows) and not failed and (
        not prefilter_enabled or prefilter.get("complete", False)
    ):
        state["phase"] = "complete"
        state["initial_complete"] = True
    state["last_attempt"] = {
        "phase": "followers", "candidate_count": len(rows),
        "start_index": cursor, "next_index": next_cursor,
        "fresh_follower_requests": collector.fresh_follower_requests,
        "priority_fresh_requests": priority_fresh_requests,
        "cached_reuses": collector.cached_follower_reuses,
        "failures": collector.follower_failures,
        "rate_limit_events": collector.follower_rate_limit_events,
        "qualified_this_batch": len(enriched),
        "published_games_total": len(master["games"]),
        "prefilter_start_index": screen["start_index"],
        "prefilter_next_index": int(prefilter.get("next_index", cursor)) if prefilter_enabled else cursor,
        "prefilter_screened": screen["screened"],
        "prefilter_priority": screen["priority"],
        "prefilter_missing": screen["missing"],
        "prefilter_complete": bool(prefilter.get("complete", False)) if prefilter_enabled else False,
        "priority_remaining": priority_remaining,
        "backfill_started": backfill,
    }
    return {"phase": state["phase"], **state["last_attempt"]}


def run(args: argparse.Namespace) -> dict[str, Any]:
    state_file = Path(args.state)
    catalog_file = Path(args.catalog)
    master_file = Path(args.master)
    state = load_json(state_file, fresh_state(taiwan_today(), args.days))
    if state.get("mode") != "two_phase_steam_year":
        raise RuntimeError("This pipeline needs its own private state; legacy state untouched")
    catalog = load_json(catalog_file, {"games": []})
    master = load_json(master_file, {"games": []})
    phase = state.get("phase")
    if phase == "discovery":
        result = run_discovery(args, state, catalog)
    elif phase == "followers":
        result = run_follower_batch(args, state, catalog, master)
    elif phase == "complete":
        result = {"phase": "complete", "candidate_count": len(catalog.get("games", []))}
    else:
        raise RuntimeError(f"Unknown phase: {phase!r}")
    stamp = datetime.now(timezone.utc).isoformat()
    state["updated_at"] = stamp
    save_json(catalog_file, catalog)
    save_json(state_file, state)
    if phase == "followers":
        save_json(master_file, master)
        output = {
            "generated_at": stamp, "source": {
                "catalog": "Steam IStoreQueryService/Query day-filter, TW",
                "followers": "Steam Community game-group memberCount",
            },
            "filter": {"country": "TW", "min_followers": 5000,
                       "release_horizon_days": args.days,
                       "release_date_timezone": "Asia/Taipei"},
            "initialization": {
                "mode": "two_phase_steam_year",
                "complete": state["initial_complete"],
                "phase": state["phase"], "days_scanned": state["days_scanned"],
                "candidate_count": catalog.get("count", 0),
                "next_follower_index": state.get("next_follower_index", 0),
                "coverage_exhaustive": False,
                "prefilter_threshold": state.get("prefilter_threshold"),
                "prefilter_next_index": state.get("prefilter_next_index"),
                "prefilter_complete": state.get("prefilter_complete", False),
                "last_attempt": state.get("last_attempt"),
            },
            "count": len(master.get("games", [])),
            "games": master.get("games", []),
        }
        write_json(output, args.output)
    LOG.info("Steam two-stage run: %s", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="data/steam_candidate_state.json")
    parser.add_argument("--catalog", default="data/steam_candidates.json")
    parser.add_argument("--master", default="data/steam_upcoming_master.json")
    parser.add_argument("--output", default="output/steam_upcoming.json")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--batch-days", type=int, default=365)
    parser.add_argument("--follower-cache", default="data/steam_followers_cache.json")
    parser.add_argument("--checkpoint", default="data/steam_followers_checkpoint.json")
    parser.add_argument("--checkpoint-branch", default="steam-state")
    parser.add_argument("--prefilter-state", default="data/steam_prefilter_state.json")
    parser.add_argument("--prefilter-batch-size", type=int, default=0)
    parser.add_argument("--prefilter-request-interval", type=float, default=0.5)
    parser.add_argument("--max-fresh-requests-per-run", type=int, default=50)
    parser.add_argument("--request-interval", type=float, default=30.0)
    parser.add_argument("--search-interval", type=float, default=1.5)
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    run(build_parser().parse_args())
