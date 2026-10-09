"""Store-only verification use cases driven by explicit transport ports."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from radar_backend.domain.official_catalog import (
    apply_store_detail_to_result, select_pending_store_results,
)
from radar_backend.domain.official_queue import TAIPEI


def _taipei_clock(clock: Callable) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Store verification clock must include an offset")
    return now.astimezone(TAIPEI)


def verify_store_date_for_result(
    result: dict, session, *, clock: Callable, fetch_store_release_details: Callable,
) -> bool:
    """Recheck the Store after ordinary official Followers qualification."""
    appid = int(result["appid"])
    details = fetch_store_release_details(
        session, [appid], today=_taipei_clock(clock).date(), interval=0.0,
    )
    detail = details.get(appid) or {"exact": False, "status": "unavailable"}
    return apply_store_detail_to_result(result, detail, checked_at=_taipei_clock(clock))


def reverify_pending_store_dates(
    checkpoint: dict, master: dict, limit: int = 25, *, clock: Callable,
    session_factory: Callable, fetch_store_release_details: Callable,
    upsert_qualified_master: Callable, dispatch_content_event: Callable,
) -> int:
    """Retry dates without querying Community Followers or Twitch imports."""
    today = _taipei_clock(clock).date()
    selected = select_pending_store_results(checkpoint, today=today, limit=limit)
    if not selected:
        return 0
    session = session_factory()
    details = fetch_store_release_details(
        session, [int(result["appid"]) for result in selected], today=today, interval=0.5,
    )
    for result in selected:
        detail = details.get(int(result["appid"])) or {"exact": False, "status": "unavailable"}
        exact = apply_store_detail_to_result(result, detail, checked_at=_taipei_clock(clock))
        if exact:
            upsert_qualified_master(master, result)
            dispatch_content_event(checkpoint, result)
    return len(selected)
