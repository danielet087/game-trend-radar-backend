"""Bounded Steam vanity/provider HTTP lookups for the follower prescreen."""
from __future__ import annotations

import time
from typing import Any, Callable

import requests

from radar_backend.domain.follower_prefilter import (
    GROUP_BASE, RATE_LIMIT_DELAYS, RETRYABLE_HTTP, RETRY_DELAYS,
    parse_bulk_counts, parse_group_id,
)


VANITY_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
BULK_URL = "https://api.steam-groups.com/api/groups/bulk"


def _request_with_retries(
    session: requests.Session, method: str, url: str, *,
    sleep: Callable | None = None,
    request_exception: Any = None,
    retryable_http: set[int] = RETRYABLE_HTTP,
    retry_delays: tuple[int, ...] = RETRY_DELAYS,
    rate_limit_delays: tuple[int, ...] = RATE_LIMIT_DELAYS,
    request_options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Retry temporary failures without emitting request or credential details."""
    sleep = sleep if sleep is not None else time.sleep
    request_exception = request_exception if request_exception is not None else requests.RequestException
    options = kwargs if request_options is None else request_options
    last_error = "temporary network failure"
    for attempt in range(len(retry_delays) + 1):
        try:
            response = (
                session.get(url, **options)
                if method == "GET" else session.post(url, **options)
            )
        except request_exception:
            response = None
        if response is not None:
            code = response.status_code
            if code not in retryable_http:
                return response
            last_error = f"HTTP {code}"
        if attempt >= len(retry_delays):
            break
        delay = (
            rate_limit_delays[attempt]
            if response is not None and response.status_code == 429
            else retry_delays[attempt]
        )
        if response is not None and response.status_code in (429, 503):
            value = getattr(response, "headers", {}).get("Retry-After", "")
            try:
                delay = max(delay, min(120, int(value)))
            except (TypeError, ValueError):
                pass
        sleep(delay)
    raise RuntimeError(
        f"Temporary lookup exhausted retries ({last_error}); "
        "priority cursor unchanged"
    )


def _group_id(
    session: requests.Session, key: str, appid: int, *,
    request: Callable | None = None, url: str = VANITY_URL,
    group_base: int = GROUP_BASE, parser: Callable | None = None,
) -> int | None:
    request = request if request is not None else _request_with_retries
    parser = parser if parser is not None else parse_group_id
    response = request(
        session, "GET", url,
        params={"key": key, "vanityurl": str(appid), "url_type": 3},
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Steam vanity returned HTTP {response.status_code}; priority cursor unchanged"
        )
    try:
        return parser(response.json(), group_base=group_base)
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Unexpected Steam vanity response; priority cursor unchanged") from None


def _bulk_counts(
    session: requests.Session, group_ids: list[int], *,
    request: Callable | None = None, url: str = BULK_URL,
    parser: Callable | None = None,
) -> dict[int, int]:
    if not group_ids:
        return {}
    request = request if request is not None else _request_with_retries
    parser = parser if parser is not None else parse_bulk_counts
    response = request(
        session, "POST", url,
        json={"ids": group_ids, "limit": len(group_ids)}, timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Third-party bulk returned HTTP {response.status_code}; priority cursor unchanged"
        )
    try:
        return parser(response.json(), group_ids)
    except (ValueError, TypeError):
        raise RuntimeError(
            "Third-party bulk response malformed; priority cursor unchanged"
        ) from None
