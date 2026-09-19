"""Bounded, resumable phase 3: ONLY Steam XML for third-party >=4000 games.

No discovery, third-party requests or XML for missing/below-4000 groups.
Commits backend state/cache/master after each batch and publishes ONLY verified
Steam >=5000 games to the public frontend. Requires a completed prefilter.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from scripts.steam_candidate_pipeline import run
from scripts.update_steam_daily import load_json

LOG = logging.getLogger(__name__)
STATE = Path("data/steam_candidate_state.json")
PREFILTER = Path("data/steam_prefilter_state.json")
CATALOG = Path("data/steam_candidates.json")
OUTPUT = Path("output/steam_upcoming.json")
PUBLIC = Path("frontend/data/steam_upcoming.json")


def cmd(*args: str) -> None:
    subprocess.run(args, check=True)


def persist_backend() -> None:
    cmd("git", "add", str(STATE), "data/steam_followers_cache.json",
        "data/steam_upcoming_master.json")
    changes = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if changes.returncode == 0:
        return
    if changes.returncode != 1:
        raise RuntimeError("Could not inspect backend checkpoint changes")
    cmd("git", "commit", "-m", "data: advance Steam-verified >=4000 shortlist")
    cmd("git", "pull", "--rebase", "origin", "main")
    cmd("git", "push", "origin", "HEAD:main")


def publish_official() -> None:
    token = os.getenv("FRONTEND_REPO_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "FRONTEND_REPO_TOKEN is required to publish official results. "
            "Private checkpoint has already been preserved."
        )
    if not PUBLIC.parent.exists():
        cmd("git", "clone", "--depth", "1",
            "https://github.com/danielet087/game-trend-radar.git", "frontend")
    body = json.loads(OUTPUT.read_text(encoding="utf-8"))
    assert body["filter"]["min_followers"] == 5000
    assert all(int(game["followers"]) >= 5000 for game in body["games"])
    assert body["initialization"]["prefilter_complete"] is True
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OUTPUT, PUBLIC)
    cmd("git", "-C", "frontend", "add", "data/steam_upcoming.json")
    changed = subprocess.run(
        ["git", "-C", "frontend", "diff", "--cached", "--quiet"], check=False,
    )
    if changed.returncode == 0:
        return
    if changed.returncode != 1:
        raise RuntimeError("Cannot inspect public JSON staging")
    cmd("git", "-C", "frontend", "commit", "-m",
        "data: publish Steam-verified upcoming games from >=4000 shortlist")
    # Token stays in runner environment, not GitHub files, report or public JSON.
    cmd("git", "-C", "frontend", "remote", "set-url", "origin",
        f"https://x-access-token:{token}@github.com/danielet087/game-trend-radar.git")
    try:
        cmd("git", "-C", "frontend", "pull", "--rebase", "origin", "main")
        cmd("git", "-C", "frontend", "push", "origin", "HEAD:main")
    finally:
        cmd("git", "-C", "frontend", "remote", "set-url", "origin",
            "https://github.com/danielet087/game-trend-radar.git")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-minutes", type=int, default=100)
    args = parser.parse_args()
    if not 6 <= args.interval <= 30:
        raise RuntimeError("Official XML interval must be 6..30 seconds")
    if not 1 <= args.batch_size <= 50:
        raise RuntimeError("Official XML batch must be 1..50 new requests")

    state = load_json(STATE, {})
    pre = load_json(PREFILTER, {})
    catalog = load_json(CATALOG, {})
    if (
        state.get("phase") not in ("followers", "complete")
        or not state.get("prefilter_complete")
        or not pre.get("complete")
        or len(pre.get("games") or {}) != len(catalog.get("games") or [])
        or len(catalog.get("games") or []) != 11467
    ):
        raise RuntimeError("Stage 2 not complete or catalog changed; refusing XML")
    if state.get("phase") == "complete":
        LOG.info("Shortlisted Steam verification already complete; nothing to re-query")
        return

    cli = argparse.Namespace(
        state=str(STATE), catalog=str(CATALOG),
        master="data/steam_upcoming_master.json", output=str(OUTPUT),
        days=365, batch_days=365, request_interval=args.interval,
        search_interval=1.5,
        follower_cache="data/steam_followers_cache.json",
        checkpoint="data/steam_followers_checkpoint.json",
        checkpoint_branch="steam-state",
        prefilter_state=str(PREFILTER), prefilter_batch_size=0,
        prefilter_request_interval=0.5,
        max_fresh_requests_per_run=args.batch_size,
    )

    started = time.monotonic()
    batch = 0
    while time.monotonic() - started < args.max_minutes * 60:
        previous = load_json(STATE, {})
        if previous.get("phase") == "complete":
            break
        summary = run(cli)
        batch += 1
        now = load_json(STATE, {})
        fresh = int(summary.get("fresh_follower_requests", 0))
        failures = int(summary.get("failures", 0))
        throttle = int(summary.get("rate_limit_events", 0))
        LOG.info(
            "OFFICIAL BATCH %s verified=%s/%s fresh=%s cache=%s "
            "qualified_new=%s failures=%s 429=%s complete=%s",
            batch, now.get("verified_priority_count"),
            now.get("priority_total"), fresh,
            summary.get("cached_reuses", 0),
            summary.get("qualified_this_batch", 0),
            failures, throttle, now.get("initial_complete"),
        )
        persist_backend()
        publish_official()
        if now.get("phase") == "complete":
            LOG.info("ALL eligible third-party >=4000 titles verified by Steam XML.")
            break
        if failures or throttle or fresh == 0:
            LOG.warning(
                "Stopping official XML batch due to failure/throttle/no progress; "
                "backend and frontend progress saved for safe resumption."
            )
            break
    else:
        LOG.warning("Hosted execution budget reached; checkpoint saved.")

    final = load_json(STATE, {})
    LOG.info(
        "STAGE3_RESULT verified=%s total=%s finished=%s "
        "historic_sequential_cursor=%s elapsed_seconds=%.1f",
        final.get("verified_priority_count"), final.get("priority_total"),
        final.get("initial_complete"), final.get("next_follower_index"),
        time.monotonic() - started,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    main()
