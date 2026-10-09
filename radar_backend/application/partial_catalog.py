"""Load exclusions before applying the partial catalog merge rules."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from radar_backend.domain import partial_catalog


def merge_partial_segment(
    existing_games: list[dict[str, Any]],
    newly_qualified: list[dict[str, Any]],
    *,
    today: date,
    excluded_appids: Callable,
    prune_released: Callable,
    is_disallowed: Callable,
    preserve_twitch_admission: Callable,
    preserve_player_categories: Callable,
) -> list[dict[str, Any]]:
    blocked = excluded_appids()
    return partial_catalog.merge_partial_segment(
        existing_games,
        newly_qualified,
        today=today,
        blocked=blocked,
        prune_released=prune_released,
        is_disallowed=is_disallowed,
        preserve_twitch_admission=preserve_twitch_admission,
        preserve_player_categories=preserve_player_categories,
    )
