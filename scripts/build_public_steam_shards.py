from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from radar_backend.adapters.steam_localized_titles import add_traditional_display_names
from radar_backend.adapters.public_catalog import (
    PLAYER_CATEGORY_FIELDS, keep_newer_release, preserve_player_categories,
    write_catalog_projection,
)
from radar_backend.application import public_shards as _shard_application
from radar_backend.domain import public_shards as _shard_rules
from radar_backend.state import public_shards as _shard_state
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.twitch_steam_admission import is_twitch_qualified, preserve_twitch_admission

CORE_FIELDS = _shard_rules.CORE_FIELDS


def load_json(path: Path, default: Any) -> Any:
    return _shard_state.load_json(path, default, json_module=json)


def write_if_changed(path: Path, payload: Any) -> bool:
    return _shard_state.write_if_changed(path, payload, load_json=load_json, json_module=json)


def valid_record(row: Any) -> bool:
    return _shard_rules.valid_record(row, date_type=date, is_twitch_qualified=is_twitch_qualified)


def merge_game(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    return _shard_rules.merge_game(
        existing, incoming, keep_newer_release=keep_newer_release,
        preserve_twitch_admission=preserve_twitch_admission,
        preserve_player_categories=preserve_player_categories,
        add_traditional_display_names=add_traditional_display_names,
        core_fields=CORE_FIELDS, player_category_fields=PLAYER_CATEGORY_FIELDS,
    )


def build(
    input_path: Path,
    frontend: Path,
    *,
    authoritative_future: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _shard_application.build(
        input_path, frontend, authoritative_future=authoritative_future, now=now,
        clock=lambda: datetime.now(timezone.utc),
        load_json=load_json, write_if_changed=write_if_changed,
        exists=_shard_state.exists, glob=_shard_state.glob, unlink=_shard_state.unlink,
        excluded_appids=excluded_appids, is_disallowed=is_disallowed,
        is_twitch_qualified=is_twitch_qualified, valid_record=valid_record,
        merge_game=merge_game, add_traditional_display_names=add_traditional_display_names,
        write_catalog_projection=write_catalog_projection,
        timezone_type=timezone, timedelta_type=timedelta,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="output/steam_upcoming.json")
    parser.add_argument("--frontend", default="frontend")
    parser.add_argument(
        "--authoritative-future",
        action="store_true",
        help="Remove future frontend AppIDs that are absent from the input master.",
    )
    args = parser.parse_args()
    result = build(
        Path(args.input),
        Path(args.frontend),
        authoritative_future=args.authoritative_future,
    )
    print("STEAM_SHARDS", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
