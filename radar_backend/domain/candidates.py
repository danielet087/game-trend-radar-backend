"""Pure Steam candidate rules; no network, filesystem, or environment reads.

Release qualification and phase prerequisites intentionally retain the existing
pipeline semantics. External adult ledgers and Twitch admission are supplied as
values/predicates rather than loaded by this module.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from radar_core.jobs import JobResult, JobStatus

TAIWAN_TZ = timezone(timedelta(hours=8))
QUERY_URL = "https://api.steampowered.com/IStoreQueryService/Query/v1/"
PRIORITY_THRESHOLD = 4000

def fresh_state(anchor: date, days: int) -> dict[str, Any]:
    return {
        "version": 1, "mode": "two_phase_steam_year",
        "phase": "discovery", "date_precision_required": True,
        "anchor_date": anchor.isoformat(),
        "end_date": (anchor + timedelta(days=days - 1)).isoformat(),
        "next_date": anchor.isoformat(), "days_scanned": 0,
        "next_follower_index": 0, "initial_complete": False,
        "coverage_exhaustive": False, "last_attempt": None,
    }

def candidate_record(item: dict[str, Any], expected_day: date) -> dict[str, Any] | None:
    if not isinstance(item, dict) or not isinstance(item.get("appid"), int):
        return None
    release = item.get("release") or {}
    if not isinstance(release, dict) or release.get("is_coming_soon") is not True:
        return None
    stamp = release.get("steam_release_date")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
        return None
    try:
        instant = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    tw_day = instant.astimezone(TAIWAN_TZ).date()
    if tw_day != expected_day:
        return None
    appid = item["appid"]
    name = str(item.get("name") or f"Steam App {appid}").strip()
    assets = item.get("assets") or {}
    if not isinstance(assets, dict):
        assets = {}
    # Store asset keys change over time. The public Steam capsule URL is a
    # fallback and a broken image is already handled in the frontend.
    capsule = (
        assets.get("small_capsule")
        or assets.get("capsule")
        or assets.get("header")
        or f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/capsule_231x87.jpg"
    )
    return {
        "appid": appid, "name": name, "name_en": name, "name_zh_tw": None,
        "release_raw": tw_day.isoformat(),
        "release_start": tw_day.isoformat(), "release_end": tw_day.isoformat(),
        "release_precision": "day", "release_date_timezone": "Asia/Taipei",
        "release_date_basis": "steam_store_query_release_time",
        "release_time_utc": instant.isoformat().replace("+00:00", "Z"),
        "release_time_source": QUERY_URL,
        "capsule_image": str(capsule), "store_url": f"https://store.steampowered.com/app/{appid}/",
        "discovered_by": "IStoreQueryService/Query",
    }

def prefilter_complete(prefilter: dict[str, Any], catalog: list[dict[str, Any]]) -> bool:
    """Third-party step cannot be skipped, including the first 685 cached games."""
    items = prefilter.get("games") or {}
    return (
        bool(prefilter.get("complete"))
        and int(prefilter.get("next_index", -1)) >= len(catalog)
        and all(str(row["appid"]) in items for row in catalog)
    )

def date_gate_candidates(state: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """The full discovery window precedes public display-date verification."""
    original = catalog.get("games", [])
    if not original or int(state.get("days_scanned", 0)) < 365:
        raise RuntimeError("Finish 365-day Steam discovery before date gate")
    return original


def prescreen_window_complete(
    prefilter: dict[str, Any], rows: list[dict[str, Any]], head_limit: int,
) -> bool:
    """Both the historical cached head and remaining window must be screened."""
    return (
        int(prefilter.get("head_next_index", 0)) >= head_limit
        and int(prefilter.get("next_index", 0)) >= len(rows)
        and all(str(row["appid"]) in prefilter["games"] for row in rows)
    )


def official_request_budget(value: Any) -> int:
    """Keep the bounded official XML allowance independent of transport."""
    budget = int(value)
    if not 1 <= budget <= 50:
        raise ValueError("Official XML budget must be within 1..50")
    return budget


def candidate_job_result(state: dict[str, Any], *, state_persisted: bool = False) -> JobResult:
    """Collection progress is separate from the workflow's durable checkpoint."""
    collection_complete = state.get("phase") == "complete" and state.get("initial_complete") is True
    attempt = state.get("last_attempt") or {}
    if collection_complete and state_persisted:
        status = JobStatus.COMPLETE
    elif attempt.get("rate_limit_events", 0):
        status = JobStatus.COOLING_DOWN
    else:
        status = JobStatus.PARTIAL
    return JobResult(
        job="steam-candidate-pipeline", status=status,
        reason=str(state.get("phase") or "unknown_phase"),
        collection_complete=collection_complete, state_persisted=state_persisted,
        # Producing output/steam_upcoming.json does not publish it to the site.
        published=False, requires_publication=False,
        target_slot=state.get("daily_refresh_slot"),
    )

def candidate_catalog_rows(
    catalog: dict[str, Any], state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Future runs use only the Store-verified eligible list.

    Old, already-completed historical catalogues are left intact, including
    their saved follower/prefilter cursors and original 11,467 records.
    """
    if state.get("date_precision_required"):
        if not catalog.get("date_precision_complete"):
            raise RuntimeError("Steam public display-date gate incomplete; no Followers allowed")
        rows = catalog["date_precision_eligible"]
    else:
        rows = catalog.get("games", [])
    return rows


def filter_candidate_rows(
    rows: list[dict[str, Any]], *, blocked: set[int],
    is_disallowed: Callable[[dict[str, Any], set[int]], bool],
) -> list[dict[str, Any]]:
    return [row for row in rows if not is_disallowed(row, blocked)]

def priority_rows(rows: list[dict[str, Any]], prefilter: dict[str, Any]) -> list[dict[str, Any]]:
    """Only measured third-party results at the existing threshold reach XML."""
    return [
        row for row in rows
        if (prefilter["games"][str(row["appid"])].get("priority")
            and isinstance(prefilter["games"][str(row["appid"])].get("third_party_followers"), int)
            and prefilter["games"][str(row["appid"])]["third_party_followers"] >= PRIORITY_THRESHOLD)
    ]


def normalize_prefilter_entries(games: dict[str, dict[str, Any]]) -> None:
    """A missing observation remains unresolved; never infer a low count."""
    for entry in games.values():
        members = entry.get("third_party_followers")
        if members is None:
            entry["priority"] = False
            entry["scheduling_band"] = "unresolved"
        elif isinstance(members, int):
            entry["priority"] = members >= PRIORITY_THRESHOLD
            entry["scheduling_band"] = "measured"


def public_candidate_rows(
    original_games: list[dict[str, Any]], catalog_rows: list[dict[str, Any]], *,
    date_precision_required: bool, today: date,
    is_twitch_qualified: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    """Retain formally qualified release history beyond the rolling future window."""
    if not date_precision_required:
        return original_games
    allowed = {row["appid"] for row in catalog_rows}
    today_iso = today.isoformat()
    return [
        row for row in original_games
        if row.get("appid") in allowed
        or is_twitch_qualified(row)
        or (
            isinstance(row.get("release_start"), str)
            and row["release_start"] <= today_iso
        )
    ]
