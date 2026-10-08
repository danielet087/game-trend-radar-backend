"""Merge growth observations into the latest queue checkpoint and record delivery.

Git publication remains in the workflow. This module only prepares a three-way
state merge and stamps acknowledgements supplied after successful Git pushes.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from scripts.external_schedule import stamp_growth_publication
from scripts.steam_official_followers import (
    aware_time, checked_numeric, read_state, save_state, valid_group_id64,
)

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


def stamp_report(path, *, state_persisted, published, target_slot=None, input_revision=None, now=None):
    result = read_state(path) if path.exists() else {"reason": "missing_collection_result"}
    stamped = stamp_growth_publication(
        result, now or datetime.now(timezone.utc), state_persisted=state_persisted,
        published=published, target_slot=target_slot or None, input_revision=input_revision or None,
    )
    save_state(path, stamped)
    return stamped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("merge")
    for flag in ("latest", "baseline", "observed", "report", "output"):
        merge.add_argument(f"--{flag}", type=Path, required=True)
    stamp = commands.add_parser("stamp")
    stamp.add_argument("--report", type=Path, required=True)
    stamp.add_argument("--state-persisted", choices=("true", "false"), required=True)
    stamp.add_argument("--published", choices=("true", "false"), required=True)
    stamp.add_argument("--target-slot", default="")
    stamp.add_argument("--input-revision", default="")
    args = parser.parse_args()
    if args.command == "merge":
        merged = merge_growth_checkpoint(
            read_state(args.latest), read_state(args.baseline), read_state(args.observed), read_state(args.report),
        )
        save_state(args.output, merged)
    else:
        stamp_report(args.report, state_persisted=args.state_persisted == "true",
                     published=args.published == "true", target_slot=args.target_slot,
                     input_revision=args.input_revision)


if __name__ == "__main__":
    main()
