"""Bounded Steam retries and conservative migration of old six-hour receipts.

This module does not perform network calls or change catalog eligibility.  A
server-provided Retry-After always takes precedence over a local fallback.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re


POLICY_VERSION = 1
COMMUNITY_STAGE = "steam_community"
METADATA_STAGES = {"steam_store_browse", "steam_appdetails"}


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware retry observation time is required")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError, OverflowError):
        return None


def _attempt(prior: dict | None, field: str) -> int:
    value = prior.get(field) if isinstance(prior, dict) else None
    return value + 1 if type(value) is int and value >= 0 else 1


def _backoff(first: int, cap: int, attempt: int) -> int:
    # Saturating the exponent also handles malformed or very old huge counts.
    return min(cap, first * 2 ** min(attempt - 1, 12))


def _server_retry_seconds(value, now: datetime) -> int | None:
    if not isinstance(value, str):
        return None
    header = value.strip()
    if re.fullmatch(r"[0-9]+", header):
        try:
            seconds = int(header)
            # Values outside datetime's range cannot produce a usable deadline.
            now + timedelta(seconds=seconds)
            return seconds
        except (ValueError, OverflowError):
            return None
    try:
        deadline = parsedate_to_datetime(header)
        if deadline is None:
            return None
        # HTTP-date's obsolete forms can omit an explicit timezone; HTTP dates
        # are UTC, unlike application-provided naive observation timestamps.
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0, math.ceil((_utc(deadline) - now).total_seconds()))
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def rate_limit_policy(stage: str, retry_after, now: datetime,
                      prior: dict | None = None) -> dict:
    """Return a retry receipt measured at the instant a Steam 429 is observed."""
    now = _utc(now)
    if stage not in METADATA_STAGES | {COMMUNITY_STAGE}:
        raise ValueError("Unknown Steam retry stage")
    attempt = _attempt(prior, "attempts")
    seconds = _server_retry_seconds(retry_after, now)
    source = "steam_retry_after"
    if seconds is None:
        source = "default_backoff"
        first, cap = (900, 3600) if stage == COMMUNITY_STAGE else (300, 1800)
        seconds = _backoff(first, cap, attempt)
    return {
        "retry_at": _stamp(now + timedelta(seconds=seconds)),
        "observed_at": _stamp(now), "retry_seconds": seconds,
        "retry_source": source, "retry_after": retry_after,
        "attempts": attempt,
    }


def transient_retry_policy(prior: dict | None, now: datetime) -> dict:
    """Back off ordinary per-game transport/parser failures from five minutes."""
    now = _utc(now)
    attempt = _attempt(prior, "retry_attempts")
    seconds = _backoff(300, 3600, attempt)
    return {
        "retry_attempts": attempt,
        "retry_at": _stamp(now + timedelta(seconds=seconds)),
        "retry_source": "transient_backoff",
    }


def _has_provenance(row: dict, *, allow_community_stage: bool = False) -> bool:
    # False/zero headers still count as evidence; only absent/empty fields do
    # not establish a source. Derived waiting receipts may name Community.
    fields = ("retry_source", "retry_after", "observed_at", "retry_after_source",
              "rate_limit_source", "source", "stage")
    if any(row.get(field) not in (None, "") for field in fields):
        return True
    stage = row.get("rate_limit_stage")
    return stage not in ((None, "", COMMUNITY_STAGE) if allow_community_stage else (None, ""))


def _old_version(row: dict) -> bool:
    version = row.get("validation_version")
    return type(version) is int and 0 <= version <= 2


def _migration_receipt(row: dict, observed: datetime, now: datetime) -> dict:
    after = deepcopy(row)
    after.update({
        "retry_at": _stamp(observed + timedelta(hours=1)),
        "observed_at": _stamp(observed), "retry_seconds": 3600,
        "retry_source": "legacy_default",
        "retry_migration": {
            "policy_version": POLICY_VERSION,
            "original_retry_at": row["retry_at"],
            "original_updated_at": row.get("updated_at"),
            "original_failure_at": _stamp(observed),
            "migrated_at": _stamp(now),
        },
    })
    return after


def legacy_retry_migrations(state: dict, now: datetime) -> tuple[dict, dict]:
    """Normalize only recognizable local six-hour defaults, preserving evidence.

    Returned before/after pairs are optimistic concurrency receipts, not an
    instruction to overwrite newer state unconditionally.
    """
    now = _utc(now)
    normalized = deepcopy(state)
    migrations = {"games": {}, "cooldowns": {}}
    games = state.get("games")
    if not isinstance(games, dict):
        return normalized, migrations
    originals: dict[datetime, datetime] = {}
    for aid, row in games.items():
        if (not isinstance(row, dict) or row.get("reason") != "RateLimited"
                or not _old_version(row) or _has_provenance(row)
                or row.get("retry_migration") is not None):
            continue
        observed, deadline = _time(row.get("updated_at")), _time(row.get("retry_at"))
        if (observed is None or deadline is None or observed > now
                or deadline - observed != timedelta(hours=6)):
            continue
        after = _migration_receipt(row, observed, now)
        migrations["games"][aid] = {"before": deepcopy(row), "after": after}
        normalized["games"][aid] = deepcopy(after)
        originals[deadline] = observed
    if not originals:
        return normalized, migrations

    # The old importer synthesized only a Community-wide limit from these
    # unknown-stage receipts. Other API cooldowns are not evidence of that bug.
    cooldowns = state.get("api_cooldowns")
    current = cooldowns.get(COMMUNITY_STAGE) if isinstance(cooldowns, dict) else None
    if isinstance(current, dict) and not _has_provenance(current):
        deadline, updated = _time(current.get("retry_at")), _time(current.get("updated_at"))
        if (deadline in originals and updated is not None and originals[deadline] <= updated <= now
                and current.get("retry_migration") is None):
            after = _migration_receipt(current, originals[deadline], now)
            migrations["cooldowns"][COMMUNITY_STAGE] = {"before": deepcopy(current), "after": after}
            normalized["api_cooldowns"][COMMUNITY_STAGE] = deepcopy(after)

    # Waiting on the global limit is not an independent Steam 429. A parser
    # upgrade may refresh validation_version while retaining the old deadline,
    # so the recognized seed and provenance/time checks establish inheritance.
    for aid, row in games.items():
        if (not isinstance(row, dict) or row.get("reason") != "steam_community_cooldown"
                or _has_provenance(row, allow_community_stage=True)
                or row.get("retry_migration") is not None):
            continue
        deadline, updated = _time(row.get("retry_at")), _time(row.get("updated_at"))
        if (deadline not in originals or updated is None
                or not originals[deadline] <= updated <= now):
            continue
        after = _migration_receipt(row, originals[deadline], now)
        migrations["games"][aid] = {"before": deepcopy(row), "after": after}
        normalized["games"][aid] = deepcopy(after)
    return normalized, migrations


def apply_legacy_retry_migrations(state: dict, migrations: dict, now: datetime | None) -> dict:
    """Apply proven migrations only while their complete before values match."""
    result = deepcopy(state)
    if not isinstance(migrations, dict):
        return result
    try:
        now = _utc(now)
    except ValueError:
        # Missing/malformed batch clocks cannot prove a safe migration. Ignore
        # the optional operation while leaving ordinary batch merging to its
        # caller's existing validation.
        return result
    _, expected = legacy_retry_migrations(state, now)
    for section, state_section in (("games", "games"), ("cooldowns", "api_cooldowns")):
        proposed = migrations.get(section)
        if not isinstance(proposed, dict):
            continue
        for key, pair in proposed.items():
            current = expected[section].get(key)
            if (not isinstance(pair, dict) or set(pair) != {"before", "after"}
                    or not isinstance(pair.get("before"), dict) or not isinstance(pair.get("after"), dict)
                    or current is None or pair["before"] != current["before"]):
                continue
            # Recompute the after receipt from latest state. An untrusted or
            # stale batch cannot choose a retry deadline or replace identities.
            result[state_section][key] = deepcopy(current["after"])
    return result
