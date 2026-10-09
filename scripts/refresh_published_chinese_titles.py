"""Refresh Chinese Store titles for ALREADY published Steam AppIDs only.

Do not scan candidates, touch private Followers state, recheck Followers, or
promote previously unqualified games. Update AppID shards and affected months.
"""
from __future__ import annotations

import argparse
import json
from radar_backend.adapters.public_catalog import write_catalog_projection
from scripts.twitch_steam_admission import is_twitch_qualified, preserve_twitch_admission
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from opencc import OpenCC

from radar_backend.application import published_titles as _published_application
from radar_backend.domain import published_titles as _published_rules
from radar_backend.state import published_titles as _published_state
from radar_backend.adapters import steam_published_titles as _published_transport

STORE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
HAN = re.compile(r"[\u3400-\u9fff]")
CONVERT = OpenCC("s2t")
LOG = logging.getLogger(__name__)


def read(path: Path) -> dict:
    return _published_state.read(path)


def save_changed(path: Path, data: dict) -> bool:
    return _published_state.save_changed(path, data)


def candidate(value: object, english: str) -> str | None:
    return _published_rules.candidate(value, english, han=HAN)


def localized_batch(
    session: requests.Session, appids: list[int], language: str, interval: float
) -> dict[int, str]:
    return _published_transport.localized_batch(
        session, appids, language, interval,
        sleep=time.sleep, logger=LOG, requests_module=requests, url=STORE,
    )


def update_title(
    row: dict, tw: str | None, cn: str | None,
) -> dict:
    return _published_rules.update_title(
        row, tw, cn, convert=lambda name: CONVERT.convert(name),
    )


def refresh(
    data_dir: Path,
    *,
    session: requests.Session,
    batch_size: int = 30,
    interval: float = 2.0,
) -> dict:
    return _published_application.refresh(
        data_dir, session=session, batch_size=batch_size, interval=interval,
        read=read, save_changed=save_changed, exists=lambda path: path.exists(),
        localized_batch=localized_batch, candidate=candidate, update_title=update_title,
        is_twitch_qualified=is_twitch_qualified,
        preserve_twitch_admission=preserve_twitch_admission,
        write_catalog_projection=write_catalog_projection,
        sleep=time.sleep, clock=lambda: datetime.now(timezone.utc), logger=LOG,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("frontend/data"))
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 35:
        raise SystemExit("--batch-size must be 1..35")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarChineseNameRefresh/1.0"
    result = refresh(
        args.data_dir,
        session=session,
        batch_size=args.batch_size,
        interval=args.interval,
    )
    print("CHINESE_TITLE_REFRESH_RESULT", json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
