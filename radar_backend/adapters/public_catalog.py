"""Compose accepted catalog metadata rules and the browser projection."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar_backend.application import catalog_projection as application
from radar_backend.domain import catalog_metadata as metadata
from radar_backend.domain.catalog_metadata import (
    PLAYER_CATEGORY_FIELDS, PLAYER_CATEGORY_SOURCES, RELEASE_FIELDS,
    keep_newer_release,
)
from radar_backend.domain.catalog_projection import FIELDS, catalog_payload, catalog_revision
from radar_backend.state import catalog_projection as state


def player_category_snapshot(row: dict) -> tuple[datetime, dict] | None:
    return metadata.player_category_snapshot(
        row, now=lambda: datetime.now(timezone.utc),
        datetime_type=datetime, timedelta_type=timedelta, timezone_type=timezone,
        fields=PLAYER_CATEGORY_FIELDS, sources=PLAYER_CATEGORY_SOURCES,
    )


def preserve_player_categories(existing: dict, incoming: dict) -> dict:
    return metadata.preserve_player_categories(
        existing, incoming, snapshot=player_category_snapshot, fields=PLAYER_CATEGORY_FIELDS,
    )


def write_catalog_projection(data_dir: Path, rows: list[dict], generated_at: str) -> dict:
    return application.write_catalog_projection(
        data_dir, rows, generated_at, revision_for_rows=catalog_revision,
        payload_for_rows=catalog_payload, exists=state.exists,
        read_revision=state.read_revision, write_payload=state.write_payload,
    )


__all__ = [
    "FIELDS", "PLAYER_CATEGORY_FIELDS", "PLAYER_CATEGORY_SOURCES", "RELEASE_FIELDS",
    "player_category_snapshot", "preserve_player_categories", "keep_newer_release",
    "write_catalog_projection",
]
