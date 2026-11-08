"""Pure Steam adult-descriptor and audited-ledger rules.

Ledger evidence keeps its original raw-set comparison. Candidate metadata uses
the existing integer coercion and malformed-descriptor fallback instead. These
are distinct source contracts, so neither path normalizes the other one.
"""
from __future__ import annotations

from typing import Any

from radar_backend.domain.mature_content_reviews import decide_content, reviewed_descriptors

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
    blocked = set()
    for row in rows:
        descriptors = set(row.get("excluded_descriptor_ids", []))
        if descriptors & excluded_descriptors:
            appid = int(row["appid"])
            if reviewed_descriptors(appid, descriptors) & excluded_descriptors:
                blocked.add(appid)
    return blocked


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
        normalized = {int(x) for x in descriptors}
        flagged = bool(reviewed_descriptors(appid, normalized) & excluded_descriptors)
    except (TypeError, ValueError):
        normalized, flagged = set(), False
    decision = decide_content({**row, "appid": appid, "content_descriptorids": list(normalized)}, hard_ids=excluded_descriptors)
    return appid in blocked or flagged or decision.status != "screened"
