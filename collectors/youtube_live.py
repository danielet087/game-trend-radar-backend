from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

LOGGER = logging.getLogger(__name__)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
TWITCH_JSON_URL = (
    "https://raw.githubusercontent.com/danielet087/game-trend-radar/"
    "main/data/twitch_live.json"
)
STEAM_JSON_URL = (
    "https://raw.githubusercontent.com/danielet087/game-trend-radar/"
    "main/data/steam_upcoming.json"
)
YOUTUBE_JSON_URL = (
    "https://raw.githubusercontent.com/danielet087/game-trend-radar/"
    "main/data/youtube_live.json"
)

ASIA_COUNTRIES = {
    "AE","AF","AM","AZ","BH","BD","BN","BT","CN","CY","GE","HK","ID","IL","IN",
    "IQ","IR","JO","JP","KG","KH","KP","KR","KW","KZ","LA","LB","LK","MM","MN",
    "MO","MV","MY","NP","OM","PH","PK","PS","QA","SA","SG","SY","TH","TJ","TL",
    "TM","TR","TW","UZ","VN","YE",
}


def normalize_text(value: str) -> str:
    value = (value or "").replace("™", " ").replace("®", " ").replace("©", " ")
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def fetch_public_json(url: str, *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=timeout_seconds)
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        LOGGER.warning("Could not fetch public JSON %s: %s", url, exc)
        return {}


def build_known_games(
    twitch_payload: dict[str, Any],
    steam_payload: dict[str, Any],
) -> list[str]:
    names: dict[str, str] = {}

    for row in twitch_payload.get("top_games") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("game_name") or "").strip()
        normalized = normalize_text(name)
        if len(normalized) >= 4:
            names[normalized] = name

    for row in steam_payload.get("games") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        normalized = normalize_text(name)
        if len(normalized) >= 4:
            names[normalized] = name

    return sorted(names.values(), key=lambda name: len(normalize_text(name)), reverse=True)


def build_search_terms(
    twitch_payload: dict[str, Any],
    steam_payload: dict[str, Any],
    *,
    max_terms: int = 20,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    # Upcoming Steam games are the main focus of this site.
    for row in steam_payload.get("games") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        key = normalize_text(name)
        if name and len(key) >= 4 and key not in seen:
            ordered.append(name)
            seen.add(key)

    # Fill remaining slots with current Twitch game leaders.
    for row in twitch_payload.get("top_games") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("game_name") or "").strip()
        key = normalize_text(name)
        if name and len(key) >= 4 and key not in seen:
            ordered.append(name)
            seen.add(key)
        if len(ordered) >= max_terms:
            break

    return ordered[:max_terms]


def infer_game_name(title: str, known_games: Iterable[str]) -> str | None:
    normalized_title = f" {normalize_text(title)} "
    ordered_games = sorted(
        known_games,
        key=lambda game: len(normalize_text(str(game))),
        reverse=True,
    )
    for game in ordered_games:
        normalized_game = normalize_text(game)
        if not normalized_game:
            continue
        if f" {normalized_game} " in normalized_title:
            return game
    return None


class YouTubeClient:
    def __init__(self, api_key: str, *, timeout_seconds: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params)
        query["key"] = self.api_key
        response = self.session.get(
            f"{YOUTUBE_API_BASE}/{endpoint}",
            params=query,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def search_live_games(
        self,
        *,
        tracked_query: str | None = None,
        include_gaming_topic: bool = True,
    ) -> tuple[list[str], int, str]:
        video_ids: list[str] = []
        search_calls = 0

        primary_requests: list[dict[str, Any]] = []

        if include_gaming_topic:
            primary_requests.append(
                {
                    "part": "snippet",
                    "eventType": "live",
                    "type": "video",
                    "topicId": "/m/0bzvm2",
                    "order": "viewCount",
                    "maxResults": 50,
                }
            )

        if tracked_query:
            primary_requests.append(
                {
                    "part": "snippet",
                    "eventType": "live",
                    "type": "video",
                    "q": tracked_query,
                    "order": "viewCount",
                    "maxResults": 50,
                }
            )

        for index, params in enumerate(primary_requests, start=1):
            payload = self.get("search", params)
            search_calls += 1
            rows = payload.get("items") or []

            for row in rows:
                if not isinstance(row, dict):
                    continue
                video_id = ((row.get("id") or {}).get("videoId"))
                if video_id:
                    video_ids.append(str(video_id))

            LOGGER.info("YouTube live search %d: %d videos", index, len(rows))

        if video_ids:
            return list(dict.fromkeys(video_ids)), search_calls, "event_type_live"

        # search.list(eventType=live) can intermittently return an empty result set.
        # Use one bounded fallback discovery request, then verify actual live status
        # with videos.list/liveStreamingDetails.
        published_after = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        fallback_params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "order": "date",
            "maxResults": 50,
            "publishedAfter": published_after,
        }
        if tracked_query:
            fallback_params["q"] = tracked_query
        else:
            fallback_params["q"] = "gaming|遊戲|ゲーム|게임"

        payload = self.get("search", fallback_params)
        search_calls += 1
        rows = payload.get("items") or []

        for row in rows:
            if not isinstance(row, dict):
                continue
            video_id = ((row.get("id") or {}).get("videoId"))
            if video_id:
                video_ids.append(str(video_id))

        LOGGER.info("YouTube fallback candidate search: %d videos", len(rows))
        return list(dict.fromkeys(video_ids)), search_calls, "recent_candidates_fallback"

    def get_videos(self, video_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(video_id) for video_id in video_ids if str(video_id)))
        rows: list[dict[str, Any]] = []

        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            payload = self.get(
                "videos",
                {
                    "part": "snippet,liveStreamingDetails,statistics",
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
            )
            rows.extend(row for row in payload.get("items") or [] if isinstance(row, dict))

        return rows

    def get_channels(self, channel_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(str(channel_id) for channel_id in channel_ids if str(channel_id)))
        result: dict[str, dict[str, Any]] = {}

        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            payload = self.get(
                "channels",
                {
                    "part": "snippet",
                    "id": ",".join(batch),
                    "maxResults": 50,
                },
            )
            for row in payload.get("items") or []:
                if isinstance(row, dict) and row.get("id"):
                    result[str(row["id"])] = row

        return result


def channel_region(country: str | None) -> str:
    code = (country or "").upper()
    if code == "TW":
        return "taiwan"
    if code in ASIA_COUNTRIES:
        return "asia"
    if code:
        return "other"
    return "unknown"


def aggregate_games(streams: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}

    for stream in streams:
        game_name = stream.get("game_name")
        if not game_name:
            continue

        bucket = buckets.setdefault(
            str(game_name),
            {
                "game_name": str(game_name),
                "global": {"streamer_count": 0, "viewer_count": 0},
                "asia": {"streamer_count": 0, "viewer_count": 0},
                "taiwan": {"streamer_count": 0, "viewer_count": 0},
                "unknown_country_streamers": 0,
            },
        )

        viewers = int(stream.get("concurrent_viewers") or 0)
        bucket["global"]["streamer_count"] += 1
        bucket["global"]["viewer_count"] += viewers

        region = str(stream.get("region") or "unknown")
        if region == "taiwan":
            bucket["taiwan"]["streamer_count"] += 1
            bucket["taiwan"]["viewer_count"] += viewers
            bucket["asia"]["streamer_count"] += 1
            bucket["asia"]["viewer_count"] += viewers
        elif region == "asia":
            bucket["asia"]["streamer_count"] += 1
            bucket["asia"]["viewer_count"] += viewers
        elif region == "unknown":
            bucket["unknown_country_streamers"] += 1

    result = list(buckets.values())
    result.sort(
        key=lambda row: (
            -int(row["global"]["viewer_count"]),
            -int(row["global"]["streamer_count"]),
            row["game_name"].casefold(),
        )
    )
    return result


def is_active_live_video(video: dict[str, Any]) -> bool:
    snippet = video.get("snippet") or {}
    live = video.get("liveStreamingDetails") or {}

    if snippet.get("liveBroadcastContent") == "live":
        return True

    return bool(live.get("actualStartTime")) and not bool(live.get("actualEndTime"))


def collect_youtube(api_key: str, *, search_calls: int = 2) -> dict[str, Any]:
    client = YouTubeClient(api_key)

    twitch_payload = fetch_public_json(TWITCH_JSON_URL)
    steam_payload = fetch_public_json(STEAM_JSON_URL)
    previous_youtube_payload = fetch_public_json(YOUTUBE_JSON_URL)
    known_games = build_known_games(twitch_payload, steam_payload)
    search_terms = build_search_terms(
        twitch_payload,
        steam_payload,
        max_terms=max(1, search_calls) * 10,
    )

    if not search_terms:
        search_terms = ["gaming", "遊戲", "ゲーム", "게임"]

    tracked_query = "|".join(search_terms[:10]) if search_terms else None

    # Keep a fixed two-search budget:
    # 1) official Gaming topic, 2) tracked games query.
    discovered_video_ids, actual_search_calls, discovery_mode = client.search_live_games(
        tracked_query=tracked_query,
        include_gaming_topic=True,
    )

    previous_live_ids = [
        str(row.get("video_id") or "")
        for row in previous_youtube_payload.get("streams") or []
        if isinstance(row, dict) and row.get("video_id")
    ]
    video_ids = list(dict.fromkeys(discovered_video_ids + previous_live_ids))
    videos = [
        video
        for video in client.get_videos(video_ids)
        if is_active_live_video(video)
    ]

    channel_ids = [
        str((row.get("snippet") or {}).get("channelId") or "")
        for row in videos
    ]
    channels = client.get_channels(channel_ids)

    streams: list[dict[str, Any]] = []

    for row in videos:
        snippet = row.get("snippet") or {}
        live = row.get("liveStreamingDetails") or {}
        video_id = str(row.get("id") or "")
        channel_id = str(snippet.get("channelId") or "")
        channel = channels.get(channel_id) or {}
        channel_snippet = channel.get("snippet") or {}

        viewers_raw = live.get("concurrentViewers")
        try:
            concurrent_viewers = int(viewers_raw or 0)
        except (TypeError, ValueError):
            concurrent_viewers = 0

        country = str(channel_snippet.get("country") or "").upper() or None
        title = str(snippet.get("title") or "")
        game_name = infer_game_name(title, known_games)

        thumbnails = snippet.get("thumbnails") or {}
        thumbnail = (
            (thumbnails.get("high") or {}).get("url")
            or (thumbnails.get("medium") or {}).get("url")
            or (thumbnails.get("default") or {}).get("url")
        )

        streams.append(
            {
                "video_id": video_id,
                "title": title,
                "channel_id": channel_id,
                "channel_title": str(snippet.get("channelTitle") or ""),
                "channel_country": country,
                "region": channel_region(country),
                "concurrent_viewers": concurrent_viewers,
                "game_name": game_name,
                "published_at": snippet.get("publishedAt"),
                "actual_start_time": live.get("actualStartTime"),
                "thumbnail_url": thumbnail,
                "watch_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            }
        )

    matched = [stream for stream in streams if stream.get("game_name")]
    unmatched = [stream for stream in streams if not stream.get("game_name")]

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return {
        "generated_at": generated_at,
        "source": "YouTube Data API v3",
        "coverage": {
            "search_calls_per_run": actual_search_calls,
            "estimated_daily_search_calls_at_hourly_schedule": actual_search_calls * 24,
            "search_term_count": len(search_terms),
            "discovery_mode": discovery_mode,
            "candidate_video_count": len(video_ids),
            "live_gaming_stream_sample_size": len(streams),
            "known_game_dictionary_size": len(known_games),
            "matched_stream_count": len(matched),
            "unmatched_stream_count": len(unmatched),
            "location_note": (
                "Taiwan/Asia classification uses the YouTube channel's associated country "
                "when the channel owner has configured one. It is not precise physical location."
            ),
        },
        "top_games": aggregate_games(matched),
        "streams": streams,
        "unmatched_streams": unmatched[:25],
    }


def write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
