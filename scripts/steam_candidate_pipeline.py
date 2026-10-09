"""Compatible entry point for the layered Steam candidate pipeline.

Existing workflow flags and imported helpers remain available. Rules live in
radar_backend.domain, phases in application, and I/O in adapters/state. Explicit
composition also keeps existing callers' patched source operations effective.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Support both `python scripts/steam_candidate_pipeline.py` and `python -m`.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from radar_core.jobs import JobResult, JobStatus

from radar_backend.application import candidates as candidate_application
from radar_backend.adapters.candidate_sources import (
    CandidateSources, MAX_PAGES_PER_DAY, PAGE_SIZE, QUERY_INTERVAL_SECONDS,
    SteamUpcomingCollector, UpcomingGame, api_key_from_environment, as_upcoming_game,
    fetch_metadata, build_snapshot, fetch_store_tw_names, enrich_tw_names,
    scan_batch, fetch_store_release_details, apply_store_release_detail,
    filter_confirmed_master_games, merge_partial_segment, excluded_appids,
    is_disallowed, is_twitch_qualified,
    query_one_day as query_source_day,
)
from collectors.steam_upcoming import taiwan_today
from radar_backend.domain.candidates import (
    PRIORITY_THRESHOLD, QUERY_URL, TAIWAN_TZ, candidate_record, fresh_state,
    prefilter_complete, candidate_job_result,
)
from radar_backend.jobs.candidates import build_parser
from radar_backend.state.candidate_store import CandidateStateStore, load_json, save_json, write_json

LOG = logging.getLogger(__name__)


def query_one_day(
    session: requests.Session, api_key: str, day: date, *,
    request_interval: float = QUERY_INTERVAL_SECONDS,
    page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES_PER_DAY,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return query_source_day(
        session, api_key, day, request_interval=request_interval,
        page_size=page_size, max_pages=max_pages,
        candidate_factory=candidate_record, sleep=time.sleep,
    )


def _runtime() -> candidate_application.CandidateRuntime:
    """Resolve explicitly exported adapters at call time for legacy callers."""
    return candidate_application.CandidateRuntime(
        sources=CandidateSources(
            session_factory=requests.Session, api_key=api_key_from_environment,
            query_day=query_one_day, fetch_metadata=fetch_metadata,
            build_snapshot=build_snapshot, fetch_store_tw_names=fetch_store_tw_names,
            enrich_tw_names=enrich_tw_names, scan_batch=scan_batch,
            collector_factory=SteamUpcomingCollector, as_upcoming_game=as_upcoming_game,
            fetch_store_release_details=fetch_store_release_details,
            apply_store_release_detail=apply_store_release_detail,
            filter_confirmed_master_games=filter_confirmed_master_games,
            merge_partial_segment=merge_partial_segment, excluded_appids=excluded_appids,
            is_disallowed=is_disallowed, is_twitch_qualified=is_twitch_qualified,
        ),
        state=CandidateStateStore(load=load_json, save=save_json, write_output=write_json),
        today=taiwan_today, utcnow=lambda: datetime.now(timezone.utc), sleep=time.sleep,
    )


def active_candidate_rows(catalog: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    return candidate_application.active_candidate_rows(catalog, state, _runtime())


def run_discovery(args: argparse.Namespace, state: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    return candidate_application.run_discovery(args, state, catalog, _runtime())


def run_date_precision_phase(state: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    return candidate_application.run_date_precision_phase(state, catalog, _runtime())


def run_prefilter_phase(args: argparse.Namespace, state: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    return candidate_application.run_prefilter_phase(args, state, catalog, _runtime())


def run_follower_batch(args: argparse.Namespace, state: dict[str, Any], catalog: dict[str, Any], master: dict[str, Any]) -> dict[str, Any]:
    return candidate_application.run_follower_batch(args, state, catalog, master, _runtime())


def run(args: argparse.Namespace) -> dict[str, Any]:
    return candidate_application.execute_pipeline(args, _runtime())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    run(build_parser().parse_args())
