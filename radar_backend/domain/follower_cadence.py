"""How often an accepted game's official Followers are measured before release.

Weekly while the release is more than 14 days away, every 2 days within the
last 14 days, and always on the release day, whose measurement is the final
count. Days are Taipei calendar days. The daily catchup and the growth job
share this rule, and the frontend insights builder applies the same rule to
validate the growth report's scheduled (not yet due) games.
"""
from __future__ import annotations

from datetime import date, timedelta

FAR_INTERVAL_DAYS = 7
NEAR_INTERVAL_DAYS = 2
NEAR_WINDOW_DAYS = 14


def interval_days(release: date, today: date) -> int:
    return NEAR_INTERVAL_DAYS if (release - today).days <= NEAR_WINDOW_DAYS else FAR_INTERVAL_DAYS


def due(release: date, last: date | None, today: date) -> bool:
    """Whether a measurement is due today, given the Taipei day of the last one."""
    if last is None or last > today:
        return True  # A future measurement day is not evidence.
    if today >= release:
        return last < release  # The release day always gets its final measurement.
    return (today - last).days >= interval_days(release, today)


def next_due(release: date, last: date, today: date) -> date:
    """The first later Taipei day on which a measurement becomes due."""
    day = today + timedelta(days=1)
    while not due(release, last, day):
        day += timedelta(days=1)
    return day
