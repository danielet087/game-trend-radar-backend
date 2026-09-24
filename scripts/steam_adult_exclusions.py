"""Steam official Adult Only / Frequent Sexual Content exclusion guard.

Persistent exclusions originate in data/steam_adult_exclusion.json, audited
against Steam Store api/appdetails content_descriptors 3 and 4.
Do not drop the exclusion gate when publishing already-qualified history.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXCLUSION_PATH = Path(__file__).resolve().parents[1] / "data" / "steam_adult_exclusion.json"
EXCLUDED_DESCRIPTORS = frozenset({3, 4})


def excluded_appids(path: Path = EXCLUSION_PATH) -> set[int]:
    if not path.is_file():
        raise RuntimeError(f"Steam adult exclusion ledger missing: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if set(doc["criteria"]["exclude_content_descriptor_ids"]) != EXCLUDED_DESCRIPTORS:
        raise RuntimeError("Steam adult exclusion criterion changed unexpectedly")
    rows = doc.get("games")
    if not isinstance(rows, list):
        raise RuntimeError("Steam adult exclusion ledger malformed")
    return {
        int(row["appid"]) for row in rows
        if set(row.get("excluded_descriptor_ids", [])) & EXCLUDED_DESCRIPTORS
    }


def is_disallowed(row: dict[str, Any], blocked: set[int]) -> bool:
    try:
        appid = int(row["appid"])
    except (TypeError, ValueError, KeyError):
        return True
    descriptors = row.get("content_descriptorids") or row.get("content_descriptors") or []
    if isinstance(descriptors, dict):
        descriptors = descriptors.get("ids") or []
    try:
        flagged = bool({int(x) for x in descriptors} & EXCLUDED_DESCRIPTORS)
    except (TypeError, ValueError):
        flagged = False
    return appid in blocked or flagged
