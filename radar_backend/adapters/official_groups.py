"""Compose bounded official group resolution with canonical queue and Core ports."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import time
import requests
from radar_core.domain.twitch_admission import aware_time, decimal_id
from radar_backend.adapters import queue_inputs as worker
from radar_backend.application import official_groups as _group_application
from radar_backend.domain import official_groups as _group_rules
from radar_backend.adapters import steam_official_groups as _group_transport
from radar_backend.domain.official_groups import API_URL, SOURCE, TWITCH_SOURCE, STATUSES


def stamp(value):
    return _group_rules.stamp(value, timezone_type=timezone)


def valid_group_id(value):
    return worker.valid_group_id64(value)


def fingerprint(row):
    return _group_rules.fingerprint(row, decimal_id=decimal_id, json_module=json, hashlib_module=hashlib)


def completed(checkpoint, aid):
    return _group_rules.completed(checkpoint, aid, checked_numeric=worker.checked_numeric)


def retry_after(header, now):
    return _group_rules.retry_after(header, now, timedelta_type=timedelta, timezone_type=timezone, parsedate=parsedate_to_datetime)


def failure_cooldown(prior, status, now, response=None):
    return _group_rules.failure_cooldown(prior, status, now, response, retry_after=retry_after, stamp=stamp, source=SOURCE, timedelta_type=timedelta)


def collect(checkpoint, candidates, *, api_key, now=None, session=None,
            max_requests=20, max_seconds=120, interval=1.0,
            monotonic=time.monotonic, sleep=time.sleep, clock=None):
    return _group_application.collect(
        checkpoint, candidates, api_key=api_key, now=now, session=session,
        max_requests=max_requests, max_seconds=max_seconds, interval=interval,
        monotonic=monotonic, sleep=sleep, clock=clock,
        default_clock=lambda: datetime.now(timezone.utc), stamp=stamp,
        decimal_id=decimal_id, completed=completed, valid_group_id=valid_group_id,
        aware_time=aware_time, fingerprint=fingerprint, failure_cooldown=failure_cooldown,
        request=lambda client, aid, key, remaining: _group_transport.request(
            client, aid, key, remaining, api_url=API_URL),
        session_factory=requests.Session, configure_session=_group_transport.configure_session,
        request_exception=requests.RequestException, math_module=math,
        timedelta_type=timedelta, source=SOURCE, twitch_source=TWITCH_SOURCE,
    )


def apply_batch(checkpoint, batch, *, eligible_appids=None):
    return _group_rules.apply_batch(
        checkpoint, batch, eligible_appids=eligible_appids, completed=completed,
        valid_group_id=valid_group_id, fingerprint=fingerprint, aware_time=aware_time,
        deepcopy_fn=deepcopy, statuses=STATUSES, source=SOURCE,
    )


def current_queue(checkpoint):
    return _group_application.current_queue(
        checkpoint, read=worker.read, make_queue=worker.make_queue,
        frozen=worker.FROZEN, eligible=worker.ELIGIBLE, prefilter=worker.PREFILTER,
        official_cache=worker.OFFICIAL_CACHE, original_official=worker.ORIGINAL_OFFICIAL,
        deepcopy_fn=deepcopy,
    )
