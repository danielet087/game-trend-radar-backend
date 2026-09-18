from __future__ import annotations

import calendar
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

LOGGER = logging.getLogger(__name__)

STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STEAM_FOLLOWERS_URL = "https://steamcommunity.com/games/{appid}/memberslistxml/"
USER_AGENT = "GameTrendRadar/0.1 (+GitHub Actions; Steam public data collector)"


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

    text = str(raw).strip()
    if not text:
        return ReleaseWindow(text, None, None, "unknown")

    if text.lower().replace(".", "") in {"coming soon", "soon", "tba", "tbd", "to be announced"}:
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


class SteamUpcomingCollector:
    def __init__(
        self,
        *,
        country: str = "TW",
        horizon_days: int = 365,
        min_followers: int = 5000,
        page_size: int = 100,
        max_pages: int = 200,
        follower_workers: int = 4,
        follower_request_interval: float = 0.25,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.country = country.upper()
        self.horizon_days = horizon_days
        self.min_followers = min_followers
        self.page_size = page_size
        self.max_pages = max_pages
        self.follower_workers = max(1, follower_workers)
        self.timeout_seconds = timeout_seconds
        self.rate_limiter = _RateLimiter(follower_request_interval)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout_seconds)
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")
                    delay = min(float(retry_after), 60.0) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
                    LOGGER.warning("HTTP %s from Steam; retrying in %.1fs", response.status_code, delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_error = exc
                if attempt == 4:
                    break
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"Steam request failed after retries: {last_error}")

    @staticmethod
    def _item_to_game(item: dict[str, Any]) -> UpcomingGame | None:
        appid = item.get("id")
        if not appid:
            match = re.search(r"/apps/(\d+)/", str(item.get("logo") or ""))
            appid = match.group(1) if match else None
        try:
            appid_int = int(appid)
        except (TypeError, ValueError):
            return None

        raw_date = item.get("release_date") or item.get("releasedate") or item.get("date") or ""
        window = parse_release_window(raw_date)
        image = item.get("logo") or item.get("small_capsule") or item.get("tiny_image")
        name = str(item.get("name") or "Unknown").strip() or "Unknown"

        return UpcomingGame(
            appid=appid_int,
            name=name,
            release_raw=window.raw,
            release_start=window.start.isoformat() if window.start else None,
            release_end=window.end.isoformat() if window.end else None,
            release_precision=window.precision,
            capsule_image=str(image) if image else None,
            store_url=f"https://store.steampowered.com/app/{appid_int}/",
        )

    def fetch_candidates(self, *, today: date | None = None) -> list[UpcomingGame]:
        today = today or datetime.now(timezone.utc).date()
        cutoff = today + timedelta(days=self.horizon_days)
        seen: dict[int, UpcomingGame] = {}
        start = 0

        for page in range(self.max_pages):
            response = self._request(
                STEAM_SEARCH_URL,
                params={
                    "filter": "comingsoon",
                    "json": 1,
                    "cc": self.country,
                    "l": "english",
                    "count": self.page_size,
                    "start": start,
                    "sort_by": "Released_ASC",
                },
            )
            payload = response.json()
            items = payload.get("items") or []
            if not items:
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                game = self._item_to_game(item)
                if game is None:
                    continue
                if parse_release_window(game.release_raw).overlaps(today, cutoff):
                    seen[game.appid] = game

            start += len(items)
            total = payload.get("total_count")
            LOGGER.info("Steam page %d: %d items, %d candidates kept", page + 1, len(items), len(seen))
            if isinstance(total, int) and start >= total:
                break
            if len(items) < self.page_size:
                break

        return sorted(seen.values(), key=lambda g: (g.release_start or "9999-12-31", g.name.casefold(), g.appid))

    def fetch_followers(self, appid: int) -> int:
        self.rate_limiter.wait()
        response = self._request(STEAM_FOLLOWERS_URL.format(appid=appid), params={"xml": 1})
        return parse_follower_xml(response.text)

    def _qualify_one(self, game: UpcomingGame) -> QualifiedGame | None:
        try:
            followers = self.fetch_followers(game.appid)
        except Exception as exc:
            LOGGER.warning("Followers failed for %s (%s): %s", game.appid, game.name, exc)
            return None
        if followers < self.min_followers:
            return None
        return QualifiedGame(
            **asdict(game),
            followers=followers,
            community_url=f"https://steamcommunity.com/app/{game.appid}/",
        )

    def qualify(self, games: Iterable[UpcomingGame]) -> list[QualifiedGame]:
        games = list(games)
        qualified: list[QualifiedGame] = []
        total = len(games)
        if not total:
            return qualified

        with ThreadPoolExecutor(max_workers=self.follower_workers) as executor:
            futures = {executor.submit(self._qualify_one, game): game for game in games}
            completed = 0
            for future in as_completed(futures):
                completed += 1
                result = future.result()
                if result:
                    qualified.append(result)
                if completed % 100 == 0 or completed == total:
                    LOGGER.info("Follower progress %d/%d; %d >= %d", completed, total, len(qualified), self.min_followers)

        return sorted(qualified, key=lambda g: (-g.followers, g.release_start or "9999-12-31", g.name.casefold()))

    def collect(self, *, today: date | None = None) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        today = today or now.date()
        candidates = self.fetch_candidates(today=today)
        games = self.qualify(candidates)
        return {
            "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "source": {
                "catalog": "Steam Store coming-soon search",
                "followers": "Steam Community game-group memberCount",
            },
            "filter": {
                "country": self.country,
                "release_horizon_days": self.horizon_days,
                "min_followers": self.min_followers,
                "unknown_release_dates_included": False,
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
