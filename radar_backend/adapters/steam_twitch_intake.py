"""Bounded Steam metadata HTTP requests with the shared service cooldown policy."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

STORE_BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"


class RateLimited(RuntimeError):
    def __init__(self, stage: str, retry_seconds: int, *, policy: dict | None = None):
        super().__init__("Steam HTTP 429")
        self.stage = stage
        self.retry_seconds = retry_seconds
        self.policy = policy


def request(session, url: str, *, params: dict | None = None, timeout: float = 25,
            now: datetime | None = None, clock=None, prior_cooldown: dict | None = None,
            rate_limit_policy, store_browse=STORE_BROWSE,
            datetime_type=datetime, timezone_type=timezone, urlsplit_fn=urlsplit,
            rate_limited_type=RateLimited):
    hostname = (urlsplit_fn(url).hostname or "").lower()
    if hostname == "steamcommunity.com" or hostname.endswith(".steamcommunity.com"):
        raise RuntimeError("Community requests belong to the official Followers queue")
    response = session.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        stage = "steam_store_browse" if url == store_browse else "steam_appdetails"
        observed = clock() if clock is not None else (now or datetime_type.now(timezone_type.utc))
        policy = rate_limit_policy(stage, response.headers.get("Retry-After"), observed, prior_cooldown)
        raise rate_limited_type(stage, policy["retry_seconds"], policy=policy)
    response.raise_for_status()
    return response

