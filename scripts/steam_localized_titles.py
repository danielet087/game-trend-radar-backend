"""Steam-only TW localized game names; separate from all Followers requests.

Browse language tchinese returns an official, not machine-translated, Store title.
Retain the original English title and do not interpret a localized description
as evidence of a translated name. No non-Store host or unofficial translations.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

LOG = logging.getLogger(__name__)
STORE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
HAN = re.compile(r"[\u3400-\u9fff]")
BATCH = 35


def actual_zh_tw_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or not HAN.search(name) or len(name) > 240:
        return None
    return name


def fetch_store_tw_names(
    session: requests.Session,
    ids: list[int],
    *,
    batch_size: int = BATCH,
    interval: float = 1.5,
) -> dict[int, str]:
    names: dict[int, str] = {}
    ids = sorted({int(x) for x in ids if int(x) > 0})
    last_started = 0.0
    for offset in range(0, len(ids), batch_size):
        batch = ids[offset:offset + batch_size]
        payload = {
            "ids": [{"appid": appid} for appid in batch],
            "context": {
                "country_code": "TW", "language": "tchinese", "steam_realm": 1,
            },
            "data_request": {"include_basic_info": True},
        }
        for attempt in range(4):
            time.sleep(max(0.0, interval - (time.monotonic() - last_started)))
            last_started = time.monotonic()
            try:
                response = session.get(
                    STORE_URL,
                    params={"input_json": json.dumps(payload, separators=(",", ":"))},
                    timeout=30,
                )
                if response.status_code == 429:
                    LOG.warning("Steam TW title lookup throttled; cooldown")
                    time.sleep(20 * (attempt + 1))
                    continue
                response.raise_for_status()
                rows = (response.json().get("response") or {}).get("store_items") or []
                for row in rows:
                    if (
                        isinstance(row, dict)
                        and isinstance(row.get("appid"), int)
                        and row["appid"] in batch
                        and isinstance(row.get("name"), str)
                        and row["name"].strip()
                    ):
                        names[row["appid"]] = row["name"].strip()
                break
            except (requests.RequestException, ValueError, TypeError) as exc:
                LOG.warning("Steam TW title batch retry %d: %s", attempt + 1, exc)
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(
                f"Steam TW title batch {offset // batch_size + 1} failed; "
                "do not publish incomplete localized candidates"
            )
        LOG.info("TW_TITLES_LOOKUP %d/%d resolved=%d",
                 min(offset + len(batch), len(ids)), len(ids), len(names))
    return names


def enrich_tw_names(games: list[dict], store_names: dict[int, str]) -> dict[str, int]:
    results = {"total": 0, "official_zh_tw": 0, "english_fallback": 0,
               "store_not_returned": 0}
    for game in games:
        results["total"] += 1
        appid = int(game["appid"])
        original_english = str(game.get("name_en") or game["name"]).strip()
        if not original_english:
            raise RuntimeError(f"Missing English title for app {appid}")
        game["name_en"] = original_english
        candidate = actual_zh_tw_title(store_names.get(appid))
        if candidate:
            game["name_zh_tw"] = candidate
            results["official_zh_tw"] += 1
        elif game.get("name_zh_tw"):
            results["official_zh_tw"] += 1
        else:
            results["english_fallback"] += 1
        if appid not in store_names:
            results["store_not_returned"] += 1
    return results
