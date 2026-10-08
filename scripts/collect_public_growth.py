"""Compatibility entry point for daily official Followers observations.

The use case lives in radar_backend.application.growth. Legacy callers retain
this import path and direct-script invocation while jobs compose its adapters.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET
import requests
from radar_core.jobs import JobResult, JobStatus

# Direct invocation puts scripts/, rather than the repository root, on sys.path.
if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_backend.domain.growth import TAIPEI, eligible, timestamp
from radar_backend.domain.official_queue import OfficialObservation, valid_group_id64
from radar_backend.jobs.growth import collect as _collect, main as _main
from radar_backend.state.growth import read
from radar_backend.adapters.official_followers import OfficialFollowerClient
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache, read_state, save_state
from scripts.twitch_steam_admission import is_twitch_qualified


def parse_count(body):
    root = ET.fromstring(body)
    for path in ("memberCount", "groupDetails/memberCount", ".//memberCount"):
        text = root.findtext(path, "").replace(",", "").strip()
        if text.isdigit():
            return int(text)
    raise ValueError("Missing Steam Community memberCount")

def collect(data_dir, output, *, max_requests=120, interval=30, now=None, session=None,
            sleep=time.sleep, checkpoint_path=None, official_cache_path=None,
            legacy_checkpoint_path=None, group_state_path=None, original_checkpoint_path=None, clock=None):
    # Preserve legacy monkeypatch points while the use case consumes explicit ports.
    return _collect(data_dir, output, max_requests=max_requests, interval=interval, now=now,
                    session=session, sleep=sleep, checkpoint_path=checkpoint_path,
                    official_cache_path=official_cache_path, legacy_checkpoint_path=legacy_checkpoint_path,
                    group_state_path=group_state_path, original_checkpoint_path=original_checkpoint_path,
                    clock=clock, client_factory=OfficialFollowerClient, cache_factory=OfficialFollowerCache,
                    cooldown_factory=CooldownStore, session_factory=requests.Session, monotonic=time.monotonic)


def main():
    _main(collector=collect)


if __name__ == "__main__":
    main()
