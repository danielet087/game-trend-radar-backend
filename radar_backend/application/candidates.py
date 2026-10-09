"""Coordinate discovery, display-date verification, prescreen, and official checks.

This module depends on pure candidate rules and explicit source/state ports. It
owns phase progression, while HTTP details, JSON mechanics, CLI parsing, and
remote publication remain outside the application layer.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from radar_backend.domain.candidates import (
    PRIORITY_THRESHOLD, candidate_catalog_rows, candidate_job_result,
    date_gate_candidates, filter_candidate_rows, fresh_state, normalize_prefilter_entries,
    official_request_budget, prefilter_complete, prescreen_window_complete,
    priority_rows, public_candidate_rows,
)

LOG = logging.getLogger(__name__)


class CandidateSourcePort(Protocol):
    session_factory: Callable
    api_key: Callable
    query_day: Callable
    fetch_metadata: Callable
    build_snapshot: Callable
    fetch_store_tw_names: Callable
    enrich_tw_names: Callable
    scan_batch: Callable
    collector_factory: Callable
    as_upcoming_game: Callable
    fetch_store_release_details: Callable
    apply_store_release_detail: Callable
    filter_confirmed_master_games: Callable
    merge_partial_segment: Callable
    excluded_appids: Callable
    is_disallowed: Callable
    is_twitch_qualified: Callable


class CandidateStatePort(Protocol):
    load: Callable
    save: Callable
    write_output: Callable


@dataclass(frozen=True)
class CandidateRuntime:
    sources: CandidateSourcePort
    state: CandidateStatePort
    today: Callable[[], date]
    utcnow: Callable[[], datetime]
    sleep: Callable[[float], None]


def active_candidate_rows(catalog: dict[str, Any], state: dict[str, Any], runtime: CandidateRuntime) -> list[dict[str, Any]]:
    # Reject incomplete public display-date verification before reading any
    # external exclusion ledger, preserving the original fail-closed ordering.
    rows = candidate_catalog_rows(catalog, state)
    return filter_candidate_rows(
        rows, blocked=runtime.sources.excluded_appids(),
        is_disallowed=runtime.sources.is_disallowed,
    )

def run_discovery(args: argparse.Namespace, state: dict[str, Any], catalog: dict[str, Any], runtime: CandidateRuntime) -> dict[str, Any]:
    key = runtime.sources.api_key()
    if not key:
        raise RuntimeError("STEAM_WEB_API_KEY is required for verified day-range discovery")
    start = date.fromisoformat(state["next_date"])
    end = date.fromisoformat(state["end_date"])
    session = runtime.sources.session_factory()
    session.headers.update({"User-Agent": "GameTrendRadar/0.8"})
    games_by_id = {str(x["appid"]): x for x in catalog.get("games", [])}
    summaries = []
    attempts = 0
    while start <= end and attempts < args.batch_days:
        games, info = runtime.sources.query_day(session, key, start, request_interval=args.search_interval)
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
            runtime.sleep(args.search_interval)
    state["last_attempt"] = {
        "phase": "discovery", "days": summaries,
        "new_catalog_total": len(games_by_id), "followers_queried": 0,
    }
    if start > end:
        # Query release timestamps often encode "2026" as 12/31 and "Q2"
        # as a quarter-end day. Do not prefilter Followers until Store Browse
        # confirms the displayed date is literally a full year/month/day.
        state["phase"] = "date_precision"
        state["discovery_finished_at"] = runtime.utcnow().isoformat()
    catalog["games"] = sorted(
        games_by_id.values(), key=lambda x: (x["release_start"], x["appid"])
    )
    catalog["updated_at"] = runtime.utcnow().isoformat()
    catalog["count"] = len(catalog["games"])
    return {"phase": state["phase"], "days_processed": attempts,
            "days_scanned": state["days_scanned"],
            "candidate_count": catalog["count"], "followers_queried": 0}

def run_date_precision_phase(state: dict[str, Any], catalog: dict[str, Any], runtime: CandidateRuntime) -> dict[str, Any]:
    """Full-year Store display + sexual-content gate BEFORE third-party reads."""
    original = date_gate_candidates(state, catalog)
    if catalog.get("date_precision_complete"):
        state["phase"] = "prefilter"
        return {"phase": "date_precision", "already_verified": True,
                "eligible": len(catalog["date_precision_eligible"])}
    session = runtime.sources.session_factory()
    session.headers.update({"User-Agent": "GameTrendRadarPublicReleasePrecision/1.0"})
    appids = [int(game["appid"]) for game in original]
    if len(appids) != len(set(appids)):
        raise RuntimeError("Duplicate AppID in Steam discovery catalogue")
    details = runtime.sources.fetch_metadata(session, sorted(appids))
    snapshot = runtime.sources.build_snapshot(catalog, details)
    if snapshot["count"] <= 0:
        raise RuntimeError("No eligible exact-day games; check Store Browse response")
    # Only the eligible candidates need a second, tchinese Store metadata pass.
    # Do this BEFORE third-party Followers and preserve the original English title.
    localized = runtime.sources.fetch_store_tw_names(
        session, [int(game["appid"]) for game in snapshot["games"]]
    )
    if len(localized) < snapshot["count"] * 0.95:
        raise RuntimeError("Steam Traditional Chinese lookup mostly unavailable; date gate not complete")
    name_result = runtime.sources.enrich_tw_names(snapshot["games"], localized)
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

def run_prefilter_phase(
    args: argparse.Namespace, state: dict[str, Any], catalog: dict[str, Any],
    runtime: CandidateRuntime,
) -> dict[str, Any]:
    """Phase 2: scan ONLY the third-party source, never instantiate XML collector."""
    rows = active_candidate_rows(catalog, state, runtime)
    path = Path(args.prefilter_state)
    pre = runtime.state.load(path, {"version": 1, "next_index": 0,
                           "complete": False, "games": {}})
    pre.setdefault("head_next_index", 0)
    if not isinstance(pre.get("games"), dict):
        raise RuntimeError("Invalid third-party prefilter games")
    key = runtime.sources.api_key()
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
        info = runtime.sources.scan_batch(
            subset, temporary, steam_api_key=key, initial_index=0,
            limit=args.prefilter_batch_size,
            request_interval=args.prefilter_request_interval,
        )
        pre["games"].update(temporary["games"])
        pre["head_next_index"] = int(info["next_index"])
    elif int(pre.get("next_index", 0)) < len(rows):
        info = runtime.sources.scan_batch(
            rows, pre, steam_api_key=key, initial_index=0,
            limit=args.prefilter_batch_size,
            request_interval=args.prefilter_request_interval,
        )
    else:
        info = {"start_index": len(rows), "next_index": len(rows),
                "screened": 0, "priority": 0, "missing": 0}
    # Missing third-party entries are explicitly UNRESOLVED; only measured
    # >=4000 enter the Steam verification list. Never invent a low score.
    normalize_prefilter_entries(pre["games"])
    complete = prescreen_window_complete(pre, rows, head_limit)
    pre["complete"] = complete
    pre["threshold"] = PRIORITY_THRESHOLD
    pre["updated_at"] = runtime.utcnow().isoformat()
    runtime.state.save(path, pre)
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
    runtime: CandidateRuntime,
) -> dict[str, Any]:
    """Phase 3: ONLY official XML for titles measured >=4000 in phase 2."""
    rows = active_candidate_rows(catalog, state, runtime)
    pre = runtime.state.load(Path(args.prefilter_state), {"games": {}})
    if not prefilter_complete(pre, rows):
        raise RuntimeError("Step 2 has not screened every candidate; no official XML allowed")
    today = runtime.today()
    cache_data = runtime.state.load(Path(args.follower_cache), {"games": {}})
    cache = cache_data.get("games") or {}
    selected_priority_rows = priority_rows(rows, pre)
    remaining = [row for row in selected_priority_rows if str(row["appid"]) not in cache]
    start_verified = int(state.get("verified_priority_count", 0))
    if not remaining:
        state["priority_total"] = len(selected_priority_rows)
        state["verified_priority_count"] = len(selected_priority_rows)
        state["official_verified_count"] = sum(
            str(item["appid"]) in cache for item in rows
        )
        state["phase"] = "complete"
        state["initial_complete"] = True
        state["coverage_exhaustive"] = False  # only prefiltered games validated
        state["last_attempt"] = {
            "phase": "complete", "candidate_count": len(rows),
            "priority_total": len(selected_priority_rows),
            "verified_priority_count": len(selected_priority_rows),
            "fresh_follower_requests": 0,
        }
        return dict(state["last_attempt"])
    budget = official_request_budget(getattr(args, "max_fresh_requests_per_run", 50))
    collector = runtime.sources.collector_factory(
        country="TW", horizon_days=365, min_followers=5000,
        follower_request_interval=args.request_interval,
        search_request_interval=args.search_interval,
        follower_cache_path=args.follower_cache, checkpoint_path=args.checkpoint,
        checkpoint_branch=args.checkpoint_branch, checkpoint_every=5,
        reuse_all_cached_during_initialization=True,
        max_fresh_requests_per_run=budget,
    )
    fresh_qualified = collector.qualify(list(map(runtime.sources.as_upcoming_game, remaining)))
    source_by_id = {x["appid"]: x for x in rows}

    # A cached official >=5000 result is still official. Re-use it instead of
    # wasting another Community XML request, but ALWAYS perform the same final
    # Store-date verification before it can enter the qualified master.
    qualified_items: dict[int, dict[str, Any]] = {}
    for row in fresh_qualified:
        qualified_items[row.appid] = vars(row).copy()
    for source in selected_priority_rows:
        cached = cache.get(str(source["appid"]))
        if not isinstance(cached, dict):
            continue
        try:
            followers = int(cached.get("followers"))
        except (TypeError, ValueError):
            continue
        if followers < 5000:
            continue
        qualified_items.setdefault(int(source["appid"]), {
            **source,
            "followers": followers,
            "follower_checked_at": cached.get("checked_at"),
        })

    store_details = runtime.sources.fetch_store_release_details(
        runtime.sources.session_factory(), list(qualified_items), today=today,
    )
    enriched = []
    store_date_rejected = 0
    for appid, item in qualified_items.items():
        source = source_by_id.get(appid, {})
        for field in ("name_en", "name_zh_tw", "name_zh_cn",
                      "name_zh_tw_traditional", "name_zh_cn_traditional",
                      "name_en_traditional", "language_support",
                      "release_date_timezone",
                      "release_date_basis", "release_time_utc",
                      "release_time_source", "discovered_by",
                      "sexual_content_screened"):
            if source.get(field) is not None:
                item[field] = source.get(field)
        detail = store_details.get(appid) or {"exact": False, "status": "unavailable"}
        if detail.get("exact") is not True:
            store_date_rejected += 1
            LOG.info("POST_FOLLOWERS_STORE_DATE_REJECTED appid=%s status=%s",
                     appid, detail.get("status"))
            continue
        enriched.append(runtime.sources.apply_store_release_detail(item, detail))
    master["games"] = runtime.sources.filter_confirmed_master_games(
        runtime.sources.merge_partial_segment(master.get("games", []), enriched, today=today),
        rows, today=today,
    )
    master["updated_at"] = runtime.utcnow().isoformat()
    after_cache = runtime.state.load(Path(args.follower_cache), {"games": {}}).get("games") or {}
    verified = sum(str(x["appid"]) in after_cache for x in selected_priority_rows)
    state["verified_priority_count"] = verified
    state["priority_total"] = len(selected_priority_rows)
    state["official_verified_count"] = len(after_cache)
    state["next_follower_index"] = int(state.get("next_follower_index", 0))
    if verified == len(selected_priority_rows) and not collector.failed_follower_appids:
        state["phase"] = "complete"
        state["initial_complete"] = True
    state["last_attempt"] = {
        "phase": "followers", "candidate_count": len(rows),
        "start_index": start_verified, "next_index": verified,
        "priority_total": len(selected_priority_rows),
        "verified_priority_count": verified,
        "fresh_follower_requests": collector.fresh_follower_requests,
        "cached_reuses": collector.cached_follower_reuses,
        "failures": collector.follower_failures,
        "rate_limit_events": collector.follower_rate_limit_events,
        "qualified_this_batch": len(enriched),
        "store_date_rechecked": len(qualified_items),
        "store_date_rejected": store_date_rejected,
        "published_games_total": len(master["games"]),
        "prefilter_complete": True,
    }
    return {"phase": state["phase"], **state["last_attempt"]}

def execute_pipeline(args: argparse.Namespace, runtime: CandidateRuntime) -> dict[str, Any]:
    state_file = Path(args.state)
    catalog_file = Path(args.catalog)
    master_file = Path(args.master)
    state = runtime.state.load(state_file, fresh_state(runtime.today(), args.days))
    if state.get("mode") != "two_phase_steam_year":
        raise RuntimeError("This pipeline needs its own private state; legacy state untouched")
    catalog = runtime.state.load(catalog_file, {"games": []})
    master = runtime.state.load(master_file, {"games": []})
    phase = state.get("phase")
    if phase == "discovery":
        result = run_discovery(args, state, catalog, runtime)
    elif phase == "date_precision":
        result = run_date_precision_phase(state, catalog, runtime)
    elif phase == "prefilter" or (
        phase == "followers"
        and int(getattr(args, "prefilter_batch_size", 0) or 0) > 0
        and not prefilter_complete(
            runtime.state.load(Path(args.prefilter_state), {"games": []}),
            active_candidate_rows(catalog, state, runtime),
        )
    ):
        result = run_prefilter_phase(args, state, catalog, runtime)
    elif phase == "followers":
        result = run_follower_batch(args, state, catalog, master, runtime)
    elif phase == "complete":
        result = {"phase": "complete", "candidate_count": len(catalog.get("games", []))}
    else:
        raise RuntimeError(f"Unknown phase: {phase!r}")
    stamp = runtime.utcnow().isoformat()
    state["updated_at"] = stamp
    runtime.state.save(catalog_file, catalog)
    runtime.state.save(state_file, state)
    if phase in {"followers", "prefilter"}:
        if phase == "followers":
            runtime.state.save(master_file, master)
        original_games = runtime.sources.filter_confirmed_master_games(
            master.get("games", []), active_candidate_rows(catalog, state, runtime),
            today=runtime.today(),
        )
        # The daily eligible catalogue remains a future allow-list while
        # formally qualified release history stays in the public calendar.
        public_games = public_candidate_rows(
            original_games, active_candidate_rows(catalog, state, runtime),
            date_precision_required=bool(state.get("date_precision_required")),
            today=runtime.today(), is_twitch_qualified=runtime.sources.is_twitch_qualified,
        )
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
        runtime.state.write_output(output, args.output)
    # Git persistence is owned by the workflow after this function returns.
    # Preserve legacy phase/last_attempt fields and report that boundary honestly.
    result["job_result"] = candidate_job_result(state).to_dict()
    LOG.info("Steam two-stage run: %s", result)
    return result

