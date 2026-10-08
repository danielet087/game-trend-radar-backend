"""CLI composition for the daily growth collection use case."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import requests

from radar_backend.adapters.official_followers import OfficialFollowerClient
from radar_backend.application.growth import collect_observations
from radar_backend.state.growth import GrowthProgressWriter, load_growth_inputs
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache

DESCRIPTION = "Daily official Followers observations for accepted games, through release +30d."


def collect(data_dir, output, *, max_requests=120, interval=30, now=None, session=None,
            sleep=time.sleep, checkpoint_path=None, official_cache_path=None,
            legacy_checkpoint_path=None, group_state_path=None, original_checkpoint_path=None, clock=None,
            client_factory=OfficialFollowerClient, cache_factory=OfficialFollowerCache,
            cooldown_factory=CooldownStore, session_factory=requests.Session, monotonic=time.monotonic):
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
    result = collect_observations(inputs.rows, inputs.history, inputs.checkpoint,
                                  client=client, cache=cache, cooldown=cooldown,
                                  clock=clock, sleep=sleep, monotonic=monotonic, persist=writer.save,
                                  max_requests=max_requests, interval=interval,
                                  state_saved=checkpoint_path is not None)
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
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    if args.interval < 30 or not 0 <= args.max_requests <= 120:
        parser.error("Keep at least 30 seconds between requests and at most 120 requests")
    collector(args.data_dir, args.output, max_requests=args.max_requests, interval=args.interval,
            checkpoint_path=args.checkpoint, official_cache_path=args.official_cache,
            legacy_checkpoint_path=args.legacy_checkpoint, group_state_path=args.group_state,
            original_checkpoint_path=args.original_checkpoint)


if __name__ == "__main__":
    main()
