"""Candidate CLI and production dependency composition."""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from collectors.steam_upcoming import taiwan_today
from radar_backend.adapters.candidate_sources import CandidateSources
from radar_backend.application.candidates import CandidateRuntime, execute_pipeline
from radar_backend.state.candidate_store import CandidateStateStore


def default_runtime() -> CandidateRuntime:
    return CandidateRuntime(
        sources=CandidateSources(), state=CandidateStateStore(),
        today=taiwan_today, utcnow=lambda: datetime.now(timezone.utc), sleep=time.sleep,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="data/steam_candidate_state.json")
    parser.add_argument("--catalog", default="data/steam_candidates.json")
    parser.add_argument("--master", default="data/steam_upcoming_master.json")
    parser.add_argument("--output", default="output/steam_upcoming.json")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--batch-days", type=int, default=365)
    parser.add_argument("--follower-cache", default="data/steam_followers_cache.json")
    parser.add_argument("--checkpoint", default="data/steam_followers_checkpoint.json")
    parser.add_argument("--checkpoint-branch", default="steam-state")
    parser.add_argument("--prefilter-state", default="data/steam_prefilter_state.json")
    parser.add_argument("--prefilter-batch-size", type=int, default=0)
    parser.add_argument("--prefilter-request-interval", type=float, default=0.5)
    parser.add_argument("--max-fresh-requests-per-run", type=int, default=50)
    parser.add_argument("--request-interval", type=float, default=30.0)
    parser.add_argument("--search-interval", type=float, default=1.5)
    return parser

def run(args: argparse.Namespace):
    return execute_pipeline(args, default_runtime())


def main(argv: list[str] | None = None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    main()
