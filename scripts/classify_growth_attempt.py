"""Recognize a delivered Steam cooldown without acknowledging daily completion."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path

from radar_core.jobs import JobResult, JobStatus
from radar_backend.domain.growth import TAIPEI, growth_collection_complete, timestamp
from radar_backend.publication.steam import read_json
from radar_backend.state.official_followers import save_state


def _ids(values):
    if (not isinstance(values, list)
            or any(type(value) is not int or value <= 0 for value in values)
            or len(set(values)) != len(values)):
        return None
    return set(values)


def deferred_cooldown(original, delivered, now):
    """Fail closed unless every missing observation is an evidenced cooldown.

    The original collector report is frozen before publication reconciliation.
    A changed cohort, expired identity receipt or failed delivery cannot inherit
    that original cooldown and make a genuinely failed attempt look routine.
    """
    if (not isinstance(original, dict) or not isinstance(delivered, dict)
            or now.tzinfo is None or now.utcoffset() is None):
        return False
    try:
        before = JobResult.from_dict(original.get("job_result"))
        after = JobResult.from_dict(delivered.get("job_result"))
    except (ValueError, TypeError, KeyError):
        return False
    for report, job in ((original, before), (delivered, after)):
        if (report.get("reason") != "rate_limited" or report.get("errors") != []
                or report.get("collection_complete") is not False
                or report.get("measurement_complete") is not False
                or job.job != "public-growth" or job.status is not JobStatus.COOLING_DOWN
                or job.reason != "rate_limited" or job.collection_complete or job.successful
                or not job.requires_publication):
            return False
    if (before.state_persisted or before.published
            or not after.state_persisted or not after.published):
        return False
    generated = timestamp(original.get("generated_at"))
    retry_at = timestamp(original.get("next_request_after_taipei"))
    if (generated is None or generated > now
            or generated.astimezone(TAIPEI).date() != now.astimezone(TAIPEI).date()
            or retry_at is None or retry_at <= now
            or delivered.get("next_request_after_taipei") != original.get("next_request_after_taipei")):
        return False
    budget = original.get("daily_budget")
    if (not isinstance(budget, dict)
            or type(budget.get("max_cooldown_wait")) not in {int, float}
            or type(budget.get("cooldown_wait_seconds")) not in {int, float}
            or budget["max_cooldown_wait"] != 0 or budget["cooldown_wait_seconds"] != 0
            or budget.get("stop_reason") != "cooldown_exceeds_wait_budget"):
        return False
    cohort = _ids(original.get("eligible_appids"))
    if (cohort is None or type(original.get("eligible")) is not int
            or original["eligible"] != len(cohort)
            or type(original.get("coverage_schema_version")) is not int
            or original["coverage_schema_version"] != 2
            or original.get("collection_date_taipei") != now.astimezone(TAIPEI).date().isoformat()
            or _ids(delivered.get("published_eligible_appids")) != cohort):
        return False
    for field in ("eligible", "eligible_appids", "coverage_schema_version", "collection_date_taipei",
                  "generated_at", "measurements", "unavailable", "pending", "coverage"):
        if delivered.get(field) != original.get(field):
            return False
    pending = original.get("pending")
    if (not isinstance(pending, list) or not pending
            or any(not isinstance(row, dict) or row.get("reason") != "community_cooldown"
                   for row in pending)):
        return False
    missing = _ids([row.get("appid") for row in pending])
    if missing is None or not missing <= cohort:
        return False
    measurements, unavailable = original.get("measurements"), original.get("unavailable")
    if not isinstance(measurements, list) or not isinstance(unavailable, list):
        return False
    expected = {"eligible": len(cohort), "measured": len(measurements),
                "unavailable": len(unavailable), "pending": len(missing)}
    coverage = original.get("coverage")
    if (not isinstance(coverage, dict) or coverage != expected
            or any(type(value) is not int for value in coverage.values())):
        return False
    # Reuse the real evidence validator for the accounted subset. This is only
    # a validation input: neither report nor its completion flags are changed.
    accounted = cohort - missing
    valid_evidence = {
        "reason": "completed", "errors": [], "pending": [],
        "generated_at": original["generated_at"], "coverage_schema_version": 2,
        "collection_date_taipei": original["collection_date_taipei"],
        "eligible": len(accounted), "eligible_appids": sorted(accounted),
        "measurements": measurements, "unavailable": unavailable,
        "measurement_complete": len(measurements) == len(accounted),
    }
    if not growth_collection_complete(valid_evidence, now):
        return False
    events = original.get("events")
    if (not isinstance(events, list)
            or any(not isinstance(event, dict)
                   or event.get("status") not in {"ok", "rate_limited", "cooldown_no_request"}
                   for event in events)):
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--steps-succeeded", choices=("true", "false"), required=True)
    args = parser.parse_args(argv)
    deferred = False
    # Missing/corrupt inputs must not skip the independent completion gate.
    if args.steps_succeeded == "true":
        original = read_json(args.original)
        delivered = read_json(args.report)
        now = datetime.now(timezone.utc)
        deferred = deferred_cooldown(original, delivered, now)
        delivered.pop("schedule_deferred", None)
        if deferred:
            delivered["schedule_deferred"] = {
                "reason": "steam_community_cooldown",
                "retry_after": original["next_request_after_taipei"],
                "checked_at": now.isoformat(),
                "next_attempt": "next_scheduled_six_hour_check",
            }
        save_state(args.report, delivered)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"deferred={str(deferred).lower()}\n")
    print("STEAM_GROWTH_DEFERRED" if deferred else "STEAM_GROWTH_COMPLETION_GATE_REQUIRED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
