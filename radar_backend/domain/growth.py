"""Pure daily growth eligibility and official observation coverage rules.

Calendar windows, source evidence and complete coverage are independent of
HTTP, checkpoint files and the workflow that delivers the result.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from radar_core.domain.twitch_admission import is_twitch_qualified
from radar_core.jobs import JobResult

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
        count, aid = row["followers"], row["appid"]
        return (isinstance(aid, int) and not isinstance(aid, bool) and aid > 0
                and isinstance(count, int) and not isinstance(count, bool)
                and (is_twitch_qualified(row) or count >= 5000 or count > 3000 and row.get("recent_source") in {"tracked_release", "direct_release"})
                and row.get("release_precision", "day") == "day"
                and today - timedelta(days=30) <= released <= today + timedelta(days=365))
    except (KeyError, TypeError, ValueError):
        return False


def growth_collection_complete(result: dict, now: datetime) -> bool:
    """Partial output stays publishable but cannot suppress later daily retries."""
    if not isinstance(result, dict) or result.get("reason") != "completed" or result.get("errors") != []:
        return False
    # Existing reports predate JobResult and retain the full coverage checks
    # below. Once a producer writes the typed contract, never ignore a failed,
    # unpersisted or unpublished result even if its legacy reason says complete.
    if "job_result" in result:
        try:
            job_result = JobResult.from_dict(result["job_result"])
        except (ValueError, TypeError, KeyError):
            return False
        if job_result.job != "public-growth" or not job_result.successful:
            return False
    eligible = result.get("eligible")
    measurements = result.get("measurements")
    if (not isinstance(eligible, int) or isinstance(eligible, bool) or eligible < 0
            or not isinstance(measurements, list) or len(measurements) != eligible):
        return False
    today = now.astimezone(TAIPEI).date()
    ids = set()
    for row in measurements:
        if not isinstance(row, dict):
            return False
        aid, followers = row.get("appid"), row.get("followers")
        if (not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0 or aid in ids
                or not isinstance(followers, int) or isinstance(followers, bool) or followers < 0):
            return False
        try:
            measured_at = datetime.fromisoformat(str(row.get("at", "")).replace("Z", "+00:00"))
        except ValueError:
            return False
        if (measured_at.tzinfo is None or measured_at > now
                or measured_at.astimezone(TAIPEI).date() != today):
            return False
        ids.add(aid)
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
