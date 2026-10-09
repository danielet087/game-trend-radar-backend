"""Official hourly CLI validation and process completion semantics."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import time

import requests

from radar_core.jobs import JobStatus
from radar_backend.application.official_followers import (
    OfficialBatchServices, OfficialPaths, run_official_batch,
)
from radar_backend.domain.official_queue import TAIPEI, make_queue


def clock():
    return datetime.now(TAIPEI)


def default_paths():
    """Keep the established durable files and historical cohort intact."""
    return OfficialPaths(
        frozen=Path("experiments/steam_official_nearfirst_20260922"),
        eligible=Path("data/steam_candidates_eligible.json"),
        prefilter=Path("data/steam_prefilter_state.json"),
        official_cache=Path("data/steam_followers_cache.json"),
        original_official=Path("experiments/steam_official_followers_20260922/checkpoint.json"),
        checkpoint=Path("experiments/steam_official_daily_catchup/checkpoint.json"),
        master=Path("data/steam_upcoming_master.json"),
        output=Path("output/steam_official_daily_catchup"),
        cohort="steam_official_daily_catchup_dynamic_v1",
    )


def default_services(paths):
    """Compose production sources, Store/catalog/content use cases and state."""
    from radar_backend.adapters import official_catalog as catalog
    from radar_backend.adapters.official_followers import OfficialFollowerClient
    from radar_backend.state import official_checkpoint as checkpoint
    from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache

    def queue(*sources):
        return make_queue(
            *sources, now=clock(),
            is_twitch_queue_candidate=catalog.is_twitch_queue_candidate,
            cached_follower=catalog.cached_follower,
        )

    publisher = None

    def begin(checkpoint_state, master_state):
        nonlocal publisher
        publisher = checkpoint.begin_persistence(
            checkpoint=paths.checkpoint, master=paths.master,
            checkpoint_state=checkpoint_state, master_state=master_state, clock=clock,
        )

    def persist():
        output = catalog.dashboard_output()
        return checkpoint.git_push(
            checkpoint=paths.checkpoint, master=paths.master, output=output,
            export_status=catalog.export_status,
            rebase=lambda: checkpoint.rebase_checkpoint(
                output=output, export_status=catalog.export_status,
            ),
            publisher=publisher,
        )

    return OfficialBatchServices(
        read=checkpoint.read, save=checkpoint.save, exists=checkpoint.exists,
        clock=clock, monotonic=time.monotonic, sleep=time.sleep,
        session_factory=requests.Session, make_queue=queue, git_push=persist,
        reverify_pending_store_dates=lambda data, master: catalog.reverify_pending_store_dates(
            data, master, clock=clock, session_factory=requests.Session,
        ),
        retry_pending_content_dispatches=lambda data: catalog.retry_pending_content_dispatches(data, clock=clock),
        verify_store_date_for_result=lambda result, session: catalog.verify_store_date_for_result(result, session, clock=clock),
        upsert_qualified_master=lambda master, result: catalog.upsert_qualified_master(master, result, clock=clock),
        dispatch_content_event=lambda data, result: catalog.dispatch_content_event(data, result, clock=clock),
        follower_client_factory=OfficialFollowerClient,
        follower_cache_factory=OfficialFollowerCache, cooldown_factory=CooldownStore,
        begin_persistence=begin,
    )


def manual_cooldown_override(requested):
    """Only an explicitly manual dispatch may probe before a retry deadline."""
    if not requested:
        return False
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("SCHEDULE_TRIGGER_SOURCE") != "manual"):
        raise ValueError("Skipping cooldown requires an explicit manual workflow dispatch")
    return True


def run_job(*, paths, services, argv=None, cooldown_override=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--max-seconds", type=int, default=3450)
    parser.add_argument("--save-every", type=int, default=10)
    parser.add_argument("--skip-cooldown", action="store_true",
                        help="Explicit manual dispatch only: probe one queued game during cooldown")
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests <= 250 or args.interval < 8.0:
        raise ValueError("Max 250 single-file requests, min 8 seconds apart")
    if not 120 <= args.max_seconds <= 3500 or not 1 <= args.save_every <= 20:
        raise ValueError("Invalid duration/save interval")
    skip_cooldown = (cooldown_override or manual_cooldown_override)(args.skip_cooldown)
    if skip_cooldown:
        args.max_requests = 1
    result = run_official_batch(
        args, paths=paths, services=services, skip_cooldown=skip_cooldown,
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        target_slot=os.environ.get("SCHEDULE_TARGET_SLOT") or None,
    )
    if result.status is JobStatus.FAILED:
        raise SystemExit(1)
    return result


def main(argv=None):
    paths = default_paths()
    return run_job(paths=paths, services=default_services(paths), argv=argv)


if __name__ == "__main__":
    main()
