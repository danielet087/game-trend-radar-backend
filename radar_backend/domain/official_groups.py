"""Pure official Steam group identity, receipts and cooldown rules."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json

API_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
SOURCE = "Steam ISteamUser/ResolveVanityURL"
TWITCH_SOURCE = "twitch_steam_discovery"
STATUSES = frozenset({
    "resolved", "not_found", "missing_api_key", "api_rate_limited",
    "api_forbidden", "network_error", "api_error", "invalid_response",
})


def stamp(value, *, timezone_type=timezone):
    return value.astimezone(timezone_type.utc).isoformat().replace("+00:00", "Z")


def fingerprint(row, *, decimal_id, json_module=json, hashlib_module=hashlib):
    proof = row.get("twitch_admission")
    proof = {key: value for key, value in proof.items() if key != "source_frontend_commit"} if isinstance(proof, dict) else None
    identity = {"appid": decimal_id(row.get("appid")), "source": row.get("queue_source"),
                "release_date": row.get("release_date"), "twitch_admission": proof}
    return hashlib_module.sha256(json_module.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def completed(checkpoint, aid, *, checked_numeric):
    result = (checkpoint.get("official_results") or {}).get(aid)
    return isinstance(result, dict) and (
        checked_numeric(result.get("official_followers"))
        or checked_numeric(result.get("followers"))
    )


def retry_after(header, now, *, timedelta_type=timedelta, timezone_type=timezone,
                parsedate=parsedate_to_datetime):
    """A valid server deadline wins; malformed values use local API backoff."""
    if not isinstance(header, str):
        return None
    try:
        value = header.strip()
        if value.isascii() and value.isdecimal():
            return now + timedelta_type(seconds=max(1, int(value)))
        observed = parsedate(value)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone_type.utc)
        return observed if observed > now else now + timedelta_type(seconds=1)
    except (TypeError, ValueError, OverflowError):
        return None


def failure_cooldown(prior, status, now, response=None, *, retry_after, stamp,
                     source=SOURCE, timedelta_type=timedelta):
    prior = prior if isinstance(prior, dict) else {}
    old_attempts = prior.get("attempts", 0)
    old_attempts = old_attempts if type(old_attempts) is int and old_attempts >= 0 else 0
    attempts = min(128, old_attempts + 1) if prior.get("status") == status else 1
    minutes = (24 * 60 if status == "api_forbidden" else
               min(60, 5 * (2 ** min(attempts - 1, 4))) if status == "network_error" else
               min(24 * 60, 15 * (2 ** min(attempts - 1, 7))))
    server_until = retry_after((getattr(response, "headers", {}) or {}).get("Retry-After"), now)
    local_until = now + timedelta_type(minutes=minutes)
    until = max(local_until, server_until) if server_until else local_until
    result = {"status": status, "observed_at": stamp(now), "retry_at": stamp(until),
              "attempts": attempts, "source": source,
              "retry_source": "steam_retry_after" if server_until else "group_api_backoff"}
    if response is not None:
        result["http"] = response.status_code
    return result


def apply_batch(checkpoint, batch, *, eligible_appids=None, completed,
                valid_group_id, fingerprint, aware_time, deepcopy_fn=deepcopy,
                statuses=STATUSES, source=SOURCE):
    """Apply metadata receipts against current queue membership and progress."""
    if batch.get("schema_version") != 1 or not isinstance(batch.get("results"), dict):
        raise ValueError("Invalid group resolution batch")
    merged = deepcopy_fn(checkpoint)
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
        if not isinstance(resolution, dict) or resolution.get("status") not in statuses or resolution.get("source") != source:
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
