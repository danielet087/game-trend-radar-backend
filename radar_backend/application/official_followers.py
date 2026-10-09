"""Bounded hourly official Followers batch, driven by explicit ports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Callable

from radar_core.jobs import JobResult, JobStatus
from radar_backend.domain.official_queue import (
    TAIPEI, FollowerOutcome, queue_checked_numeric as checked_numeric, valid_group_id64,
)


@dataclass(frozen=True)
class OfficialPaths:
    frozen: Path
    eligible: Path
    prefilter: Path
    official_cache: Path
    original_official: Path
    checkpoint: Path
    master: Path
    output: Path
    cohort: str


@dataclass(frozen=True)
class OfficialBatchServices:
    read: Callable
    save: Callable
    exists: Callable
    clock: Callable
    monotonic: Callable
    sleep: Callable
    session_factory: Callable
    make_queue: Callable
    git_push: Callable
    reverify_pending_store_dates: Callable
    retry_pending_content_dispatches: Callable
    verify_store_date_for_result: Callable
    upsert_qualified_master: Callable
    dispatch_content_event: Callable
    follower_client_factory: Callable
    follower_cache_factory: Callable
    cooldown_factory: Callable
    begin_persistence: Callable | None = None


def official_job_result(report, *, state_persisted, target_slot=None):
    """Keep an hourly checkpoint distinct from complete current-day coverage."""
    reason = report["stop_reason"]
    source_status = report.get("source_status", {})
    current_source_complete = (
        source_status.get("status") == "current_day_prefilter_complete"
    )
    awaiting_resolution = bool(
        source_status.get("parked_group_xml_appids")
        or report.get("awaiting_group_resolution")
        or report.get("unresolved_candidates")
    )
    collection_complete = (
        type(report.get("remaining_queue")) is int
        and report["remaining_queue"] == 0
        and current_source_complete
        and not awaiting_resolution
    )
    if reason in {"git_checkpoint_failure", "final_git_checkpoint_failure"} or not state_persisted:
        status = JobStatus.FAILED
    elif collection_complete:
        status = JobStatus.COMPLETE
    elif "cooldown" in reason or reason == "first_http_429":
        status = JobStatus.COOLING_DOWN
    else:
        status = JobStatus.PARTIAL
    return JobResult(
        job="official-followers", status=status, reason=reason,
        collection_complete=collection_complete, state_persisted=state_persisted,
        # Saving the master or dispatching content is not a frontend publication.
        published=False, requires_publication=False,
        target_slot=target_slot,
    )


def run_official_batch(args, *, paths: OfficialPaths, services: OfficialBatchServices,
                       skip_cooldown=False, run_id="", target_slot=None):
    started = services.monotonic()
    started_at_taipei = services.clock().isoformat()
    original = services.read(paths.frozen / "source_queue.json")
    oldgroups = services.read(paths.frozen / "source_unresolved.json")
    oldcp = services.read(paths.frozen / "checkpoint.json")
    eligible = services.read(paths.eligible)
    prefilter = services.read(paths.prefilter)
    official_cache = services.read(paths.official_cache)
    other_official = services.read(paths.original_official)

    if services.exists(paths.checkpoint):
        cp = services.read(paths.checkpoint)
        if cp.get("cohort") != paths.cohort:
            raise ValueError("Dynamic cohort checkpoint version mismatch")
    else:
        cp = {
            "version": 1,
            "cohort": paths.cohort,
            "pending_candidates": {},
            "official_results": {},
            "attempt_events": [],
            "next_request_after_taipei": None,
            "rate_limit_count": 0,
            "created_at_taipei": services.clock().isoformat(),
        }
    master = services.read(paths.master) if services.exists(paths.master) else {"version": 1, "games": []}
    if services.begin_persistence is not None:
        services.begin_persistence(cp, master)
    cp.setdefault("content_dispatches", {})
    store_rechecks = services.reverify_pending_store_dates(cp, master)
    retried_dispatches = services.retry_pending_content_dispatches(cp)
    if store_rechecks or retried_dispatches:
        services.save(paths.checkpoint, cp)
        services.save(paths.master, master)

    q, source_status = services.make_queue(
        cp, original, oldcp, oldgroups, eligible, prefilter,
        official_cache, other_official
    )
    # Missing group IDs stay in the same queue. Resolve them in the existing
    # workflow before using Community XML; they must not consume XML attempts.
    follower_cache = services.follower_cache_factory(cp, official_cache, oldcp, other_official, prefilter)
    cooldown_store = services.cooldown_factory(cp, oldcp)
    known_group_queue = [{**row, "group_id64": gid} for row in q
                         if (gid := valid_group_id64(row.get("group_id64"))
                             or follower_cache.group_id(row["appid"])) is not None]
    attempts = []
    run_metadata = {"run_id": run_id} if run_id.isdecimal() else {}
    cp["scheduler_batch"] = {
        **run_metadata, "active": True, "status": "preparing",
        "manual_cooldown_override": skip_cooldown,
        "request_limit": args.max_requests,
        "started_at": started_at_taipei, "last_updated_at": services.clock().isoformat(),
        "requests_this_run": 0, "official_new_this_run": 0, "http_429_this_run": 0,
    }
    stop = "nothing_pending" if not q else "time_budget"
    last_start = None
    client = services.session_factory()
    client.headers["User-Agent"] = "GameTrendRadarOfficialDailyCatchup/1.0"
    follower_client = services.follower_client_factory(
        session=client, clock=services.clock, cooldown=cooldown_store,
    )
    cooldown = cp.get("next_request_after_taipei")
    legacy_cooldown = oldcp.get("next_request_after_taipei")
    previous_deadlines = (cooldown, legacy_cooldown,
                          (cp.get("community_cooldown") or {}).get("retry_at"))
    if not q:
        stop = "nothing_pending"
    elif not skip_cooldown and services.cooldown_factory(cp).blocked(services.clock()):
        stop = "official_429_cooldown_no_request"
    elif not skip_cooldown and services.cooldown_factory(oldcp).blocked(services.clock()):
        stop = "legacy_official_429_cooldown_no_request"
    elif not known_group_queue:
        stop = "awaiting_group_resolution_no_request"
    else:
        stop = "batch_request_limit"
        for row in known_group_queue[:args.max_requests]:
            if services.monotonic() - started >= args.max_seconds - 40:
                stop = "hour_time_budget"
                break
            if last_start is not None:
                delay = args.interval - (services.monotonic() - last_start)
                if delay > 0:
                    services.sleep(delay)
            last_start = services.monotonic()
            aid = row["appid"]
            event = {
                "appid": aid, "when_taipei": services.clock().isoformat(),
                "release_date": row["release_date"],
                "queue_source": row["queue_source"], "http": None,
                "status": "request_started",
            }
            if run_id.isdecimal():
                event["github_run_id"] = run_id
            if skip_cooldown:
                event["manual_cooldown_override"] = True
            cached = follower_cache.latest(aid, services.clock(), today_only=True, expected_group=row["group_id64"])
            if cached is not None:
                outcome = FollowerOutcome("ok", datetime.fromisoformat(cached.checked_at.replace("Z", "+00:00")),
                                          followers=cached.followers)
                event["cache_reused"] = True
            else:
                outcome = follower_client.fetch(row["group_id64"], manual_override=skip_cooldown)
            event.update(status=outcome.status, http=outcome.http)
            if outcome.retry_after:
                event["retry_after_header"] = outcome.retry_after[:128]
            if outcome.error_type:
                event["error_type"] = outcome.error_type
            for key in ("content_type", "response_bytes", "response_prefix"):
                value = getattr(outcome, key)
                if value is not None:
                    event[key] = value
            if outcome.status == "ok":
                count = outcome.followers
                event["official_followers"] = count
                cp["official_results"][str(aid)] = {
                    **row, "group_id64": row["group_id64"],
                    "official_followers": count, "official_ge5000": count >= 5000,
                    "official_checked_at_taipei": outcome.observed_at.astimezone(TAIPEI).isoformat(),
                    "official_source": "Steam Community XML memberCount",
                }
                # Twitch admissions still use the shared cache-only importer.
                if count >= 5000 and row.get("queue_source") != "twitch_steam_discovery":
                    official = cp["official_results"][str(aid)]
                    exact = services.verify_store_date_for_result(official, client)
                    event["store_date_exact"] = exact
                    event["store_date_status"] = official.get("store_date_status")
                    if exact:
                        event["release_date"] = official["release_date"]
                        services.upsert_qualified_master(master, official)
                        services.save(paths.master, master)
                        event["content_dispatch"] = services.dispatch_content_event(cp, official)
                    else:
                        event["content_dispatch"] = "store_date_not_verified"
            else:
                stop = {
                    "rate_limited": "first_http_429",
                    "access_or_server_error": "http_access_or_server_error",
                    "unexpected_http": "unexpected_http",
                    "missing_count_or_group_mismatch": "invalid_official_xml",
                    "transport_or_xml_error": "transport_or_xml_error",
                    "cooldown_no_request": "official_429_cooldown_no_request",
                }.get(outcome.status, "awaiting_group_resolution_no_request")
                if cp.get("next_request_after_taipei"):
                    event["next_request_after_taipei"] = cp["next_request_after_taipei"]
            attempts.append(event)
            cp["attempt_events"].append(event)
            success_count = len({item["appid"] for item in attempts if item["status"] == "ok"})
            cp["scheduler_batch"].update(
                status="querying", last_updated_at=services.clock().isoformat(),
                last_appid=aid, last_name=row.get("name") or f"Steam App {aid}",
                requests_this_run=sum(not item.get("cache_reused") for item in attempts),
                official_new_this_run=success_count,
                http_429_this_run=sum(item.get("http") == 429 for item in attempts),
            )
            if len(cp["attempt_events"]) > 2000:
                cp["attempt_events"] = cp["attempt_events"][-1500:]
            if len(attempts) <= 4 or len(attempts) % 10 == 0 or event["status"] != "ok":
                print("DAILY_CATCHUP_ITEM", json.dumps({
                    "appid": aid,
                    "status": event["status"],
                    "followers": event.get("official_followers"),
                    "attempted": len(attempts),
                    "successes": success_count,
                    "elapsed_seconds": round(services.monotonic() - started, 2),
                }, ensure_ascii=False), flush=True)
            services.save(paths.checkpoint, cp)
            if success_count and success_count % args.save_every == 0 or event["status"] != "ok":
                if not services.git_push():
                    stop = "git_checkpoint_failure"
                    break
                # A clean rebase can merge another producer's AppIDs. Reload
                # that merged state before the next in-memory save.
                latest_checkpoint, latest_master = services.read(paths.checkpoint), services.read(paths.master)
                cp.clear()
                cp.update(latest_checkpoint)
                master.clear()
                master.update(latest_master)
            if event["status"] != "ok":
                break

    successful = len({item["appid"] for item in attempts if item["status"] == "ok"})
    # Concurrent producers can add results during a checkpoint reload. They
    # reduce the remaining queue but never count as this worker's observations.
    pending_left = sum(str(row["appid"]) not in cp["official_results"] for row in q)
    report = {
        "source_status": source_status,
        "start_taipei": started_at_taipei,
        "finish_taipei": services.clock().isoformat(),
        "stop_reason": stop,
        "elapsed_seconds": round(services.monotonic() - started, 2),
        "request_limit": args.max_requests,
        "manual_cooldown_override": skip_cooldown,
        "request_interval_seconds": args.interval,
        "requests_this_run": sum(not item.get("cache_reused") for item in attempts),
        "official_new_this_run": successful,
        "store_date_rechecks_before_followers": store_rechecks,
        "new_ge5000_this_run": sum(item.get("official_followers", -1) >= 5000
                                   for item in attempts if item["status"] == "ok"),
        "completed_legacy": len(oldcp["official_results"]),
        "completed_dynamic": len(cp["official_results"]),
        "combined_official_completed": len(oldcp["official_results"]) + len(cp["official_results"]),
        "remaining_queue": pending_left,
        "awaiting_group_resolution": len(q) - len(known_group_queue),
        "http_429_this_run": sum(e["http"] == 429 for e in attempts),
        "twitch_priority_pending": source_status.get("twitch_priority_pending", 0),
        "twitch_official_success_this_run": sum(e.get("queue_source") == "twitch_steam_discovery"
                                                and e["status"] == "ok" for e in attempts),
        "next_request_after_taipei": cp.get("next_request_after_taipei"),
        "production_cache_modified": False,
        "github_actions_hourly_schedule": "03:00-23:00 Asia/Taipei",
    }
    cp["scheduler_batch"].update(
        active=False, status="finished", last_updated_at=report["finish_taipei"],
        finished_at=report["finish_taipei"], stop_reason=stop,
        requests_this_run=report["requests_this_run"], official_new_this_run=successful,
        http_429_this_run=report["http_429_this_run"],
    )
    services.save(paths.checkpoint, cp)
    services.save(paths.master, master)
    services.save(paths.output / "report.json", report)
    services.save(paths.output / "attempts.json", attempts)
    state_persisted = services.git_push()
    if not state_persisted:
        report["stop_reason"] = "final_git_checkpoint_failure"
        cp["scheduler_batch"]["stop_reason"] = report["stop_reason"]
        services.save(paths.checkpoint, cp)
    job_result = official_job_result(report, state_persisted=state_persisted, target_slot=target_slot)
    report["job_result"] = job_result.to_dict()
    services.save(paths.output / "report.json", report)
    print("DAILY_CATCHUP_FINAL", json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return job_result
