"""Steam scheduled release timestamps with the established partial-data policy.

This lookup enriches already selected games. Unlike eligibility metadata,
unavailable batches do not prevent callers from using announced Store dates.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Callable

import requests


LOG = logging.getLogger(__name__)
STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
BATCH_SIZE = 35


def fetch_release_timestamps(
    session: requests.Session,
    appids: list[int],
    *,
    country: str = "TW",
    batch_size: int = BATCH_SIZE,
    request_interval: float = 2.0,
    sleep: Callable | None = None,
    monotonic: Callable | None = None,
    logger=None,
    requests_module=None,
    url: str = STORE_BROWSE_URL,
    fromtimestamp: Callable | None = None,
) -> dict[int, dict[str, Any]]:
    """Read public scheduled timestamps, preserving partial results on failure.

    Ports are resolved at call time so compatibility entries and offline tests
    can supply their clock, logger, HTTP exception type and timestamp parser.
    """
    sleep = sleep if sleep is not None else time.sleep
    monotonic = monotonic if monotonic is not None else time.monotonic
    logger = logger if logger is not None else LOG
    requests_module = requests_module if requests_module is not None else requests
    fromtimestamp = fromtimestamp if fromtimestamp is not None else datetime.fromtimestamp
    ids = sorted({int(appid) for appid in appids if int(appid) > 0})
    releases: dict[int, dict[str, Any]] = {}
    last_request: float | None = None
    for start in range(0, len(ids), max(1, batch_size)):
        batch = ids[start:start + max(1, batch_size)]
        payload = {
            "ids": [{"appid": appid} for appid in batch],
            "context": {"country_code": country, "language": "english", "steam_realm": 1},
            "data_request": {"include_release": True},
        }
        for attempt in range(3):
            if last_request is not None:
                sleep(max(0.0, request_interval - (monotonic() - last_request)))
            last_request = monotonic()
            try:
                response = session.get(
                    url,
                    params={"input_json": json.dumps(payload, separators=(",", ":"))},
                    timeout=25,
                )
                if response.status_code == 429:
                    sleep(15 * (attempt + 1))
                    continue
                response.raise_for_status()
                result = response.json()
                rows = (result.get("response") or {}).get("store_items") or []
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    appid = item.get("appid")
                    release = item.get("release")
                    if not isinstance(appid, int) or appid not in batch or not isinstance(release, dict):
                        continue
                    stamp = release.get("steam_release_date")
                    # Scheduled timestamps are the only accepted source. Do
                    # not infer an unlock time from other release fields.
                    if isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
                        continue
                    try:
                        seconds = int(stamp)
                        instant = fromtimestamp(seconds, tz=timezone.utc)
                    except (OverflowError, OSError, ValueError, TypeError):
                        continue
                    if not 2010 <= instant.year <= 2100:
                        continue
                    releases[appid] = {
                        "steam_release_date": seconds,
                        "release_time_source": url,
                        "is_coming_soon": release.get("is_coming_soon"),
                    }
                break
            except (requests_module.RequestException, ValueError, TypeError, AttributeError) as exc:
                logger.warning("Store Browse date lookup failed: %s", exc)
                if attempt < 2:
                    sleep(5 * (attempt + 1))
        logger.info(
            "Steam Store Browse timestamps: %d/%d apps processed",
            min(start + len(batch), len(ids)), len(ids),
        )
    return releases
