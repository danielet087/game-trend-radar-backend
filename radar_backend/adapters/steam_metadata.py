"""Steam Store Browse metadata transport with the established retry policy."""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import requests

from radar_core.domain.mature_content import decide_content


LOG = logging.getLogger(__name__)
STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
BATCH_SIZE = 35


def fetch_metadata(
    session: requests.Session, ids: list[int], *, batch_size: int = BATCH_SIZE,
    interval: float = 1.5, sleep: Callable | None = None,
    monotonic: Callable | None = None, logger=None, requests_module=None,
    url: str = STORE_BROWSE_URL, max_content_details: int = 200, context_offset: int | None = None,
) -> dict[int, dict]:
    """Keep pacing, retry boundaries and partial response handling unchanged."""
    sleep = sleep if sleep is not None else time.sleep
    monotonic = monotonic if monotonic is not None else time.monotonic
    logger = logger if logger is not None else LOG
    requests_module = requests_module if requests_module is not None else requests
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
            sleep(max(0.0, interval - (monotonic() - last_start)))
            last_start = monotonic()
            try:
                response = session.get(
                    url,
                    params={"input_json": json.dumps(request, separators=(",", ":"))},
                    timeout=30,
                )
                if response.status_code == 429:
                    logger.warning("Steam Store Browse HTTP 429 at batch %d; cooling down",
                                   start // batch_size + 1)
                    sleep(20 * (attempt + 1))
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
            except (requests_module.RequestException, ValueError, TypeError) as exc:
                logger.warning("Steam Browse batch retry %d: %s", attempt + 1, exc)
                if attempt < 3:
                    sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(
                f"Steam Browse batch {start // batch_size + 1} failed; "
                "refusing to publish an incomplete eligibility snapshot"
            )
        logger.info("DATE_GATE_CHECKED %s/%s resolved=%s",
                    min(start + len(batch), len(ids)), len(ids), len(rows))
    # Only contextual descriptor-4 rows need a second official request. A
    # bounded failure leaves retryable review instead of inventing an adult verdict.
    contextual = [(appid, item) for appid, item in sorted(rows.items())
        if decide_content(item).reason == "official_content_context_unavailable"]
    if contextual:
        # Daily/recovery runs rotate the bounded batch every six hours, so a
        # large contextual pool cannot permanently starve higher AppIDs.
        offset = (int(time.time() // 21600) * max_content_details
            if context_offset is None else context_offset) % len(contextual)
        contextual = contextual[offset:] + contextual[:offset]
    detail_requests = 0
    for appid, item in contextual:
        if detail_requests >= max_content_details:
            item["content_context_status"] = "budget_exhausted"
            continue
        detail_requests += 1
        sleep(max(0.0, interval - (monotonic() - last_start)))
        last_start = monotonic()
        try:
            response = session.get("https://store.steampowered.com/api/appdetails",
                params={"appids": appid, "cc": "TW", "l": "english"}, timeout=30)
            if response.status_code == 429:
                item["content_context_status"] = "rate_limited"
                break
            response.raise_for_status()
            wrapper = response.json().get(str(appid), {})
            details = wrapper.get("data") if wrapper.get("success") is True else None
            if (not isinstance(details, dict) or type(details.get("steam_appid")) is not int
                    or details["steam_appid"] != appid):
                item["content_context_status"] = "unavailable"
                continue
            item["content_details"] = {field: details[field] for field in (
                "steam_appid", "type", "name", "short_description", "about_the_game",
                "detailed_description", "content_descriptors") if field in details}
            item["content_context_status"] = "checked"
        except (requests_module.RequestException, ValueError, TypeError, AttributeError):
            item["content_context_status"] = "retry"
    return rows
