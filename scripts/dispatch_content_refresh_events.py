"""Replay content-refresh events for currently published qualified Steam games.

This script deliberately reads the public frontend catalogue as the canonical
"currently publishable" set. It never promotes historical Backend A rows that
are no longer public. Every selected row must already have official Followers
>= 5000 and an exact release date.

Events are paced so Backend B has time to enrich and push the shared frontend
JSON before the next refresh event arrives.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

FRONTEND_JSON = (
    "https://raw.githubusercontent.com/"
    "danielet087/game-trend-radar/main/data/steam_upcoming.json"
)
TARGET_REPO = "danielet087/game-trend-radar-content-backend"
TAIPEI = ZoneInfo("Asia/Taipei")


def valid_date(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return datetime.fromisoformat(value).date().isoformat() == value
    except ValueError:
        return False


def fetch_public_catalogue() -> dict:
    request = urllib.request.Request(
        FRONTEND_JSON + f"?t={int(time.time())}",
        headers={"User-Agent": "GameTrendRadarContentRefreshReplay/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
        raise RuntimeError("Frontend steam_upcoming.json is malformed")
    return payload


def select_games(payload: dict, window_days: int | None) -> list[dict]:
    today = datetime.now(TAIPEI).date()
    end = today + timedelta(days=window_days) if window_days is not None else None
    selected: list[dict] = []
    seen: set[int] = set()

    for row in payload["games"]:
        if not isinstance(row, dict):
            continue
        try:
            appid = int(row["appid"])
            followers = int(row["followers"])
        except (KeyError, TypeError, ValueError):
            continue
        release = row.get("release_start") or row.get("release_date")
        if (
            appid <= 0
            or followers < 5000
            or not valid_date(release)
            or row.get("release_precision") not in (None, "day")
            or row.get("release_display_precision") not in (None, "date_full")
        ):
            continue
        release_day = datetime.fromisoformat(release).date()
        if window_days is not None and not (today <= release_day <= end):
            continue
        if appid in seen:
            raise RuntimeError(f"Duplicate AppID in public catalogue: {appid}")
        seen.add(appid)
        selected.append(
            {
                "appid": appid,
                "name": row.get("name") or row.get("name_en") or f"Steam App {appid}",
                "followers": followers,
                "release_date": release,
                "follower_checked_at": row.get("follower_checked_at"),
            }
        )

    selected.sort(key=lambda x: (x["release_date"], -x["followers"], x["appid"]))
    return selected


def dispatch(token: str, row: dict, reason: str) -> None:
    payload = {
        "event_type": "steam_game_refresh",
        "client_payload": {
            "appid": row["appid"],
            "official_followers": row["followers"],
            "release_date": row["release_date"],
            "official_checked_at_taipei": row.get("follower_checked_at"),
            "source_repository": os.environ.get(
                "GITHUB_REPOSITORY", "danielet087/game-trend-radar-backend"
            ),
            "refresh_reason": reason,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    url = f"https://api.github.com/repos/{TARGET_REPO}/dispatches"

    for attempt in range(4):
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "GameTrendRadarContentRefreshReplay/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 204:
                    return
                raise RuntimeError(
                    f"Unexpected dispatch HTTP {response.status} for {row['appid']}"
                )
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(
                f"Dispatch failed HTTP {error.code} for AppID {row['appid']}"
            ) from None
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(
                f"Dispatch transport failure for AppID {row['appid']}"
            ) from None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="Only refresh games releasing today through N days from today.",
    )
    parser.add_argument("--interval-seconds", type=int, default=40)
    parser.add_argument(
        "--reason",
        default="manual_bulk_refresh",
    )
    args = parser.parse_args()

    if args.window_days is not None and not 0 <= args.window_days <= 365:
        raise SystemExit("--window-days must be 0..365")
    if not 20 <= args.interval_seconds <= 180:
        raise SystemExit("--interval-seconds must be 20..180")

    token = os.environ.get("CONTENT_BACKEND_TOKEN", "").strip()
    if not token:
        raise SystemExit("CONTENT_BACKEND_TOKEN is required")

    payload = fetch_public_catalogue()
    rows = select_games(payload, args.window_days)
    mode = (
        f"today_to_{args.window_days}_days"
        if args.window_days is not None
        else "all_published_ge5000"
    )
    print(
        "CONTENT_REFRESH_SELECTION",
        json.dumps(
            {
                "mode": mode,
                "frontend_generated_at": payload.get("generated_at"),
                "selected": len(rows),
                "interval_seconds": args.interval_seconds,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    if not rows:
        print("CONTENT_REFRESH_COMPLETE selected=0 dispatched=0", flush=True)
        return

    for index, row in enumerate(rows, start=1):
        dispatch(token, row, args.reason)
        print(
            "CONTENT_REFRESH_DISPATCH",
            json.dumps(
                {
                    "index": index,
                    "total": len(rows),
                    "appid": row["appid"],
                    "name": row["name"],
                    "followers": row["followers"],
                    "release_date": row["release_date"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if index < len(rows):
            time.sleep(args.interval_seconds)

    print(
        f"CONTENT_REFRESH_COMPLETE selected={len(rows)} dispatched={len(rows)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
