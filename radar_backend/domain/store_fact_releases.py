"""Move master rows to Steam's current release time from the store facts.

The content backend reads every published Steam game from Store Browse four
times a day and keeps Steam's current release time in data/store_facts.json
as release: {at, coming_soon, display}. The master only re-checks a game's
date when the game comes up for Followers again, so a game Steam moved after
that kept its old date: Never Be Alone stayed on 2026-12-08 after Steam moved
it to 2027 Q1, and Nomad Drive on 2026-10-20 though it launched on 2026-10-28.

A row whose release time is more than 12 hours from Steam's current one takes
Steam's time. A full coming-soon date, or a release that has happened, is an
exact date again. A release has happened once its time has passed and Steam
does not call it coming soon: Store Browse leaves the coming-soon flag out for
released games (on 2026-11-25 every released game had it null, so six launched
games were wrongly taken off the calendar). A coming-soon date Steam now shows
only as a month, quarter, year or text loses its full-date status, so it
leaves the published calendar until discovery confirms an exact date and the
Followers phase takes it back; a row this module took off the calendar comes
back as soon as Steam's date is exact again, even at the same time. Small
differences (a launch a few minutes or hours early) are left alone, and so
are rows whose Taiwan Store date is the authority, history more than 60 days
old on both sides, and facts not confirmed in the last day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from radar_core.domain.twitch_admission import has_taiwan_store_date_authority

TAIPEI = ZoneInfo("Asia/Taipei")
STORE_FACTS_PROVIDER = "Steam IStoreBrowseService/GetItems"
EXACT_BASIS = "steam_store_facts_release_time"
NOT_FULL_BASIS = "steam_store_facts_not_full_date"
TOLERANCE = timedelta(hours=12)
FRESH = timedelta(hours=24)
HISTORY = timedelta(days=60)
# Store-day evidence from before the move says nothing about the new date.
STALE_FIELDS = ("release_store_date", "release_date_normalization", "release_date_conflict_note")


def _time(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fact_release(entry: object, now: datetime) -> dict[str, Any] | None:
    """Steam's current release from one store facts entry, or None when it is
    missing, malformed or not confirmed in the last day."""
    if not isinstance(entry, dict) or not isinstance(entry.get("release"), dict):
        return None
    release, checked = entry["release"], _time(entry.get("checked_at"))
    at, soon = _time(release.get("at")), release.get("coming_soon")
    if at is None or checked is None or not timedelta(0) <= now - checked <= FRESH:
        return None
    if soon is not None and type(soon) is not bool:
        return None
    display = release.get("display")
    return {"at": at, "coming_soon": soon, "display": display if isinstance(display, str) else None,
            "checked_at": checked}


def _master_time(row: dict[str, Any]) -> datetime | None:
    exact = _time(row.get("release_time_utc"))
    if exact is not None:
        return exact
    try:
        day = datetime.fromisoformat(str(row.get("release_start")))
    except (TypeError, ValueError):
        return None
    return day.replace(tzinfo=TAIPEI).astimezone(timezone.utc)


def corrected_row(row: object, fact: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """The row on Steam's current release, or None when it stays as it is."""
    if not isinstance(row, dict) or fact is None or has_taiwan_store_date_authority(row):
        return None
    before = _master_time(row)
    at = fact["at"]
    # Steam's time is the launch itself once it has passed and Steam does not
    # call the game coming soon; before that only a full date shown on the
    # Store is an exact date.
    exact = (fact["coming_soon"] is not True and at <= now) or (
        fact["coming_soon"] is not False and fact["display"] == "date_full")
    restored = exact and row.get("release_date_basis") == NOT_FULL_BASIS
    if (before is None or (abs(at - before) <= TOLERANCE and not restored)
            or max(at, before) < now - HISTORY):
        return None
    day = at.astimezone(TAIPEI).date().isoformat()
    result = {key: value for key, value in row.items() if key not in STALE_FIELDS}
    result.update({
        "release_raw": day, "release_start": day, "release_end": day, "release_precision": "day",
        "release_timestamp_taipei_date": day, "release_time_utc": _stamp(at),
        "release_date_timezone": "Asia/Taipei", "release_date_conflict": False,
        "release_date_verified_at": _stamp(fact["checked_at"]),
    })
    if exact:
        # Store Browse confirmed this date, as the Followers phase's own Store check would.
        result.update({"release_display_precision": "date_full", "release_display_provider": STORE_FACTS_PROVIDER,
                       "release_date_basis": EXACT_BASIS, "post_followers_store_verified": True,
                       "post_followers_store_verified_at": _stamp(fact["checked_at"])})
        if "release_time_source" in row:
            result["release_time_source"] = STORE_FACTS_PROVIDER
    else:
        # Not a full date any more: off the published calendar until it is re-verified.
        result.update({"release_display_precision": None, "release_date_basis": NOT_FULL_BASIS,
                       "post_followers_store_verified": False})
    return result


def apply_store_fact_releases(games: list[Any], facts: object, now: datetime) -> tuple[list[Any], list[dict[str, Any]]]:
    """The master rows with Steam's current release times, and what changed."""
    entries = facts.get("games") if isinstance(facts, dict) else None
    if not isinstance(entries, dict) or now.tzinfo is None:
        return games, []
    result, changes = [], []
    for row in games:
        appid = row.get("appid") if isinstance(row, dict) else None
        corrected = corrected_row(row, fact_release(entries.get(str(appid)), now), now) if appid else None
        if corrected is None:
            result.append(row)
            continue
        result.append(corrected)
        changes.append({"appid": appid, "from": row.get("release_start"), "from_time": row.get("release_time_utc"),
                        "to": corrected["release_start"], "to_time": corrected["release_time_utc"],
                        "exact": corrected["release_display_precision"] == "date_full"})
    return result, changes
