"""Steam official adult-only and primary-content screening guard.

Persistent exclusions originate in data/steam_adult_exclusion.json, audited
against Steam Store api/appdetails. Descriptor 4 requires current purpose context.
Do not drop the exclusion gate when publishing already-qualified history.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from radar_backend.domain import adult_exclusions as _rules
from radar_backend.state import adult_exclusions as _ledger

EXCLUSION_PATH = _ledger.EXCLUSION_PATH
EXCLUDED_DESCRIPTORS = _rules.EXCLUDED_DESCRIPTORS


def excluded_appids(path: Path = EXCLUSION_PATH) -> set[int]:
    return _ledger.excluded_appids(path, excluded_descriptors=EXCLUDED_DESCRIPTORS)


def is_disallowed(row: dict[str, Any], blocked: set[int]) -> bool:
    return _rules.is_disallowed(row, blocked, excluded_descriptors=EXCLUDED_DESCRIPTORS)
