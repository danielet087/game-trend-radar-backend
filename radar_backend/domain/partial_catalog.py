"""Pure rules for retaining history and merging an incomplete lookup batch."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable


def prune_released(games: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Keep calendar history; the legacy name does not imply date pruning."""
    del today
    return [game for game in games if isinstance(game, dict)]


def merge_partial_segment(
    existing_games: list[dict[str, Any]],
    newly_qualified: list[dict[str, Any]],
    *,
    today: date,
    blocked,
    prune_released: Callable,
    is_disallowed: Callable,
    preserve_twitch_admission: Callable,
    preserve_player_categories: Callable,
) -> list[dict[str, Any]]:
    """Only replace an AppID actually returned by this partial batch."""
    by_appid: dict[int, dict[str, Any]] = {}
    for game in prune_released(existing_games, today) + newly_qualified:
        if is_disallowed(game, blocked):
            continue
        try:
            appid = int(game["appid"])
        except (KeyError, ValueError, TypeError):
            continue
        prior = by_appid.get(appid, {})
        by_appid[appid] = preserve_player_categories(
            prior, preserve_twitch_admission(prior, game),
        )
    return sorted(
        by_appid.values(),
        key=lambda game: (
            -int(game.get("followers") or 0),
            str(game.get("release_start") or "9999-12-31"),
            int(game["appid"]),
        ),
    )
