"""Validate external workflow slots and complete daily collection results."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import os
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
WORKFLOWS = {"daily-discovery", "official-followers", "public-growth"}
DAILY_DISCOVERY_HOURS = {0, 6, 12, 18}
PUBLIC_GROWTH_HOURS = {1, 7, 13, 19}


def daily_slot(day: date) -> str:
    return datetime.combine(day, time(), TAIPEI).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def daily_reset_required(state: dict, day: date, *, force: bool = False) -> bool:
    """A persisted slot only owns progress when its candidate anchor agrees."""
    if force or state.get("anchor_date") != day.isoformat() or state.get("mode") != "two_phase_steam_year":
        return True
    # Adopt today's pre-migration refresh without discarding discovery/prefilter
    # progress. An older anchor can never qualify through missing markers.
    if not state.get("daily_refresh_slot") and not state.get("last_reset_date_taipei"):
        return False
    return not (state.get("daily_refresh_slot") == daily_slot(day)
                and state.get("last_reset_date_taipei") == day.isoformat())


def growth_collection_complete(result: dict, now: datetime) -> bool:
    """Partial output stays publishable but cannot suppress later daily retries."""
    if not isinstance(result, dict) or result.get("reason") != "completed" or result.get("errors") != []:
        return False
    eligible = result.get("eligible")
    measurements = result.get("measurements")
    if (not isinstance(eligible, int) or isinstance(eligible, bool) or eligible < 0
            or not isinstance(measurements, list) or len(measurements) != eligible):
        return False
    today = now.astimezone(TAIPEI).date()
    ids = set()
    for row in measurements:
        if not isinstance(row, dict):
            return False
        aid, followers = row.get("appid"), row.get("followers")
        if (not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0 or aid in ids
                or not isinstance(followers, int) or isinstance(followers, bool) or followers < 0):
            return False
        try:
            measured_at = datetime.fromisoformat(str(row.get("at", "")).replace("Z", "+00:00"))
        except ValueError:
            return False
        if (measured_at.tzinfo is None or measured_at > now
                or measured_at.astimezone(TAIPEI).date() != today):
            return False
        ids.add(aid)
    return True


def schedule_decision(
    workflow: str,
    event_name: str,
    trigger_source: str,
    target_slot: str,
    refresh_today: bool,
    now: datetime,
) -> tuple[bool, str]:
    if workflow not in WORKFLOWS:
        raise ValueError("Unknown scheduled workflow")
    source = trigger_source or "manual"
    if source not in {"manual", "cloudflare"}:
        raise ValueError("Unknown trigger_source")
    current = now.astimezone(TAIPEI)
    if source != "cloudflare":
        if workflow == "official-followers" and event_name != "workflow_dispatch":
            return 3 <= current.hour <= 23, "GitHub schedule Taiwan execution window"
        return True, "Manual or existing GitHub trigger"
    if event_name != "workflow_dispatch":
        raise ValueError("Cloudflare must use workflow_dispatch")
    if not target_slot:
        raise ValueError("Cloudflare target_slot is required")
    try:
        slot = datetime.fromisoformat(target_slot.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("target_slot must be an ISO UTC timestamp") from exc
    if slot.tzinfo is None or slot.utcoffset().total_seconds() != 0:
        raise ValueError("target_slot must include the UTC timezone")
    local = slot.astimezone(TAIPEI)
    if local.second != 0 or local.microsecond != 0:
        raise ValueError("target_slot must use a whole scheduled minute")
    if workflow == "daily-discovery":
        if not refresh_today:
            raise ValueError("Cloudflare daily discovery requires refresh_today=true")
        if local.hour not in DAILY_DISCOVERY_HOURS or local.minute != 0:
            raise ValueError("Daily discovery target_slot must be Taiwan 00:00/06:00/12:00/18:00")
    elif workflow == "public-growth":
        if local.hour not in PUBLIC_GROWTH_HOURS or local.minute != 15:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", choices=sorted(WORKFLOWS), required=True)
    parser.add_argument("--fail-on-skip", action="store_true")
    args = parser.parse_args()
    proceed, reason = schedule_decision(
        args.workflow,
        os.environ.get("SCHEDULE_EVENT", ""),
        os.environ.get("SCHEDULE_TRIGGER_SOURCE", ""),
        os.environ.get("SCHEDULE_TARGET_SLOT", ""),
        os.environ.get("SCHEDULE_REFRESH_TODAY", "false").lower() == "true",
        datetime.now(timezone.utc),
    )
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"proceed={str(proceed).lower()}\n")
    message = f"EXTERNAL_SCHEDULE {'proceed' if proceed else 'skipped'}: {reason}"
    print(message, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"- {message}\n")
    if not proceed and args.fail_on_skip:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
