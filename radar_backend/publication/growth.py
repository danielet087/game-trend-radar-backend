"""Pure delivery receipt stamping for daily public growth reports."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from radar_core.jobs import JobResult, JobStatus
from radar_backend.domain.growth import TAIPEI, eligible, group_unavailable, growth_collection_complete, timestamp
from radar_backend.domain.official_queue import valid_group_id64
from radar_backend.state.official_followers import OfficialFollowerCache


def _supersede_unavailable(payload, appids):
    if not appids:
        return
    retained, stale = [], []
    for row in payload.get("unavailable", []):
        (stale if isinstance(row, dict) and row.get("appid") in appids else retained).append(row)
    payload["unavailable"] = retained
    audit = payload.setdefault("superseded_unavailable", [])
    for row in stale:
        entry = {**deepcopy(row), "superseded_reason": payload["reason"]}
        if entry not in audit:
            audit.append(entry)


def _current_unavailability(row, payload, now):
    receipt = row.get("group_resolution")
    if not group_unavailable(receipt, now):
        return False
    aid = row.get("appid")
    resolved = set(payload.get("checkpoint_resolvable_appids", [])) | set(payload.get("published_resolvable_appids", []))
    if aid in resolved:
        return False
    receipts = payload.get("checkpoint_group_resolutions")
    if receipts is not None:
        current = receipts.get(str(aid)) if isinstance(receipts, dict) else None
        if (not group_unavailable(current, now)
                or timestamp(current["checked_at"]) < timestamp(receipt["checked_at"])):
            return False
    return True


def _refresh_growth_coverage(payload, appids, now):
    """Count effective outcomes without double-counting retained audit rows."""
    eligible_ids = set(appids)
    today = now.astimezone(TAIPEI).date()
    measured = set()
    for row in payload.get("measurements", []):
        if not isinstance(row, dict):
            continue
        at = timestamp(row.get("at"))
        if (type(row.get("appid")) is int and row["appid"] in eligible_ids
                and type(row.get("followers")) is int and row["followers"] >= 0
                and row.get("source", "steam_community") == "steam_community"
                and at is not None and at <= now and at.astimezone(TAIPEI).date() == today):
            measured.add(row["appid"])
    pending = {row.get("appid") for row in payload.get("pending", []) if isinstance(row, dict)}
    resolved = set(payload.get("checkpoint_resolvable_appids", [])) | set(payload.get("published_resolvable_appids", []))
    unavailable = set()
    for row in payload.get("unavailable", []):
        if not isinstance(row, dict):
            continue
        aid = row.get("appid")
        if (type(aid) is not int or aid not in eligible_ids or aid in measured | pending | resolved
                or not _current_unavailability(row, payload, now)):
            continue
        unavailable.add(aid)
    payload["coverage"] = {"eligible": len(eligible_ids), "measured": len(measured),
                           "unavailable": len(unavailable),
                           "pending": len(eligible_ids - measured - unavailable)}


def growth_checkpoint_payload(result: dict, checkpoint: dict, *, now: datetime | None = None) -> dict:
    """Reconcile frozen availability against the checkpoint actually committed."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware checkpoint delivery clock required")
    payload = deepcopy(result)
    appids = payload.get("eligible_appids")
    if appids is None:
        appids = [row.get("appid") for row in payload.get("measurements", [])
                  if isinstance(row, dict)]
    if (not isinstance(appids, list) or any(type(aid) is not int or aid <= 0 for aid in appids)
            or len(set(appids)) != len(appids)):
        raise ValueError("Verified collection AppIDs required for checkpoint coverage")
    cache = OfficialFollowerCache(checkpoint)
    resolvable = sorted(aid for aid in appids if cache.group_id(aid) is not None)
    receipts = {str(aid): receipt for aid in appids
                if (receipt := cache.group_resolution(aid)) is not None}
    payload["checkpoint_resolvable_appids"] = resolvable
    payload["checkpoint_group_resolutions"] = receipts
    stale_groups = set()
    stale_receipts = set()
    for row in payload.get("unavailable", []):
        if not isinstance(row, dict):
            continue
        aid = row.get("appid")
        if aid in resolvable:
            stale_groups.add(aid)
            continue
        current = receipts.get(str(aid))
        original = row.get("group_resolution") or {}
        checked = timestamp(original.get("checked_at"))
        if (current is None or not group_unavailable(current, now)
                or checked is None or timestamp(current["checked_at"]) < checked):
            stale_receipts.add(aid)
    if stale_groups or stale_receipts:
        payload["collection_complete"] = False
        payload["reason"] = "official_group_now_resolved" if stale_groups else "official_group_resolution_changed"
        pending = payload.setdefault("pending", [])
        existing = {row.get("appid") for row in pending if isinstance(row, dict)}
        for aid in sorted(stale_groups | stale_receipts):
            if aid not in existing:
                pending.append({"appid": aid, "reason": "official_group_now_resolved" if aid in stale_groups
                                else "official_group_resolution_changed"})
        _supersede_unavailable(payload, stale_groups | stale_receipts)
    coverage = deepcopy(payload)
    coverage.pop("job_result", None)
    if payload.get("reason") == "completed" and not growth_collection_complete(coverage, now):
        payload["collection_complete"] = False
        payload["reason"] = "checkpoint_coverage_incomplete"
    _refresh_growth_coverage(payload, appids, now)
    return payload


def growth_publication_payload(result: dict, catalog: dict, now: datetime) -> dict:
    """Bind frozen collection coverage to the actual latest accepted catalog.

    Publishing retained history is useful even when the accepted cohort changed.
    Those bytes do not establish coverage for a newly accepted or newly
    resolvable game, and must not turn off the later daily retry.
    """
    rows = catalog.get("games") if isinstance(catalog, dict) else None
    if (not isinstance(rows, list) or type(catalog.get("count")) is not int
            or catalog["count"] != len(rows)
            or any(not isinstance(row, dict) or type(row.get("appid")) is not int
                   or row["appid"] <= 0 for row in rows)
            or len({row["appid"] for row in rows}) != len(rows)):
        raise ValueError("A complete latest public growth catalog is required")
    if now.tzinfo is None:
        raise ValueError("Timezone-aware growth publication clock required")
    today = now.astimezone(TAIPEI).date()
    current = [row for row in rows if eligible(row, today)]
    published_ids = sorted(row["appid"] for row in current)
    resolvable = []
    for row in current:
        checked = timestamp(row.get("follower_checked_at"))
        if (valid_group_id64(row.get("group_id64") or row.get("official_group_id64"))
                or (type(row.get("followers")) is int and row["followers"] >= 0
                    and checked is not None and checked <= now
                    and checked.astimezone(TAIPEI).date() == today)):
            resolvable.append(row["appid"])
    payload = deepcopy(result)
    for field in ("job_result", "publication_receipt", "checkpoint_receipt"):
        payload.pop(field, None)
    payload["published_eligible_appids"] = published_ids
    payload["published_resolvable_appids"] = sorted(resolvable)
    unavailable_ids = {row.get("appid") for row in payload.get("unavailable", [])
                       if isinstance(row, dict)}
    collected_ids = payload.get("eligible_appids")
    if collected_ids is None:
        collected_ids = [row.get("appid") for row in payload.get("measurements", [])
                         if isinstance(row, dict)]
    stale_group_ids = unavailable_ids & set(resolvable)
    changed = set(collected_ids) != set(published_ids)
    if changed or stale_group_ids:
        payload["collection_complete"] = False
        payload["reason"] = "accepted_catalog_changed" if changed else "official_group_now_resolved"
        pending = payload.setdefault("pending", [])
        existing = {row.get("appid") for row in pending if isinstance(row, dict)}
        for aid in sorted((set(published_ids) - set(collected_ids)) | stale_group_ids):
            if aid not in existing:
                pending.append({"appid": aid, "reason": payload["reason"]})
        _supersede_unavailable(payload, stale_group_ids | (unavailable_ids - set(published_ids)))
    stale_receipts = {row["appid"] for row in payload.get("unavailable", [])
                      if isinstance(row, dict) and type(row.get("appid")) is int
                      and not _current_unavailability(row, payload, now)}
    if stale_receipts:
        payload["collection_complete"] = False
        if payload.get("reason") == "completed":
            payload["reason"] = "official_group_resolution_changed"
        pending = payload.setdefault("pending", [])
        existing = {row.get("appid") for row in pending if isinstance(row, dict)}
        for aid in sorted(stale_receipts):
            if aid not in existing:
                pending.append({"appid": aid, "reason": "official_group_resolution_changed"})
        _supersede_unavailable(payload, stale_receipts)
    payload["publication_coverage_complete"] = growth_collection_complete(payload, now)
    if payload.get("reason") == "completed" and not payload["publication_coverage_complete"]:
        payload["collection_complete"] = False
        payload["reason"] = "publication_coverage_incomplete"
    _refresh_growth_coverage(payload, published_ids, now)
    return payload


def stamp_growth_publication(
    result: dict, now: datetime, *, state_persisted: bool, published: bool,
    target_slot: str | None = None, input_revision: str | None = None,
) -> dict:
    """A publisher may acknowledge delivery only after its Git operation succeeds.

    Collection coverage still comes from the original measurement checks. An
    earlier receipt cannot be reused to make a failed retry look successful.
    """
    if not isinstance(result, dict):
        raise TypeError("Growth result must be an object")
    if type(state_persisted) is not bool or type(published) is not bool:
        raise TypeError("Publication acknowledgements must be booleans")
    stamped = deepcopy(result)
    stamped.pop("job_result", None)
    collection_complete = growth_collection_complete(stamped, now)
    reason = str(stamped.get("reason") or "missing_collection_result")
    if not state_persisted or not published:
        status = JobStatus.FAILED
        reason = "state_persistence_not_confirmed" if not state_persisted else "publication_not_confirmed"
    elif collection_complete:
        status = JobStatus.COMPLETE
    elif reason == "rate_limited":
        status = JobStatus.COOLING_DOWN
    else:
        status = JobStatus.PARTIAL
    stamped["job_result"] = JobResult(
        job="public-growth", status=status, reason=reason,
        collection_complete=collection_complete, state_persisted=state_persisted,
        published=published, requires_publication=True,
        target_slot=target_slot, input_revision=input_revision,
    ).to_dict()
    return stamped


