"""Resolve official game groups before the existing Followers XML collector.

Collection uses only Steam's ResolveVanityURL Web API. Application patches a
fresh queue and never clears its Community cooldown or verified counts. The
API key is neither logged nor included in receipts, errors or persisted state.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time

import requests

from scripts import experiment_official_daily_catchup_250 as worker
from scripts.twitch_steam_admission import aware_time, decimal_id


API_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
SOURCE = "Steam ISteamUser/ResolveVanityURL"
TWITCH_SOURCE = "twitch_steam_discovery"
STATUSES = frozenset({
    "resolved", "not_found", "missing_api_key", "api_rate_limited",
    "api_forbidden", "network_error", "api_error", "invalid_response",
})


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def valid_group_id(value):
    return worker.valid_group_id64(value)


def fingerprint(row):
    proof = row.get("twitch_admission")
    proof = {key: value for key, value in proof.items() if key != "source_frontend_commit"} if isinstance(proof, dict) else None
    identity = {"appid": decimal_id(row.get("appid")), "source": row.get("queue_source"),
                "release_date": row.get("release_date"), "twitch_admission": proof}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def completed(checkpoint, aid):
    result = (checkpoint.get("official_results") or {}).get(aid)
    return isinstance(result, dict) and (
        worker.checked_numeric(result.get("official_followers"))
        or worker.checked_numeric(result.get("followers"))
    )


def retry_after(header, now):
    """A valid server deadline wins; malformed values use local API backoff."""
    if not isinstance(header, str):
        return None
    try:
        value = header.strip()
        if value.isascii() and value.isdecimal():
            return now + timedelta(seconds=max(1, int(value)))
        observed = parsedate_to_datetime(value)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return observed if observed > now else now + timedelta(seconds=1)
    except (TypeError, ValueError, OverflowError):
        return None


def failure_cooldown(prior, status, now, response=None):
    prior = prior if isinstance(prior, dict) else {}
    old_attempts = prior.get("attempts", 0)
    old_attempts = old_attempts if type(old_attempts) is int and old_attempts >= 0 else 0
    attempts = min(128, old_attempts + 1) if prior.get("status") == status else 1
    minutes = (24 * 60 if status == "api_forbidden" else
               min(60, 5 * (2 ** min(attempts - 1, 4))) if status == "network_error" else
               min(24 * 60, 15 * (2 ** min(attempts - 1, 7))))
    server_until = retry_after((getattr(response, "headers", {}) or {}).get("Retry-After"), now)
    local_until = now + timedelta(minutes=minutes)
    until = max(local_until, server_until) if server_until else local_until
    result = {"status": status, "observed_at": stamp(now), "retry_at": stamp(until),
              "attempts": attempts, "source": SOURCE,
              "retry_source": "steam_retry_after" if server_until else "group_api_backoff"}
    if response is not None:
        result["http"] = response.status_code
    return result


def collect(checkpoint, candidates, *, api_key, now=None, session=None,
            max_requests=20, max_seconds=120, interval=1.0,
            monotonic=time.monotonic, sleep=time.sleep, clock=None):
    if type(max_requests) is not int or type(max_seconds) is not int or not 1 <= max_requests <= 20 or not 1 <= max_seconds <= 120 or not math.isfinite(interval) or interval < 1.0:
        raise ValueError("Group resolver requires <=20 requests, <=120 seconds and >=1s pacing")
    fixed_now = now
    clock = clock or (lambda: fixed_now if fixed_now is not None else datetime.now(timezone.utc))
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
    selected.sort(key=lambda item: 0 if item[1].get("queue_source") == TWITCH_SOURCE else 1)
    prior_cooldown = checkpoint.get("group_resolution_api_cooldown") or {}
    cooldown_until = aware_time(prior_cooldown.get("retry_at")) if isinstance(prior_cooldown, dict) else None

    def receipt(aid, row, status, observed, *, attempted, http=None, retry_at=None, group=None, api_result=None):
        resolution = {"status": status, "checked_at": stamp(observed), "source": SOURCE,
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
                    retry_at=stamp(now + timedelta(hours=1)))
        batch["stop_reason"] = "missing_api_key" if selected else "nothing_due"
    elif cooldown_until is not None and cooldown_until > now:
        batch["stop_reason"] = "api_cooldown"
    elif selected:
        client = session or requests.Session()
        client.headers.update({"User-Agent": "GameTrendRadarOfficialGroupResolver/1.0"})
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
                response = client.get(
                    API_URL, params={"vanityurl": aid, "url_type": 3, "format": "json"},
                    headers={"x-webapi-key": api_key},
                    timeout=min(15.0, max(0.1, remaining / 2)), allow_redirects=False,
                )
            except requests.RequestException:
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
            per_game_retry = stamp(observed + timedelta(hours=24))
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
                    "observed_at": stamp(clock()), "attempts": 0, "source": SOURCE}
                prior_cooldown = batch["api_cooldown_update"]
            else:
                status = "not_found" if code == 42 else "invalid_response" if code == 1 else "api_error"
                if code == 42 and row.get("queue_source") == TWITCH_SOURCE:
                    per_game_retry = stamp(observed + timedelta(hours=3))
                receipt(aid, row, status, observed, attempted=True, http=http,
                        retry_at=per_game_retry, api_result=code)
                if code == 42:
                    batch["api_cooldown_update"] = {"status": "available", "retry_at": None,
                        "observed_at": stamp(clock()), "attempts": 0, "source": SOURCE}
                    prior_cooldown = batch["api_cooldown_update"]
    batch["finished_at"] = stamp(clock())
    return batch


def apply_batch(checkpoint, batch, *, eligible_appids=None):
    """Apply metadata receipts against current queue membership and progress."""
    if batch.get("schema_version") != 1 or not isinstance(batch.get("results"), dict):
        raise ValueError("Invalid group resolution batch")
    merged = deepcopy(checkpoint)
    pending = merged.get("pending_candidates") or {}
    eligible = {str(aid) for aid in eligible_appids} if eligible_appids is not None else None
    for aid, item in batch["results"].items():
        row = pending.get(aid)
        if not isinstance(row, dict) or not isinstance(item, dict) or (eligible is not None and aid not in eligible):
            continue
        if completed(merged, aid) or valid_group_id(row.get("group_id64")):
            continue
        if item.get("candidate_fingerprint") != fingerprint(row):
            continue
        resolution = item.get("group_resolution")
        if not isinstance(resolution, dict) or resolution.get("status") not in STATUSES or resolution.get("source") != SOURCE:
            continue
        checked = aware_time(resolution.get("checked_at"))
        prior = row.get("group_resolution") or {}
        prior_checked = aware_time(prior.get("checked_at")) if isinstance(prior, dict) else None
        if checked is None or (prior_checked is not None and prior_checked >= checked):
            continue
        retry = resolution.get("retry_at")
        if retry is not None and aware_time(retry) is None:
            continue
        group = valid_group_id(item.get("group_id64"))
        if resolution["status"] == "resolved" and group is None:
            continue
        row["group_resolution"] = {key: value for key, value in resolution.items() if key in {
            "status", "checked_at", "source", "attempted", "http", "retry_at", "api_result",
        }}
        if resolution["status"] == "resolved":
            row["group_id64"] = group
    update = batch.get("api_cooldown_update")
    old = merged.get("group_resolution_api_cooldown") or {}
    old = old if isinstance(old, dict) else {}
    if isinstance(update, dict):
        observed, prior_observed = aware_time(update.get("observed_at")), aware_time(old.get("observed_at"))
        deadline = update.get("retry_at")
        if observed is not None and (prior_observed is None or observed > prior_observed) and (
            deadline is None or aware_time(deadline) is not None
        ):
            merged["group_resolution_api_cooldown"] = {key: value for key, value in update.items() if key in {
                "status", "observed_at", "retry_at", "attempts", "source", "retry_source", "http",
            }}
    return merged


def current_queue(checkpoint):
    """Reconcile current inputs without using an old batch to restore candidates."""
    projected = deepcopy(checkpoint)
    rows, status = worker.make_queue(
        projected, worker.read(worker.FROZEN / "source_queue.json"),
        worker.read(worker.FROZEN / "checkpoint.json"), worker.read(worker.FROZEN / "source_unresolved.json"),
        worker.read(worker.ELIGIBLE), worker.read(worker.PREFILTER), worker.read(worker.OFFICIAL_CACHE),
        worker.read(worker.ORIGINAL_OFFICIAL),
    )
    rows += [projected["unresolved_candidates"][aid]
             for aid in status.get("parked_group_xml_appids", [])]
    return projected, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("collect", "apply"), required=True)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=120)
    args = parser.parse_args()
    checkpoint, candidates = current_queue(worker.read(worker.CHECKPOINT))
    if args.phase == "collect":
        batch = collect(checkpoint, candidates, api_key=os.environ.get("STEAM_WEB_API_KEY", ""),
                        max_requests=args.max_requests, max_seconds=args.max_seconds)
        worker.save(args.batch, batch)
    else:
        batch = worker.read(args.batch)
        checkpoint = apply_batch(checkpoint, batch, eligible_appids=[row["appid"] for row in candidates])
        worker.save(worker.CHECKPOINT, checkpoint)
    print("OFFICIAL_GROUP_RESOLUTION", args.phase, "requests", batch["requests_this_run"],
          "results", len(batch["results"]), "stop", batch["stop_reason"])


if __name__ == "__main__":
    main()
