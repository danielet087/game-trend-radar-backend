"""Existing official cache and cooldown checkpoint fields.

These stores update the caller's state object; they do not own queues or claim
that an in-memory observation has been committed to Git.
"""
from __future__ import annotations

from datetime import timedelta, timezone
import json
from pathlib import Path

from radar_backend.domain.official_queue import (
    TAIPEI, OfficialObservation, aware_time, checked_numeric,
    group_to_gid, steam_429_cooldown, valid_group_id64,
)

def read_state(path: Path, *, optional=False):
    """An absent optional source is allowed; a corrupt source never becomes {}."""
    if optional and not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected state object: {path.name}")
    return value


def save_state(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class OfficialFollowerCache:
    def __init__(self, checkpoint: dict, *sources: dict):
        self.checkpoint = checkpoint
        self.sources = (checkpoint, *sources)
        for source in self.sources:
            if not isinstance(source, dict):
                raise ValueError("Malformed official Followers source state")
            for field in ("official_results", "official_growth_observations", "games", "verified", "pending_candidates"):
                if field in source and not isinstance(source[field], dict):
                    raise ValueError(f"Malformed official Followers {field} state")

    def _rows(self, appid, *, observations_only=False):
        aid = str(appid)
        for source in self.sources:
            for field in ("official_results", "official_growth_observations", "games", "verified", "pending_candidates"):
                if observations_only and field == "pending_candidates":
                    continue
                rows = source.get(field) or {}
                if isinstance(rows, dict) and isinstance(rows.get(aid), dict):
                    row = rows[aid]
                    if row.get("appid") is None or str(row["appid"]) == aid:
                        yield row

    def group_id(self, appid):
        for row in self._rows(appid):
            if group := valid_group_id64(row.get("group_id64") or row.get("official_group_id64")):
                return group
            short = row.get("group_short_id")
            if type(short) is int and 0 < short <= 2 ** 32 - 1:
                return group_to_gid(short)
        return None

    def latest(self, appid, now, *, today_only=False, expected_group=None):
        candidates = []
        for row in self._rows(appid, observations_only=True):
            count = row.get("official_followers", row.get("followers"))
            checked_at = row.get("official_checked_at_taipei") or row.get("checked_at") or row.get("follower_checked_at")
            checked = aware_time(checked_at)
            group = valid_group_id64(row.get("group_id64") or row.get("official_group_id64"))
            if expected_group is not None and group is not None and group != valid_group_id64(expected_group):
                continue
            if (checked_numeric(count) and checked is not None and checked <= now
                    and (not today_only or checked.astimezone(TAIPEI).date() == now.astimezone(TAIPEI).date())):
                candidates.append((checked, OfficialObservation(int(appid), group, count, checked_at)))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def remember(self, observation: OfficialObservation):
        rows = self.checkpoint.setdefault("official_growth_observations", {})
        if not isinstance(rows, dict):
            raise ValueError("Malformed official growth observation state")
        rows[str(observation.appid)] = {
            "appid": observation.appid, "group_id64": observation.group_id64,
            "official_followers": observation.followers,
            "official_checked_at_taipei": observation.checked_at,
            "official_source": "Steam Community XML memberCount",
        }


class CooldownStore:
    """Uses the established checkpoint keys, without owning queue persistence."""
    def __init__(self, checkpoint: dict, *legacy_sources: dict):
        self.checkpoint = checkpoint
        self.sources = (checkpoint, *legacy_sources)

    def deadline(self):
        deadlines = []
        for source in self.sources:
            cooldown = source.get("community_cooldown")
            if cooldown is not None and not isinstance(cooldown, dict):
                raise ValueError("Malformed Community cooldown state")
            for value in (source.get("next_request_after_taipei"), (cooldown or {}).get("retry_at")):
                if value is None:
                    continue
                parsed = aware_time(value)
                if parsed is None:
                    raise ValueError("Malformed Community retry deadline")
                deadlines.append(parsed)
        return max(deadlines) if deadlines else None

    def blocked(self, now):
        until = self.deadline()
        return until is not None and now < until

    def rate_limited(self, response, now):
        count = self.checkpoint.get("rate_limit_count", 0)
        if not checked_numeric(count):
            raise ValueError("Malformed Community rate-limit counter")
        count += 1
        calculated = steam_429_cooldown(response, count, now)
        until = max(calculated, self.deadline() or calculated)
        header = (getattr(response, "headers", None) or {}).get("Retry-After")
        self.checkpoint["rate_limit_count"] = count
        self.checkpoint["next_request_after_taipei"] = until.astimezone(TAIPEI).isoformat()
        self.checkpoint["community_cooldown"] = {
            "retry_at": until.astimezone(timezone.utc).isoformat(),
            "observed_at": now.astimezone(timezone.utc).isoformat(),
            "updated_at": now.astimezone(timezone.utc).isoformat(),
            "retry_seconds": int((until - now).total_seconds()), "retry_after": header,
            "retry_source": ("existing_deadline_with_new_backoff" if until > calculated
                             else "steam_retry_after_with_default_minimum" if header else "existing_queue_backoff"),
            "attempts": count,
        }
        return until

    def temporary_failure(self, now, *, access_error=False):
        if access_error:
            until = now + timedelta(hours=24)
        else:
            prior = self.checkpoint.get("temporary_error_count", 0)
            if not checked_numeric(prior):
                raise ValueError("Malformed Community temporary-error counter")
            count = min(8, prior + 1)
            self.checkpoint["temporary_error_count"] = count
            until = now + timedelta(minutes=min(24 * 60, 15 * 2 ** (count - 1)))
        until = max(until, self.deadline() or until)
        self.checkpoint["next_request_after_taipei"] = until.astimezone(TAIPEI).isoformat()
        return until

    def success(self, now):
        self.checkpoint.update(rate_limit_count=0, temporary_error_count=0,
                               next_request_after_taipei=None, community_cooldown=None,
                               community_last_success_at=now.astimezone(TAIPEI).isoformat())
