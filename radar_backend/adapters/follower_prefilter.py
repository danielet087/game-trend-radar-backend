"""Canonical third-party priority prescreen composition; counts stay unofficial."""
from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any
import requests
from radar_backend.domain import follower_prefilter as _prefilter_rules
from radar_backend.application import follower_prefilter as _prefilter_application
from radar_backend.adapters import steam_follower_prefilter as _prefilter_transport
from radar_backend.domain.follower_prefilter import (
    GROUP_BASE, PRIORITY_THRESHOLD, STEAM_PUBLIC_THRESHOLD, DEFAULT_BATCH_SIZE,
    RETRYABLE_HTTP, RETRY_DELAYS, RATE_LIMIT_DELAYS,
)
from radar_backend.adapters.steam_follower_prefilter import VANITY_URL, BULK_URL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_with_retries(
    session: requests.Session, method: str, url: str, **kwargs: Any,
) -> requests.Response:
    return _prefilter_transport._request_with_retries(
        session, method, url, sleep=time.sleep, request_exception=requests.RequestException,
        retryable_http=RETRYABLE_HTTP, retry_delays=RETRY_DELAYS,
        rate_limit_delays=RATE_LIMIT_DELAYS, request_options=kwargs,
    )


def _group_id(session: requests.Session, key: str, appid: int) -> int | None:
    return _prefilter_transport._group_id(
        session, key, appid, request=_request_with_retries, url=VANITY_URL,
        group_base=GROUP_BASE, parser=_prefilter_rules.parse_group_id,
    )


def _bulk_counts(
    session: requests.Session, group_ids: list[int],
) -> dict[int, int]:
    return _prefilter_transport._bulk_counts(
        session, group_ids, request=_request_with_retries, url=BULK_URL,
        parser=_prefilter_rules.parse_bulk_counts,
    )


def scan_batch(
    catalog: list[dict[str, Any]],
    prefilter: dict[str, Any],
    *,
    steam_api_key: str,
    initial_index: int,
    limit: int = DEFAULT_BATCH_SIZE,
    request_interval: float = 0.5,
    session: requests.Session | None = None,
) -> dict[str, int | bool]:
    return _prefilter_application.scan_batch(
        catalog, prefilter, steam_api_key=steam_api_key, initial_index=initial_index,
        limit=limit, request_interval=request_interval, session=session,
        session_factory=requests.Session, group_id=_group_id, bulk_counts=_bulk_counts,
        clock_stamp=_now, sleep=time.sleep, priority_threshold=PRIORITY_THRESHOLD,
    )


def pending_priorities(
    catalog: list[dict[str, Any]],
    prefilter: dict[str, Any],
    official_cache: dict[str, Any],
    *,
    min_start_index: int,
    max_candidates: int = 50,
) -> list[dict[str, Any]]:
    return _prefilter_rules.pending_priorities(catalog, prefilter, official_cache, min_start_index=min_start_index, max_candidates=max_candidates)
