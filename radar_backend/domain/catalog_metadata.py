"""Pure freshness rules for accepted catalog metadata."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable


RELEASE_FIELDS = (
    'release_raw', 'release_start', 'release_end', 'release_precision',
    'release_display_precision', 'release_display_provider', 'release_date_timezone',
    'release_date_basis', 'release_date_verified_at', 'release_time_utc',
    'release_time_source', 'release_timestamp_taipei_date', 'release_date_conflict',
    'post_followers_store_verified', 'post_followers_store_verified_at',
    'release_store_date', 'release_date_normalization',
)

PLAYER_CATEGORY_FIELDS = ('categories', 'categories_source', 'categories_checked_at')
PLAYER_CATEGORY_SOURCES = {
    'Steam IStoreBrowseService/GetItems supported_player_categoryids',
    'Steam Store appdetails cc=TW categories',
}


def player_category_snapshot(
    row: dict, *, now: Callable[[], datetime],
    datetime_type=datetime, timedelta_type=timedelta, timezone_type=timezone,
    fields=PLAYER_CATEGORY_FIELDS, sources=PLAYER_CATEGORY_SOURCES,
) -> tuple[datetime, dict] | None:
    """Accept explicit official Store categories, including an observed empty list."""
    if row.get('categories_source') not in sources:
        return None
    categories = row.get('categories')
    if not isinstance(categories, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get('id'), int) or isinstance(item.get('id'), bool)
        or item['id'] <= 0 or not isinstance(item.get('description'), str)
        for item in categories
    ):
        return None
    try:
        checked = datetime_type.fromisoformat(
            str(row.get('categories_checked_at')).replace('Z', '+00:00'),
        )
        if checked.tzinfo is None or checked.utcoffset() != timedelta_type(0):
            return None
        current = now()
        if checked > current + timedelta_type(minutes=5):
            return None
    except (ValueError, TypeError):
        return None
    return checked, {key: row[key] for key in fields}


def preserve_player_categories(
    existing: dict, incoming: dict, *, snapshot: Callable,
    fields=PLAYER_CATEGORY_FIELDS,
) -> dict:
    """Followers-only snapshots cannot erase or replace newer official player modes."""
    result = dict(incoming)
    for key in fields:
        result.pop(key, None)
    observed = snapshot(incoming)
    prior = snapshot(existing) if existing.get('appid') == incoming.get('appid') else None
    if prior is not None and (observed is None or prior[0] > observed[0]):
        observed = prior
    if observed is not None:
        result.update(observed[1])
    return result


def keep_newer_release(
    existing: dict, incoming: dict, *, datetime_type=datetime,
    timezone_type=timezone, fields=RELEASE_FIELDS,
) -> dict:
    """Content events and master snapshots cannot roll back a newer date audit."""
    def checked_at(row):
        try:
            value = datetime_type.fromisoformat(
                str(row.get('release_date_verified_at')).replace('Z', '+00:00'),
            )
            return value.replace(tzinfo=timezone_type.utc) if value.tzinfo is None else value
        except (ValueError, TypeError):
            return datetime_type.min.replace(tzinfo=timezone_type.utc)

    result = dict(incoming)
    if (
        existing.get('release_display_precision') == 'date_full'
        and checked_at(existing) > checked_at(incoming)
    ):
        for key in fields:
            if key in existing:
                result[key] = existing[key]
            else:
                result.pop(key, None)
    return result
