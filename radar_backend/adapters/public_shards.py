"""Compose accepted Steam records into the existing public shard layout."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from radar_core.domain.twitch_admission import (
    is_twitch_qualified, preserve_twitch_admission,
)
from radar_backend.adapters.public_catalog import (
    PLAYER_CATEGORY_FIELDS, keep_newer_release, preserve_player_categories,
    write_catalog_projection,
)
from radar_backend.adapters.steam_localized_titles import add_traditional_display_names
from radar_backend.application import public_shards as application
from radar_backend.domain import public_shards as rules
from radar_backend.domain.adult_exclusions import is_disallowed
from radar_backend.domain.public_shards import CORE_FIELDS
from radar_backend.state import public_shards as state
from radar_backend.state.adult_exclusions import excluded_appids


def load_json(path: Path, default: Any) -> Any:
    return state.load_json(path, default)


def write_if_changed(path: Path, payload: Any) -> bool:
    return state.write_if_changed(path, payload, load_json=load_json)


def valid_record(row: Any) -> bool:
    return rules.valid_record(row, date_type=date, is_twitch_qualified=is_twitch_qualified)


def merge_game(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    return rules.merge_game(
        existing, incoming, keep_newer_release=keep_newer_release,
        preserve_twitch_admission=preserve_twitch_admission,
        preserve_player_categories=preserve_player_categories,
        add_traditional_display_names=add_traditional_display_names,
        core_fields=CORE_FIELDS, player_category_fields=PLAYER_CATEGORY_FIELDS,
    )


def build(
    input_path: Path, frontend: Path, *,
    authoritative_future: bool = False, now: datetime | None = None,
) -> dict[str, Any]:
    return application.build(
        input_path, frontend, authoritative_future=authoritative_future, now=now,
        clock=lambda: datetime.now(timezone.utc),
        load_json=load_json, write_if_changed=write_if_changed,
        exists=state.exists, glob=state.glob, unlink=state.unlink,
        excluded_appids=excluded_appids, is_disallowed=is_disallowed,
        is_twitch_qualified=is_twitch_qualified, valid_record=valid_record,
        merge_game=merge_game, add_traditional_display_names=add_traditional_display_names,
        write_catalog_projection=write_catalog_projection,
        timezone_type=timezone, timedelta_type=timedelta,
    )


__all__ = ["CORE_FIELDS", "load_json", "write_if_changed", "valid_record", "merge_game", "build"]
