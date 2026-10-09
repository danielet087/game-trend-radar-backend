"""Canonical official queue inputs, file ports and projection composition."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from radar_backend.adapters import twitch_intake
from radar_backend.domain import official_queue
from radar_backend.state import official_checkpoint

ROOT = Path("experiments/steam_official_daily_catchup")
FROZEN = Path("experiments/steam_official_nearfirst_20260922")
ELIGIBLE = Path("data/steam_candidates_eligible.json")
PREFILTER = Path("data/steam_prefilter_state.json")
OFFICIAL_CACHE = Path("data/steam_followers_cache.json")
ORIGINAL_OFFICIAL = Path("experiments/steam_official_followers_20260922/checkpoint.json")
CHECKPOINT = ROOT / "checkpoint.json"
MASTER = Path("data/steam_upcoming_master.json")
TZ = official_queue.TAIPEI
valid_group_id64 = official_queue.valid_group_id64
checked_numeric = official_queue.queue_checked_numeric


def clock():
    return datetime.now(TZ)


def read(path):
    return official_checkpoint.read(path)


def save(path, value):
    return official_checkpoint.save(path, value)


def make_queue(cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter,
               official_cache, other_official, *, now=None):
    return official_queue.make_queue(
        cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter,
        official_cache, other_official, now=now or clock(),
        is_twitch_queue_candidate=twitch_intake.is_twitch_queue_candidate,
        cached_follower=twitch_intake.cached_follower,
    )
