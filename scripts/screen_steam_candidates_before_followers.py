"""Build an eligible shortlist from the already-discovered Steam year catalogue.

Read-only Steam Store Browse metadata (release.coming_soon_display,
content_descriptorids, basic_info and tags). Never issue Steam Community XML,
query any Followers count, reset a saved cursor, or modify the original
11,467-game catalog/state. Publish a SEPARATE candidate snapshot only after
every batch is checked. A date-only timestamp from IStoreQueryService is not
proof of an announced full release date.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.steam_adult_exclusions import excluded_appids
from radar_backend.adapters import steam_metadata as _metadata
from radar_backend.application import candidate_screening as _screening_application
from radar_backend.domain import candidate_screening as _screening_rules

LOG = logging.getLogger(__name__)
STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
DEFAULT_INPUT = Path("data/steam_candidates.json")
DEFAULT_OUTPUT = Path("data/steam_candidates_eligible.json")
BATCH_SIZE = 35
SEXUAL_CONTENT_IDS = _screening_rules.SEXUAL_CONTENT_IDS
EXPLICIT_DESC = _screening_rules.EXPLICIT_DESC


def is_explicit_sex_game(store_item: dict) -> bool:
    """Avoid treating violence, general maturity, or mere romance as porn."""
    return _screening_rules.is_explicit_sex_game(
        store_item, sexual_content_ids=SEXUAL_CONTENT_IDS, explicit_pattern=EXPLICIT_DESC,
    )


def classify(existing: dict, item: dict | None) -> tuple[dict | None, str]:
    """Only a published full-day Store display is follower-eligible."""
    return _screening_rules.classify(existing, item, explicit_predicate=is_explicit_sex_game)


def fetch_metadata(
    session: requests.Session, ids: list[int], *,
    batch_size: int = BATCH_SIZE,
    interval: float = 1.5,
) -> dict[int, dict]:
    return _metadata.fetch_metadata(
        session, ids, batch_size=batch_size, interval=interval,
        sleep=time.sleep, monotonic=time.monotonic, logger=LOG,
        requests_module=requests, url=STORE_BROWSE_URL,
    )


def build_snapshot(catalog: dict, store_items: dict[int, dict]) -> dict:
    return _screening_application.build_snapshot(
        catalog, store_items, clock=lambda: datetime.now(timezone.utc),
        exclusion_loader=excluded_appids, classifier=classify,
    )


def run(source: Path = DEFAULT_INPUT, output: Path = DEFAULT_OUTPUT) -> dict:
    if source.resolve() == output.resolve():
        raise ValueError("Never overwrite the original discovery catalogue")
    if output.exists():
        raise RuntimeError("Eligibility snapshot already exists; refusing overwrite")
    catalog = json.loads(source.read_text(encoding="utf-8"))
    games = catalog.get("games")
    if not isinstance(games, list) or not 10000 <= len(games) <= 15000:
        raise RuntimeError("Unexpected candidate catalogue size; refusing to scan")
    ids = [int(game["appid"]) for game in games]
    if len(set(ids)) != len(ids):
        raise RuntimeError("Original catalogue contains duplicate appids")
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarReleasePrecisionGate/1.0"
    store = fetch_metadata(session, sorted(ids))
    snapshot = build_snapshot(catalog, store)
    if snapshot["count"] == 0 or snapshot["count"] == snapshot["source_count"]:
        raise RuntimeError("Suspicious exact-date screening result; refusing publication")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    info = {k: v for k, v in snapshot.items() if k != "games"}
    LOG.info("ELIGIBILITY_COMPLETE %s", json.dumps(info, ensure_ascii=False))
    return info


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    print(json.dumps(run(args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
