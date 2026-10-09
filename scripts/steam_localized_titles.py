"""Compatible Steam-only titles and separate Traditional display fields.

Official raw names and supported-language evidence retain their source values.
Shared rules, enrichment and Store transport live in their explicit layers.
"""
from __future__ import annotations

import logging
import time
import requests
from opencc import OpenCC

from radar_backend.adapters import steam_localized_titles as _source
from radar_backend.application import localized_titles as _application
from radar_backend.domain import localized_titles as _rules

_CONVERT_TO_TRADITIONAL = _source._CONVERT_TO_TRADITIONAL
LOG = logging.getLogger(__name__)
STORE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
HAN = _rules.HAN
BATCH = 35


def display_in_traditional(raw: object) -> str | None:
    return _rules.display_in_traditional(raw, convert=_CONVERT_TO_TRADITIONAL.convert)


def add_traditional_display_names(game: dict) -> None:
    return _rules.add_traditional_display_names(game, display=display_in_traditional, han=HAN)


def actual_zh_tw_title(value: object) -> str | None:
    return _rules.actual_zh_tw_title(value, han=HAN)


def fetch_store_tw_names(
    session: requests.Session, ids: list[int], *, batch_size: int = BATCH,
    interval: float = 1.5,
) -> dict[int, str]:
    return _source.fetch_store_tw_names(
        session, ids, batch_size=batch_size, interval=interval,
        sleep=time.sleep, monotonic=time.monotonic, logger=LOG,
        requests_module=requests, url=STORE_URL,
    )


def enrich_tw_names(games: list[dict], store_names: dict[int, str]) -> dict[str, int]:
    return _application.enrich_tw_names(
        games, store_names, select_title=actual_zh_tw_title,
        display_names=add_traditional_display_names,
    )
