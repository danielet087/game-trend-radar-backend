"""Read-only daily follower workload, separate from collection completion."""
from copy import deepcopy

from radar_backend.domain.official_queue import TAIPEI, aware_time, queue_checked_numeric


def build_plan(checkpoint, frozen_rows, legacy_cp, old_group_rows, eligible,
               prefilter, official_cache, other_official, *, now, pending,
               source_status, make_queue):
    local = now.astimezone(TAIPEI)
    today = local.date().isoformat()
    result = {
        "schema_version": 1, "date_taipei": today, "status": "pending",
        "checked_at": None, "needed": None, "planned_count": None,
        "completed_count": None, "remaining_count": None, "due_now_count": None,
    }
    # A partial scan is unknown, even if its current queue happens to be empty.
    source_times = [aware_time(prefilter.get("updated_at")), aware_time(eligible.get("screened_at"))]
    if (source_status.get("status") != "current_day_prefilter_complete"
            or any(at is None or at > now or at.astimezone(TAIPEI).date() != local.date()
                   for at in source_times)):
        return result
    projected = deepcopy(checkpoint)
    planned, projected_source = make_queue(
        projected, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter,
        official_cache, other_official, now=now,
        refresh_by=local.replace(hour=23, minute=59, second=59, microsecond=999999),
    )
    # This projection only widens the refresh deadline. First checks and
    # Twitch admissions remain outside the daily periodic workload.
    planned_ids = {str(row["appid"]) for row in planned if row.get("follower_recheck") is True
                   and row.get("queue_source") != "twitch_steam_discovery"}
    planned_ids.update(str(aid) for aid in projected_source.get("parked_group_xml_appids", [])
                       if projected["unresolved_candidates"][str(aid)].get("follower_recheck") is True)
    completed = set()
    for aid, row in checkpoint.get("official_results", {}).items():
        at = aware_time(row.get("official_checked_at_taipei"))
        if (row.get("follower_recheck") is True
                and row.get("queue_source") != "twitch_steam_discovery"
                and queue_checked_numeric(row.get("official_followers"))
                and at is not None and at <= now and at.astimezone(TAIPEI).date() == local.date()):
            completed.add(str(aid))
    # Keep today's total after successful items disappear from the live queue.
    planned_ids |= completed
    remaining = planned_ids - completed
    due = {str(row["appid"]) for row in pending if row.get("follower_recheck") is True}
    due.update(str(aid) for aid in source_status.get("parked_group_xml_appids", []))
    result.update(
        status="completed", checked_at=now.isoformat(), needed=bool(planned_ids),
        planned_count=len(planned_ids), completed_count=len(completed),
        remaining_count=len(remaining), due_now_count=len(due & remaining),
    )
    return result
