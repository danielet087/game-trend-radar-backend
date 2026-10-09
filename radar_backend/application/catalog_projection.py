"""Publish the browser projection through explicit revision and state ports."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from radar_backend.domain.catalog_projection import catalog_payload, catalog_revision


def write_catalog_projection(
    data_dir: Path,
    rows: list[dict],
    generated_at: str,
    *,
    revision_for_rows: Callable = catalog_revision,
    payload_for_rows: Callable = catalog_payload,
    exists: Callable,
    read_revision: Callable,
    write_payload: Callable,
) -> dict:
    revision = revision_for_rows(rows)
    path = data_dir / 'catalog.json'
    payload = payload_for_rows(rows, generated_at, revision)
    if exists(path):
        try:
            if read_revision(path) == revision:
                return {'catalog_path': 'catalog.json', 'catalog_revision': revision}
        except (OSError, ValueError, TypeError):
            pass
    write_payload(path, payload)
    return {'catalog_path': 'catalog.json', 'catalog_revision': revision}
