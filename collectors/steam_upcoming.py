from __future__ import annotations

import calendar
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STEAM_FOLLOWERS_URL = "https://steamcommunity.com/games/{appid}/memberslistxml/"
USER_AGENT = "Mozilla/5.0 (compatible; GameTrendRadar/0.3; +https://github.com/danielet087/game-trend-radar)"


@dataclass(frozen=True)
class ReleaseWindow:
    raw: str
    start: date | None
    end: date | None
    precision: str

    def overlaps(self, start: date, end: date) -> bool:
        return self.start is not None and self.end is not None and self.end >= start and self.start <= end


@dataclass(frozen=True)
class UpcomingGame:
    appid: int
    name: str
    release_raw: str
    release_start: str | None
    release_end: str | None
    release_precision: str
    capsule_image: str | None
    store_url: str


@dataclass(frozen=True)
class QualifiedGame:
    appid: int
    name: str
    release_raw: str
    release_start: str | None
    release_end: str | None
    release_precision: str
    followers: int
    follower_checked_at: str | None
    capsule_image: str | None
    store_url: str
    community_url: str


def _month_number(text: str) -> int | None:
    return {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }.get(text.strip().lower()[:3])


def parse_release_window(raw: Any) -> ReleaseWindow:
    if raw is None:
        return ReleaseWindow("", None, None, "unknown")

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            d = datetime.fromtimestamp(float(raw), tz=timezone.utc).date()
            return ReleaseWindow(str(raw), d, d, "day")
        except (OverflowError, OSError, ValueError):
            return ReleaseWindow(str(raw), None, None, "unknown")

    text = " ".join(str(raw).split())
    if not text:
        return ReleaseWindow(text, None, None, "unknown")

    normalized = text.lower().replace(".", "")
    if normalized in {
        "coming soon", "soon", "tba", "tbd", "to be announced",
        "date to be announced", "announced later",
    }:
        return ReleaseWindow(text, None, None, "unknown")

    try:
        d = date.fromisoformat(text)
        return ReleaseWindow(text, d, d, "day")
    except ValueError:
        pass

    match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})[,]?\s+(\d{4})", text)
    if match:
        month = _month_number(match.group(2))
        if month:
            try:
                d = date(int(match.group(3)), month, int(match.group(1)))
                return ReleaseWindow(text, d, d, "day")
            except ValueError:
                pass

    match = re.fullmatch(r"([A-Za-z]{3,9})\s+(\d{1,2})[,]?\s+(\d{4})", text)
    if match:
        month = _month_number(match.group(1))
        if month:
            try:
                d = date(int(match.group(3)), month, int(match.group(2)))
                return ReleaseWindow(text, d, d, "day")
            except ValueError:
                pass

    match = re.fullmatch(r"([A-Za-z]{3,9})\s+(\d{4})", text)
    if match:
        month = _month_number(match.group(1))
        year = int(match.group(2))
        if month:
            last = calendar.monthrange(year, month)[1]
            return ReleaseWindow(text, date(year, month, 1), date(year, month, last), "month")

    match = re.fullmatch(r"Q([1-4])[,]?\s*(\d{4})", text, flags=re.IGNORECASE)
    if match:
        quarter = int(match.group(1))
        year = int(match.group(2))
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        last = calendar.monthrange(year, end_month)[1]
        return ReleaseWindow(text, date(year, start_month, 1), date(year, end_month, last), "quarter")

    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        return ReleaseWindow(text, date(year, 1, 1), date(year, 12, 31), "year")

    return ReleaseWindow(text, None, None, "unknown")


def parse_follower_xml(xml_text: str) -> int:
    root = ET.fromstring(xml_text)
    for path in ("memberCount", "groupDetails/memberCount", ".//memberCount"):
        node = root.find(path)
        if node is not None and node.text:
            value = node.text.replace(",", "").strip()
            if value.isdigit():
                return int(value)
    raise ValueError("Steam follower XML did not contain memberCount")


def parse_search_results_html(results_html: str) -> list[UpcomingGame]:
    if not results_html:
        return []

    soup = BeautifulSoup(results_html, "html.parser")
    games: list[UpcomingGame] = []

    for row in soup.select("a.search_result_row"):
        appid_raw = row.get("data-ds-appid") or ""
        if not str(appid_raw).isdigit():
            href = row.get("href") or ""
            match = re.search(r"/app/(\d+)/", href)
            appid_raw = match.group(1) if match else ""
        if not str(appid_raw).isdigit():
            continue

        appid = int(appid_raw)
        name_node = row.select_one("span.title")
        release_node = row.select_one("div.search_released")
        image_node = row.select_one(".search_capsule img")

        name = " ".join(name_node.get_text(" ", strip=True).split()) if name_node else f"App {appid}"
        release_raw = " ".join(release_node.get_text(" ", strip=True).split()) if release_node else ""
        release = parse_release_window(release_raw)
        image = None
        if image_node:
            image = image_node.get("src") or image_node.get("data-src")

        games.append(
            UpcomingGame(
                appid=appid,
                name=name,
                release_raw=release.raw,
                release_start=release.start.isoformat() if release.start else None,
                release_end=release.end.isoformat() if release.end else None,
                release_precision=release.precision,
                capsule_image=str(image) if image else None,
                store_url=f"https://store.steampowered.com/app/{appid}/",
            )
        )

    return games


class _RateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self.lock = threading.Lock()
        self.next_allowed = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_allowed - now)
            if delay:
                time.sleep(delay)
            self.next_allowed = time.monotonic() + self.interval


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class SteamUpcomingCollector:
    def __init__(
        self,
        *,
        country: str = "TW",
        horizon_days: int = 365,
        min_followers: int = 5000,
        page_size: int = 100,
        max_pages: int = 100,
        follower_request_interval: float = 12.0,
        search_request_interval: float = 10.0,
        timeout_seconds: float = 20.0,
        follower_cache_path: str | Path = "data/steam_followers_cache.json",
        cache_flush_every: int = 20,
    ) -> None:
        self.country = country.upper()
        self.horizon_days = horizon_days
        self.min_followers = min_followers
        self.page_size = max(1, min(page_size, 100))
        self.max_pages = max(1, max_pages)
        self.timeout_seconds = timeout_seconds
        self.follower_rate_limiter = _RateLimiter(follower_request_interval)
        self.search_rate_limiter = _RateLimiter(search_request_interval)
        self.follower_cache_path = Path(follower_cache_path)
        self.cache_flush_every = max(1, cache_flush_every)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.8",
        })

        self.follower_cache = self._load_follower_cache()
        self.fresh_follower_requests = 0
        self.cached_follower_reuses = 0

    def _load_follower_cache(self) -> dict[str, dict[str, Any]]:
        if not self.follower_cache_path.exists():
            return {}
        try:
            payload = json.loads(self.follower_cache_path.read_text(encoding="utf-8"))
            games = payload.get("games", payload)
            if isinstance(games, dict):
                return {str(k): v for k, v in games.items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            LOGGER.warning("Could not read follower cache; starting with an empty cache")
        return {}

    def _save_follower_cache(self) -> None:
        self.follower_cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "games": self.follower_cache,
        }
        temp = self.follower_cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.follower_cache_path)

    def _cache_ttl(self, followers: int) -> timedelta:
        if followers >= 5000:
            return timedelta(days=1)
        if followers >= 3000:
            return timedelta(days=3)
        return timedelta(days=30)

    def _cached_follower_entry(self, appid: int, now: datetime) -> tuple[int, str] | None:
        entry = self.follower_cache.get(str(appid))
        if not entry:
            return None

        try:
            followers = int(entry["followers"])
        except (KeyError, TypeError, ValueError):
            return None

        checked_at_raw = entry.get("checked_at")
        checked_at = _parse_iso_datetime(checked_at_raw)
        if checked_at is None:
            return None

        if now - checked_at <= self._cache_ttl(followers):
            return followers, str(checked_at_raw)
        return None

    def _update_follower_cache(self, appid: int, followers: int, checked_at: datetime) -> None:
        self.follower_cache[str(appid)] = {
            "followers": followers,
            "checked_at": checked_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        limiter: _RateLimiter | None = None,
    ) -> requests.Response:
        last_error: Exception | str | None = None

        for attempt in range(6):
            if limiter is not None:
                limiter.wait()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout_seconds)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "").strip()
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = 180.0 * (attempt + 1)
                    delay = min(900.0, max(60.0, delay))
                    last_error = "HTTP 429"
                    LOGGER.warning(
                        "HTTP 429 from Steam; backing off %.0fs before retry %d/6",
                        delay,
                        attempt + 1,
                    )
                    time.sleep(delay)
                    continue

                if response.status_code == 503:
                    delay = min(180.0, 30.0 * (attempt + 1))
                    last_error = "HTTP 503"
                    LOGGER.warning("HTTP 503 from Steam; backing off %.0fs", delay)
                    time.sleep(delay)
                    continue

                if response.status_code in {500, 502, 504}:
                    delay = min(60.0, 5.0 * (attempt + 1))
                    last_error = f"HTTP {response.status_code}"
                    LOGGER.warning("HTTP %s from Steam; retrying in %.0fs", response.status_code, delay)
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response

            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_error = exc
                if attempt == 5:
                    break
                time.sleep(min(60.0, 5.0 * (attempt + 1)))

        raise RuntimeError(f"Steam request failed after retries: {last_error}")

    def fetch_candidates(
        self,
        *,
        today: date | None = None,
        window_start: date | None = None,
        window_end: date | None = None,
    ) -> list[UpcomingGame]:
        today = today or _utc_now().date()
        target_start = window_start or today
        target_end = window_end or (today + timedelta(days=self.horizon_days))
        seen: dict[int, UpcomingGame] = {}
        start = 0
        pages_past_cutoff = 0

        for page in range(self.max_pages):
            response = self._request(
                STEAM_SEARCH_URL,
                params={
                    "filter": "comingsoon",
                    "sort_by": "Released_ASC",
                    "start": start,
                    "count": self.page_size,
                    "infinite": 1,
                    "force_infinite": 1,
                    "category1": 998,
                    "cc": self.country,
                    "l": "english",
                },
                limiter=self.search_rate_limiter,
            )
            payload = response.json()
            rows = parse_search_results_html(payload.get("results_html") or "")

            if not rows:
                LOGGER.info("Steam page %d returned no searchable app rows; stopping", page + 1)
                break

            in_window = 0
            known_dates = 0
            min_known_start: date | None = None

            for game in rows:
                release = parse_release_window(game.release_raw)
                if release.start is not None:
                    known_dates += 1
                    min_known_start = release.start if min_known_start is None else min(min_known_start, release.start)
                if release.overlaps(target_start, target_end):
                    seen[game.appid] = game
                    in_window += 1

            LOGGER.info(
                "Steam page %d: %d rows, %d dated, %d within horizon, %d candidates total",
                page + 1,
                len(rows),
                known_dates,
                in_window,
                len(seen),
            )

            if known_dates == len(rows) and min_known_start is not None and min_known_start > target_end:
                pages_past_cutoff += 1
            else:
                pages_past_cutoff = 0

            if pages_past_cutoff >= 2:
                LOGGER.info("Two consecutive pages are fully beyond %s; stopping early", target_end)
                break

            start += len(rows)
            total = payload.get("total_count")
            if isinstance(total, int) and start >= total:
                break
            if len(rows) < self.page_size:
                break

        return sorted(
            seen.values(),
            key=lambda g: (g.release_start or "9999-12-31", g.name.casefold(), g.appid),
        )

    def fetch_followers(self, appid: int) -> int:
        response = self._request(
            STEAM_FOLLOWERS_URL.format(appid=appid),
            params={"xml": 1},
            limiter=self.follower_rate_limiter,
        )
        return parse_follower_xml(response.text)

    def qualify(self, games: Iterable[UpcomingGame]) -> list[QualifiedGame]:
        games = list(games)
        total = len(games)
        qualified: list[QualifiedGame] = []
        cache_now = _utc_now()

        for index, game in enumerate(games, start=1):
            cached = self._cached_follower_entry(game.appid, cache_now)

            if cached is not None:
                followers, checked_at = cached
                self.cached_follower_reuses += 1
            else:
                try:
                    followers = self.fetch_followers(game.appid)
                except Exception as exc:
                    LOGGER.warning("Followers failed for %s (%s): %s", game.appid, game.name, exc)
                    continue

                checked_at_dt = _utc_now()
                checked_at = checked_at_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                self._update_follower_cache(game.appid, followers, checked_at_dt)
                self.fresh_follower_requests += 1

                if self.fresh_follower_requests % self.cache_flush_every == 0:
                    self._save_follower_cache()

            if followers >= self.min_followers:
                qualified.append(
                    QualifiedGame(
                        **asdict(game),
                        followers=followers,
                        follower_checked_at=checked_at,
                        community_url=f"https://steamcommunity.com/app/{game.appid}/",
                    )
                )

            if index % 50 == 0 or index == total:
                LOGGER.info(
                    "Follower progress %d/%d; qualified=%d; fresh=%d; cache=%d",
                    index,
                    total,
                    len(qualified),
                    self.fresh_follower_requests,
                    self.cached_follower_reuses,
                )

        self._save_follower_cache()

        return sorted(
            qualified,
            key=lambda g: (-g.followers, g.release_start or "9999-12-31", g.name.casefold()),
        )

    def collect(
        self,
        *,
        today: date | None = None,
        window_start: date | None = None,
        window_end: date | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        today = today or now.date()
        target_start = window_start or today
        target_end = window_end or (today + timedelta(days=self.horizon_days))
        candidates = self.fetch_candidates(
            today=today,
            window_start=target_start,
            window_end=target_end,
        )
        games = self.qualify(candidates)

        return {
            "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "source": {
                "catalog": "Steam Store coming-soon search results_html",
                "followers": "Steam Community game-group memberCount",
            },
            "filter": {
                "country": self.country,
                "release_horizon_days": self.horizon_days,
                "release_window_start": target_start.isoformat(),
                "release_window_end": target_end.isoformat(),
                "min_followers": self.min_followers,
                "unknown_release_dates_included": False,
            },
            "request_policy": {
                "search_interval_seconds": self.search_rate_limiter.interval,
                "follower_interval_seconds": self.follower_rate_limiter.interval,
                "follower_concurrency": 1,
                "max_pages": self.max_pages,
            },
            "collection": {
                "candidate_count": len(candidates),
                "fresh_follower_requests": self.fresh_follower_requests,
                "cached_follower_reuses": self.cached_follower_reuses,
            },
            "candidate_count": len(candidates),
            "count": len(games),
            "games": [asdict(game) for game in games],
        }


def write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
