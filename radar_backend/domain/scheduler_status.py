"""Pure read-only scheduler queue dashboard projection rules."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

REPOSITORY = "danielet087/game-trend-radar-backend"
TWITCH_SOURCE = "twitch_steam_discovery"
MAX_EVENTS = 100

def utc(value, *, aware_time, datetime_type=datetime, timezone_type=timezone):
    observed = value if isinstance(value, datetime_type) and value.tzinfo is not None else aware_time(value)
    return observed.astimezone(timezone_type.utc).isoformat().replace("+00:00", "Z") if observed else None


def latest_time(values, *, aware_time, utc):
    times = [observed for value in values if (observed := aware_time(value)) is not None]
    return utc(max(times)) if times else None


def next_official_slot(now, cooldown_until=None, *, aware_time, utc, tz,
                       timedelta_type=timedelta):
    """Next eligible hourly opportunity, not a promise of dispatch or success."""
    after = max(now, aware_time(cooldown_until) or now).astimezone(tz)
    candidate = after.replace(minute=0, second=0, microsecond=0)
    if candidate < after:
        candidate += timedelta_type(hours=1)
    for _ in range(25):
        if 3 <= candidate.hour <= 23:
            return utc(candidate)
        candidate += timedelta_type(hours=1)
    raise ValueError("No official hourly slot found")


def public_run(data, *, repository=REPOSITORY):
    """Run links are derived only from persisted numeric GitHub identifiers."""
    run_id = data.get("run_id") or data.get("github_run_id")
    if isinstance(run_id, bool) or not str(run_id or "").isdecimal():
        return {}
    return {"run_id": str(run_id), "run_url": f"https://github.com/{repository}/actions/runs/{run_id}"}


def build_status(checkpoint, frozen_rows, legacy_cp, old_group_rows, eligible,
                 prefilter, official_cache, other_official, *, now,
                 candidate_state=None, twitch_state=None, make_queue, aware_time,
                 utc, latest_time, next_official_slot, public_run, valid_group_id,
                 tz, checkpoint_path, deepcopy_fn=deepcopy, repository=REPOSITORY,
                 twitch_source=TWITCH_SOURCE, max_events=MAX_EVENTS):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Dashboard snapshot requires an aware time")
    # make_queue intentionally reconciles ordinary candidates and parked rows.
    # The dashboard must never persist these projected mutations to its source.
    projected = deepcopy_fn(checkpoint)
    pending, source_status = make_queue(
        projected, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter,
        official_cache, other_official, now=now,
    )
    today = now.astimezone(tz).date().isoformat()
    valid_events = [event for event in checkpoint.get("attempt_events", [])
                    if isinstance(event, dict) and aware_time(event.get("when_taipei")) is not None
                    and aware_time(event.get("when_taipei")) <= now]
    valid_events.sort(key=lambda event: aware_time(event["when_taipei"]))
    latest_attempt = {str(event.get("appid")): event for event in valid_events}
    today_events = [event for event in valid_events
                    if aware_time(event["when_taipei"]).astimezone(tz).date().isoformat() == today]
    cooldowns = [value for value in (checkpoint.get("next_request_after_taipei"),
                                    legacy_cp.get("next_request_after_taipei"))
                 if aware_time(value) is not None]
    cooldown_until = latest_time(cooldowns)
    active_cooldown = bool(cooldown_until and aware_time(cooldown_until) > now)
    last_event = valid_events[-1] if valid_events else {}
    last_batch = checkpoint.get("scheduler_batch")
    last_batch = last_batch if isinstance(last_batch, dict) else {}
    rows_by_id = {str(row.get("appid")): row for row in projected.get("pending_candidates", {}).values()
                  if isinstance(row, dict)}
    rows_by_id.update({str(row.get("appid")): row for row in projected.get("official_results", {}).values()
                       if isinstance(row, dict)})

    def game(row, *, state):
        aid = str(row["appid"])
        attempt = latest_attempt.get(aid) or {}
        result = {
            "appid": int(aid), "name": row.get("name") or f"Steam App {aid}",
            "steam_url": f"https://store.steampowered.com/app/{aid}/",
            "release_date": row.get("release_date"), "source": row.get("queue_source"),
            "priority": row.get("queue_source") == twitch_source, "state": state,
            "group_id64": valid_group_id(row.get("group_id64")),
            "admission_fallback": "twitch" if valid_group_id(row.get("group_id64")) is None else None,
            "fallback_status": ("awaiting_twitch_qualification"
                                if valid_group_id(row.get("group_id64")) is None else None),
            "group_resolution": deepcopy_fn(row.get("group_resolution")) if isinstance(row.get("group_resolution"), dict) else None,
            "last_attempt_at": utc(attempt.get("when_taipei")),
            "last_attempt_status": attempt.get("status"),
        }
        return result

    queue = [{"position": index, **game(row, state=("awaiting_group" if valid_group_id(row.get("group_id64")) is None
                                                  else "cooldown" if active_cooldown else "waiting"))}
             for index, row in enumerate(pending, start=1)]
    parked = []
    for aid in source_status.get("parked_group_xml_appids", []):
        row = projected["unresolved_candidates"][aid]
        parked.append({**game(row, state="parked"), "reason": row["status"],
                       "resolution": row.get("resolution")})
    parked.sort(key=lambda row: (row.get("release_date") or "9999-12-31", row["appid"]))
    events = []
    for event in valid_events[-max_events:]:
        row = rows_by_id.get(str(event.get("appid")), {})
        item = {
            "at": utc(event["when_taipei"]), "kind": "official_attempt",
            "appid": event.get("appid"), "name": row.get("name") or f"Steam App {event.get('appid')}",
            "status": event.get("status"), "http": event.get("http"),
            "source": event.get("queue_source"), "official_followers": event.get("official_followers"),
            "retry_at": utc(event.get("next_request_after_taipei")),
            **public_run(event),
        }
        if event.get("error_type"):
            item["error_type"] = event["error_type"]
        if event.get("manual_cooldown_override") is True:
            item["manual_cooldown_override"] = True
        events.append(item)
    summary = {
        "normal_pending": sum(not row["priority"] for row in queue),
        "twitch_priority_pending": sum(row["priority"] for row in queue),
        "parked": len(parked), "ready_pending": len(queue),
        "followers_ready_pending": sum(row["group_id64"] is not None for row in queue),
        "awaiting_group_pending": sum(row["group_id64"] is None for row in queue),
        "total_pending": len(queue) + len(parked),
        "today_attempts": len(today_events),
        "today_successes": sum(event.get("status") == "ok" for event in today_events),
        "today_429": sum(event.get("http") == 429 for event in today_events),
    }
    source_update_values = [checkpoint.get("created_at_taipei"),
                            *(event.get("when_taipei") for event in valid_events),
                            last_batch.get("last_updated_at")]
    group_update_values = [
        row["group_resolution"].get("checked_at")
        for row in checkpoint.get("pending_candidates", {}).values()
        if isinstance(row, dict) and isinstance(row.get("group_resolution"), dict)
    ]
    api_cooldown = checkpoint.get("group_resolution_api_cooldown")
    if isinstance(api_cooldown, dict):
        group_update_values.append(api_cooldown.get("observed_at"))
    batch = {key: last_batch.get(key) for key in (
        "active", "status", "started_at", "last_updated_at", "finished_at", "stop_reason",
        "last_appid", "last_name", "requests_this_run", "official_new_this_run", "http_429_this_run",
        "manual_cooldown_override",
        "request_limit",
    ) if key in last_batch}
    batch.update(public_run(last_batch))
    return {
        "schema_version": 1, "generated_at": utc(now), "today_taipei": today,
        "source": {
            "repository": repository,
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_updated_at": latest_time(source_update_values),
            "group_resolution_updated_at": latest_time(group_update_values),
            "candidate_state_updated_at": utc((candidate_state or {}).get("updated_at")),
            "prefilter_updated_at": utc(prefilter.get("updated_at")),
            "eligible_screened_at": utc(eligible.get("screened_at")),
            "twitch_import_updated_at": utc((twitch_state or {}).get("updated_at")),
            "last_official_success_at": utc(checkpoint.get("community_last_success_at")),
            "queue_projection": "official_collector_make_queue",
        },
        "source_status": source_status, "summary": summary,
        "group_resolution_api_cooldown": deepcopy_fn(checkpoint.get("group_resolution_api_cooldown")),
        "cooldown": {
            "active": active_cooldown, "until": cooldown_until,
            "reason": ("steam_http_429" if last_event.get("http") == 429
                       else last_event.get("status")) if active_cooldown else None,
            "next_eligible_slot": next_official_slot(now, cooldown_until) if queue else None,
            "schedule_taipei": "03:00–23:00 hourly",
        },
        "batch": batch or None, "queue": queue, "parked": parked, "events": events,
        "events_limit": max_events,
    }
