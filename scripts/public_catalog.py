"""Small browser projection, derived exclusively from accepted AppID records."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar_backend.application import catalog_projection as _application
from radar_backend.domain import catalog_metadata as _metadata
from radar_backend.domain import catalog_projection as _projection
from radar_backend.state import catalog_projection as _state

RELEASE_FIELDS = _metadata.RELEASE_FIELDS
PLAYER_CATEGORY_FIELDS = _metadata.PLAYER_CATEGORY_FIELDS
PLAYER_CATEGORY_SOURCES = _metadata.PLAYER_CATEGORY_SOURCES
FIELDS = _projection.FIELDS


def player_category_snapshot(row: dict) -> tuple[datetime, dict] | None:
    return _metadata.player_category_snapshot(
        row, now=lambda: datetime.now(timezone.utc), datetime_type=datetime,
        timedelta_type=timedelta, timezone_type=timezone,
        fields=PLAYER_CATEGORY_FIELDS, sources=PLAYER_CATEGORY_SOURCES,
    )


def preserve_player_categories(existing: dict, incoming: dict) -> dict:
    return _metadata.preserve_player_categories(
        existing, incoming, snapshot=player_category_snapshot, fields=PLAYER_CATEGORY_FIELDS,
    )


def keep_newer_release(existing: dict, incoming: dict) -> dict:
    return _metadata.keep_newer_release(
        existing, incoming, datetime_type=datetime,
        timezone_type=timezone, fields=RELEASE_FIELDS,
    )


def write_catalog_projection(data_dir: Path, rows: list[dict], generated_at: str) -> dict:
    return _application.write_catalog_projection(
        data_dir, rows, generated_at,
        revision_for_rows=lambda values: _projection.catalog_revision(
            values, json_module=json, hashlib_module=hashlib,
        ),
        payload_for_rows=lambda values, at, revision: _projection.catalog_payload(
            values, at, revision, fields=FIELDS,
        ),
        exists=_state.exists,
        read_revision=lambda path: _state.read_revision(path, json_module=json),
        write_payload=lambda path, payload: _state.write_payload(path, payload, json_module=json),
    )
