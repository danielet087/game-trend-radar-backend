"""Preview metadata and locale sequencing with explicit external dependencies."""
from __future__ import annotations

from typing import Any, Callable

from radar_backend.domain import preview_metadata as rules


def add_traditional_name(
    session, appid: int, game: dict[str, Any], english_details: dict[str, Any], *,
    delay_seconds: float, sleep: Callable, app_details: Callable,
    localized_names: Callable, add_traditional_display_names: Callable,
) -> None:
    sleep(delay_seconds)
    traditional_details = app_details(session, appid, language="tchinese")
    name_en, name_zh_tw = localized_names(english_details, traditional_details)
    game["name"] = name_en or game["name"]
    game["name_en"] = name_en or game["name"]
    game["name_zh_tw"] = name_zh_tw
    add_traditional_display_names(game)


def normalized_metadata(
    appid: int, details: dict[str, Any], followers: int | None,
    checked_at: str | None, *, browse_release: dict[str, Any] | None = None,
    resolved_store_date: Callable, preserve_player_categories: Callable,
    clock: Callable,
) -> dict[str, Any] | None:
    release_details = details.get("release_date") or {}
    release = resolved_store_date(
        appid, release_details.get("date"), browse_release, fallback_detail=release_details,
    )
    if release["release_precision"] != "day" or not release["release_start"]:
        return None
    game = rules.normalized_metadata_fields(appid, details, followers, checked_at, release)
    if rules.has_official_player_categories(appid, details):
        game.update({
            "categories": details["categories"],
            "categories_source": rules.PLAYER_CATEGORY_SOURCE,
            "categories_checked_at": clock(),
        })
    return preserve_player_categories({}, game)
