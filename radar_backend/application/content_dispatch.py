"""Official content delivery and checkpoint-backed retry use cases."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from radar_backend.domain import content_dispatch as domain


@dataclass(frozen=True)
class ContentDispatchServices:
    clock: Callable
    configuration: Callable
    send: Callable
    emit: Callable


def dispatch_content_event(checkpoint: dict, result: dict, *, services: ContentDispatchServices,
                           signature: Callable | None = None, persist: Callable | None = None) -> str:
    gate = domain.qualification_status(result)
    if gate is not None:
        return gate
    configuration = services.configuration()
    if not configuration.configured:
        return "not_configured"

    signature = signature if signature is not None else domain.content_dispatch_signature
    aid = str(int(result["appid"]))
    event_signature = signature(result)
    registry = checkpoint.setdefault("content_dispatches", {})
    prior = registry.get(aid) or {}
    if prior.get("signature") == event_signature and prior.get("status") == "dispatched":
        return "already_dispatched"

    payload = domain.content_dispatch_payload(
        result, source_repository=configuration.source_repository, signature=event_signature,
    )
    attempted = services.clock().isoformat()
    if persist is not None:
        registry[aid] = {
            "signature": event_signature, "status": "pending",
            "target_repository": configuration.target_repository,
            "event_type": domain.EVENT_TYPE, "attempted_at_taipei": attempted,
        }
        # An external event may only refer to an acknowledged master/result.
        persist(checkpoint)
        registry = checkpoint.setdefault("content_dispatches", {})
        if (registry.get(aid, {}).get("signature") == event_signature
                and registry[aid].get("status") == "dispatched"):
            return "already_dispatched"
        authoritative = (checkpoint.get("official_results") or {}).get(aid)
        if (not isinstance(authoritative, dict)
                or domain.qualification_status(authoritative) is not None
                or signature(authoritative) != event_signature):
            # A concurrent newer observation won the DB merge. Retry that
            # accepted version later instead of dispatching the stale payload.
            return "superseded"
    delivery = services.send(configuration, payload)
    if delivery.accepted:
        registry[aid] = {
            "signature": event_signature,
            "status": "dispatched",
            "target_repository": configuration.target_repository,
            "event_type": domain.EVENT_TYPE,
            "dispatched_at_taipei": attempted,
        }
        if persist is not None:
            persist(checkpoint)
        services.emit("CONTENT_DISPATCH_OK", aid, event_signature, flush=True)
        return "dispatched"

    registry[aid] = {
        "signature": event_signature,
        "status": "failed",
        "target_repository": configuration.target_repository,
        **({"error_type": delivery.error_type} if delivery.error_type is not None
           else {"http": delivery.http}),
        "attempted_at_taipei": attempted,
    }
    if persist is not None:
        persist(checkpoint)
    detail = delivery.error_type if delivery.error_type is not None else delivery.http
    services.emit("CONTENT_DISPATCH_FAILED", aid, detail, flush=True)
    return "failed"


def retry_pending_content_dispatches(checkpoint: dict, limit: int = 25, *, dispatch: Callable,
                                     signature: Callable | None = None) -> int:
    retried = 0
    for result in domain.retry_candidates(checkpoint, limit, signature=signature):
        dispatch(checkpoint, result)
        retried += 1
    return retried
