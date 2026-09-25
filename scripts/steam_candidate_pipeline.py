"""Steam year initialization: discover exact TW release days BEFORE querying Followers.

Phase 1 visits 365 Taiwan calendar-day intervals through the authenticated Steam
IStoreQueryService (release_date_type=1), saves a private candidate catalog and
resume cursor. Phase 2 screens ALL candidates via third-party bulk. Phase 3 verifies ONLY\ntitles measured >=4,000 via Steam XML; official >=5,000 qualifies.
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
    PRIORITY_THRESHOLD, scan_batch,
)
from scripts.screen_steam_candidates_before_followers import (
    build_snapshot, fetch_metadata,
)
from scripts.steam_localized_titles import enrich_tw_names, fetch_store_tw_names
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.steam_master_date_gate import filter_confirmed_master_games

LOG = logging.getLogger(__name__)
QUERY_URL = "https://api.steampowered.com/IStoreQueryService/Query/v1/"
PAGE_SIZE = 1000
MAX_PAGES_PER_DAY = 30
QUERY_INTERVAL_SECONDS = 1.5


def fresh_state(anchor: date, days: int) -> dict[str, Any]:
    return {
        "version": 1, "mode": "two_phase_steam_year",
        "phase": "discovery", "date_precision_required": True,
        "anchor_date": anchor.isoformat(),
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
        # Never print a request URL: requests exceptions can contain the key.
        try:
            response = session.get(
                QUERY_URL,
                params={"key": api_key, "input_json": json.dumps(query, separators=(",", ":"))},
                timeout=35,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"HTTP {status}" if status is not None else type(exc).__name__
            raise RuntimeError(
                f"Steam Query failed for {day} at offset {offset}: {reason}"
            ) from None
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
        # Query release timestamps often encode "2026" as 12/31 and "Q2"
        # as a quarter-end day. Do not prefilter Followers until Store Browse
        # confirms the displayed date is literally a full year/month/day.
        state["phase"] = "date_precision"
        state["discovery_finished_at"] = datetime.now(timezone.utc).isoformat()
    catalog["games"] = sorted(
        games_by_id.values(), key=lambda x: (x["release_start"], x["appid"])
    )
    catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    catalog["count"] = len(catalog["games"])
    return {"phase": state["phase"], "days_processed": attempts,
            "days_scanned": state["days_scanned"],
            "candidate_count": catalog["count"], "followers_queried": 0}


def active_candidate_rows(catalog: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Future runs use only the Store-verified eligible list.

    Old, already-completed historical catalogues are left intact, including
    their saved follower/prefilter cursors and original 11,467 records.
    """
    if state.get("date_precision_required"):
        if not catalog.get("date_precision_complete"):
            raise RuntimeError("Steam public display-date gate incomplete; no Followers allowed")
        rows = catalog["date_precision_eligible"]
    else:
        rows = catalog.get("games", [])
    blocked = excluded_appids()
    return [row for row in rows if not is_disallowed(row, blocked)]


def run_date_precision_phase(state: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Full-year Store display + sexual-content gate BEFORE third-party reads."""
    original = catalog.get("games", [])
    if not original or int(state.get("days_scanned", 0)) < 365:
        raise RuntimeError("Finish 365-day Steam discovery before date gate")
    if catalog.get("date_precision_complete"):
        state["phase"] = "prefilter"
        return {"phase": "date_precision", "already_verified": True,
                "eligible": len(catalog["date_precision_eligible"])}
    session = requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadarPublicReleasePrecision/1.0"})
    appids = [int(game["appid"]) for game in original]
    if len(appids) != len(set(appids)):
        raise RuntimeError("Duplicate AppID in Steam discovery catalogue")
    details = fetch_metadata(session, sorted(appids))
    snapshot = build_snapshot(catalog, details)
    if snapshot["count"] <= 0:
        raise RuntimeError("No eligible exact-day games; check Store Browse response")
    # Only the eligible candidates need a second, tchinese Store metadata pass.
    # Do this BEFORE third-party Followers and preserve the original English title.
    localized = fetch_store_tw_names(
        session, [int(game["appid"]) for game in snapshot["games"]]
    )
    if len(localized) < snapshot["count"] * 0.95:
        raise RuntimeError("Steam Traditional Chinese lookup mostly unavailable; date gate not complete")
    name_result = enrich_tw_names(snapshot["games"], localized)
    snapshot["official_zh_tw_titles_summary"] = name_result
    snapshot["official_zh_tw_titles_provider"] = (
        "Steam IStoreBrowseService/GetItems tchinese TW"
    )
    # Do not rewrite catalog['games'] or the historical discovery count. Save a
    # derived candidate list, and only then allow third-party and XML stages.
    catalog["date_precision_eligible"] = snapshot["games"]
    catalog["date_precision_summary"] = {
        k: v for k, v in snapshot.items() if k != "games"
    }
    catalog["date_precision_complete"] = True
    state["date_precision_complete"] = True
    state["date_precision_eligible_count"] = snapshot["count"]
    state["date_precision_excluded_count"] = snapshot["excluded"]
    state["phase"] = "prefilter"
    state["last_attempt"] = {
        "phase": "date_precision",
        "candidate_count": len(original),
        "eligible_count": snapshot["count"],
        "excluded_count": snapshot["excluded"],
        "official_zh_tw_titles": name_result["official_zh_tw"],
        "reasons": snapshot["reasons"],
        "fresh_follower_requests": 0,
    }
    return dict(state["last_attempt"])


def as_upcoming_game(raw: dict[str, Any]) -> UpcomingGame:
    return UpcomingGame(
        appid=raw["appid"], name=raw["name"], release_raw=raw["release_raw"],
        release_start=raw["release_start"], release_end=raw["release_end"],
        release_precision="day", capsule_image=raw.get("capsule_image"),
        store_url=raw["store_url"],
    )


def prefilter_complete(prefilter: dict[str, Any], catalog: list[dict[str, Any]]) -> bool:
    """Third-party step cannot be skipped, including the first 685 cached games."""
    items = prefilter.get("games") or {}
    return (
        bool(prefilter.get("complete"))
        and int(prefilter.get("next_index", -1)) >= len(catalog)
        and all(str(row["appid"]) in items for row in catalog)
    )


def run_prefilter_phase(
    args: argparse.Namespace, state: dict[str, Any], catalog: dict[str, Any],
) -> dict[str, Any]:
    """Phase 2: scan ONLY the third-party source, never instantiate XML collector."""
    rows = active_candidate_rows(catalog, state)
    path = Path(args.prefilter_state)
    pre = load_json(path, {"version": 1, "next_index": 0,
                           "complete": False, "games": {}})
    pre.setdefault("head_next_index", 0)
    if not isinstance(pre.get("games"), dict):
        raise RuntimeError("Invalid third-party prefilter games")
    key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("STEAM_WEB_API_KEY required for third-party prefilter")
    # Resume the 685 titles that were officially checked before step 2 was
    # introduced. Already prescreened titles remain untouched.
    head = int(pre.get("head_next_index", 0))
    head_limit = min(int(state.get("next_follower_index", 0)), len(rows))
    if head < head_limit:
        subset = rows[:head_limit]
        temporary = {"version": 1, "next_index": head,
                     "complete": False, "games": {}}
        info = scan_batch(
            subset, temporary, steam_api_key=key, initial_index=0,
            limit=args.prefilter_batch_size,
            request_interval=args.prefilter_request_interval,
        )
        pre["games"].update(temporary["games"])
        pre["head_next_index"] = int(info["next_index"])
    elif int(pre.get("next_index", 0)) < len(rows):
        info = scan_batch(
            rows, pre, steam_api_key=key, initial_index=0,
            limit=args.prefilter_batch_size,
            request_interval=args.prefilter_request_interval,
        )
    else:
        info = {"start_index": len(rows), "next_index": len(rows),
                "screened": 0, "priority": 0, "missing": 0}
    # Missing third-party entries are explicitly UNRESOLVED; only measured
    # >=4000 enter the Steam verification list. Never invent a low score.
    for entry in pre["games"].values():
        members = entry.get("third_party_followers")
        if members is None:
            entry["priority"] = False
            entry["scheduling_band"] = "unresolved"
        elif isinstance(members, int):
            entry["priority"] = members >= PRIORITY_THRESHOLD
            entry["scheduling_band"] = "measured"
    complete = (
        int(pre.get("head_next_index", 0)) >= head_limit
        and int(pre.get("next_index", 0)) >= len(rows)
        and all(str(row["appid"]) in pre["games"] for row in rows)
    )
    pre["complete"] = complete
    pre["threshold"] = PRIORITY_THRESHOLD
    pre["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(path, pre)
    state["prefilter_next_index"] = int(pre.get("next_index", 0))
    state["prefilter_head_next_index"] = int(pre.get("head_next_index", 0))
    state["prefilter_complete"] = complete
    state["prefilter_threshold"] = PRIORITY_THRESHOLD
    state["prefilter_screened_count"] = len(pre["games"])
    state["prefilter_missing_count"] = sum(
        entry.get("third_party_followers") is None for entry in pre["games"].values()
    )
    state["prefilter_matched_count"] = sum(
        entry.get("priority") is True for entry in pre["games"].values()
    )
    state["phase"] = "followers" if complete else "prefilter"
    state["last_attempt"] = {
        "phase": "prefilter", "candidate_count": len(rows),
        "prefilter_start_index": info["start_index"],
        "prefilter_next_index": info["next_index"],
        "prefilter_screened": info["screened"],
        "prefilter_priority": info["priority"],
        "prefilter_missing": info["missing"],
        "prefilter_head_next_index": pre["head_next_index"],
        "prefilter_complete": complete,
        "priority_total": sum(bool(x.get("priority")) for x in pre["games"].values()),
        "missing_total": sum(x.get("third_party_followers") is None
                             for x in pre["games"].values()),
        "fresh_follower_requests": 0,
    }
    return dict(state["last_attempt"])


def run_follower_batch(
    args: argparse.Namespace,
    state: dict[str, Any],
    catalog: dict[str, Any],
    master: dict[str, Any],
) -> dict[str, Any]:
    """Phase 3: ONLY official XML for titles measured >=4000 in phase 2."""
    rows = active_candidate_rows(catalog, state)
    pre = load_json(Path(args.prefilter_state), {"games": {}})
    if not prefilter_complete(pre, rows):
        raise RuntimeError("Step 2 has not screened every candidate; no official XML allowed")
    today = taiwan_today()
    # The old master may predate Store Browse date verification. Never carry
    # unverified future timestamps into the next official Followers batch.
    master["games"] = filter_confirmed_master_games(
        master.get("games", []), rows, today=today,
    )
    cache_data = load_json(Path(args.follower_cache), {"games": {}})
    cache = cache_data.get("games") or {}
    priority_rows = [
        row for row in rows
        if (pre["games"][str(row["appid"])].get("priority")
            and isinstance(pre["games"][str(row["appid"])].get("third_party_followers"), int)
            and pre["games"][str(row["appid"])]["third_party_followers"] >= PRIORITY_THRESHOLD)
    ]
    remaining = [row for row in priority_rows if str(row["appid"]) not in cache]
    start_verified = int(state.get("verified_priority_count", 0))
    if not remaining:
        state["priority_total"] = len(priority_rows)
        state["verified_priority_count"] = len(priority_rows)
        state["official_verified_count"] = sum(
            str(item["appid"]) in cache for item in rows
        )
        state["phase"] = "complete"
        state["initial_complete"] = True
        state["coverage_exhaustive"] = False  # only prefiltered games validated
        state["last_attempt"] = {
            "phase": "complete", "candidate_count": len(rows),
            "priority_total": len(priority_rows),
            "verified_priority_count": len(priority_rows),
            "fresh_follower_requests": 0,
        }
        return dict(state["last_attempt"])
    budget = int(getattr(args, "max_fresh_requests_per_run", 50))
    if not 1 <= budget <= 50:
        raise ValueError("Official XML budget must be within 1..50")
    collector = SteamUpcomingCollector(
        country="TW", horizon_days=365, min_followers=5000,
        follower_request_interval=args.request_interval,
        search_request_interval=args.search_interval,
        follower_cache_path=args.follower_cache, checkpoint_path=args.checkpoint,
        checkpoint_branch=args.checkpoint_branch, checkpoint_every=5,
        reuse_all_cached_during_initialization=True,
        max_fresh_requests_per_run=budget,
    )
    qualified = collector.qualify(list(map(as_upcoming_game, remaining)))
    source_by_id = {x["appid"]: x for x in rows}
    enriched = []
    for row in qualified:
        item = vars(row).copy()
        source = source_by_id.get(row.appid, {})
        for field in ("name_en", "name_zh_tw", "name_zh_cn",
                      "name_zh_tw_traditional", "name_zh_cn_traditional",
                      "name_en_traditional", "language_support",
                      "release_date_timezone",
                      "release_date_basis", "release_time_utc",
                      "release_time_source", "discovered_by",
                      "release_display_precision", "sexual_content_screened"):
            item[field] = source.get(field)
        enriched.append(item)
    blocked = excluded_appids()
    master["games"] = filter_confirmed_master_games(
        merge_partial_segment(master.get("games", []), enriched, today=today),
        rows, today=today,
    )
    master["updated_at"] = datetime.now(timezone.utc).isoformat()
    after_cache = load_json(Path(args.follower_cache), {"games": {}}).get("games") or {}
    verified = sum(str(x["appid"]) in after_cache for x in priority_rows)
    state["verified_priority_count"] = verified
    state["priority_total"] = len(priority_rows)
    state["official_verified_count"] = len(after_cache)
    state["next_follower_index"] = int(state.get("next_follower_index", 0))
    if verified == len(priority_rows) and not collector.failed_follower_appids:
        state["phase"] = "complete"
        state["initial_complete"] = True
    state["last_attempt"] = {
        "phase": "followers", "candidate_count": len(rows),
        "start_index": start_verified, "next_index": verified,
        "priority_total": len(priority_rows),
        "verified_priority_count": verified,
        "fresh_follower_requests": collector.fresh_follower_requests,
        "cached_reuses": collector.cached_follower_reuses,
        "failures": collector.follower_failures,
        "rate_limit_events": collector.follower_rate_limit_events,
        "qualified_this_batch": len(enriched),
        "published_games_total": len(master["games"]),
        "prefilter_complete": True,
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
    elif phase == "date_precision":
        result = run_date_precision_phase(state, catalog)
    elif phase == "prefilter" or (
        phase == "followers"
        and int(getattr(args, "prefilter_batch_size", 0) or 0) > 0
        and not prefilter_complete(
            load_json(Path(args.prefilter_state), {"games": []}),
            active_candidate_rows(catalog, state),
        )
    ):
        result = run_prefilter_phase(args, state, catalog)
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
    if phase in {"followers", "prefilter"}:
        if phase == "followers":
            save_json(master_file, master)
        blocked = excluded_appids()
        original_games = filter_confirmed_master_games(
            master.get("games", []), active_candidate_rows(catalog, state),
            today=taiwan_today(),
        )
        if state.get("date_precision_required"):
            # The daily eligible catalogue is a rolling FUTURE window. Keep
            # using it as the allow-list for future titles, but never let it
            # erase a game that was already formally qualified and has since
            # released. Historical calendar dates are persistent.
            allowed = {row["appid"] for row in active_candidate_rows(catalog, state)}
            today_iso = taiwan_today().isoformat()
            public_games = [
                row for row in original_games
                if row.get("appid") in allowed
                or (
                    isinstance(row.get("release_start"), str)
                    and row["release_start"] <= today_iso
                )
            ]
        else:
            public_games = original_games
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
                "date_precision_eligible_count": state.get("date_precision_eligible_count"),
                "date_precision_excluded_count": state.get("date_precision_excluded_count"),
                "date_precision_complete": state.get("date_precision_complete", False),
                "next_follower_index": state.get("next_follower_index", 0),
                "coverage_exhaustive": False,
                "prefilter_threshold": state.get("prefilter_threshold"),
                "prefilter_next_index": state.get("prefilter_next_index"),
                "prefilter_head_next_index": state.get("prefilter_head_next_index"),
                "prefilter_complete": state.get("prefilter_complete", False),
                "priority_total": state.get("priority_total"),
                "verified_priority_count": state.get("verified_priority_count"),
                "prefilter_screened_count": state.get("prefilter_screened_count", 0),
                "prefilter_missing_count": state.get("prefilter_missing_count", 0),
                "prefilter_matched_count": state.get("prefilter_matched_count", 0),
                "official_verified_count": state.get("official_verified_count", 0),
                "last_attempt": state.get("last_attempt"),
            },
            "count": len(public_games),
            "games": public_games,
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
