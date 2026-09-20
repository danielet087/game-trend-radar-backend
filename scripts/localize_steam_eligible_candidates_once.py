"""One-time: add Steam Traditional Chinese titles to saved 3,137 eligible games.

Only overwrite name_en/name_zh_tw within the derived eligible snapshot.
Never change original discovery catalog, saved follower counts or cursors.
No user-supplied unofficial translations or Steam Community XML requests.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.steam_localized_titles import enrich_tw_names, fetch_store_tw_names

LOG = logging.getLogger(__name__)
DEFAULT_SNAPSHOT = Path("data/steam_candidates_eligible.json")


def run(snapshot_path: Path) -> dict:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    games = snapshot.get("games")
    if (
        not isinstance(games, list)
        or not 2000 <= len(games) <= 6000
        or snapshot.get("count") != len(games)
        or snapshot.get("source_count") != 11467
    ):
        raise RuntimeError("Unexpected eligible candidate snapshot; no data changed")
    ids = [int(g["appid"]) for g in games]
    if len(set(ids)) != len(ids):
        raise RuntimeError("Duplicate AppIDs in eligible snapshot")
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarOfficialTWTitleSync/1.0"
    names = fetch_store_tw_names(session, ids)
    if len(names) < len(ids) * 0.95:
        raise RuntimeError(
            f"Steam returned {len(names)}/{len(ids)} locale entries; "
            "refusing to publish partial lookup"
        )
    results = enrich_tw_names(games, names)
    snapshot["official_zh_tw_titles_checked_at"] = (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )
    snapshot["official_zh_tw_titles_provider"] = (
        "Steam IStoreBrowseService/GetItems context language=tchinese country=TW"
    )
    snapshot["official_zh_tw_titles_summary"] = results
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    LOG.info("ELIGIBLE_TITLES_RESULT %s", json.dumps(results, ensure_ascii=False))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    print(json.dumps(run(args.snapshot), ensure_ascii=False))
