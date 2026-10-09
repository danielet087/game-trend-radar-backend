"""Compose candidate classification with exclusion and observation ports."""
from __future__ import annotations

from typing import Callable

from radar_backend.domain.candidate_screening import snapshot_records


def build_snapshot(
    catalog: dict, store_items: dict[int, dict], *, clock: Callable,
    exclusion_loader: Callable, classifier: Callable | None = None,
) -> dict:
    """Validate before reading exclusions; timestamp a complete classification."""
    original = catalog.get("games")
    if not isinstance(original, list):
        raise ValueError("Invalid original candidate games array")
    blocked = exclusion_loader()
    records = snapshot_records(
        {"games": original}, store_items, blocked=blocked, classifier=classifier,
        sort_reasons=False,
    )
    snapshot = {"screened_at": clock().isoformat(), **records}
    snapshot["reasons"] = dict(sorted(snapshot["reasons"].items()))
    return snapshot
