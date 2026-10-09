"""Official group receipt CLI using the canonical group and queue composition."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
from radar_backend.adapters import queue_inputs as worker
from radar_backend.adapters.official_groups import collect, apply_batch, current_queue

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("collect", "apply"), required=True)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=120)
    args = parser.parse_args()
    checkpoint, candidates = current_queue(worker.read(worker.CHECKPOINT))
    if args.phase == "collect":
        batch = collect(checkpoint, candidates, api_key=os.environ.get("STEAM_WEB_API_KEY", ""),
                        max_requests=args.max_requests, max_seconds=args.max_seconds)
        worker.save(args.batch, batch)
    else:
        batch = worker.read(args.batch)
        checkpoint = apply_batch(checkpoint, batch, eligible_appids=[row["appid"] for row in candidates])
        worker.save(worker.CHECKPOINT, checkpoint)
    print("OFFICIAL_GROUP_RESOLUTION", args.phase, "requests", batch["requests_this_run"],
          "results", len(batch["results"]), "stop", batch["stop_reason"])

if __name__ == "__main__":
    main()
