"""Compose the read-only official queue dashboard using canonical input ports."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from radar_core.domain.twitch_admission import aware_time
from radar_backend.adapters import queue_inputs as worker
from radar_backend.domain import scheduler_status as _status_rules
from radar_backend.application import scheduler_status as _status_application
from radar_backend.domain.scheduler_status import REPOSITORY, TWITCH_SOURCE, MAX_EVENTS
OUTPUT = Path("data/scheduler_queue_status.json")


def utc(value):
    return _status_rules.utc(value, aware_time=aware_time, datetime_type=datetime, timezone_type=timezone)


def latest_time(values):
    return _status_rules.latest_time(values, aware_time=aware_time, utc=utc)


def next_official_slot(now, cooldown_until=None):
    return _status_rules.next_official_slot(now, cooldown_until, aware_time=aware_time, utc=utc, tz=worker.TZ, timedelta_type=timedelta)


def public_run(data):
    return _status_rules.public_run(data, repository=REPOSITORY)


def build_status(checkpoint, frozen_rows, legacy_cp, old_group_rows, eligible,
                 prefilter, official_cache, other_official, *, now,
                 candidate_state=None, twitch_state=None):
    return _status_rules.build_status(
        checkpoint, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter,
        official_cache, other_official, now=now, candidate_state=candidate_state,
        twitch_state=twitch_state, make_queue=worker.make_queue, aware_time=aware_time,
        utc=utc, latest_time=latest_time, next_official_slot=next_official_slot,
        public_run=public_run, valid_group_id=worker.valid_group_id64, tz=worker.TZ,
        checkpoint_path=worker.CHECKPOINT, deepcopy_fn=deepcopy, repository=REPOSITORY,
        twitch_source=TWITCH_SOURCE, max_events=MAX_EVENTS,
    )


def export_status(*, output=OUTPUT, now=None):
    return _status_application.export_status(
        output=output, now=now, clock=worker.clock, read=worker.read,
        is_file=lambda path: path.is_file(), save=worker.save, build_status=build_status,
        checkpoint_path=worker.CHECKPOINT, frozen=worker.FROZEN,
        eligible_path=worker.ELIGIBLE, prefilter_path=worker.PREFILTER,
        official_cache_path=worker.OFFICIAL_CACHE, original_official_path=worker.ORIGINAL_OFFICIAL,
        candidate_state_path=Path("data/steam_candidate_state.json"),
        twitch_state_path=Path("data/twitch_steam_import_state.json"),
    )
