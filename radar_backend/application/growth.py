"""Collect daily observations through injected official source and state ports.

This use case owns run ordering, request budgets and partial progress. It does
not create an HTTP session, choose file paths or acknowledge publication.
"""
from __future__ import annotations

from datetime import timezone
import math
from radar_core.jobs import JobResult, JobStatus
from radar_backend.domain.growth import (
    TAIPEI, eligible, group_unavailable, growth_collection_complete,
    latest_measurement, timestamp, validated_group_resolution,
)
from radar_backend.domain.official_queue import OfficialObservation


def collect_observations(rows, history, checkpoint, *, client, cache, cooldown,
                         clock, sleep, monotonic, persist, max_requests=120,
                         interval=30, state_saved=False, group_resolver=None,
                         max_seconds=3600):
    if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError("Growth collection requires a positive finite seconds budget")
    if type(max_requests) is not int or max_requests < 0:
        raise ValueError("Growth collection requires a non-negative integer request budget")
    now = clock()
    if now.tzinfo is None:
        raise ValueError("Growth collection needs an aware clock")
    today = now.astimezone(TAIPEI).date()
    # Validate the retry state before considering any network operation.
    cooldown.deadline()
    rows = [row for row in rows if eligible(row, today)]
    eligible_appids = [row["appid"] for row in rows]
    if len(set(eligible_appids)) != len(eligible_appids):
        raise ValueError("Growth catalog contains duplicate eligible AppIDs")

    def latest(row):
        record = history.get(str(row["appid"])) or {}
        shared = cache.latest(row["appid"], clock(), expected_group=cache.group_id(row["appid"]))
        return latest_measurement(row, record, shared, clock())

    rows.sort(key=lambda row: (latest(row) or {}).get("at") or "")
    measurements, errors, pending, events, unavailable = [], [], [], [], []
    attempts = reused = 0
    previous_start = None
    previous_tick = None

    def read_monotonic():
        nonlocal previous_tick
        tick = monotonic()
        if (isinstance(tick, bool) or not isinstance(tick, (int, float))
                or not math.isfinite(tick)
                or (previous_tick is not None and tick < previous_tick)):
            raise ValueError("Growth collection requires a finite non-decreasing monotonic clock")
        previous_tick = tick
        return tick

    started = read_monotonic()
    reason = "completed"
    group_batch = None

    def save_progress():
        persist(checkpoint, {"measurements": measurements, "errors": errors,
                             "pending": pending, "unavailable": unavailable,
                             "reason": reason, "group_resolution_batch": group_batch})

    if group_resolver is not None:
        unresolved = []
        for row in rows:
            previous = latest(row)
            if (cache.group_id(row["appid"]) is None and not (
                previous and timestamp(previous["at"]).astimezone(TAIPEI).date() == today
            )):
                unresolved.append(row)
        if unresolved:
            group_batch = group_resolver(checkpoint, unresolved, now=clock())
            if (not isinstance(group_batch, dict) or group_batch.get("schema_version") != 1
                    or not isinstance(group_batch.get("results"), dict)):
                raise ValueError("Malformed growth group resolution batch")
            requested_ids = {str(row["appid"]) for row in unresolved}
            for aid, item in group_batch["results"].items():
                if aid not in requested_ids or not isinstance(item, dict):
                    raise ValueError("Unexpected growth group resolution result")
                if validated_group_resolution(item.get("group_resolution"), now=clock()) is None:
                    raise ValueError("Unverified growth group resolution receipt")
                cache.remember_group_resolution(int(aid), item)
            # Persist newly resolved identities / absence receipts before XML
            # requests, so interrupted collection can resume the real evidence.
            save_progress()

    def unavailable_outcome(aid):
        read_resolution = getattr(cache, "group_resolution", None)
        resolution = read_resolution(aid) if read_resolution is not None else None
        if group_unavailable(resolution, clock()):
            return {"appid": aid, "reason": "official_group_not_found",
                    "group_resolution": resolution}
        return None

    for row in rows:
        aid = row["appid"]
        previous = latest(row)
        if previous and timestamp(previous["at"]).astimezone(TAIPEI).date() == today:
            measurements.append({"appid": aid, "followers": previous["followers"],
                                 "at": previous["at"], "source": "steam_community"})
            reused += 1
            continue
        gid = cache.group_id(aid)
        if gid is None:
            if missing := unavailable_outcome(aid):
                unavailable.append(missing)
            else:
                pending.append({"appid": aid, "reason": "awaiting_group_resolution"})
            continue
        if cooldown.blocked(clock()):
            reason = "rate_limited"
            pending.append({"appid": aid, "reason": "community_cooldown"})
            continue
        next_start = read_monotonic()
        delay = max(0, interval - (next_start - previous_start)) if previous_start is not None else 0
        # Include the pacing delay in the deadline. Checking only before sleep
        # permits an extra request after the configured collection budget.
        if attempts >= max_requests or next_start + delay - started >= max_seconds:
            reason = "bounded_run"
            pending.append({"appid": aid, "reason": "bounded_run"})
            continue
        if previous_start is not None:
            sleep(delay)
        previous_start = read_monotonic()
        # A real scheduler can wake late; never start a request after its budget.
        if previous_start - started >= max_seconds:
            reason = "bounded_run"
            pending.append({"appid": aid, "reason": "bounded_run"})
            continue
        outcome = client.fetch(gid)
        if outcome.http is not None or outcome.error_type is not None:
            attempts += 1
        events.append({"appid": aid, "group_id64": gid, "status": outcome.status,
                       "http": outcome.http, "observed_at": outcome.observed_at.isoformat()})
        if outcome.status == "ok":
            at = outcome.observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            cache.remember(OfficialObservation(aid, gid, outcome.followers, at))
            measurements.append({"appid": aid, "followers": outcome.followers, "at": at, "source": "steam_community"})
        elif outcome.status in {"rate_limited", "cooldown_no_request"}:
            reason = "rate_limited"
            pending.append({"appid": aid, "reason": "community_cooldown"})
        else:
            errors.append(aid)
            pending.append({"appid": aid, "reason": outcome.status})
            reason = "source_unavailable"
        save_progress()
        # Invalid XML/unexpected HTTP also stop this run; they never imply zero.
        if outcome.status not in {"ok", "rate_limited", "cooldown_no_request"}:
            measured = {item["appid"] for item in measurements}
            accounted = {item["appid"] for item in pending + unavailable}
            for later in rows:
                if later["appid"] not in measured | accounted:
                    recent = latest(later)
                    if recent and timestamp(recent["at"]).astimezone(TAIPEI).date() == today:
                        measurements.append({"appid": later["appid"], "followers": recent["followers"],
                                             "at": recent["at"], "source": "steam_community"})
                        reused += 1
                    elif cache.group_id(later["appid"]) is None and (missing := unavailable_outcome(later["appid"])):
                        unavailable.append(missing)
                    else:
                        pending.append({"appid": later["appid"], "reason": "source_unavailable"})
            break
    if reason == "completed" and pending:
        reason = "awaiting_group_resolution"
    save_progress()
    finished = clock()
    result = {"generated_at": finished.isoformat(), "eligible": len(rows),
              "max_requests": max_requests, "max_seconds": max_seconds,
              "elapsed_seconds": read_monotonic() - started,
              "coverage_schema_version": 2, "eligible_appids": eligible_appids,
              "collection_date_taipei": today.isoformat(),
              "requests": attempts, "reused": reused, "errors": errors, "reason": reason,
              "pending": pending, "unavailable": unavailable,
              "events": events, "measurements": measurements,
              "group_resolution_requests": (group_batch or {}).get("requests_this_run", 0),
              "group_resolution_batch": group_batch,
              "coverage": {"eligible": len(rows), "measured": len(measurements),
                           "unavailable": len(unavailable), "pending": len(pending)},
              "next_request_after_taipei": checkpoint.get("next_request_after_taipei"),
              "measurement_complete": len(measurements) == len(rows),
              "state_saved": state_saved}
    result["collection_complete"] = growth_collection_complete(result, finished)
    result["job_result"] = JobResult(
        job="public-growth", status=(JobStatus.COOLING_DOWN if reason == "rate_limited" else JobStatus.PARTIAL),
        reason=reason, collection_complete=result["collection_complete"],
        state_persisted=False, published=False, requires_publication=True,
        target_slot=today.isoformat(),
    ).to_dict()
    persist(checkpoint, result)
    return result
