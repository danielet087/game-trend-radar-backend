"""Pure daily growth eligibility and official observation coverage rules.

Calendar windows, source evidence and complete coverage are independent of
HTTP, checkpoint files and the workflow that delivers the result.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from radar_core.domain.twitch_admission import is_twitch_qualified
from radar_core.jobs import JobResult
from radar_backend.domain.official_groups import SOURCE as GROUP_SOURCE, STATUSES as GROUP_STATUSES

TAIPEI = timezone(timedelta(hours=8))


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def eligible(row, today):
    try:
        released = date.fromisoformat(row["release_start"])
        count, aid = row.get("followers"), row["appid"]
        return (isinstance(aid, int) and not isinstance(aid, bool) and aid > 0
                and (is_twitch_qualified(row) or (
                    type(count) is int and (count >= 5000 or count > 3000
                    and row.get("recent_source") in {"tracked_release", "direct_release"})))
                and row.get("release_precision", "day") == "day"
                and today - timedelta(days=30) <= released <= today + timedelta(days=365))
    except (KeyError, TypeError, ValueError):
        return False


def validated_group_resolution(value, *, now=None):
    """Read an official identity receipt without inventing follower evidence.

    A missing identity is authoritative only when Valve actually answered the
    identity request with HTTP 200 / EResult 42. Transport/authentication errors
    remain retryable, regardless of how many times the request failed.
    """
    if not isinstance(value, dict) or value.get("source") != GROUP_SOURCE:
        return None
    status, checked = value.get("status"), timestamp(value.get("checked_at"))
    if (type(status) is not str or status not in GROUP_STATUSES or checked is None
            or type(value.get("attempted")) is not bool):
        return None
    if now is not None and (now.tzinfo is None or checked > now):
        return None
    retry = timestamp(value.get("retry_at"))
    if value.get("retry_at") is not None and (retry is None or retry <= checked):
        return None
    if "http" in value and (type(value["http"]) is not int or not 100 <= value["http"] <= 599):
        return None
    if "api_result" in value and type(value["api_result"]) is not int:
        return None
    if status in {"not_found", "resolved"}:
        if (value["attempted"] is not True or value.get("http") != 200
                or value.get("api_result") != (42 if status == "not_found" else 1)
                or (status == "not_found" and retry is None)):
            return None
    elif status == "missing_api_key":
        if value["attempted"] is not False or "http" in value or "api_result" in value:
            return None
    elif value["attempted"] is not True:
        return None
    return {key: value[key] for key in (
        "status", "checked_at", "source", "attempted", "http", "api_result", "retry_at",
    ) if key in value}


def group_unavailable(resolution, now):
    """A verified missing group is a bounded availability outcome, not zero."""
    receipt = validated_group_resolution(resolution, now=now)
    return (receipt is not None and receipt["status"] == "not_found"
            and timestamp(receipt["retry_at"]) > now)


def _appid_set(rows):
    if not isinstance(rows, list) or any(type(aid) is not int or aid <= 0 for aid in rows):
        return None
    ids = set(rows)
    return ids if len(ids) == len(rows) else None


def growth_collection_complete(result: dict, now: datetime) -> bool:
    """Require coverage by real measurements or evidenced availability outcomes.

    Unavailable rows retain their official identity receipts separately from
    measurements. Pending, expired evidence, changed public cohorts or failed
    delivery never suppress later daily retries.
    """
    if (not isinstance(result, dict) or now.tzinfo is None
            or result.get("reason") != "completed" or result.get("errors") != []
            or result.get("pending", []) != []
            or ("collection_complete" in result and result["collection_complete"] is not True)):
        return False
    # Existing reports predate JobResult and retain the full coverage checks
    # below. Once a producer writes the typed contract, never ignore a failed,
    # unpersisted or unpublished result even if its legacy reason says complete.
    if "job_result" in result:
        try:
            job_result = JobResult.from_dict(result["job_result"])
        except (ValueError, TypeError, KeyError):
            return False
        if (job_result.job != "public-growth" or not job_result.successful
                or not job_result.requires_publication or not job_result.published):
            return False
    eligible = result.get("eligible")
    measurements = result.get("measurements")
    unavailable = result.get("unavailable", [])
    if (not isinstance(eligible, int) or isinstance(eligible, bool) or eligible < 0
            or not isinstance(measurements, list) or not isinstance(unavailable, list)
            or len(measurements) + len(unavailable) != eligible):
        return False
    today = now.astimezone(TAIPEI).date()
    # Version 2 adds an explicit cohort and day. Legacy all-measurement reports
    # remain readable; no legacy receipt can silently acquire an exclusion.
    new_coverage = "coverage_schema_version" in result or bool(unavailable)
    if new_coverage:
        generated = timestamp(result.get("generated_at"))
        cohort = _appid_set(result.get("eligible_appids"))
        if (type(result.get("coverage_schema_version")) is not int
                or result["coverage_schema_version"] != 2
                or cohort is None or len(cohort) != eligible
                or type(result.get("measurement_complete")) is not bool
                or result.get("collection_date_taipei") != today.isoformat()
                or generated is None or generated > now
                or generated.astimezone(TAIPEI).date() != today):
            return False
    elif "eligible_appids" in result:
        cohort = _appid_set(result["eligible_appids"])
        if cohort is None or len(cohort) != eligible:
            return False
    else:
        cohort = None
    if "measurement_complete" in result and (
        type(result["measurement_complete"]) is not bool
        or result["measurement_complete"] != (len(measurements) == eligible)
    ):
        return False
    if "coverage" in result:
        expected = {"eligible": eligible, "measured": len(measurements),
                    "unavailable": len(unavailable), "pending": 0}
        coverage = result["coverage"]
        if (not isinstance(coverage, dict) or coverage != expected
                or any(type(value) is not int for value in coverage.values())):
            return False
    ids = set()
    for row in measurements:
        if not isinstance(row, dict):
            return False
        aid, followers = row.get("appid"), row.get("followers")
        if (not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0 or aid in ids
                or not isinstance(followers, int) or isinstance(followers, bool) or followers < 0):
            return False
        measured_at = timestamp(row.get("at"))
        if (measured_at is None or measured_at > now
                or row.get("source", "steam_community") != "steam_community"
                or measured_at.astimezone(TAIPEI).date() != today):
            return False
        ids.add(aid)
    unavailable_ids = set()
    for row in unavailable:
        if not isinstance(row, dict):
            return False
        aid = row.get("appid")
        if (type(aid) is not int or aid <= 0 or aid in ids
                or "followers" in row or "at" in row
                or row.get("reason") != "official_group_not_found"
                or not group_unavailable(row.get("group_resolution"), now)):
            return False
        ids.add(aid)
        unavailable_ids.add(aid)
    if cohort is not None and ids != cohort:
        return False
    if "published_eligible_appids" in result:
        published_ids = _appid_set(result["published_eligible_appids"])
        if published_ids is None or published_ids != (cohort if cohort is not None else ids):
            return False
    if "published_resolvable_appids" in result:
        resolvable = _appid_set(result["published_resolvable_appids"])
        if resolvable is None or resolvable & unavailable_ids:
            return False
    if "checkpoint_resolvable_appids" in result:
        resolvable = _appid_set(result["checkpoint_resolvable_appids"])
        if resolvable is None or resolvable & unavailable_ids:
            return False
    if "checkpoint_group_resolutions" in result:
        checkpoint_receipts = result["checkpoint_group_resolutions"]
        if not isinstance(checkpoint_receipts, dict):
            return False
        for aid, receipt in checkpoint_receipts.items():
            if (not isinstance(aid, str) or not aid.isascii() or not aid.isdecimal()
                    or int(aid) <= 0 or str(int(aid)) != aid
                    or validated_group_resolution(receipt, now=now) is None):
                return False
        for row in unavailable:
            receipt = checkpoint_receipts.get(str(row["appid"]))
            # The acknowledged checkpoint may contain a concurrent resolution
            # absent from the public catalog. New retryable evidence supersedes
            # an earlier absence; an older receipt cannot acknowledge a newer
            # observation that was never durably saved.
            if (not group_unavailable(receipt, now)
                    or timestamp(receipt["checked_at"])
                    < timestamp(row["group_resolution"]["checked_at"])):
                return False
    return True


def latest_measurement(row, record, shared, now):
    """Choose the latest actual official evidence, never a future observation."""
    if not isinstance(record, dict) or not isinstance(record.get("history", []), list):
        raise ValueError("Malformed per-game insights history")
    options = [dict(at=row.get("follower_checked_at"), followers=row.get("followers")),
               *record.get("history", [])]
    if shared is not None:
        options.append({"at": shared.checked_at, "followers": shared.followers})
    options = [p for p in options if isinstance(p, dict) and timestamp(p.get("at"))
               and timestamp(p["at"]) <= now and type(p.get("followers")) is int
               and p["followers"] >= 0 and p.get("source", "steam_community") == "steam_community"]
    return max(options, key=lambda p: timestamp(p["at"]), default=None)
