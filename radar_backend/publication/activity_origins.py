"""Record the publisher that actually changed an accepted Steam catalog.

The append-only sidecar shares its commit with the catalog. Comparing the
latest checkout before and after rendering keeps publication retries from
claiming changes that another publisher already delivered.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import os

from radar_core.domain.twitch_admission import (
    aware_time, is_twitch_qualified, normalize_twitch_admission,
)
from radar_core.publication import snapshot_revision

ACTIVITY_ORIGINS_PATH = "data/publication/activity_origins.json"
MAX_ACTIVITY_ORIGINS = 1000
ACTIVITY_REASONS = frozenset({
    "twitch_rankings", "steam_daily_search", "steam_official_verification",
    "steam_metadata_refresh", "steam_catalog_sync",
})
WORKFLOW_REASONS = {
    "Steam candidates then Followers": "steam_daily_search",
    "Steam official Followers - hourly auto 250": "steam_official_verification",
    "Steam official Followers - hourly bounded queue": "steam_official_verification",
}


def freeze_activity_context(*, now: datetime, activity_source: str | None = None) -> dict:
    """Capture the collection reason and clock before any Git refresh or retry."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware activity publication clock required")
    reason = activity_source if activity_source is not None else WORKFLOW_REASONS.get(
        os.environ.get("GITHUB_WORKFLOW"), "steam_catalog_sync",
    )
    if not isinstance(reason, str) or reason not in ACTIVITY_REASONS:
        raise ValueError("A supported activity update reason required")
    return {"update_reason": reason,
            "at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}


def _day(value) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _catalog_rows(catalog: dict) -> dict[int, dict]:
    if not isinstance(catalog, dict):
        raise ValueError("Complete Steam catalog required for activity origins")
    # An absent catalog on the first deployment is an empty previous snapshot.
    rows = catalog.get("games", []) if not catalog else catalog.get("games")
    if not isinstance(rows, list) or catalog.get("count", len(rows)) != len(rows):
        raise ValueError("Complete Steam catalog required for activity origins")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("appid")) is not int
                or row["appid"] <= 0 or row["appid"] in result
                or not _day(row.get("release_start"))):
            raise ValueError("Unique, dated Steam rows required for activity origins")
        result[row["appid"]] = row
    return result


def _existing_events(previous: dict | None) -> list[dict]:
    if previous is None or previous == {}:
        return []
    if (not isinstance(previous, dict) or type(previous.get("schema_version")) is not int
            or previous["schema_version"] != 1 or not isinstance(previous.get("events"), list)):
        raise ValueError("Invalid activity origins sidecar")
    snapshot_revision(previous)
    for event in previous["events"]:
        if (not isinstance(event, dict) or type(event.get("appid")) is not int
                or event["appid"] <= 0 or event.get("type") not in {"added", "release_date"}
                or not _day(event.get("date")) or aware_time(event.get("at")) is None
                or not isinstance(event.get("update_reason"), str)
                or event["update_reason"] not in ACTIVITY_REASONS
                or (event["type"] == "release_date"
                    and not _day(event.get("previous_date")))):
            raise ValueError("Invalid activity origin event")
    return deepcopy(previous["events"])


def catalog_activity_origins(previous: dict | None, before: dict, after: dict, *,
                             update_reason: str, at: str, payload_revision: str | None = None) -> dict:
    """Append actual added/date transitions while preserving earlier evidence.

    A persistent Twitch admission identifies a new game's enrollment only.
    It does not identify the producer of a later release-date correction.
    """
    clock = aware_time(at)
    if (clock is None or not isinstance(update_reason, str) or update_reason not in ACTIVITY_REASONS):
        raise ValueError("A frozen activity reason and aware timestamp required")
    if payload_revision is not None and (
            not isinstance(payload_revision, str) or not payload_revision.strip()):
        raise ValueError("Invalid activity origin payload revision")
    events = _existing_events(previous)
    old_rows, new_rows = _catalog_rows(before), _catalog_rows(after)
    identities = {snapshot_revision(event) for event in events}
    for appid, row in new_rows.items():
        prior = old_rows.get(appid)
        kind = "added" if prior is None else (
            "release_date" if prior["release_start"] != row["release_start"] else None
        )
        if kind is None:
            continue
        reason = update_reason
        if kind == "added" and is_twitch_qualified(row):
            proof = normalize_twitch_admission(row.get("twitch_admission"), appid)
            if proof is not None and aware_time(proof["checked_at"]) <= clock:
                reason = "twitch_rankings"
        event = {"appid": appid, "type": kind, "date": row["release_start"],
                 "update_reason": reason, "at": at}
        if kind == "release_date":
            event["previous_date"] = prior["release_start"]
        if payload_revision is not None:
            event["payload_revision"] = payload_revision
        identity = snapshot_revision(event)
        if identity not in identities:
            events.append(event)
            identities.add(identity)
    return {**deepcopy(previous or {}), "schema_version": 1,
            "events": events[-MAX_ACTIVITY_ORIGINS:]}
