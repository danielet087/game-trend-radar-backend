"""Enrich candidate names with explicit title and display operations."""
from __future__ import annotations

from typing import Callable

from radar_backend.domain.localized_titles import actual_zh_tw_title


def enrich_tw_names(
    games: list[dict], store_names: dict[int, str], *,
    select_title: Callable = actual_zh_tw_title, display_names: Callable,
) -> dict[str, int]:
    """Keep original names, reviewed titles and existing mutation order."""
    results = {
        "total": 0, "official_zh_tw": 0, "english_fallback": 0,
        "store_not_returned": 0,
    }
    for game in games:
        results["total"] += 1
        appid = int(game["appid"])
        original_english = str(game.get("name_en") or game["name"]).strip()
        if not original_english:
            raise RuntimeError(f"Missing English title for app {appid}")
        game["name_en"] = original_english
        candidate = select_title(store_names.get(appid))
        if candidate:
            game["name_zh_tw"] = candidate
            results["official_zh_tw"] += 1
        elif game.get("name_zh_tw"):
            results["official_zh_tw"] += 1
        else:
            results["english_fallback"] += 1
        if appid not in store_names:
            results["store_not_returned"] += 1
        display_names(game)
    return results
