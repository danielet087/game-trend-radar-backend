"""Parse Steam announcements and aware instants into Taiwan date windows."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

TAIWAN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ReleaseWindow:
    raw: str
    start: date | None
    end: date | None
    precision: str

    def overlaps(self, start: date, end: date) -> bool:
        return self.start is not None and self.end is not None and self.end >= start and self.start <= end


def _month_number(text: str) -> int | None:
    return {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }.get(text.strip().lower()[:3])


def parse_release_window(
    raw: Any,
    *,
    datetime_type: Any = datetime,
    date_type: Any = date,
    taiwan_tz: Any = TAIWAN_TZ,
    window_type: Any = ReleaseWindow,
    month_number: Callable[[str], int | None] | None = None,
    parse_window: Callable[[Any], ReleaseWindow] | None = None,
) -> ReleaseWindow:
    """Preserve date-only announcements; convert only supplied release times.

    The optional parser primitives let the collector's compatibility wrapper
    retain its existing call-time patch points without owning these rules.
    """
    month_lookup = _month_number if month_number is None else month_number
    if raw is None:
        return window_type("", None, None, "unknown")

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            stamp = float(raw)
            if abs(stamp) >= 100_000_000_000:
                stamp /= 1000
            d = datetime_type.fromtimestamp(stamp, tz=taiwan_tz).date()
            return window_type(str(raw), d, d, "day")
        except (OverflowError, OSError, ValueError):
            return window_type(str(raw), None, None, "unknown")

    text = " ".join(str(raw).split())
    if not text:
        return window_type(text, None, None, "unknown")

    if re.fullmatch(r"\d{10}|\d{13}", text):
        if parse_window is None:
            release = parse_release_window(
                int(text), datetime_type=datetime_type, date_type=date_type,
                taiwan_tz=taiwan_tz, window_type=window_type,
                month_number=month_lookup,
            )
        else:
            release = parse_window(int(text))
        return window_type(text, release.start, release.end, release.precision)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:?\d{2})", text):
        try:
            instant = datetime_type.fromisoformat(text.replace("Z", "+00:00"))
            if instant.tzinfo is not None:
                d = instant.astimezone(taiwan_tz).date()
                return window_type(text, d, d, "day")
        except ValueError:
            pass

    normalized = text.lower().replace(".", "")
    if normalized in {
        "coming soon", "soon", "tba", "tbd", "to be announced",
        "date to be announced", "announced later",
    }:
        return window_type(text, None, None, "unknown")

    try:
        d = date_type.fromisoformat(text)
        return window_type(text, d, d, "day")
    except ValueError:
        pass

    match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})[,]?\s+(\d{4})", text)
    if match:
        month = month_lookup(match.group(2))
        if month:
            try:
                d = date_type(int(match.group(3)), month, int(match.group(1)))
                return window_type(text, d, d, "day")
            except ValueError:
                pass

    match = re.fullmatch(r"([A-Za-z]{3,9})\s+(\d{1,2})[,]?\s+(\d{4})", text)
    if match:
        month = month_lookup(match.group(1))
        if month:
            try:
                d = date_type(int(match.group(3)), month, int(match.group(2)))
                return window_type(text, d, d, "day")
            except ValueError:
                pass

    match = re.fullmatch(r"([A-Za-z]{3,9})\s+(\d{4})", text)
    if match:
        month = month_lookup(match.group(1))
        year = int(match.group(2))
        if month:
            last = calendar.monthrange(year, month)[1]
            return window_type(text, date_type(year, month, 1), date_type(year, month, last), "month")

    match = re.fullmatch(r"Q([1-4])[,]?\s*(\d{4})", text, flags=re.IGNORECASE)
    if match:
        quarter = int(match.group(1))
        year = int(match.group(2))
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        last = calendar.monthrange(year, end_month)[1]
        return window_type(text, date_type(year, start_month, 1), date_type(year, end_month, last), "quarter")

    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        return window_type(text, date_type(year, 1, 1), date_type(year, 12, 31), "year")

    return window_type(text, None, None, "unknown")
