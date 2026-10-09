"""Store title lookup for already published games, with fail-closed retries."""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import requests


STORE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
LOG = logging.getLogger(__name__)


def localized_batch(
    session: requests.Session, appids: list[int], language: str, interval: float,
    *, sleep: Callable | None = None, logger=None, requests_module=None,
    url: str = STORE,
) -> dict[int, str]:
    """Look up one caller-owned batch; the application owns interval pacing."""
    sleep = sleep if sleep is not None else time.sleep
    logger = logger if logger is not None else LOG
    requests_module = requests_module if requests_module is not None else requests
    payload = {
        "ids": [{"appid": appid} for appid in appids],
        "context": {"country_code": "TW", "language": language, "steam_realm": 1},
        "data_request": {"include_basic_info": True},
    }
    for attempt in range(5):
        try:
            response = session.get(
                url,
                params={"input_json": json.dumps(payload, separators=(",", ":"))},
                timeout=45,
            )
            if response.status_code == 429:
                logger.warning("%s throttled; attempt %d/5", language, attempt + 1)
                sleep(15 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            rows = (data.get("response") or {}).get("store_items") or []
            if not isinstance(rows, list):
                raise ValueError("Invalid Steam store_items")
            return {
                int(item["appid"]): item["name"].strip()
                for item in rows
                if isinstance(item, dict)
                and type(item.get("appid")) is int
                and item["appid"] in appids
                and isinstance(item.get("name"), str)
                and item["name"].strip()
            }
        except (requests_module.RequestException, ValueError, TypeError) as error:
            logger.warning("Store lookup %s attempt %d/5: %s", language, attempt + 1, error)
            if attempt == 4:
                raise RuntimeError(f"Failed Steam {language} Store lookup") from error
            sleep(5 * (attempt + 1))
    raise RuntimeError(f"Steam {language} rate limit persisted; no partial publish")
