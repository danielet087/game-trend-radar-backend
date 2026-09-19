from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from collectors.steam_upcoming import (
    STEAM_FOLLOWERS_URL, STEAM_SEARCH_URL, parse_follower_xml,
    parse_release_window, parse_search_results_html, write_json,
)

LOGGER = logging.getLogger(__name__)
APP_DETAILS = "https://store.steampowered.com/api/appdetails"
FEATURED = "https://store.steampowered.com/api/featuredcategories"
UA = "Mozilla/5.0 (compatible; GameTrendRadar/0.4)"


def steam_get(session: requests.Session, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=25)
            if response.status_code == 429:
                delay = max(60, min(180, 60 * (attempt + 1)))
                LOGGER.warning("Steam Store returned 429; sleeping %ds", delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else None
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("Steam Store metadata request failed: %s", exc)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return None


def app_details(
    session: requests.Session, appid: int, *, language: str = "english"
) -> dict[str, Any] | None:
    data = steam_get(session, APP_DETAILS, {"appids": appid, "cc": "TW", "l": language})
    item = (data or {}).get(str(appid), {})
    if isinstance(item, dict) and item.get("success") and isinstance(item.get("data"), dict):
        return item["data"]
    return None


def localized_names(
    english_details: dict[str, Any], traditional_details: dict[str, Any] | None
) -> tuple[str, str | None]:
    english_name = str(english_details.get("name") or "").strip()
    traditional_name = str((traditional_details or {}).get("name") or "").strip()
    return english_name, traditional_name if traditional_name and traditional_name != english_name else None


def add_traditional_name(
    session: requests.Session, appid: int, game: dict[str, Any], english_details: dict[str, Any],
    *, delay_seconds: float,
) -> None:
    # Request names in tchinese, but keep the English locale for parsing release dates.
    # Some games have no localized Store title; never invent a translation.
    time.sleep(delay_seconds)
    traditional_details = app_details(session, appid, language="tchinese")
    name_en, name_zh_tw = localized_names(english_details, traditional_details)
    game["name"] = name_en or game["name"]
    game["name_en"] = name_en or game["name"]
    game["name_zh_tw"] = name_zh_tw


def normalized_metadata(appid: int, details: dict[str, Any], followers: int | None, checked_at: str | None) -> dict[str, Any] | None:
    raw = str((details.get("release_date") or {}).get("date") or "").strip()
    release = parse_release_window(raw)
    if release.precision != "day" or not release.start:
        return None
    return {
        "appid": appid,
        "name": str(details.get("name") or f"Steam App {appid}"),
        "name_en": str(details.get("name") or f"Steam App {appid}"),
        "name_zh_tw": None,
        "release_raw": release.raw,
        "release_start": release.start.isoformat(),
        "release_end": release.end.isoformat() if release.end else None,
        "release_precision": "day",
        "followers": followers,
        "follower_checked_at": checked_at,
        "capsule_image": details.get("capsule_image") or details.get("header_image"),
        "header_image": details.get("header_image"),
        "store_url": f"https://store.steampowered.com/app/{appid}/",
    }


RECENT_DAYS = 30
FIRST_WEEK_DAYS = 7  # Release day through seven calendar days afterward.
DIRECT_FOLLOWERS_MIN = 3001  # "Over 3,000" is strictly greater than 3,000.
MAX_NEW_RELEASE_REQUESTS = 45
MAX_SCAN_SECONDS = 1800  # Finish and persist private state before the 45-minute CI timeout.


def first_week_release(release_day: date, observed_day: date) -> bool:
    """A direct-launch title may qualify from release day through day seven."""
    return release_day <= observed_day <= release_day + timedelta(days=FIRST_WEEK_DAYS)


def recent_release(game: dict[str, Any], today: date) -> bool:
    try:
        day = date.fromisoformat(str(game["release_start"]))
        followers = int(game["followers"])
    except (KeyError, ValueError, TypeError):
        return False
    source = game.get("recent_source")
    if not (today - timedelta(days=RECENT_DAYS) <= day <= today and followers >= DIRECT_FOLLOWERS_MIN):
        return False
    if source == "tracked_release":
        return True
    if source != "direct_release":
        return False
    # Existing older state without the new field must also prove qualification
    # occurred during the first week; do not retroactively call a month-old hit a dark horse.
    first_observed = game.get("first_week_qualified_at") or game.get("follower_checked_at")
    try:
        checked = datetime.fromisoformat(str(first_observed).replace("Z", "+00:00"))
        observed_day = checked.astimezone(timezone(timedelta(hours=8))).date()
    except (ValueError, TypeError):
        return False
    return first_week_release(day, observed_day)


def checked_followers(
    appid: int, checkpoint: dict[str, Any], recently_checked: dict[str, Any],
    now: datetime, *, allow_tracked_history: bool = False,
) -> tuple[int, str] | None:
    options = [checkpoint.get(str(appid)), recently_checked.get(str(appid))]
    valid: list[tuple[datetime, int, str]] = []
    for item in options:
        if not isinstance(item, dict):
            continue
        try:
            count = int(item["followers"])
            checked_at = str(item["checked_at"])
            dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            if dt.tzinfo is None or count < 0:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        valid.append((dt, count, checked_at))
    if not valid:
        return None
    last, count, checked_at = max(valid, key=lambda value: value[0])
    # Previously tracked games are admitted on the stored pre-release measurement.
    if allow_tracked_history and count >= 5000:
        return count, checked_at
    # A fresh discovery may reuse recent checkpoint measurements, but not old
    # low-follower measurements which could hide a sudden release crossing 3000.
    # Under-threshold new releases must be checked again on following days:
    # keeping a 3/7-day low-follower TTL would miss first-week growth.
    freshness = timedelta(hours=20 if count < DIRECT_FOLLOWERS_MIN else 24)
    if now - last < freshness:
        return count, checked_at
    return None


def fetch_new_release_appids(
    session: requests.Session, today: date, *, max_pages: int = 2,
) -> list[int]:
    """Browse new Steam Store releases, including apps never in comingsoon."""
    seen: set[int] = set()
    dated: list[tuple[date, int]] = []
    for page in range(max_pages):
        response = steam_get(session, STEAM_SEARCH_URL, {
            "filter": "newreleases",
            "sort_by": "Released_DESC",
            "start": page * 100,
            "count": 100,
            "infinite": 1,
            "force_infinite": 1,
            "category1": 998,
            "cc": "TW",
            "l": "english",
        })
        if not response:
            break
        rows = parse_search_results_html(str(response.get("results_html") or ""))
        if not rows:
            break
        for row in rows:
            if row.appid in seen:
                continue
            try:
                search_date = date.fromisoformat(row.release_start or "")
                if not first_week_release(search_date, today):
                    continue
            except ValueError:
                # Unknown search date: leave eligibility to Store appdetails,
                # checked before making a Community Followers request.
                search_date = today
            seen.add(row.appid)
            dated.append((search_date, row.appid))
        if len(rows) < 100:
            break
    # An about-to-expire first-week candidate must be considered before today's
    # brand-new releases when the bounded daily Followers budget is tight.
    return [appid for _, appid in sorted(dated, key=lambda row: (row[0], row[1]))]


def fetch_new_release_followers(session: requests.Session, appid: int) -> int | None:
    """Only read Community XML; never infer Followers from sales or player counts."""
    url = STEAM_FOLLOWERS_URL.format(appid=appid)
    for attempt in range(3):
        try:
            response = session.get(url, params={"xml": 1}, timeout=25)
            if response.status_code == 429:
                delay = 60 * (attempt + 1)
                LOGGER.warning("Follower 429 for app %d; sleeping %ds", appid, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return parse_follower_xml(response.text)
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("Cannot verify followers for app %d: %s", appid, exc)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return None


def release_from_store(
    session: requests.Session, appid: int, followers: int, checked_at: str,
    *, today: date, delay_seconds: float, recent_source: str,
) -> dict[str, Any] | None:
    details = app_details(session, appid)
    if not details or details.get("type") != "game":
        return None
    # The scheduled date alone is not proof of an actual launch.
    if (details.get("release_date") or {}).get("coming_soon") is not False:
        return None
    game = normalized_metadata(appid, details, followers, checked_at)
    if game is None:
        return None
    day = date.fromisoformat(game["release_start"])
    if not (today - timedelta(days=RECENT_DAYS) <= day <= today):
        return None
    if recent_source == "direct_release" and not first_week_release(day, today):
        return None
    add_traditional_name(session, appid, game, details, delay_seconds=delay_seconds)
    game["recent_source"] = recent_source
    if recent_source == "direct_release":
        game["first_week_qualified_at"] = checked_at
    game["confirmed_released_at"] = datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    return game


def run(
    *, checkpoint_path: Path, output_path: Path, min_followers: int = 5000,
    delay_seconds: float = 2.5,
    recent_state_path: Path = Path("data/steam_recent_launch_state.json"),
    today: date | None = None, max_new_followers: int = MAX_NEW_RELEASE_REQUESTS,
    follower_interval: float = 30.0,
) -> dict[str, Any]:
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    cached = checkpoint.get("games", {})
    if not isinstance(cached, dict):
        raise ValueError("Checkpoint games must be an object")
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    now = datetime.now(timezone.utc)
    state: dict[str, Any] = {}
    if recent_state_path.exists():
        state = json.loads(recent_state_path.read_text(encoding="utf-8"))
    checks: dict[str, Any] = dict(state.get("checked") or {})
    prior_released: dict[str, Any] = dict(state.get("released") or {})
    recently_released: dict[str, dict[str, Any]] = {
        key: value for key, value in prior_released.items()
        if isinstance(value, dict) and recent_release(value, today)
    }
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})

    qualified: list[tuple[int, int, str]] = []
    for key, entry in cached.items():
        if not isinstance(entry, dict):
            continue
        try:
            appid, followers = int(key), int(entry["followers"])
        except (ValueError, TypeError, KeyError):
            continue
        if followers >= min_followers:
            qualified.append((appid, followers, str(entry.get("checked_at") or "")))
    qualified.sort(key=lambda row: (-row[1], row[0]))

    games: list[dict[str, Any]] = []
    skipped_missing_date = 0
    processed_tracked: set[int] = set()
    started = time.monotonic()
    for index, (appid, followers, checked_at) in enumerate(qualified, start=1):
        if index > 1:
            time.sleep(delay_seconds)
        details = app_details(session, appid)
        if not details or details.get("type") != "game":
            LOGGER.warning("No Steam game metadata for tracked app %d", appid)
            continue
        game = normalized_metadata(appid, details, followers, checked_at)
        if game is None:
            skipped_missing_date += 1
            LOGGER.info("Skipping %d: Store has no exact release day", appid)
            continue
        day = date.fromisoformat(game["release_start"])
        coming_soon = (details.get("release_date") or {}).get("coming_soon")
        if today - timedelta(days=RECENT_DAYS) <= day <= today and coming_soon is False:
            add_traditional_name(session, appid, game, details, delay_seconds=delay_seconds)
            game["recent_source"] = "tracked_release"
            game["confirmed_released_at"] = now.replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
            recently_released[str(appid)] = game
            processed_tracked.add(appid)
        elif day >= today and day <= today + timedelta(days=365) and coming_soon is True:
            add_traditional_name(session, appid, game, details, delay_seconds=delay_seconds)
            games.append(game)
            processed_tracked.add(appid)
        elif coming_soon is True:
            # A previous release listing was rescheduled or not truly launched.
            recently_released.pop(str(appid), None)
            processed_tracked.add(appid)
        LOGGER.info("Tracked metadata %d/%d app=%d", index, len(qualified), appid)

    # New launches never included in Upcoming: independently scan Store releases.
    candidate_ids = fetch_new_release_appids(session, today)
    direct_requests = 0
    candidate_checked = 0
    follower_clock: float | None = None
    # Do not spend today's 45 Community calls repeatedly on old low-follower
    # entries while new, never-scanned first-week launches wait behind them.
    candidate_ids.sort(key=lambda appid: (
        appid in processed_tracked or str(appid) in recently_released,
        str(appid) in checks or str(appid) in cached,
    ))
    for appid in candidate_ids:
        if appid in processed_tracked:
            continue
        if time.monotonic() - started >= MAX_SCAN_SECONDS:
            LOGGER.info("Released-game scan reached time budget; remaining IDs deferred")
            break
        # Validate actual Store launch and the seven-day window BEFORE spending
        # a Community request. Store search dates may be imprecise or changed.
        if str(appid) not in recently_released:
            details = app_details(session, appid)
            if not details or details.get("type") != "game":
                continue
            if (details.get("release_date") or {}).get("coming_soon") is not False:
                continue
            exact = normalized_metadata(appid, details, None, None)
            if exact is None or not first_week_release(
                date.fromisoformat(exact["release_start"]), today
            ):
                continue
        # Newly published release candidates can be rechecked on subsequent days.
        previous = checked_followers(appid, cached, checks, now)
        if previous is not None and str(appid) not in recently_released:
            # A pre-release measurement cannot prove post-launch followers;
            # check it again so the first-week badge has valid evidence.
            checked_date = datetime.fromisoformat(
                previous[1].replace("Z", "+00:00")
            ).astimezone(timezone(timedelta(hours=8))).date()
            if checked_date < date.fromisoformat(exact["release_start"]):
                previous = None
        if previous is None:
            if direct_requests >= max_new_followers:
                LOGGER.info("Released-game follower budget reached: %d", direct_requests)
                break
            if follower_clock is not None:
                time.sleep(max(0.0, follower_interval - (time.monotonic() - follower_clock)))
            follower_clock = time.monotonic()
            count = fetch_new_release_followers(session, appid)
            if count is None:
                continue
            direct_requests += 1
            checked_at = datetime.now(timezone.utc).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
            checks[str(appid)] = {"followers": count, "checked_at": checked_at}
        else:
            count, checked_at = previous
        candidate_checked += 1
        if count < DIRECT_FOLLOWERS_MIN:
            recently_released.pop(str(appid), None)
            continue
        # Existing confirmed entries are retained in the 30-day rolling state;
        # re-read Store only when this AppID hasn't yet been verified as released.
        if str(appid) in recently_released:
            game = recently_released[str(appid)]
            game["followers"] = count
            game["follower_checked_at"] = checked_at
            continue
        time.sleep(delay_seconds)
        game = release_from_store(
            session, appid, count, checked_at, today=today,
            delay_seconds=delay_seconds, recent_source="direct_release",
        )
        if game:
            recently_released[str(appid)] = game

    # Keep private negative lookups only as long as they can help avoid
    # immediately querying the same recently launched AppID again.
    checks = {
        key: value for key, value in checks.items()
        if isinstance(value, dict) and (
            checked_followers(int(key), {}, {key: value}, now) is not None
            or key in recently_released
        )
    }
    recently_released = {
        key: value for key, value in recently_released.items()
        if recent_release(value, today)
    }
    recent = sorted(
        recently_released.values(),
        key=lambda item: (-int(item["followers"]), -date.fromisoformat(item["release_start"]).toordinal(), int(item["appid"])),
    )
    state_payload = {
        "version": 1,
        "updated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "checked": checks,
        "released": recently_released,
    }
    recent_state_path.parent.mkdir(parents=True, exist_ok=True)
    recent_state_path.write_text(
        json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    output = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "Steam Store metadata + private Followers checkpoint + Steam new-releases scan",
        "is_partial_preview": True,
        "checkpoint_count": len(cached),
        "checkpoint_updated_at": checkpoint.get("updated_at"),
        "qualified_checkpoint_count": len(qualified),
        "exact_date_games_count": len(games),
        "missing_or_ambiguous_release_date": skipped_missing_date,
        "min_followers": min_followers,
        "recent_min_followers_exclusive": 3000,
        "direct_release_qualification_window_days": FIRST_WEEK_DAYS,
        "recent_dark_horse_label": "近期黑馬",
        "recent_new_candidates_seen": len(candidate_ids),
        "recent_new_follower_requests": direct_requests,
        "recent_candidates_checked": candidate_checked,
        "games": games,
        "recent_games": recent,
    }
    write_json(output, output_path)
    LOGGER.info(
        "Steam preview: upcoming=%d released=%d new candidates=%d fresh follower checks=%d",
        len(games), len(recent), len(candidate_ids), direct_requests,
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build upcoming calendar and recent real launches from Steam Followers"
    )
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", default="output/steam_preview.json", type=Path)
    parser.add_argument("--recent-state", default="data/steam_recent_launch_state.json", type=Path)
    parser.add_argument("--min-followers", type=int, default=5000)
    parser.add_argument("--delay", type=float, default=2.5)
    parser.add_argument("--max-new-followers", type=int, default=MAX_NEW_RELEASE_REQUESTS)
    parser.add_argument("--follower-interval", type=float, default=30.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    run(
        checkpoint_path=args.checkpoint, output_path=args.output,
        recent_state_path=args.recent_state, min_followers=args.min_followers,
        delay_seconds=args.delay, max_new_followers=args.max_new_followers,
        follower_interval=args.follower_interval,
    )


if __name__ == "__main__":
    main()
