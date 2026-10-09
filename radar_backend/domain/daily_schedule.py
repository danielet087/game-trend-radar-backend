"""Pure daily reset ownership and external workflow slot rules."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
WORKFLOWS = {"daily-discovery", "official-followers", "public-growth"}
DAILY_DISCOVERY_HOURS = {0, 6, 12, 18}
PUBLIC_GROWTH_HOURS = {1, 7, 13, 19}


def daily_slot(
    day: date,
    *,
    datetime_type=datetime,
    time_type=time,
    taipei=TAIPEI,
    timezone_type=timezone,
) -> str:
    return datetime_type.combine(day, time_type(), taipei).astimezone(timezone_type.utc).isoformat().replace("+00:00", "Z")


def daily_reset_required(
    state: dict,
    day: date,
    *,
    force: bool = False,
    daily_slot_fn: Callable[[date], str] | None = None,
) -> bool:
    """A persisted slot only owns progress when its candidate anchor agrees."""
    if force or state.get("anchor_date") != day.isoformat() or state.get("mode") != "two_phase_steam_year":
        return True
    # Adopt today's pre-migration refresh without discarding discovery/prefilter
    # progress. An older anchor can never qualify through missing markers.
    if not state.get("daily_refresh_slot") and not state.get("last_reset_date_taipei"):
        return False
    slot_for_day = daily_slot if daily_slot_fn is None else daily_slot_fn
    return not (state.get("daily_refresh_slot") == slot_for_day(day)
                and state.get("last_reset_date_taipei") == day.isoformat())


def schedule_decision(
    workflow: str,
    event_name: str,
    trigger_source: str,
    target_slot: str,
    refresh_today: bool,
    now: datetime,
    *,
    datetime_type=datetime,
    taipei=TAIPEI,
    workflows=WORKFLOWS,
    daily_discovery_hours=DAILY_DISCOVERY_HOURS,
    public_growth_hours=PUBLIC_GROWTH_HOURS,
) -> tuple[bool, str]:
    if workflow not in workflows:
        raise ValueError("Unknown scheduled workflow")
    source = trigger_source or "manual"
    if source not in {"manual", "cloudflare"}:
        raise ValueError("Unknown trigger_source")
    current = now.astimezone(taipei)
    if source != "cloudflare":
        if workflow == "official-followers" and event_name != "workflow_dispatch":
            return 3 <= current.hour <= 23, "GitHub schedule Taiwan execution window"
        return True, "Manual or existing GitHub trigger"
    if event_name != "workflow_dispatch":
        raise ValueError("Cloudflare must use workflow_dispatch")
    if not target_slot:
        raise ValueError("Cloudflare target_slot is required")
    try:
        slot = datetime_type.fromisoformat(target_slot.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("target_slot must be an ISO UTC timestamp") from exc
    if slot.tzinfo is None or slot.utcoffset().total_seconds() != 0:
        raise ValueError("target_slot must include the UTC timezone")
    local = slot.astimezone(taipei)
    if local.second != 0 or local.microsecond != 0:
        raise ValueError("target_slot must use a whole scheduled minute")
    if workflow == "daily-discovery":
        if not refresh_today:
            raise ValueError("Cloudflare daily discovery requires refresh_today=true")
        if local.hour not in daily_discovery_hours or local.minute != 0:
            raise ValueError("Daily discovery target_slot must be Taiwan 00:00/06:00/12:00/18:00")
    elif workflow == "public-growth":
        if local.hour not in public_growth_hours or local.minute != 15:
            raise ValueError("Growth target_slot must be Taiwan 01:15/07:15/13:15/19:15")
    elif not (3 <= local.hour <= 23 and local.minute == 0):
        raise ValueError("Followers target_slot must be a Taiwan 03:00–23:00 hour")
    if slot > now:
        return False, "External scheduled slot is in the future"
    if local.date() != current.date():
        return False, "External scheduled slot belongs to another Taiwan day"
    if workflow == "official-followers" and local.hour != current.hour:
        return False, "External Followers slot crossed its Taiwan execution hour"
    return True, "External scheduled slot is current"
