"""Three-way merge growth evidence without overwriting remote queue changes."""
from __future__ import annotations

from copy import deepcopy
from datetime import timezone
from radar_backend.domain.official_queue import aware_time, checked_numeric, valid_group_id64
from radar_backend.domain.growth import validated_group_resolution
from radar_backend.domain.official_groups import SOURCE as GROUP_SOURCE, STATUSES as GROUP_STATUSES
from radar_backend.state.official_followers import GROWTH_GROUPS, OfficialFollowerCache

COMMUNITY_FIELDS = (
    "rate_limit_count", "temporary_error_count", "next_request_after_taipei",
    "community_cooldown", "community_last_success_at",
)
FAILURE_STATUSES = {"rate_limited", "access_or_server_error", "transport_or_xml_error"}


def _timestamp(value, field):
    if value is None:
        return None
    parsed = aware_time(value)
    if parsed is None:
        raise ValueError(f"Malformed growth checkpoint {field}")
    return parsed


def _validate(state):
    if not isinstance(state, dict):
        raise ValueError("Growth checkpoint must be an object")
    observations = state.get("official_growth_observations", {})
    if not isinstance(observations, dict):
        raise ValueError("Malformed official growth observations")
    for key, row in observations.items():
        if (not isinstance(row, dict) or type(row.get("appid")) is not int
                or row["appid"] <= 0 or str(row["appid"]) != key
                or not checked_numeric(row.get("official_followers"))
                or valid_group_id64(row.get("group_id64")) is None
                or row.get("official_source") != "Steam Community XML memberCount"):
            raise ValueError("Unverified official growth observation")
        if _timestamp(row.get("official_checked_at_taipei"), "observation time") is None:
            raise ValueError("Missing official growth observation time")
    resolutions = state.get(GROWTH_GROUPS, {})
    if not isinstance(resolutions, dict):
        raise ValueError("Malformed growth group resolutions")
    for key, row in resolutions.items():
        if (not isinstance(row, dict) or type(row.get("appid")) is not int
                or row["appid"] <= 0 or str(row["appid"]) != key
                or "followers" in row or "official_followers" in row):
            raise ValueError("Unverified growth group resolution identity")
        receipt = validated_group_resolution(row.get("group_resolution"))
        if (receipt is None
                or (receipt["status"] == "resolved" and valid_group_id64(row.get("group_id64")) is None)
                or (receipt["status"] != "resolved" and row.get("group_id64") is not None)):
            raise ValueError("Unverified growth group resolution")
    for field in ("rate_limit_count", "temporary_error_count"):
        if field in state and not checked_numeric(state[field]):
            raise ValueError(f"Malformed growth checkpoint {field}")
    _timestamp(state.get("next_request_after_taipei"), "retry deadline")
    _timestamp(state.get("community_last_success_at"), "last success")
    cooldown = state.get("community_cooldown")
    if cooldown is not None:
        if not isinstance(cooldown, dict):
            raise ValueError("Malformed Community cooldown")
        for field in ("retry_at", "observed_at", "updated_at"):
            _timestamp(cooldown.get(field), f"cooldown {field}")
    api_cooldown = state.get("group_resolution_api_cooldown")
    if api_cooldown is not None:
        if (not isinstance(api_cooldown, dict) or api_cooldown.get("source") != GROUP_SOURCE
                or api_cooldown.get("status") not in GROUP_STATUSES | {"available"}
                or _timestamp(api_cooldown.get("observed_at"), "Group API observed time") is None):
            raise ValueError("Malformed growth Group API cooldown")
        observed = _timestamp(api_cooldown["observed_at"], "Group API observed time")
        retry = _timestamp(api_cooldown.get("retry_at"), "Group API retry time")
        if ((retry is not None and retry <= observed)
                or (api_cooldown["status"] == "available" and retry is not None)
                or ("attempts" in api_cooldown and not checked_numeric(api_cooldown["attempts"]))):
            raise ValueError("Malformed growth Group API cooldown deadline")


def _merge_group_evidence(merged, latest, baseline, observed):
    baseline_rows = baseline.get(GROWTH_GROUPS, {})
    remote_cache = OfficialFollowerCache(latest)
    for key, row in observed.get(GROWTH_GROUPS, {}).items():
        if row == baseline_rows.get(key):
            continue
        receipt = row["group_resolution"]
        # A metadata failure cannot remove a concurrently resolved group or
        # a GroupID preserved with an actual official count.
        if receipt["status"] != "resolved" and remote_cache.group_id(row["appid"]) is not None:
            continue
        remote = remote_cache.group_resolution(row["appid"])
        if remote is not None and aware_time(remote["checked_at"]) >= aware_time(receipt["checked_at"]):
            continue
        prior = latest.get(GROWTH_GROUPS, {}).get(key) or {}
        merged.setdefault(GROWTH_GROUPS, {})[key] = {**deepcopy(prior), **deepcopy(row)}
        if receipt["status"] != "resolved":
            merged[GROWTH_GROUPS][key].pop("group_id64", None)
    update = observed.get("group_resolution_api_cooldown")
    if update is None or update == baseline.get("group_resolution_api_cooldown"):
        return
    remote = latest.get("group_resolution_api_cooldown") or {}
    checked, remote_checked = aware_time(update["observed_at"]), aware_time(remote.get("observed_at"))
    if remote_checked is None or checked > remote_checked:
        merged["group_resolution_api_cooldown"] = {**deepcopy(remote), **deepcopy(update)}


def _deadline(state):
    cooldown = state.get("community_cooldown") or {}
    values = [state.get("next_request_after_taipei"), cooldown.get("retry_at")]
    parsed = [aware_time(value) for value in values if value is not None]
    return max(parsed, default=None)


def _failure_time(state):
    cooldown = state.get("community_cooldown") or {}
    return max((aware_time(cooldown[key]) for key in ("updated_at", "observed_at")
                if cooldown.get(key) is not None), default=None)


def _community(state):
    return {field: state[field] for field in COMMUNITY_FIELDS if field in state}


def merge_growth_checkpoint(latest, baseline, observed, report):
    """Keep remote queue edits and apply only observations and Community state.

The baseline makes clearing a cooldown safe only when its remote value is still
the value the collector read. Conflicts use actual request timestamps; a stale
success cannot erase a newer failure, and a stale failure cannot undo success.
"""
    for state in (latest, baseline, observed):
        _validate(state)
    if not isinstance(report, dict) or not isinstance(report.get("events", []), list):
        raise ValueError("Malformed growth report")
    merged = deepcopy(latest)
    _merge_group_evidence(merged, latest, baseline, observed)
    baseline_rows = baseline.get("official_growth_observations", {})
    for key, row in observed.get("official_growth_observations", {}).items():
        if row == baseline_rows.get(key):
            continue
        remote = latest.get("official_growth_observations", {}).get(key)
        if (remote is None or aware_time(row["official_checked_at_taipei"])
                > aware_time(remote["official_checked_at_taipei"])):
            merged.setdefault("official_growth_observations", {})[key] = {
                **deepcopy(remote or {}), **deepcopy(row),
            }

    changed = {field: deepcopy(observed[field]) for field in COMMUNITY_FIELDS
               if field in observed and observed[field] != baseline.get(field)}
    if not changed:
        return merged
    if _community(latest) == _community(baseline):
        merged.update(changed)
        return merged

    failures = []
    for event in report.get("events", []):
        if not isinstance(event, dict):
            raise ValueError("Malformed growth event")
        if event.get("status") in FAILURE_STATUSES:
            when = _timestamp(event.get("observed_at"), "request time")
            if when is None:
                raise ValueError("Missing failed request time")
            failures.append(when)
    observed_failure = max(failures, default=_failure_time(observed))
    observed_success = aware_time(observed.get("community_last_success_at"))
    latest_success = aware_time(latest.get("community_last_success_at"))
    latest_failure = _failure_time(latest)
    deadline = _deadline(latest)
    success_is_newest = (observed_success is not None
                         and (observed_failure is None or observed_success >= observed_failure))
    if success_is_newest:
        if latest_success is not None and latest_success > observed_success:
            return merged
        if latest_failure is not None and latest_failure > observed_success:
            return merged
        # Temporary failures in older workers lack an event timestamp. Retain
        # their active remote deadline rather than guessing that it is older.
        if latest_failure is None and deadline is not None and deadline > observed_success:
            return merged
        if (deadline is not None and deadline > observed_success
                and deadline != _deadline(baseline) and latest_failure == _failure_time(baseline)):
            return merged
        merged.update(changed)
        return merged
    if observed_failure is None:
        # An interrupted output may lack its request receipt. Preserve any
        # concurrent cooldown and keep the longest evidenced retry deadline.
        baseline_success = aware_time(baseline.get("community_last_success_at"))
        if latest_success is not None and (baseline_success is None or latest_success > baseline_success):
            return merged
        observed_deadline = _deadline(observed)
        if observed_deadline is not None and (deadline is None or observed_deadline > deadline):
            merged["next_request_after_taipei"] = observed_deadline.isoformat()
            for field in ("rate_limit_count", "temporary_error_count"):
                if field in changed:
                    merged[field] = max(latest.get(field, 0), observed[field])
        return merged
    if latest_success is not None and latest_success >= observed_failure:
        return merged

    observed_deadline = _deadline(observed)
    deadlines = [value for value in (deadline, observed_deadline) if value is not None]
    if deadlines:
        retry_at = max(deadlines)
        merged["next_request_after_taipei"] = retry_at.isoformat()
        remote_cooldown = latest.get("community_cooldown")
        observed_cooldown = observed.get("community_cooldown")
        if observed_cooldown is not None and (latest_failure is None or observed_failure >= latest_failure):
            cooldown = {**deepcopy(remote_cooldown or {}), **deepcopy(observed_cooldown)}
        else:
            cooldown = deepcopy(remote_cooldown)
        if cooldown is not None:
            cooldown["retry_at"] = retry_at.astimezone(timezone.utc).isoformat()
            merged["community_cooldown"] = cooldown
    for field in ("rate_limit_count", "temporary_error_count"):
        if field in changed:
            merged[field] = max(latest.get(field, 0), observed[field])
    return merged

