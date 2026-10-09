"""Pure official queue rules and observation values.

Queue admission receives the existing Twitch rule/cache readers as explicit
ports while those shared helpers await their own migration. No network, disk,
Git, environment or wall clock is consulted here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from radar_core.domain.twitch_admission import aware_time as admission_aware_time

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


@dataclass(frozen=True)
class OfficialObservation:
    appid: int
    group_id64: str | None
    followers: int
    checked_at: str


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


def queue_checked_numeric(data):
    return isinstance(data, int) and not isinstance(data, bool) and data >= 0


def valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).date().isoformat() == value and len(value) == 10
    except ValueError:
        return False


def utc_date_as_taipei(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TAIPEI).date().isoformat()
    except (TypeError, ValueError):
        return None


def make_queue(cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter, official_cache, other_official, *, now, is_twitch_queue_candidate, cached_follower):
    observed = now.astimezone(TAIPEI)
    if len(frozen_rows) != 1317 or len(old_group_rows) != 1358:
        raise ValueError("Frozen original input changed")
    if legacy_cp.get("cohort") != "steam_fresh_20260922_post_adult_1317_near_release":
        raise ValueError("Legacy official checkpoint cohort mismatch")
    existing_official = set(legacy_cp["official_results"]) | set(cp["official_results"])
    existing_official |= {
        str(k) for k, v in official_cache.get("games", {}).items()
        if isinstance(v, dict) and queue_checked_numeric(v.get("followers"))
    }
    existing_official |= {
        str(k) for k, v in other_official.get("verified", {}).items()
        if isinstance(v, dict) and queue_checked_numeric(v.get("official_followers"))
    }
    group_by_id = {str(x["appid"]): x.get("group_short_id") for x in old_group_rows}

    for source in frozen_rows:
        aid = str(int(source["appid"]))
        if not valid_date(source.get("release_date")):
            raise ValueError("Frozen candidate has no exact day")
        group = group_by_id.get(aid)
        gid = group_to_gid(group)
        new = {
            "appid": int(aid),
            "name": source.get("name"),
            "release_date": source["release_date"],
            "group_id64": gid,
            "steam_url": source.get("steam_url"),
            "queue_source": "frozen_20260922_original_third_party_missing",
        }
        # Keep the first validated release date until the actual daily scan
        # confirms a newer date and updates this pending record.
        cp["pending_candidates"].setdefault(aid, new)

    daily = {
        "status": "no_current_day_prefilter",
        "daily_prefilter_updated_at": prefilter.get("updated_at"),
        "eligible_screened_at": eligible.get("screened_at"),
        "added_from_daily": 0,
        "eligible_count": len(eligible.get("games", [])),
        "parked_group_xml_appids": [],
    }
    today = observed.date().isoformat()
    if (utc_date_as_taipei(prefilter.get("updated_at")) == today
            and utc_date_as_taipei(eligible.get("screened_at")) == today):
        rows = eligible.get("games")
        pre = prefilter.get("games")
        if not isinstance(rows, list) or not isinstance(pre, dict):
            raise ValueError("Fresh daily follower source has malformed rows")
        daily["status"] = "current_day_partial_or_complete"
        if not prefilter.get("complete"):
            daily["status"] = "current_day_prefilter_incomplete"
        for source in rows:
            aid = str(int(source["appid"]))
            record = pre.get(aid)
            if not isinstance(record, dict):
                continue
            followers = record.get("third_party_followers")
            # Catch true third-party misses, and higher-signal games for which
            # the existing daily official cache has no numeric value.
            if followers is not None and not (
                queue_checked_numeric(followers) and followers >= 4000
            ):
                continue
            release = source.get("release_start")
            if not valid_date(release) or source.get("release_precision") != "day":
                continue
            # Only Store date_full, official adult screen eligible candidates.
            if source.get("release_display_precision") != "date_full" or (
                source.get("sexual_content_screened") is not True
            ):
                continue
            if aid in existing_official:
                continue
            new = {
                "appid": int(aid),
                "name": source.get("name"),
                "release_date": release,
                "group_id64": group_to_gid(record.get("group_short_id")),
                "steam_url": source.get("store_url") or f"https://store.steampowered.com/app/{aid}/",
                "queue_source": "fresh_daily_prefilter_unresolved" if followers is None
                                else "fresh_daily_prefilter_ge4000_pending_official",
                "daily_source_updated_at": prefilter.get("updated_at"),
            }
            if aid not in cp["pending_candidates"]:
                daily["added_from_daily"] += 1
            previous = cp["pending_candidates"].get(aid) or {}
            # Daily metadata refresh must retain a separately resolved group.
            new["group_id64"] = valid_group_id64(new["group_id64"]) or valid_group_id64(previous.get("group_id64"))
            if isinstance(previous.get("group_resolution"), dict):
                new["group_resolution"] = previous["group_resolution"]
            if previous.get("queue_source") == "twitch_steam_discovery":
                # Keep the verified Twitch admission and its higher priority;
                # the ordinary row remains available when Twitch expires.
                cp["pending_candidates"][aid] = {
                    **previous, "normal_candidate": new,
                    "group_id64": new["group_id64"] or previous.get("group_id64"),
                }
            else:
                cp["pending_candidates"][aid] = new
        daily["status"] = (
            "current_day_prefilter_complete" if prefilter.get("complete")
            else "current_day_prefilter_incomplete"
        )

    # Skip all verified numbers, even if another producer discovered the same
    # AppID today. Never imply old cache is a fresh follower verification.
    # If a fallback /games/{appid}/memberslistxml endpoint has already returned
    # HTML for an AppID with no official group ID, park ONLY that AppID.
    # Do not falsely record an official follower count or stall all later games.
    bad_missing_group = {
        str(event.get("appid")) for event in cp.get("attempt_events", [])
        if event.get("http") == 200
        and event.get("error_type") == "ParseError"
        and "text/html" in event.get("content_type", "").lower()
    }
    pending = []
    for aid, row in cp["pending_candidates"].items():
        twitch = row.get("queue_source") == "twitch_steam_discovery"
        if twitch:
            if not is_twitch_queue_candidate(row, now=observed):
                continue
            if cached_follower(int(aid), [cp, legacy_cp, official_cache, other_official], observed) is not None:
                continue
        elif aid in existing_official:
            continue
        if aid in bad_missing_group and valid_group_id64(row.get("group_id64")) is None:
            cp.setdefault("unresolved_candidates", {})[aid] = {
                **row,
                "status": "official_xml_fallback_returned_html",
                "resolution": "retry when a valid official group ID is available",
                "official_followers": None,
            }
            daily["parked_group_xml_appids"].append(aid)
            continue
        cp.setdefault("unresolved_candidates", {}).pop(aid, None)
        pending.append(row)
    # Rotate priority candidates after a bounded attempt. An unavailable XML
    # endpoint must not always be first after the shared cooldown expires.
    last_twitch_attempt = {}
    for event in cp.get("attempt_events", []):
        if not isinstance(event, dict) or event.get("queue_source") != "twitch_steam_discovery":
            continue
        attempted_at = admission_aware_time(event.get("when_taipei"))
        if attempted_at is not None and attempted_at <= observed:
            aid = str(event.get("appid"))
            last_twitch_attempt[aid] = max(last_twitch_attempt.get(aid, 0), attempted_at.timestamp())
    pending.sort(key=lambda r: (
        0 if r.get("queue_source") == "twitch_steam_discovery" else 1,
        last_twitch_attempt.get(str(r["appid"]), 0) if r.get("queue_source") == "twitch_steam_discovery" else 0,
        r["release_date"] < today,
        r["release_date"] if r["release_date"] >= today
        else -datetime.fromisoformat(r["release_date"]).date().toordinal(),
        r["appid"],
    ))
    daily["combined_pending_now"] = len(pending)
    daily["completed_legacy"] = len(legacy_cp["official_results"])
    daily["completed_new"] = len(cp["official_results"])
    daily["twitch_priority_pending"] = sum(r.get("queue_source") == "twitch_steam_discovery" for r in pending)
    return pending, daily


def preserve_cooldown_deadline(until, *previous):
    """An unsuccessful manual probe cannot shorten an existing retry deadline."""
    for value in previous:
        if (deadline := admission_aware_time(value)) is not None:
            until = max(until, deadline)
    return until
