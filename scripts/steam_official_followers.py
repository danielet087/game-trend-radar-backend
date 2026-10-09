"""Steam Community boundary shared by queue and public growth jobs.

No game eligibility or publication rules belong here. Callers retain their
queues and persist the existing checkpoint; only official observations and
Community retry fields are changed by this adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
from typing import Callable
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
GROUP_BASE = 103582791429521408


def aware_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def checked_numeric(value):
    return type(value) is int and value >= 0


def valid_group_id64(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return str(number) if GROUP_BASE < number <= GROUP_BASE + (2 ** 32 - 1) else None


def group_to_gid(short):
    if short is None:
        return None
    if type(short) is not int or not 0 < short <= 2 ** 32 - 1:
        raise ValueError("Bad Steam short Group ID; no fabricated IDs")
    return str(GROUP_BASE + short)


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


def steam_429_cooldown(response, failure_count, now):
    """Existing 15m exponential backoff, at most 24h; honor longer Retry-After."""
    minutes = min(24 * 60, 15 * 2 ** min(max(failure_count - 1, 0), 7))
    until = now + timedelta(minutes=minutes)
    header = (getattr(response, "headers", None) or {}).get("Retry-After")
    if header:
        try:
            value = header.strip()
            server_until = (now + timedelta(seconds=int(value)) if value.isdigit()
                            else parsedate_to_datetime(value))
            if server_until.tzinfo is None:
                server_until = server_until.replace(tzinfo=timezone.utc)
            until = max(until, server_until)
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
    return until


def parse_official_xml(body, expected_group):
    """A numeric memberCount is evidence only for the requested clan identity."""
    expected_group = valid_group_id64(expected_group)
    if expected_group is None:
        raise ValueError("An official Group ID is required")
    root = ET.fromstring(body)
    count = (root.findtext(".//memberCount") or "").replace(",", "").strip()
    group = (root.findtext(".//groupID64") or "").strip()
    if not count.isascii() or not count.isdigit() or valid_group_id64(group) != expected_group:
        raise ValueError("Missing count or mismatched official Steam group")
    return int(count)


@dataclass(frozen=True)
class OfficialObservation:
    appid: int
    group_id64: str | None
    followers: int
    checked_at: str


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


@dataclass(frozen=True)
class FollowerOutcome:
    status: str
    observed_at: datetime
    followers: int | None = None
    http: int | None = None
    error_type: str | None = None
    retry_after: str | None = None
    content_type: str | None = None
    response_bytes: int | None = None
    response_prefix: str | None = None


class OfficialFollowerClient:
    def __init__(self, *, session=None, clock: Callable = None, cooldown: CooldownStore = None):
        self.session = session if session is not None else requests.Session()
        self.clock = clock or (lambda: datetime.now(TAIPEI))
        self.cooldown = cooldown

    def fetch(self, group_id64, *, manual_override=False):
        now = self.clock()
        gid = valid_group_id64(group_id64)
        if gid is None:
            return FollowerOutcome("awaiting_group_resolution", now)
        if self.cooldown and not manual_override and self.cooldown.blocked(now):
            return FollowerOutcome("cooldown_no_request", now)
        response = None
        try:
            response = self.session.get(f"https://steamcommunity.com/gid/{gid}/memberslistxml/?xml=1", timeout=(8, 24))
            observed = self.clock()
            if response.status_code == 429:
                if self.cooldown:
                    self.cooldown.rate_limited(response, observed)
                return FollowerOutcome("rate_limited", observed, http=429, retry_after=(getattr(response, "headers", None) or {}).get("Retry-After"))
            if response.status_code in (401, 403) or response.status_code >= 500:
                if self.cooldown:
                    self.cooldown.temporary_failure(observed, access_error=True)
                return FollowerOutcome("access_or_server_error", observed, http=response.status_code)
            if response.status_code != 200:
                return FollowerOutcome("unexpected_http", observed, http=response.status_code)
            body = getattr(response, "content", None)
            if body is None:
                body = response.text
            try:
                count = parse_official_xml(body, gid)
            except ValueError:
                return FollowerOutcome("missing_count_or_group_mismatch", observed, http=200)
            if self.cooldown:
                self.cooldown.success(observed)
            return FollowerOutcome("ok", observed, followers=count, http=200)
        except (requests.RequestException, ET.ParseError) as exc:
            observed = self.clock()
            if self.cooldown:
                self.cooldown.temporary_failure(observed)
            body = getattr(response, "content", b"") if response is not None else b""
            if isinstance(body, str):
                body = body.encode()
            headers = (getattr(response, "headers", None) or {}) if response is not None else {}
            return FollowerOutcome("transport_or_xml_error", observed,
                http=getattr(response, "status_code", None), error_type=type(exc).__name__,
                content_type=headers.get("Content-Type", "")[:100] if isinstance(exc, ET.ParseError) else None,
                response_bytes=len(body) if isinstance(exc, ET.ParseError) else None,
                response_prefix=body[:180].decode("utf-8", errors="replace") if isinstance(exc, ET.ParseError) else None)
