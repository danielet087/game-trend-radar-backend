"""Pure Steam adult-descriptor and audited-ledger rules.

Ledger evidence keeps its original raw-set comparison. Candidate metadata uses
the existing integer coercion and malformed-descriptor fallback instead. These
are distinct source contracts, so neither path normalizes the other one.
"""
from __future__ import annotations

from typing import Any

EXCLUDED_DESCRIPTORS = frozenset({3, 4})


def excluded_appids_from_document(
    doc: Any, *, excluded_descriptors: frozenset[int] = EXCLUDED_DESCRIPTORS,
) -> set[int]:
    """Read audited AppIDs without hiding malformed ledger evidence."""
    if set(doc["criteria"]["exclude_content_descriptor_ids"]) != excluded_descriptors:
        raise RuntimeError("Steam adult exclusion criterion changed unexpectedly")
    rows = doc.get("games")
    if not isinstance(rows, list):
        raise RuntimeError("Steam adult exclusion ledger malformed")
    return {
        int(row["appid"]) for row in rows
        if set(row.get("excluded_descriptor_ids", [])) & excluded_descriptors
    }


def is_disallowed(
    row: dict[str, Any], blocked: set[int], *,
    excluded_descriptors: frozenset[int] = EXCLUDED_DESCRIPTORS,
) -> bool:
    """Apply the existing AppID and descriptor decisions to candidate metadata."""
    try:
        appid = int(row["appid"])
    except (TypeError, ValueError, KeyError):
        return True
    descriptors = row.get("content_descriptorids") or row.get("content_descriptors") or []
    if isinstance(descriptors, dict):
        descriptors = descriptors.get("ids") or []
    try:
        flagged = bool({int(x) for x in descriptors} & excluded_descriptors)
    except (TypeError, ValueError):
        flagged = False
    return appid in blocked or flagged
