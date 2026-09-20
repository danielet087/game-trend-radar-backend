"""Resume the FULL third-party stage and checkpoint each successful 200-title chunk.

The Steam XML collector is not imported or invoked. The user requested strict:
Steam discovery (already complete) -> all 11,467 third-party group lookups
-> only THEN official Steam XML of measured >=4,000 titles.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.steam_candidate_pipeline import active_candidate_rows, run_prefilter_phase
from scripts.update_steam_daily import load_json, save_json

LOG = logging.getLogger(__name__)
STATE = Path("data/steam_candidate_state.json")
PREFILTER = Path("data/steam_prefilter_state.json")
CATALOG = Path("data/steam_candidates.json")


def push_private_checkpoint(state: dict, next_index: int) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE, state)
    subprocess.run(["git", "add", str(STATE), str(PREFILTER)], check=True)
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if changed.returncode == 0:
        return
    if changed.returncode != 1:
        raise RuntimeError("Could not verify staged checkpoint changes")
    subprocess.run([
        "git", "commit", "-m",
        f"data: third-party prescreen checkpoint through {next_index}",
    ], check=True)
    subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)


BATCH_RETRY_DELAYS = (20, 60, 150, 240, 300)


def is_retryable_batch_failure(error: RuntimeError) -> bool:
    """Only retry temporary provider/network faults, not missing credentials."""
    message = str(error)
    if "Temporary lookup exhausted retries" in message:
        return True
    for code in (408, 425, 429, 500, 502, 503, 504):
        if f"HTTP {code}" in message:
            return True
    return False


def scan_with_recovery(args: argparse.Namespace, state: dict, catalog: dict) -> dict:
    """Never advance persisted cursor for a partially fetched 200-title batch."""
    for attempt in range(len(BATCH_RETRY_DELAYS) + 1):
        try:
            return run_prefilter_phase(args, state, catalog)
        except RuntimeError as error:
            if not is_retryable_batch_failure(error) or attempt >= len(BATCH_RETRY_DELAYS):
                raise
            cooldown = BATCH_RETRY_DELAYS[attempt]
            LOG.warning(
                "Temporary prescreen lookup error; saved cursor intact. "
                "Retry current batch in %s seconds (attempt %s/%s).",
                cooldown, attempt + 1, len(BATCH_RETRY_DELAYS),
            )
            time.sleep(cooldown)
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--max-minutes", type=int, default=162)
    options = parser.parse_args()
    if not 1 <= options.batch_size <= 200:
        raise RuntimeError("Batch size must stay within previously tested 200")
    if options.request_interval < 0.5:
        raise RuntimeError("Do not send Steam group-ID requests faster than tested")
    if not os.environ.get("STEAM_WEB_API_KEY", "").strip():
        raise RuntimeError("STEAM_WEB_API_KEY missing; private checkpoint unchanged")

    state = load_json(STATE, {})
    catalog = load_json(CATALOG, {})
    if (state.get("mode") != "two_phase_steam_year"
        or state.get("days_scanned") != 365
        or state.get("phase") not in ("prefilter", "followers")):
        raise RuntimeError("Year discovery incomplete or state unexpected; refusing to scan")
    rows = active_candidate_rows(catalog, state)
    if not rows or (not state.get("date_precision_required") and len(rows) < 11000):
        raise RuntimeError("No verified eligible candidates or unexpected legacy catalog")
    args = argparse.Namespace(
        prefilter_state=str(PREFILTER),
        prefilter_batch_size=options.batch_size,
        prefilter_request_interval=options.request_interval,
    )
    started = time.monotonic()
    count = 0
    while True:
        current = load_json(PREFILTER, {
            "version": 1, "next_index": 0, "games": {}, "complete": False,
        })
        if current.get("complete") and len(current.get("games") or {}) == len(rows):
            state["phase"] = "followers"
            state["prefilter_complete"] = True
            push_private_checkpoint(state, len(rows))
            LOG.info("ALL %s Steam candidate games third-party screened.", len(rows))
            break
        if (time.monotonic() - started) >= options.max_minutes * 60:
            LOG.warning("Hosted time budget reached; checkpoint saved, can resume.")
            break
        info = scan_with_recovery(args, state, catalog)
        count += 1
        LOG.info(
            "PREFILTER BATCH %s head=%s tail=%s/%s records=%s priority=%s "
            "missing=%s STEAM_XML=0 done=%s",
            count, state.get("prefilter_head_next_index", 0),
            state.get("prefilter_next_index", 0), len(rows),
            state.get("prefilter_screened_count", 0),
            state.get("prefilter_matched_count", 0),
            state.get("prefilter_missing_count", 0),
            state.get("prefilter_complete", False),
        )
        push_private_checkpoint(state, int(state.get("prefilter_next_index", 0)))
        if state.get("prefilter_complete"):
            LOG.info(
                "Prescreen complete, stage 3 eligible next run. NO STEAM XML "
                "was queried in the prescreen job."
            )
            break


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    main()
