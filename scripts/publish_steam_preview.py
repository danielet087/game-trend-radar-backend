from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from collectors.steam_upcoming import parse_release_window, write_json

LOGGER = logging.getLogger(__name__)
APP_DETAILS = "https://store.steampowered.com/api/appdetails"
FEATURED = "https://store.steampowered.com/api/featuredcategories"
UA = "Mozilla/5.0 (compatible; GameTrendRadar/0.4)"


def steam_get(session: requests.Session, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=25)
            if response.status_code == 429:
                delay = max(60, min(180, 60 * (attempt + 1)))
                LOGGER.warning("Steam Store returned 429; sleeping %ds", delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else None
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("Steam Store metadata request failed: %s", exc)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return None


def app_details(session: requests.Session, appid: int) -> dict[str, Any] | None:
    data = steam_get(session, APP_DETAILS, {"appids": appid, "cc": "TW", "l": "english"})
    item = (data or {}).get(str(appid), {})
    if isinstance(item, dict) and item.get("success") and isinstance(item.get("data"), dict):
        return item["data"]
    return None


def normalized_metadata(appid: int, details: dict[str, Any], followers: int | None, checked_at: str | None) -> dict[str, Any] | None:
    raw = str((details.get("release_date") or {}).get("date") or "").strip()
    release = parse_release_window(raw)
    if release.precision != "day" or not release.start:
        return None
    return {
        "appid": appid,
        "name": str(details.get("name") or f"Steam App {appid}"),
        "release_raw": release.raw,
        "release_start": release.start.isoformat(),
        "release_end": release.end.isoformat() if release.end else None,
        "release_precision": "day",
        "followers": followers,
        "follower_checked_at": checked_at,
        "capsule_image": details.get("capsule_image") or details.get("header_image"),
        "header_image": details.get("header_image"),
        "store_url": f"https://store.steampowered.com/app/{appid}/",
    }


def run(*, checkpoint_path: Path, output_path: Path, min_followers: int = 5000, delay_seconds: float = 2.5) -> dict[str, Any]:
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    cached = checkpoint.get("games", {})
    if not isinstance(cached, dict):
        raise ValueError("Checkpoint games must be an object")

    today = datetime.now(timezone(timedelta(hours=8))).date()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})

    qualified = []
    for key, entry in cached.items():
        if not isinstance(entry, dict):
            continue
        try:
            appid, followers = int(key), int(entry["followers"])
        except (ValueError, TypeError, KeyError):
            continue
        if followers >= min_followers:
            qualified.append((appid, followers, str(entry.get("checked_at") or "")))
    qualified.sort(key=lambda row: (-row[1], row[0]))

    games: list[dict[str, Any]] = []
    skipped_missing_date = 0
    for index, (appid, followers, checked_at) in enumerate(qualified, start=1):
        if index > 1:
            time.sleep(delay_seconds)
        details = app_details(session, appid)
        if not details:
            LOGGER.warning("No Store metadata for %d", appid)
            continue
        game = normalized_metadata(appid, details, followers, checked_at)
        if game is None:
            skipped_missing_date += 1
            LOGGER.info("Skipping %d: Store has no exact release day", appid)
            continue
        day = date.fromisoformat(game["release_start"])
        if today - timedelta(days=30) <= day <= today + timedelta(days=365):
            games.append(game)
        LOGGER.info("Metadata %d/%d: AppID=%d followers=%d", index, len(qualified), appid, followers)

    # Steam Store's top_sellers list, restricted to titles released in the last 30 days.
    # Do not label these records as having follower data: this is a different metric.
    featured = steam_get(session, FEATURED, {"cc": "TW", "l": "english"}) or {}
    recent: list[dict[str, Any]] = []
    seen: set[int] = set()
    for source in ("top_sellers", "new_releases"):
        group = featured.get(source) or {}
        for item in (group.get("items") or [])[:15]:
            try:
                appid = int(item.get("id") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if appid < 1 or appid in seen:
                continue
            seen.add(appid)
            time.sleep(delay_seconds)
            details = app_details(session, appid)
            if not details:
                continue
            game = normalized_metadata(appid, details, None, None)
            if not game:
                continue
            day = date.fromisoformat(game["release_start"])
            if today - timedelta(days=30) <= day <= today and not (details.get("release_date") or {}).get("coming_soon", False):
                game["recent_source"] = source
                recent.append(game)
            if len(recent) >= 8:
                break
        if len(recent) >= 8:
            break

    output = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "Steam Store appdetails + private follower checkpoint; recent releases: Steam Store top_sellers/new_releases",
        "is_partial_preview": True,
        "checkpoint_count": len(cached),
        "checkpoint_updated_at": checkpoint.get("updated_at"),
        "qualified_checkpoint_count": len(qualified),
        "exact_date_games_count": len(games),
        "missing_or_ambiguous_release_date": skipped_missing_date,
        "min_followers": min_followers,
        "games": games,
        "recent_games": recent,
    }
    write_json(output, output_path)
    LOGGER.info("Wrote preview with %d exact-date games, %d recent releases from %d cached apps", len(games), len(recent), len(cached))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a public Steam calendar preview without refetching followers")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", default="output/steam_preview.json", type=Path)
    parser.add_argument("--min-followers", type=int, default=5000)
    parser.add_argument("--delay", type=float, default=2.5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(checkpoint_path=args.checkpoint, output_path=args.output, min_followers=args.min_followers, delay_seconds=args.delay)


if __name__ == "__main__":
    main()
