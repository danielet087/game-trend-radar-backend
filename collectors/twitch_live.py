from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

LOGGER = logging.getLogger(__name__)

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"
DEFAULT_STEAM_JSON_URL = (
    "https://raw.githubusercontent.com/danielet087/game-trend-radar/"
    "main/data/steam_upcoming.json"
)


@dataclass(frozen=True)
class TwitchGame:
    id: str
    name: str
    box_art_url: str | None
    igdb_id: str | None


class TwitchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout_seconds: float = 20.0,
        request_interval: float = 0.15,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_seconds = timeout_seconds
        self.request_interval = max(0.0, request_interval)
        self.session = requests.Session()
        self.access_token: str | None = None
        self._last_request_at = 0.0

    def authenticate(self) -> None:
        # Send credentials in the request body, never in a URL or an exception.
        try:
            response = self.session.post(
                TWITCH_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"HTTP {status}" if status is not None else type(exc).__name__
            raise RuntimeError(f"Twitch OAuth request failed: {reason}") from None
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Twitch OAuth response did not include access_token")
        self.access_token = str(token)

    def _wait(self) -> None:
        if self.request_interval <= 0:
            return
        now = time.monotonic()
        delay = self.request_interval - (now - self._last_request_at)
        if delay > 0:
            time.sleep(delay)

    def get(
        self,
        endpoint: str,
        *,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.access_token:
            self.authenticate()

        url = f"{TWITCH_API_BASE}/{endpoint.lstrip('/')}"
        headers = {
            "Client-Id": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
        }

        last_error: Exception | str | None = None
        for attempt in range(5):
            self._wait()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                self._last_request_at = time.monotonic()

                if response.status_code == 401 and attempt == 0:
                    self.access_token = None
                    self.authenticate()
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    continue

                if response.status_code == 429:
                    reset_at = response.headers.get("Ratelimit-Reset")
                    delay = 10.0
                    if reset_at and reset_at.isdigit():
                        delay = max(1.0, int(reset_at) - time.time() + 1.0)
                    delay = min(delay, 120.0)
                    LOGGER.warning("Twitch rate limit reached; sleeping %.1fs", delay)
                    time.sleep(delay)
                    continue

                if response.status_code in {500, 502, 503, 504}:
                    delay = min(2 ** attempt, 30)
                    last_error = f"HTTP {response.status_code}"
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()

            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                last_error = f"HTTP {status}" if status is not None else type(exc).__name__
                if attempt == 4:
                    break
                time.sleep(min(2 ** attempt, 30))

        raise RuntimeError(f"Twitch API request failed: {last_error}")

    def get_stream_pages(
        self,
        *,
        game_ids: Iterable[str] | None = None,
        max_pages: int = 10,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        page_size = max(1, min(page_size, 100))
        max_pages = max(1, max_pages)
        game_ids = [str(x) for x in (game_ids or []) if str(x)]

        params: list[tuple[str, str]] = [("first", str(page_size))]
        for game_id in game_ids[:100]:
            params.append(("game_id", game_id))

        streams: list[dict[str, Any]] = []
        after: str | None = None

        for page in range(max_pages):
            page_params = list(params)
            if after:
                page_params.append(("after", after))

            payload = self.get("streams", params=page_params)
            rows = payload.get("data") or []
            if not isinstance(rows, list) or not rows:
                break

            streams.extend(row for row in rows if isinstance(row, dict))
            LOGGER.info(
                "Twitch streams page %d/%d: %d rows, %d total",
                page + 1,
                max_pages,
                len(rows),
                len(streams),
            )

            cursor = ((payload.get("pagination") or {}).get("cursor"))
            if not cursor or len(rows) < page_size:
                break
            after = str(cursor)

        return streams

    def get_games_by_names(self, names: Iterable[str]) -> dict[str, TwitchGame]:
        names = [str(name).strip() for name in names if str(name).strip()]
        found: dict[str, TwitchGame] = {}

        for start in range(0, len(names), 100):
            batch = names[start:start + 100]
            params: list[tuple[str, str]] = [("name", name) for name in batch]
            payload = self.get("games", params=params)

            for row in payload.get("data") or []:
                if not isinstance(row, dict):
                    continue
                game = TwitchGame(
                    id=str(row.get("id") or ""),
                    name=str(row.get("name") or ""),
                    box_art_url=str(row.get("box_art_url") or "") or None,
                    igdb_id=str(row.get("igdb_id") or "") or None,
                )
                if game.id and game.name:
                    found[game.name.casefold()] = game

        return found

    def get_games_by_ids(self, game_ids: Iterable[str]) -> dict[str, TwitchGame]:
        game_ids = list(dict.fromkeys(str(game_id).strip() for game_id in game_ids if str(game_id).strip()))
        found: dict[str, TwitchGame] = {}

        for start in range(0, len(game_ids), 100):
            batch = game_ids[start:start + 100]
            params: list[tuple[str, str]] = [("id", game_id) for game_id in batch]
            payload = self.get("games", params=params)

            for row in payload.get("data") or []:
                if not isinstance(row, dict):
                    continue
                game = TwitchGame(
                    id=str(row.get("id") or ""),
                    name=str(row.get("name") or ""),
                    box_art_url=str(row.get("box_art_url") or "") or None,
                    igdb_id=str(row.get("igdb_id") or "") or None,
                )
                if game.id:
                    found[game.id] = game

        return found


def load_steam_games(url: str = DEFAULT_STEAM_JSON_URL, *, timeout_seconds: float = 15.0) -> list[dict[str, Any]]:
    try:
        response = requests.get(url, timeout=timeout_seconds)
        if response.status_code == 404:
            LOGGER.info("Steam public JSON is not available yet; tracked Twitch games will be empty")
            return []
        response.raise_for_status()
        payload = response.json()
        games = payload.get("games") or []
        return [game for game in games if isinstance(game, dict)]
    except (requests.RequestException, ValueError, TypeError) as exc:
        LOGGER.warning("Could not load Steam public JSON: %s", exc)
        return []


def aggregate_streams(streams: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}

    for stream in streams:
        game_id = str(stream.get("game_id") or "")
        if not game_id:
            continue

        bucket = buckets.setdefault(
            game_id,
            {
                "game_id": game_id,
                "game_name": str(stream.get("game_name") or ""),
                "streamer_count": 0,
                "viewer_count": 0,
                "languages": Counter(),
            },
        )
        bucket["streamer_count"] += 1
        bucket["viewer_count"] += int(stream.get("viewer_count") or 0)

        language = str(stream.get("language") or "other")
        bucket["languages"][language] += 1

    result: list[dict[str, Any]] = []
    for bucket in buckets.values():
        languages: Counter = bucket.pop("languages")
        bucket["language_streamers"] = dict(languages.most_common())
        result.append(bucket)

    result.sort(
        key=lambda row: (
            -int(row["viewer_count"]),
            -int(row["streamer_count"]),
            str(row["game_name"]).casefold(),
        )
    )
    return result


def build_tracked_game_summary(
    steam_games: list[dict[str, Any]],
    twitch_games: dict[str, TwitchGame],
    tracked_streams: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    aggregates = {row["game_id"]: row for row in aggregate_streams(tracked_streams)}
    rows: list[dict[str, Any]] = []

    for steam_game in steam_games:
        steam_name = str(steam_game.get("name") or "").strip()
        twitch_game = twitch_games.get(steam_name.casefold())
        if not twitch_game:
            rows.append(
                {
                    "steam_appid": steam_game.get("appid"),
                    "steam_name": steam_name,
                    "twitch_game_id": None,
                    "twitch_name": None,
                    "matched": False,
                    "streamer_count": 0,
                    "viewer_count": 0,
                    "language_streamers": {},
                }
            )
            continue

        aggregate = aggregates.get(twitch_game.id) or {}
        rows.append(
            {
                "steam_appid": steam_game.get("appid"),
                "steam_name": steam_name,
                "twitch_game_id": twitch_game.id,
                "twitch_name": twitch_game.name,
                "matched": True,
                "streamer_count": int(aggregate.get("streamer_count") or 0),
                "viewer_count": int(aggregate.get("viewer_count") or 0),
                "language_streamers": aggregate.get("language_streamers") or {},
                "box_art_url": twitch_game.box_art_url,
                "igdb_id": twitch_game.igdb_id,
            }
        )

    rows.sort(
        key=lambda row: (
            -int(row["viewer_count"]),
            -int(row["streamer_count"]),
            str(row["steam_name"]).casefold(),
        )
    )
    return rows


def collect_twitch(
    *,
    client_id: str,
    client_secret: str,
    global_pages: int = 10,
    tracked_pages: int = 50,
    top_games_limit: int = 100,
    steam_json_url: str = DEFAULT_STEAM_JSON_URL,
) -> dict[str, Any]:
    client = TwitchClient(client_id, client_secret)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # Global discovery is intentionally a stable sample of Twitch's most-viewed streams,
    # not a claim that every live channel on Twitch was enumerated.
    global_streams = client.get_stream_pages(max_pages=global_pages)
    global_aggregates = aggregate_streams(global_streams)
    global_game_meta = client.get_games_by_ids(row["game_id"] for row in global_aggregates)

    top_games: list[dict[str, Any]] = []
    for row in global_aggregates:
        meta = global_game_meta.get(row["game_id"])
        if not meta or not meta.igdb_id:
            continue
        enriched = dict(row)
        enriched["box_art_url"] = meta.box_art_url
        enriched["igdb_id"] = meta.igdb_id
        top_games.append(enriched)
        if len(top_games) >= top_games_limit:
            break

    steam_games = load_steam_games(steam_json_url)
    names = [str(game.get("name") or "") for game in steam_games]
    twitch_games = client.get_games_by_names(names) if names else {}

    tracked_ids = [game.id for game in twitch_games.values()]
    tracked_streams: list[dict[str, Any]] = []
    if tracked_ids:
        tracked_streams = client.get_stream_pages(
            game_ids=tracked_ids[:100],
            max_pages=tracked_pages,
        )

    tracked_games = build_tracked_game_summary(
        steam_games,
        twitch_games,
        tracked_streams,
    )

    return {
        "generated_at": generated_at,
        "source": "Twitch Helix API",
        "coverage": {
            "global_stream_sample_size": len(global_streams),
            "global_stream_pages": global_pages,
            "global_metrics_are_sampled": True,
            "global_game_filter": "Twitch categories with a non-empty IGDB game ID",
            "tracked_game_stream_pages": tracked_pages,
            "tracked_game_count": len(steam_games),
            "tracked_game_matches": sum(1 for row in tracked_games if row["matched"]),
            "region_note": (
                "Twitch Helix exposes broadcast language but not broadcaster physical location. "
                "No Taiwan/Asia location inference is applied in this dataset."
            ),
        },
        "top_games": top_games,
        "tracked_games": tracked_games,
    }


def write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def collect_from_environment(
    *,
    output_path: str | Path = "output/twitch_live.json",
    global_pages: int = 10,
    tracked_pages: int = 50,
) -> Path:
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")

    payload = collect_twitch(
        client_id=client_id,
        client_secret=client_secret,
        global_pages=global_pages,
        tracked_pages=tracked_pages,
    )
    return write_json(payload, output_path)
