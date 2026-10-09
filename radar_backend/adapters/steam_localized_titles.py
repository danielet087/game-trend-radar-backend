"""Steam TW name transport and OpenCC display conversion composition."""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import requests
from opencc import OpenCC

from radar_backend.application import localized_titles as application
from radar_backend.domain import localized_titles as rules
from radar_backend.domain.localized_titles import HAN, actual_zh_tw_title


LOG = logging.getLogger(__name__)
STORE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
BATCH = 35
_CONVERT_TO_TRADITIONAL = OpenCC("s2t")


def display_in_traditional(raw: object) -> str | None:
    return rules.display_in_traditional(raw, convert=_CONVERT_TO_TRADITIONAL.convert)


def add_traditional_display_names(game: dict) -> None:
    rules.add_traditional_display_names(game, display=display_in_traditional, han=HAN)


def enrich_tw_names(games: list[dict], store_names: dict[int, str]) -> dict[str, int]:
    return application.enrich_tw_names(
        games, store_names, select_title=actual_zh_tw_title,
        display_names=add_traditional_display_names,
    )


def fetch_store_tw_names(
    session: requests.Session, ids: list[int], *,
    batch_size: int = BATCH, interval: float = 1.5,
    sleep: Callable | None = None, monotonic: Callable | None = None,
    logger=None, requests_module=None, url: str = STORE_URL,
) -> dict[int, str]:
    """Keep four attempts and fail closed if any requested batch fails."""
    sleep = sleep if sleep is not None else time.sleep
    monotonic = monotonic if monotonic is not None else time.monotonic
    logger = logger if logger is not None else LOG
    requests_module = requests_module if requests_module is not None else requests
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
            sleep(max(0.0, interval - (monotonic() - last_started)))
            last_started = monotonic()
            try:
                response = session.get(
                    url,
                    params={"input_json": json.dumps(payload, separators=(",", ":"))},
                    timeout=30,
                )
                if response.status_code == 429:
                    logger.warning("Steam TW title lookup throttled; cooldown")
                    sleep(20 * (attempt + 1))
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
            except (requests_module.RequestException, ValueError, TypeError) as exc:
                logger.warning("Steam TW title batch retry %d: %s", attempt + 1, exc)
                if attempt < 3:
                    sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(
                f"Steam TW title batch {offset // batch_size + 1} failed; "
                "do not publish incomplete localized candidates"
            )
        logger.info("TW_TITLES_LOOKUP %d/%d resolved=%d",
                    min(offset + len(batch), len(ids)), len(ids), len(names))
    return names
