"""Compose the existing name-only refresh for already published Steam games.

Catalog projection remains an explicit bridge until its separate migration.
This adapter does not query Followers, admit new games, or publish Git commits.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from opencc import OpenCC

from radar_core.domain.twitch_admission import (
    is_twitch_qualified, preserve_twitch_admission,
)
from radar_backend.adapters.steam_published_titles import localized_batch
from radar_backend.application import published_titles as application
from radar_backend.domain.published_titles import candidate
from radar_backend.domain.published_titles import update_title as update_title_rule
from radar_backend.state.published_titles import exists, read, save_changed
from scripts.public_catalog import write_catalog_projection

CONVERT = OpenCC("s2t")
LOG = logging.getLogger(__name__)


def update_title(row: dict, tw: str | None, cn: str | None) -> dict:
    return update_title_rule(row, tw, cn, convert=lambda name: CONVERT.convert(name))


def refresh(
    data_dir: Path, *, session: requests.Session,
    batch_size: int = 30, interval: float = 2.0,
) -> dict:
    return application.refresh(
        data_dir, session=session, batch_size=batch_size, interval=interval,
        read=read, save_changed=save_changed, exists=exists,
        localized_batch=localized_batch, candidate=candidate, update_title=update_title,
        is_twitch_qualified=is_twitch_qualified,
        preserve_twitch_admission=preserve_twitch_admission,
        write_catalog_projection=write_catalog_projection,
        sleep=time.sleep, clock=lambda: datetime.now(timezone.utc), logger=LOG,
    )


__all__ = ["candidate", "update_title", "localized_batch", "refresh"]
