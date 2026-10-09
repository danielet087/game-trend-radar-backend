"""Compose partial master merging with adult exclusions and retained evidence."""
from __future__ import annotations
from datetime import date
from typing import Any
from radar_core.domain.twitch_admission import preserve_twitch_admission
from radar_backend.adapters.public_catalog import preserve_player_categories
from radar_backend.domain.adult_exclusions import is_disallowed
from radar_backend.state.adult_exclusions import excluded_appids
from radar_backend.domain import partial_catalog as _partial_rules
from radar_backend.application import partial_catalog as _partial_application


def prune_released(games: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    return _partial_rules.prune_released(games, today)


def merge_partial_segment(
    existing_games: list[dict[str, Any]],
    newly_qualified: list[dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, Any]]:
    return _partial_application.merge_partial_segment(
        existing_games, newly_qualified, today=today, excluded_appids=excluded_appids,
        prune_released=prune_released, is_disallowed=is_disallowed,
        preserve_twitch_admission=preserve_twitch_admission,
        preserve_player_categories=preserve_player_categories,
    )
