"""Bounded retries for Steam metadata APIs and per-game transport failures.

This module does not perform network calls or change catalog eligibility.  A
server-provided Retry-After always takes precedence over a local fallback.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re


METADATA_STAGES = {"steam_store_browse", "steam_appdetails"}


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware retry observation time is required")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _attempt(prior: dict | None, field: str) -> int:
    value = prior.get(field) if isinstance(prior, dict) else None
    return value + 1 if type(value) is int and value >= 0 else 1


def _backoff(first: int, cap: int, attempt: int) -> int:
    # Saturating the exponent also handles malformed or very old huge counts.
    return min(cap, first * 2 ** min(attempt - 1, 12))


def _server_retry_seconds(value, now: datetime) -> int | None:
    if not isinstance(value, str):
        return None
    header = value.strip()
    if re.fullmatch(r"[0-9]+", header):
        try:
            seconds = int(header)
            # Values outside datetime's range cannot produce a usable deadline.
            now + timedelta(seconds=seconds)
            return seconds
        except (ValueError, OverflowError):
            return None
    try:
        deadline = parsedate_to_datetime(header)
        if deadline is None:
            return None
        # HTTP-date's obsolete forms can omit an explicit timezone; HTTP dates
        # are UTC, unlike application-provided naive observation timestamps.
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0, math.ceil((_utc(deadline) - now).total_seconds()))
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def rate_limit_policy(stage: str, retry_after, now: datetime,
                      prior: dict | None = None) -> dict:
    """Return a metadata retry receipt measured when a Steam 429 is observed."""
    now = _utc(now)
    if stage not in METADATA_STAGES:
        raise ValueError("Unknown Steam retry stage")
    attempt = _attempt(prior, "attempts")
    seconds = _server_retry_seconds(retry_after, now)
    source = "steam_retry_after"
    if seconds is None:
        source = "default_backoff"
        seconds = _backoff(300, 1800, attempt)
    return {
        "retry_at": _stamp(now + timedelta(seconds=seconds)),
        "observed_at": _stamp(now), "retry_seconds": seconds,
        "retry_source": source, "retry_after": retry_after,
        "attempts": attempt,
    }


def transient_retry_policy(prior: dict | None, now: datetime) -> dict:
    """Back off ordinary per-game transport/parser failures from five minutes."""
    now = _utc(now)
    attempt = _attempt(prior, "retry_attempts")
    seconds = _backoff(300, 3600, attempt)
    return {
        "retry_attempts": attempt,
        "retry_at": _stamp(now + timedelta(seconds=seconds)),
        "retry_source": "transient_backoff",
    }
