"""Steam candidate source boundary and default HTTP integrations.

Application phases receive this object explicitly. Legacy collectors and Store
helpers remain concrete adapters during this bounded migration; no phase reads
requests or environment variables directly.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import requests

from collectors.steam_upcoming import SteamUpcomingCollector, UpcomingGame
from scripts.steam_follower_prefilter import scan_batch
from scripts.screen_steam_candidates_before_followers import build_snapshot, fetch_metadata
from scripts.steam_localized_titles import enrich_tw_names, fetch_store_tw_names
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.twitch_steam_admission import is_twitch_qualified
from scripts.steam_master_date_gate import (
    apply_store_release_detail, fetch_store_release_details, filter_confirmed_master_games,
)
from scripts.update_steam_daily import merge_partial_segment
from radar_backend.domain.candidates import QUERY_URL, TAIWAN_TZ, candidate_record

PAGE_SIZE = 1000
MAX_PAGES_PER_DAY = 30
QUERY_INTERVAL_SECONDS = 1.5

def query_one_day(
    session: requests.Session, api_key: str, day: date, *,
    request_interval: float = QUERY_INTERVAL_SECONDS,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES_PER_DAY,
    candidate_factory: Callable = candidate_record,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read every paginated result for one Taiwan release day.

    Fail closed on unexpected pagination, missing metadata, or a rate limit.
    Never mark a day scanned merely because a page yielded no usable titles.
    """
    utc_start = datetime(day.year, day.month, day.day, tzinfo=TAIWAN_TZ).astimezone(timezone.utc)
    utc_end = utc_start + timedelta(days=1)
    by_id: dict[int, dict[str, Any]] = {}
    skipped_no_precise_time = 0
    reported_total: int | None = None
    offset = 0
    pages = 0
    for _ in range(max_pages):
        query = {
            "query": {
                "start": offset, "count": page_size,
                "filters": {
                    "coming_soon_only": True,
                    "type_filters": {"include_games": True},
                    "release_date_filter": {
                        "release_date_type": 1,
                        "start_date": int(utc_start.timestamp()),
                        "end_date": int(utc_end.timestamp()),
                    },
                },
            },
            "context": {"country_code": "TW", "language": "english"},
            "data_request": {
                "include_basic_info": True, "include_release": True, "include_assets": True,
            },
        }
        # Never print a request URL: requests exceptions can contain the key.
        try:
            response = session.get(
                QUERY_URL,
                params={"key": api_key, "input_json": json.dumps(query, separators=(",", ":"))},
                timeout=35,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"HTTP {status}" if status is not None else type(exc).__name__
            raise RuntimeError(
                f"Steam Query failed for {day} at offset {offset}: {reason}"
            ) from None
        data = response.json()
        result = data.get("response")
        if not isinstance(result, dict):
            raise RuntimeError("Steam Query has no response; day not verified")
        metadata = result.get("metadata") or {}
        if not isinstance(metadata.get("total_matching_records"), int):
            raise RuntimeError("Steam Query has no total_matching_records; cannot verify pagination")
        count = metadata["total_matching_records"]
        if reported_total is None:
            reported_total = count
        elif count != reported_total:
            raise RuntimeError(f"Steam Query changed matching total within {day}; retry later")
        rows = result.get("store_items") or []
        if count > offset and not rows:
            raise RuntimeError(f"Steam Query returned an incomplete page at {day}, offset {offset}")
        for item in rows:
            game = candidate_factory(item, day)
            if game is None:
                skipped_no_precise_time += 1
            else:
                by_id[game["appid"]] = game
        offset += len(rows)
        pages += 1
        if offset >= count:
            return (
                sorted(by_id.values(), key=lambda x: x["appid"]),
                {"day": day.isoformat(), "api_matches": count,
                 "candidates": len(by_id),
                 "unverified_rows": skipped_no_precise_time,
                 "pages": pages},
            )
        if len(rows) > page_size or len(rows) == 0:
            raise RuntimeError(f"Invalid Steam Query pagination for {day}")
        if request_interval:
            sleep(request_interval)
    raise RuntimeError(f"Steam Query page ceiling reached for {day} ({reported_total} matches)")

def as_upcoming_game(raw: dict[str, Any]) -> UpcomingGame:
    return UpcomingGame(
        appid=raw["appid"], name=raw["name"], release_raw=raw["release_raw"],
        release_start=raw["release_start"], release_end=raw["release_end"],
        release_precision="day", capsule_image=raw.get("capsule_image"),
        store_url=raw["store_url"],
    )

def api_key_from_environment() -> str:
    return os.environ.get("STEAM_WEB_API_KEY", "").strip()


@dataclass(frozen=True)
class CandidateSources:
    """Explicit replaceable operations; defaults are the production adapters."""
    session_factory: Callable = requests.Session
    api_key: Callable = api_key_from_environment
    query_day: Callable = query_one_day
    fetch_metadata: Callable = fetch_metadata
    build_snapshot: Callable = build_snapshot
    fetch_store_tw_names: Callable = fetch_store_tw_names
    enrich_tw_names: Callable = enrich_tw_names
    scan_batch: Callable = scan_batch
    collector_factory: Callable = SteamUpcomingCollector
    as_upcoming_game: Callable = as_upcoming_game
    fetch_store_release_details: Callable = fetch_store_release_details
    apply_store_release_detail: Callable = apply_store_release_detail
    filter_confirmed_master_games: Callable = filter_confirmed_master_games
    merge_partial_segment: Callable = merge_partial_segment
    excluded_appids: Callable = excluded_appids
    is_disallowed: Callable = is_disallowed
    is_twitch_qualified: Callable = is_twitch_qualified
