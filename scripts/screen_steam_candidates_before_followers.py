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
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.steam_adult_exclusions import excluded_appids

LOG = logging.getLogger(__name__)
STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
DEFAULT_INPUT = Path("data/steam_candidates.json")
DEFAULT_OUTPUT = Path("data/steam_candidates_eligible.json")
BATCH_SIZE = 35
SEXUAL_CONTENT_IDS = frozenset({3, 4})
EXPLICIT_DESC = re.compile(
    r"\b(?:nsfw|hentai|pornograph(?:y|ic)|erotic(?:a)?|sex game|adult game|"
    r"sexually explicit|explicit sexual|uncensored sexual|sex scenes|"
    r"sexual acts|lots of sex)\b", re.I,
)


def is_explicit_sex_game(store_item: dict) -> bool:
    """Avoid treating violence, general maturity, or mere romance as porn."""
    ids = set(store_item.get("content_descriptorids") or [])
    if ids & SEXUAL_CONTENT_IDS:
        return True
    tags = [
        row.get("tagid") for row in (store_item.get("tags") or [])
        if isinstance(row, dict)
    ]
    description = str(
        (store_item.get("basic_info") or {}).get("short_description") or ""
    )
    return bool(
        12095 in tags[:5]
        and (9130 in tags[:10] or 6650 in tags[:5])
        and EXPLICIT_DESC.search(description)
    )


def classify(existing: dict, item: dict | None) -> tuple[dict | None, str]:
    """Only a published full-day Store display is follower-eligible."""
    if not item or not isinstance(item.get("release"), dict):
        return None, "unavailable"
    label = item["release"].get("coming_soon_display")
    if label != "date_full":
        return None, str(label or "unknown")
    if existing.get("release_precision") != "day" or not existing.get("release_start"):
        return None, "invalid_original_date"
    if is_explicit_sex_game(item):
        return None, "sexual_content"
    verified = dict(existing)
    verified["release_display_precision"] = "date_full"
    verified["release_display_provider"] = "Steam IStoreBrowseService/GetItems"
    verified["sexual_content_screened"] = True
    return verified, "eligible"


def fetch_metadata(
    session: requests.Session, ids: list[int], *,
    batch_size: int = BATCH_SIZE,
    interval: float = 1.5,
) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    last_start = 0.0
    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        request = {
            "ids": [{"appid": appid} for appid in batch],
            "context": {
                "country_code": "TW", "language": "english", "steam_realm": 1,
            },
            "data_request": {
                "include_release": True,
                "include_basic_info": True,
                "include_tag_count": 20,
            },
        }
        for attempt in range(4):
            time.sleep(max(0.0, interval - (time.monotonic() - last_start)))
            last_start = time.monotonic()
            try:
                response = session.get(
                    STORE_BROWSE_URL,
                    params={"input_json": json.dumps(request, separators=(",", ":"))},
                    timeout=30,
                )
                if response.status_code == 429:
                    LOG.warning("Steam Store Browse HTTP 429 at batch %d; cooling down",
                                start // batch_size + 1)
                    time.sleep(20 * (attempt + 1))
                    continue
                response.raise_for_status()
                result = response.json()
                items = (result.get("response") or {}).get("store_items") or []
                for item in items:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("appid"), int)
                        and item["appid"] in batch
                    ):
                        rows[item["appid"]] = item
                break
            except (requests.RequestException, ValueError, TypeError) as exc:
                LOG.warning("Steam Browse batch retry %d: %s", attempt + 1, exc)
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(
                f"Steam Browse batch {start // batch_size + 1} failed; "
                "refusing to publish an incomplete eligibility snapshot"
            )
        LOG.info("DATE_GATE_CHECKED %s/%s resolved=%s",
                 min(start + len(batch), len(ids)), len(ids), len(rows))
    return rows


def build_snapshot(catalog: dict, store_items: dict[int, dict]) -> dict:
    original = catalog.get("games")
    if not isinstance(original, list):
        raise ValueError("Invalid original candidate games array")
    result: list[dict] = []
    reasons = Counter()
    blocked = excluded_appids()
    for game in original:
        if int(game["appid"]) in blocked:
            reasons["audited_adult_exclusion"] += 1
            continue
        row, status = classify(game, store_items.get(int(game["appid"])))
        reasons[status] += 1
        if row is not None:
            result.append(row)
    if sum(reasons.values()) != len(original):
        raise RuntimeError("Eligibility classification coverage incomplete")
    return {
        "screened_at": datetime.now(timezone.utc).isoformat(),
        "source": "data/steam_candidates.json",
        "source_count": len(original),
        "rule": (
            "Steam Store Browse coming_soon_display=date_full; exclude "
            "sexual content descriptors 3/4 and strongly explicit adult games"
        ),
        "count": len(result),
        "excluded": len(original) - len(result),
        "reasons": dict(sorted(reasons.items())),
        "games": result,
    }


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
