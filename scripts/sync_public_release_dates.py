"""Refresh Steam release timestamps in public JSON without querying Followers.

This is a lightweight one-time sync, separate from the six-hour Steam
initialization and the daily recent-launch scanner.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.steam_release_dates import corrected_games, fetch_store_browse_releases

LOGGER = logging.getLogger(__name__)


def run(frontend_data_dir: Path, *, request_interval: float = 2.0) -> dict:
    documents: dict[str, dict] = {}
    for name in ("steam_preview.json", "steam_upcoming.json"):
        path = frontend_data_dir / name
        if not path.exists():
            LOGGER.info("No %s; skipping", path)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload.get("games"), list):
            raise ValueError(f"{path}: games must be a list")
        documents[name] = payload
    if not documents:
        raise RuntimeError("No public Steam JSON files found")

    appids: set[int] = set()
    for payload in documents.values():
        for field in ("games", "recent_games"):
            for game in payload.get(field) or []:
                appids.add(int(game["appid"]))

    session = requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadar/0.5"})
    released = fetch_store_browse_releases(
        session, sorted(appids), country="TW", request_interval=request_interval,
    )
    if not released:
        raise RuntimeError("Steam Browse returned no valid timestamps; public data unchanged")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    outcome = {"apps_queried": len(appids), "timestamps_found": len(released), "files": {}}
    for name, payload in documents.items():
        changes = []
        for field in ("games", "recent_games"):
            if not isinstance(payload.get(field), list):
                continue
            original = payload[field]
            updated = corrected_games(original, released)
            for before, after in zip(original, updated):
                if before.get("release_start") != after.get("release_start"):
                    changes.append({
                        "appid": after["appid"],
                        "from": before.get("release_start"),
                        "to": after.get("release_start"),
                        "source": after.get("release_date_basis"),
                    })
            payload[field] = updated
        # This is a metadata-only update: never claim the Followers were refreshed.
        payload["release_times_refreshed_at"] = now
        payload["release_time_provider"] = "Steam IStoreBrowseService/GetItems"
        path = frontend_data_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outcome["files"][name] = {"date_changes": changes, "count": len(payload["games"])}
    LOGGER.info("Store release metadata sync: %s", json.dumps(outcome, ensure_ascii=False))
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-data", type=Path, required=True)
    parser.add_argument("--request-interval", type=float, default=2.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.frontend_data, request_interval=args.request_interval)


if __name__ == "__main__":
    main()
