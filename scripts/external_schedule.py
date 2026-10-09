"""Validate external workflow slots and complete daily collection results."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_backend.domain.growth import growth_collection_complete
from radar_backend.publication.growth import stamp_growth_publication
from radar_backend.domain import daily_schedule as _schedule_rules


TAIPEI = ZoneInfo("Asia/Taipei")
WORKFLOWS = {"daily-discovery", "official-followers", "public-growth"}
DAILY_DISCOVERY_HOURS = {0, 6, 12, 18}
PUBLIC_GROWTH_HOURS = {1, 7, 13, 19}


def daily_slot(day: date) -> str:
    return _schedule_rules.daily_slot(day, datetime_type=datetime, time_type=time, taipei=TAIPEI, timezone_type=timezone)


def daily_reset_required(state: dict, day: date, *, force: bool = False) -> bool:
    return _schedule_rules.daily_reset_required(state, day, force=force, daily_slot_fn=daily_slot)


def schedule_decision(
    workflow: str,
    event_name: str,
    trigger_source: str,
    target_slot: str,
    refresh_today: bool,
    now: datetime,
) -> tuple[bool, str]:
    return _schedule_rules.schedule_decision(
        workflow, event_name, trigger_source, target_slot, refresh_today, now,
        datetime_type=datetime, taipei=TAIPEI, workflows=WORKFLOWS,
        daily_discovery_hours=DAILY_DISCOVERY_HOURS, public_growth_hours=PUBLIC_GROWTH_HOURS,
    )


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
