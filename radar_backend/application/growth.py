"""Collect daily observations through injected official source and state ports.

This use case owns run ordering, request budgets and partial progress. It does
not create an HTTP session, choose file paths or acknowledge publication.
"""
from __future__ import annotations

from datetime import timezone
from radar_core.jobs import JobResult, JobStatus
from radar_backend.domain.growth import TAIPEI, eligible, latest_measurement, timestamp
from radar_backend.domain.official_queue import OfficialObservation


def collect_observations(rows, history, checkpoint, *, client, cache, cooldown,
                         clock, sleep, monotonic, persist, max_requests=120,
                         interval=30, state_saved=False):
    now = clock()
    if now.tzinfo is None:
        raise ValueError("Growth collection needs an aware clock")
    today = now.astimezone(TAIPEI).date()
    # Validate the retry state before considering any network operation.
    cooldown.deadline()
    rows = [row for row in rows if eligible(row, today)]

    def latest(row):
        record = history.get(str(row["appid"])) or {}
        shared = cache.latest(row["appid"], clock(), expected_group=cache.group_id(row["appid"]))
        return latest_measurement(row, record, shared, clock())

    rows.sort(key=lambda row: (latest(row) or {}).get("at") or "")
    measurements, errors, pending, events = [], [], [], []
    attempts = reused = 0
    previous_start = None
    started = monotonic()
    reason = "completed"

    def save_progress():
        persist(checkpoint, {"measurements": measurements, "errors": errors,
                             "pending": pending, "reason": reason})

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
            pending.append({"appid": aid, "reason": "awaiting_group_resolution"})
            continue
        if cooldown.blocked(clock()):
            reason = "rate_limited"
            pending.append({"appid": aid, "reason": "community_cooldown"})
            continue
        if attempts >= max_requests or monotonic() - started > 3600:
            reason = "bounded_run"
            pending.append({"appid": aid, "reason": "bounded_run"})
            continue
        if previous_start is not None:
            sleep(max(0, interval - (monotonic() - previous_start)))
        previous_start = monotonic()
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
            accounted = {item["appid"] for item in pending}
            for later in rows:
                if later["appid"] not in measured | accounted:
                    recent = latest(later)
                    if recent and timestamp(recent["at"]).astimezone(TAIPEI).date() == today:
                        measurements.append({"appid": later["appid"], "followers": recent["followers"],
                                             "at": recent["at"], "source": "steam_community"})
                        reused += 1
                    else:
                        pending.append({"appid": later["appid"], "reason": "source_unavailable"})
            break
    if reason == "completed" and pending:
        reason = "awaiting_group_resolution"
    save_progress()
    result = {"generated_at": clock().isoformat(), "eligible": len(rows),
              "requests": attempts, "reused": reused, "errors": errors, "reason": reason,
              "pending": pending, "events": events, "measurements": measurements,
              "next_request_after_taipei": checkpoint.get("next_request_after_taipei"),
              "collection_complete": len(measurements) == len(rows) and not errors and not pending,
              "state_saved": state_saved}
    result["job_result"] = JobResult(
        job="steam_public_growth", status=(JobStatus.COOLING_DOWN if reason == "rate_limited" else JobStatus.PARTIAL),
        reason=reason, collection_complete=result["collection_complete"],
        state_persisted=False, published=False, requires_publication=True,
        target_slot=today.isoformat(),
    ).to_dict()
    persist(checkpoint, result)
    return result
