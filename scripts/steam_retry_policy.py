"""Compatibility entry points for pure Steam metadata retry rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re

from radar_backend.domain import steam_retry as _rules

METADATA_STAGES = _rules.METADATA_STAGES


def _utc(value: datetime) -> datetime:
    return _rules._utc(value, datetime_type=datetime, timezone_type=timezone)


def _stamp(value: datetime) -> str:
    return _rules._stamp(value, utc=_utc)


def _attempt(prior: dict | None, field: str) -> int:
    return _rules._attempt(prior, field)


def _backoff(first: int, cap: int, attempt: int) -> int:
    return _rules._backoff(first, cap, attempt)


def _server_retry_seconds(value, now: datetime) -> int | None:
    return _rules._server_retry_seconds(
        value, now, regex_module=re, timedelta_type=timedelta, utc=_utc,
        parsedate=parsedate_to_datetime, timezone_type=timezone, math_module=math,
    )


def rate_limit_policy(stage: str, retry_after, now: datetime,
                      prior: dict | None = None) -> dict:
    return _rules.rate_limit_policy(
        stage, retry_after, now, prior, utc=_utc, metadata_stages=METADATA_STAGES,
        attempt=_attempt, server_retry_seconds=_server_retry_seconds,
        backoff=_backoff, stamp=_stamp, timedelta_type=timedelta,
    )


def transient_retry_policy(prior: dict | None, now: datetime) -> dict:
    return _rules.transient_retry_policy(
        prior, now, utc=_utc, attempt=_attempt, backoff=_backoff,
        stamp=_stamp, timedelta_type=timedelta,
    )
