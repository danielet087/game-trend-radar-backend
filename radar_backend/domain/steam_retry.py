"""Pure bounded retry rules for Steam metadata and per-game failures."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re
METADATA_STAGES = {'steam_store_browse', 'steam_appdetails'}

def _utc(value: datetime, *, datetime_type=datetime, timezone_type=timezone) -> datetime:
    if not isinstance(value, datetime_type) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('An aware retry observation time is required')
    return value.astimezone(timezone_type.utc)

def _stamp(value: datetime, *, utc=_utc) -> str:
    return utc(value).isoformat().replace('+00:00', 'Z')

def _attempt(prior: dict | None, field: str) -> int:
    value = prior.get(field) if isinstance(prior, dict) else None
    return value + 1 if type(value) is int and value >= 0 else 1

def _backoff(first: int, cap: int, attempt: int) -> int:
    return min(cap, first * 2 ** min(attempt - 1, 12))

def _server_retry_seconds(
    value,
    now: datetime,
    *,
    regex_module = re,
    timedelta_type = timedelta,
    utc = _utc,
    parsedate = parsedate_to_datetime,
    timezone_type = timezone,
    math_module = math,
) -> int | None:
    if not isinstance(value, str):
        return None
    header = value.strip()
    if regex_module.fullmatch('[0-9]+', header):
        try:
            seconds = int(header)
            now + timedelta_type(seconds=seconds)
            return seconds
        except (ValueError, OverflowError):
            return None
    try:
        deadline = parsedate(header)
        if deadline is None:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone_type.utc)
        return max(0, math_module.ceil((utc(deadline) - now).total_seconds()))
    except (ValueError, TypeError, OverflowError, IndexError):
        return None

def rate_limit_policy(
    stage: str,
    retry_after,
    now: datetime,
    prior: dict | None = None,
    *,
    utc = _utc,
    metadata_stages = METADATA_STAGES,
    attempt = _attempt,
    server_retry_seconds = _server_retry_seconds,
    backoff = _backoff,
    stamp = _stamp,
    timedelta_type = timedelta,
) -> dict:
    """Return a metadata retry receipt measured when a Steam 429 is observed."""
    now = utc(now)
    if stage not in metadata_stages:
        raise ValueError('Unknown Steam retry stage')
    attempt = attempt(prior, 'attempts')
    seconds = server_retry_seconds(retry_after, now)
    source = 'steam_retry_after'
    if seconds is None:
        source = 'default_backoff'
        seconds = backoff(300, 1800, attempt)
    return {'retry_at': stamp(now + timedelta_type(seconds=seconds)), 'observed_at': stamp(now), 'retry_seconds': seconds, 'retry_source': source, 'retry_after': retry_after, 'attempts': attempt}

def transient_retry_policy(
    prior: dict | None,
    now: datetime,
    *,
    utc = _utc,
    attempt = _attempt,
    backoff = _backoff,
    stamp = _stamp,
    timedelta_type = timedelta,
) -> dict:
    """Back off ordinary per-game transport/parser failures from five minutes."""
    now = utc(now)
    attempt = attempt(prior, 'retry_attempts')
    seconds = backoff(300, 3600, attempt)
    return {'retry_attempts': attempt, 'retry_at': stamp(now + timedelta_type(seconds=seconds)), 'retry_source': 'transient_backoff'}
