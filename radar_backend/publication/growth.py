"""Pure delivery receipt stamping for daily public growth reports."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from radar_core.jobs import JobResult, JobStatus
from radar_backend.domain.growth import growth_collection_complete


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


