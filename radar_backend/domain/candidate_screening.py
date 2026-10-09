"""Pure Store display-date and explicit-content candidate screening rules."""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable


SEXUAL_CONTENT_IDS = frozenset({3, 4})
EXPLICIT_DESC = re.compile(
    r"\b(?:nsfw|hentai|pornograph(?:y|ic)|erotic(?:a)?|sex game|adult game|"
    r"sexually explicit|explicit sexual|uncensored sexual|sex scenes|"
    r"sexual acts|lots of sex)\b", re.I,
)


def is_explicit_sex_game(
    store_item: dict, *, sexual_content_ids=SEXUAL_CONTENT_IDS,
    explicit_pattern=EXPLICIT_DESC,
) -> bool:
    """Keep ordinary violence, maturity and romance outside the adult rule."""
    ids = set(store_item.get("content_descriptorids") or [])
    if ids & sexual_content_ids:
        return True
    tags = [
        row.get("tagid") for row in (store_item.get("tags") or [])
        if isinstance(row, dict)
    ]
    description = str(
        (store_item.get("basic_info") or {}).get("short_description") or ""
    )
    return bool(
        12095 in tags[:5]
        and (9130 in tags[:10] or 6650 in tags[:5])
        and explicit_pattern.search(description)
    )


def classify(
    existing: dict, item: dict | None, *, explicit_predicate: Callable | None = None,
) -> tuple[dict | None, str]:
    """Only a published full-day Store display is follower-eligible."""
    if not item or not isinstance(item.get("release"), dict):
        return None, "unavailable"
    label = item["release"].get("coming_soon_display")
    if label != "date_full":
        return None, str(label or "unknown")
    if existing.get("release_precision") != "day" or not existing.get("release_start"):
        return None, "invalid_original_date"
    predicate = explicit_predicate if explicit_predicate is not None else is_explicit_sex_game
    if predicate(item):
        return None, "sexual_content"
    verified = dict(existing)
    verified["release_display_precision"] = "date_full"
    verified["release_display_provider"] = "Steam IStoreBrowseService/GetItems"
    verified["sexual_content_screened"] = True
    return verified, "eligible"


def snapshot_records(
    catalog: dict, store_items: dict[int, dict], *, blocked: set[int],
    classifier: Callable | None = None, sort_reasons: bool = True,
) -> dict:
    """Classify every original row without reading a ledger or a clock."""
    original = catalog.get("games")
    if not isinstance(original, list):
        raise ValueError("Invalid original candidate games array")
    classify_row = classifier if classifier is not None else classify
    result: list[dict] = []
    reasons = Counter()
    for game in original:
        if int(game["appid"]) in blocked:
            reasons["audited_adult_exclusion"] += 1
            continue
        row, status = classify_row(game, store_items.get(int(game["appid"])))
        reasons[status] += 1
        if row is not None:
            result.append(row)
    if sum(reasons.values()) != len(original):
        raise RuntimeError("Eligibility classification coverage incomplete")
    return {
        "source": "data/steam_candidates.json",
        "source_count": len(original),
        "rule": (
            "Steam Store Browse coming_soon_display=date_full; exclude "
            "sexual content descriptors 3/4 and strongly explicit adult games"
        ),
        "count": len(result),
        "excluded": len(original) - len(result),
        "reasons": dict(sorted(reasons.items())) if sort_reasons else dict(reasons),
        "games": result,
    }
