"""Merge validated Twitch discoveries into the existing official Followers queue.

No Community request is made here.  Queue metadata is separate from verified
Followers results; retaining a discovery never invents a numeric result.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from scripts.screen_steam_candidates_before_followers import is_explicit_sex_game
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.twitch_steam_admission import aware_time, decimal_id, is_twitch_qualified, normalize_twitch_admission, resolve_store_release_day, has_taiwan_store_date_authority
from radar_backend.domain import twitch_official_queue as _queue_rules
TAIPEI = ZoneInfo('Asia/Taipei')
TWITCH_QUEUE_SOURCE = _queue_rules.TWITCH_QUEUE_SOURCE
TWITCH_QUEUE_PRIORITY = _queue_rules.TWITCH_QUEUE_PRIORITY
WITHDRAW_REASONS = _queue_rules.WITHDRAW_REASONS
FOLLOWER_FIELDS = _queue_rules.FOLLOWER_FIELDS

def _now(value: datetime) -> datetime:
    return _queue_rules._now(value, datetime_type=datetime)

def _exact_day(value: object) -> date | None:
    return _queue_rules._exact_day(value, date_type=date)

def _valid_descriptors(metadata: dict) -> bool:
    return _queue_rules._valid_descriptors(metadata, is_disallowed=is_disallowed)

def is_twitch_queue_candidate(row: object, now: datetime | None=None) -> bool:
    return _queue_rules.is_twitch_queue_candidate(
        row,
        now,
        decimal_id=decimal_id,
        normalize_twitch_admission=normalize_twitch_admission,
        exact_day=_exact_day,
        valid_descriptors=_valid_descriptors,
        disallowed_current=lambda metadata: is_disallowed(metadata, excluded_appids()),
        is_explicit_sex_game=is_explicit_sex_game,
        aware_time=aware_time,
        checked_now=_now,
        resolve_store_release_day=resolve_store_release_day,
        has_taiwan_store_date_authority=has_taiwan_store_date_authority,
        is_twitch_qualified=is_twitch_qualified,
        taipei=TAIPEI,
        timedelta_type=timedelta,
        queue_source=TWITCH_QUEUE_SOURCE,
    )

def _restore_normal(pending: dict, aid: str, row: dict) -> None:
    return _queue_rules._restore_normal(
        pending,
        aid,
        row,
        decimal_id=decimal_id,
        deepcopy_fn=deepcopy,
        queue_source=TWITCH_QUEUE_SOURCE,
    )

def sync_twitch_queue(checkpoint: dict, batch: dict, now: datetime) -> dict:
    return _queue_rules.sync_twitch_queue(
        checkpoint,
        batch,
        now,
        checked_now=_now,
        decimal_id=decimal_id,
        is_twitch_queue_candidate=is_twitch_queue_candidate,
        restore_normal=_restore_normal,
        normalize_twitch_admission=normalize_twitch_admission,
        deepcopy_fn=deepcopy,
        queue_source=TWITCH_QUEUE_SOURCE,
        queue_priority=TWITCH_QUEUE_PRIORITY,
        withdraw_reasons=WITHDRAW_REASONS,
        follower_fields=FOLLOWER_FIELDS,
    )
