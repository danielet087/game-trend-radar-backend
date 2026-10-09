"""Read the required Steam adult-exclusion ledger without fallback state."""
from __future__ import annotations

import json
from pathlib import Path

from radar_backend.domain.adult_exclusions import (
    EXCLUDED_DESCRIPTORS, excluded_appids_from_document,
)

EXCLUSION_PATH = Path(__file__).resolve().parents[2] / "data" / "steam_adult_exclusion.json"


def excluded_appids(
    path: Path = EXCLUSION_PATH, *,
    excluded_descriptors: frozenset[int] = EXCLUDED_DESCRIPTORS,
) -> set[int]:
    """Keep missing-file and malformed-JSON errors visible to every consumer."""
    if not path.is_file():
        raise RuntimeError(f"Steam adult exclusion ledger missing: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    return excluded_appids_from_document(doc, excluded_descriptors=excluded_descriptors)
