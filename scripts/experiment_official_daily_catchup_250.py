"""Dynamic near-release official Followers catch-up, one GitHub-scheduled hourly batch.

Inputs: persisted September 22 missing-source cohort plus eligible candidates
from a genuinely fresh daily follower prefilter. A stale/disabled daily scan is
reported honestly; never represented as the current day's full coverage.

Uses Steam Community official XML memberCount, NOT Store appdetails,
wishlist count, Community online count or third-party estimates.
Does NOT edit the production follower cache, candidate progress or frontend.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from radar_core.jobs import JobResult, JobStatus

# Permit the historical direct-script entry alongside python -m.
if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_backend.application.official_followers import (
    OfficialBatchServices, OfficialPaths, official_job_result as _official_job_result,
)
from radar_backend.domain.official_queue import (
    make_queue as _make_queue, preserve_cooldown_deadline,
    queue_checked_numeric as checked_numeric, utc_date_as_taipei, valid_date,
)
from radar_backend.jobs.official_followers import manual_cooldown_override, run_job
from radar_backend.state import official_checkpoint as _checkpoint_state
from radar_backend.application import official_catalog as _catalog_application
from radar_backend.application import content_dispatch as _dispatch_application
from radar_backend.domain import official_catalog as _catalog_rules
from radar_backend.domain import content_dispatch as _dispatch_rules
from radar_backend.adapters.github_content_dispatch import compose_services as _dispatch_services

from scripts.steam_master_date_gate import fetch_store_release_details
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.import_twitch_steam_discoveries import cached_follower
from scripts.twitch_official_queue import is_twitch_queue_candidate
from scripts.twitch_steam_admission import aware_time
from scripts.steam_official_followers import (
    GROUP_BASE, CooldownStore, FollowerOutcome, OfficialFollowerCache,
    OfficialFollowerClient, group_to_gid, steam_429_cooldown, valid_group_id64,
)

ROOT = Path("experiments/steam_official_daily_catchup")
FROZEN = Path("experiments/steam_official_nearfirst_20260922")
ELIGIBLE = Path("data/steam_candidates_eligible.json")
PREFILTER = Path("data/steam_prefilter_state.json")
OFFICIAL_CACHE = Path("data/steam_followers_cache.json")
ORIGINAL_OFFICIAL = Path("experiments/steam_official_followers_20260922/checkpoint.json")
OUT = Path("output/steam_official_daily_catchup")
CHECKPOINT = ROOT / "checkpoint.json"
MASTER = Path("data/steam_upcoming_master.json")
TZ = ZoneInfo("Asia/Taipei")
COHORT = "steam_official_daily_catchup_dynamic_v1"
_PERSISTENCE = None


def clock():
    return datetime.now(TZ)


def read(path):
    return _checkpoint_state.read(path)


def save(path, data):
    return _checkpoint_state.save(path, data)


def upsert_qualified_master(master, result):
    """Keep call-time ledger/clock ports for the historical public helper."""
    if not _catalog_rules.qualifies_for_master(result):
        return False
    blocked = excluded_appids()
    return _catalog_rules.upsert_qualified_master(
        master, result, now=datetime.now(timezone.utc),
        blocked=blocked, is_disallowed=is_disallowed,
    )


def content_dispatch_signature(result):
    return _dispatch_rules.content_dispatch_signature(result)


def dispatch_content_event(cp, result):
    return _dispatch_application.dispatch_content_event(
        cp, result,
        services=_dispatch_services(clock, os.environ, requests.post, emit=print),
        signature=content_dispatch_signature,
    )


def verify_store_date_for_result(result, session):
    return _catalog_application.verify_store_date_for_result(
        result, session, clock=clock,
        fetch_store_release_details=fetch_store_release_details,
    )


def reverify_pending_store_dates(cp, master, limit=25):
    return _catalog_application.reverify_pending_store_dates(
        cp, master, limit, clock=clock, session_factory=requests.Session,
        fetch_store_release_details=fetch_store_release_details,
        upsert_qualified_master=upsert_qualified_master,
        dispatch_content_event=dispatch_content_event,
    )


def retry_pending_content_dispatches(cp, limit=25):
    return _dispatch_application.retry_pending_content_dispatches(
        cp, limit, dispatch=dispatch_content_event,
        signature=content_dispatch_signature,
    )


def rebase_checkpoint():
    from scripts.export_scheduler_queue_status import OUTPUT, export_status
    return _checkpoint_state.rebase_checkpoint(output=OUTPUT, export_status=export_status)


def git_push():
    from scripts.export_scheduler_queue_status import OUTPUT, export_status
    return _checkpoint_state.git_push(
        checkpoint=CHECKPOINT, master=MASTER, output=OUTPUT,
        export_status=export_status, rebase=rebase_checkpoint,
        publisher=_PERSISTENCE,
    )


def begin_persistence(checkpoint_state, master_state):
    global _PERSISTENCE
    _PERSISTENCE = _checkpoint_state.begin_persistence(
        checkpoint=CHECKPOINT, master=MASTER, checkpoint_state=checkpoint_state,
        master_state=master_state, clock=clock,
    )


def official_job_result(report, *, state_persisted):
    return _official_job_result(
        report, state_persisted=state_persisted,
        target_slot=os.environ.get("SCHEDULE_TARGET_SLOT") or None,
    )


def make_queue(cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter, official_cache, other_official, *, now=None):
    return _make_queue(
        cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter,
        official_cache, other_official, now=now or clock(),
        is_twitch_queue_candidate=is_twitch_queue_candidate,
        cached_follower=cached_follower,
    )


def main():
    """Historical composition root; evaluate callbacks at invocation for callers.

    Store date verification, master promotion and content dispatch use thin
    compatibility wrappers that compose the shared application and adapter ports.
    Queue, Community transport, cache/cooldown, persistence and batch coordination
    already live in their layers rather than behind renamed script modules.
    """
    paths = OfficialPaths(
        frozen=FROZEN, eligible=ELIGIBLE, prefilter=PREFILTER,
        official_cache=OFFICIAL_CACHE, original_official=ORIGINAL_OFFICIAL,
        checkpoint=CHECKPOINT, master=MASTER, output=OUT, cohort=COHORT,
    )
    services = OfficialBatchServices(
        read=read, save=save, exists=_checkpoint_state.exists,
        clock=clock, monotonic=time.monotonic,
        sleep=time.sleep, session_factory=requests.Session, make_queue=make_queue,
        git_push=git_push, reverify_pending_store_dates=reverify_pending_store_dates,
        retry_pending_content_dispatches=retry_pending_content_dispatches,
        verify_store_date_for_result=verify_store_date_for_result,
        upsert_qualified_master=upsert_qualified_master,
        dispatch_content_event=dispatch_content_event,
        follower_client_factory=OfficialFollowerClient,
        follower_cache_factory=OfficialFollowerCache, cooldown_factory=CooldownStore,
        begin_persistence=begin_persistence,
    )
    run_job(paths=paths, services=services, cooldown_override=manual_cooldown_override)


if __name__ == "__main__":
    main()
