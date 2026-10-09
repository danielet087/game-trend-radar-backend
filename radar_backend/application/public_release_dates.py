"""Apply public release metadata to copied rows without follower requests."""
from __future__ import annotations

from typing import Any, Callable

from radar_backend.domain.public_release_dates import resolved_store_date


def corrected_games(
    games: list[dict[str, Any]],
    browse_releases: dict[int, dict[str, Any]] | None = None,
    *,
    resolve: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Preserve records and their cached timestamp provenance during correction."""
    resolver = resolved_store_date if resolve is None else resolve
    releases = browse_releases or {}
    result: list[dict[str, Any]] = []
    for record in games:
        game = dict(record)
        appid = int(game["appid"])
        fallback = None
        if (game.get("release_time_utc") and game.get("release_date_basis")
                in {"steam_structured_release_time", "steam_store_browse_release_time"}):
            key = ("steam_release_date" if game["release_date_basis"] == "steam_store_browse_release_time"
                   else "release_time_utc")
            fallback = {
                key: game["release_time_utc"],
                "release_time_source": game.get("release_time_source"),
            }
        release = resolver(
            appid, game.get("release_raw"), releases.get(appid),
            fallback_detail=fallback,
        )
        game.update(release)
        result.append(game)
    return result
