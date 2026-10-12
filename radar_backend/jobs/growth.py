"""CLI composition for the daily growth collection use case."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import requests

from radar_backend.adapters.official_followers import OfficialFollowerClient
from radar_backend.adapters.official_groups import collect as collect_groups
from radar_backend.adapters.official_groups import apply_api_cooldown
from radar_backend.application.growth import collect_observations
from radar_backend.application.growth_daily import collect_daily_observations
from radar_backend.state.growth import GrowthProgressWriter, load_growth_inputs
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache

DESCRIPTION = "Daily official Followers observations for accepted games, through release +30d."


def collect(data_dir, output, *, max_requests=120, max_seconds=3600, interval=30,
            wait_for_cooldown=False, max_cooldown_wait=3600, now=None, session=None,
            sleep=time.sleep, checkpoint_path=None, official_cache_path=None,
            legacy_checkpoint_path=None, group_state_path=None, original_checkpoint_path=None, clock=None,
            client_factory=OfficialFollowerClient, cache_factory=OfficialFollowerCache,
            cooldown_factory=CooldownStore, session_factory=requests.Session, monotonic=time.monotonic,
            api_key=None, group_collector=collect_groups):
    if clock is None:
        clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    if clock().tzinfo is None:
        raise ValueError("Growth collection needs an aware clock")
    inputs = load_growth_inputs(data_dir, checkpoint_path=checkpoint_path,
                               official_cache_path=official_cache_path,
                               legacy_checkpoint_path=legacy_checkpoint_path,
                               group_state_path=group_state_path,
                               original_checkpoint_path=original_checkpoint_path)
    cache = cache_factory(inputs.checkpoint, *inputs.sources)
    cooldown = cooldown_factory(inputs.checkpoint, inputs.legacy)
    # A malformed retry state must fail before constructing the HTTP client.
    cooldown.deadline()
    http = session if session is not None else session_factory()
    http.headers.update({"User-Agent": "GameTrendRadar/1.0 (+https://github.com/danielet087/game-trend-radar)"})
    client = client_factory(session=http, clock=clock, cooldown=cooldown)
    writer = GrowthProgressWriter(output, checkpoint_path)
    key = os.environ.get("STEAM_WEB_API_KEY", "") if api_key is None else api_key
    group_requests_used = 0
    group_seconds_used = 0.0

    def resolve_groups(checkpoint, candidates, *, now):
        nonlocal group_requests_used, group_seconds_used
        # Reuse the official identity resolver, independently of whether an old
        # Followers observation exists. Admission remains the catalog's job.
        candidates = [{**row, "release_date": row["release_start"],
                       "queue_source": "accepted_steam_growth"} for row in candidates]
        remaining_requests = 20 - group_requests_used
        remaining_seconds = int(max(0, 120 - group_seconds_used))
        if remaining_requests <= 0 or remaining_seconds <= 0:
            # A runner resume does not reset the existing official API budget
            # or invent new identity evidence for candidates not requested.
            return {"schema_version": 1, "generated_at": now.isoformat(),
                    "finished_at": now.isoformat(), "results": {},
                    "requests_this_run": 0, "stop_reason": "growth_group_budget_exhausted"}
        group_started = monotonic()
        batch = group_collector(checkpoint, candidates, api_key=key, now=now,
                                session=http, clock=clock, monotonic=monotonic,
                                sleep=sleep, resolve_for_growth=True,
                                max_requests=remaining_requests, max_seconds=remaining_seconds)
        group_requests_used += batch.get("requests_this_run", 0)
        group_seconds_used += max(0, monotonic() - group_started)
        checkpoint.update(apply_api_cooldown(checkpoint, batch))
        return batch

    collection = collect_daily_observations if wait_for_cooldown else collect_observations
    options = {"max_cooldown_wait": max_cooldown_wait,
               "on_wait": lambda deadline, seconds: print(
                   f"GROWTH_WAIT_SHARED_COOLDOWN until={deadline.isoformat()} remaining_seconds={seconds:.1f}",
                   flush=True)} if wait_for_cooldown else {}
    result = collection(inputs.rows, inputs.history, inputs.checkpoint,
                                  client=client, cache=cache, cooldown=cooldown,
                                  clock=clock, sleep=sleep, monotonic=monotonic, persist=writer.save,
                                  max_requests=max_requests, max_seconds=max_seconds, interval=interval,
                                  state_saved=checkpoint_path is not None,
                                  group_resolver=resolve_groups, **options)
    print(json.dumps({k: v for k, v in result.items() if k not in {"measurements", "events", "pending"}}))
    return result

def main(collector=collect):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=Path("experiments/steam_official_daily_catchup/checkpoint.json"))
    parser.add_argument("--official-cache", type=Path, default=Path("data/steam_followers_cache.json"))
    parser.add_argument("--legacy-checkpoint", type=Path, default=Path("experiments/steam_official_nearfirst_20260922/checkpoint.json"))
    parser.add_argument("--group-state", type=Path, default=Path("data/steam_prefilter_state.json"))
    parser.add_argument("--original-checkpoint", type=Path, default=Path("experiments/steam_official_followers_20260922/checkpoint.json"))
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--max-seconds", type=float, default=3600)
    parser.add_argument("--wait-for-cooldown", action="store_true")
    parser.add_argument("--max-cooldown-wait", type=float, default=3600)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    if (not math.isfinite(args.interval) or args.interval < 30
            or not 0 <= args.max_requests <= 250):
        parser.error("Keep at least 30 seconds between requests and at most 250 requests")
    if not math.isfinite(args.max_seconds) or not 0 < args.max_seconds <= 9000:
        parser.error("Keep a finite collection budget of at most 9000 seconds")
    if not math.isfinite(args.max_cooldown_wait) or not 0 <= args.max_cooldown_wait <= 3600:
        parser.error("Keep a finite cooldown wait budget of at most 3600 seconds")
    collector(args.data_dir, args.output, max_requests=args.max_requests,
            max_seconds=args.max_seconds, interval=args.interval,
            wait_for_cooldown=args.wait_for_cooldown, max_cooldown_wait=args.max_cooldown_wait,
            checkpoint_path=args.checkpoint, official_cache_path=args.official_cache,
            legacy_checkpoint_path=args.legacy_checkpoint, group_state_path=args.group_state,
            original_checkpoint_path=args.original_checkpoint)


if __name__ == "__main__":
    main()
