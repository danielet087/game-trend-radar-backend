"""Bounded official group collection and fresh queue projection use cases.

All transport, input, timing and parsing operations are provided by the caller.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import math

from radar_backend.domain.official_groups import SOURCE, TWITCH_SOURCE

def collect(checkpoint, candidates, *, api_key, now=None, session=None,
            max_requests=20, max_seconds=120, interval=1.0,
            monotonic, sleep, clock=None, default_clock, stamp, decimal_id,
            completed, valid_group_id, aware_time, fingerprint, failure_cooldown,
            request, session_factory, configure_session, request_exception,
            math_module=math, timedelta_type=timedelta, source=SOURCE,
            twitch_source=TWITCH_SOURCE):
    if type(max_requests) is not int or type(max_seconds) is not int or not 1 <= max_requests <= 20 or not 1 <= max_seconds <= 120 or not math_module.isfinite(interval) or interval < 1.0:
        raise ValueError("Group resolver requires <=20 requests, <=120 seconds and >=1s pacing")
    fixed_now = now
    clock = clock or (lambda: fixed_now if fixed_now is not None else default_clock())
    now = now or clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Group resolver requires aware timestamps")
    started = monotonic()
    deadline = started + max_seconds
    batch = {"schema_version": 1, "generated_at": stamp(now), "results": {},
             "requests_this_run": 0, "stop_reason": "nothing_due"}
    pending = checkpoint.get("pending_candidates") or {}
    selected = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        aid = decimal_id(candidate.get("appid"))
        if aid is None or aid in seen or completed(checkpoint, aid):
            continue
        seen.add(aid)
        # The latest checkpoint wins over a stale projected candidate.
        row = pending.get(aid) if isinstance(pending.get(aid), dict) else candidate
        if valid_group_id(row.get("group_id64")):
            continue
        resolution = row.get("group_resolution") or {}
        future_retry = aware_time(resolution.get("retry_at")) if isinstance(resolution, dict) else None
        if future_retry is not None and future_retry > now and not (
            api_key and resolution.get("status") == "missing_api_key"
        ):
            continue
        selected.append((aid, row))
    # Keep formal queue ordering within each source class.
    selected.sort(key=lambda item: 0 if item[1].get("queue_source") == twitch_source else 1)
    prior_cooldown = checkpoint.get("group_resolution_api_cooldown") or {}
    cooldown_until = aware_time(prior_cooldown.get("retry_at")) if isinstance(prior_cooldown, dict) else None

    def receipt(aid, row, status, observed, *, attempted, http=None, retry_at=None, group=None, api_result=None):
        resolution = {"status": status, "checked_at": stamp(observed), "source": source,
                      "attempted": attempted}
        if http is not None:
            resolution["http"] = http
        if retry_at is not None:
            resolution["retry_at"] = retry_at
        if type(api_result) is int:
            resolution["api_result"] = api_result
        item = {"candidate_fingerprint": fingerprint(row), "group_resolution": resolution}
        if group is not None:
            item["group_id64"] = group
        batch["results"][aid] = item

    if not api_key:
        for aid, row in selected[:max_requests]:
            receipt(aid, row, "missing_api_key", now, attempted=False,
                    retry_at=stamp(now + timedelta_type(hours=1)))
        batch["stop_reason"] = "missing_api_key" if selected else "nothing_due"
    elif cooldown_until is not None and cooldown_until > now:
        batch["stop_reason"] = "api_cooldown"
    elif selected:
        client = session or session_factory()
        configure_session(client)
        last_start = None
        batch["stop_reason"] = "request_limit" if len(selected) > max_requests else "complete"
        for aid, row in selected[:max_requests]:
            if last_start is not None:
                delay = max(0, interval - (monotonic() - last_start))
                if delay:
                    sleep(min(delay, max(0, deadline - monotonic())))
            remaining = deadline - monotonic()
            if remaining <= 0:
                batch["stop_reason"] = "time_budget"
                break
            last_start = monotonic()
            observed = clock()
            batch["requests_this_run"] += 1
            try:
                response = request(client, aid, api_key, remaining)
            except request_exception:
                cooldown = failure_cooldown(prior_cooldown, "network_error", clock())
                receipt(aid, row, "network_error", observed, attempted=True, retry_at=cooldown["retry_at"])
                batch.update(api_cooldown_update=cooldown, stop_reason="network_error")
                break
            http = response.status_code
            if http == 429 or http in (401, 403) or http >= 500:
                status = "api_rate_limited" if http == 429 else "api_forbidden" if http in (401, 403) else "api_error"
                cooldown = failure_cooldown(prior_cooldown, status, clock(), response)
                receipt(aid, row, status, observed, attempted=True, http=http, retry_at=cooldown["retry_at"])
                batch.update(api_cooldown_update=cooldown, stop_reason=status)
                break
            per_game_retry = stamp(observed + timedelta_type(hours=24))
            if http != 200:
                receipt(aid, row, "api_error", observed, attempted=True, http=http, retry_at=per_game_retry)
                continue
            try:
                payload = response.json()
                result = payload.get("response") if isinstance(payload, dict) else None
            except (ValueError, TypeError):
                result = None
            if not isinstance(result, dict) or type(result.get("success")) is not int:
                receipt(aid, row, "invalid_response", observed, attempted=True, http=http, retry_at=per_game_retry)
                continue
            code = result["success"]
            group = valid_group_id(result.get("steamid"))
            if code in (15, 84):  # Valve EResult AccessDenied / RateLimitExceeded.
                status = "api_forbidden" if code == 15 else "api_rate_limited"
                cooldown = failure_cooldown(prior_cooldown, status, clock(), response)
                receipt(aid, row, status, observed, attempted=True, http=http,
                        retry_at=cooldown["retry_at"], api_result=code)
                batch.update(api_cooldown_update=cooldown, stop_reason=status)
                break
            if code == 1 and group is not None:
                receipt(aid, row, "resolved", observed, attempted=True, http=http, group=group, api_result=code)
                batch["api_cooldown_update"] = {"status": "available", "retry_at": None,
                    "observed_at": stamp(clock()), "attempts": 0, "source": source}
                prior_cooldown = batch["api_cooldown_update"]
            else:
                status = "not_found" if code == 42 else "invalid_response" if code == 1 else "api_error"
                if code == 42 and row.get("queue_source") == twitch_source:
                    per_game_retry = stamp(observed + timedelta_type(hours=3))
                receipt(aid, row, status, observed, attempted=True, http=http,
                        retry_at=per_game_retry, api_result=code)
                if code == 42:
                    batch["api_cooldown_update"] = {"status": "available", "retry_at": None,
                        "observed_at": stamp(clock()), "attempts": 0, "source": source}
                    prior_cooldown = batch["api_cooldown_update"]
    batch["finished_at"] = stamp(clock())
    return batch


def current_queue(checkpoint, *, read, make_queue, frozen, eligible, prefilter,
                  official_cache, original_official, deepcopy_fn=deepcopy):
    """Reconcile current inputs without using an old batch to restore candidates."""
    projected = deepcopy_fn(checkpoint)
    rows, status = make_queue(
        projected, read(frozen / "source_queue.json"),
        read(frozen / "checkpoint.json"), read(frozen / "source_unresolved.json"),
        read(eligible), read(prefilter), read(official_cache),
        read(original_official),
    )
    rows += [projected["unresolved_candidates"][aid]
             for aid in status.get("parked_group_xml_appids", [])]
    return projected, rows
