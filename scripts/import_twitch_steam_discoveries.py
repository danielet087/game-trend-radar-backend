"""Import confirmed Twitch discoveries and queue missing official Followers.

Collect caches a bounded API batch. Apply merges that batch into latest main
without network calls. Dispatch runs only after the accepted master is saved.
Both accepted records and dispatch receipts use the same conflict-safe apply.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from collectors.steam_upcoming import parse_release_window
from radar_backend.adapters.public_catalog import keep_newer_release
from scripts.screen_steam_candidates_before_followers import is_explicit_sex_game
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.steam_master_date_gate import parse_store_release_detail
from scripts.steam_retry_policy import rate_limit_policy, transient_retry_policy
from scripts.twitch_steam_admission import (
    METHOD, aware_time, decimal_id, is_twitch_qualified,
    normalize_twitch_admission, preserve_twitch_admission, valid_enrollment, validate_twitch_snapshot,
    resolve_store_release_day, TW_STORE_DATE_AUTHORITY, TW_STORE_DATE_PROVIDER,
)

TAIPEI = ZoneInfo("Asia/Taipei")
STORE_BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
STATE_VALIDATION_VERSION = 4


class RateLimited(RuntimeError):
    def __init__(self, stage: str, retry_seconds: int, *, policy: dict | None = None):
        super().__init__("Steam HTTP 429")
        self.stage = stage
        self.retry_seconds = retry_seconds
        self.policy = policy


def stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path, *, optional: bool = False) -> dict:
    if optional and not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_snapshot(discovery: dict, tracking: dict, catalog: dict, commit: str,
                      now: datetime) -> list[tuple[int, dict]]:
    """Cross-check links against the authoritative registry in one git commit."""
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("An immutable frontend commit is required")
    if discovery.get("schema_version") != 1 or not isinstance(discovery.get("games"), dict):
        raise ValueError("Malformed Twitch discovery state")
    if tracking.get("schema_version") != 1 or not isinstance(tracking.get("games"), dict):
        raise ValueError("Malformed Twitch tracking registry")
    if not isinstance(catalog.get("games"), list) or catalog.get("count") != len(catalog["games"]):
        raise ValueError("Malformed Steam public catalog")
    source_id = decimal_id(discovery.get("steam_source_id"))
    accepted = {}
    for gid, row in discovery["games"].items():
        if not isinstance(row, dict) or row.get("status") != "matched" or row.get("active") is not True:
            continue
        if decimal_id(gid) != decimal_id(row.get("twitch_game_id")) or row.get("method") != METHOD:
            continue
        entry = tracking["games"].get(str(gid))
        if not isinstance(entry, dict) or decimal_id(entry.get("game_id")) != decimal_id(gid):
            continue
        source = (entry.get("tracking_sources") or {}).get("twitch_new")
        if not isinstance(source, dict) or source.get("source") != "twitch_new" or source.get("status") != "active":
            continue
        expiry = aware_time(source.get("expires_at"))
        if expiry is not None and expiry <= now:
            continue
        enrollment = source.get("enrollment")
        if not valid_enrollment(enrollment) or row.get("twitch_enrollment") != enrollment:
            continue
        igdb = decimal_id(row.get("igdb_id"))
        recorded_igdb = decimal_id(entry.get("igdb_id") or (entry.get("last_observation") or {}).get("igdb_id"))
        if igdb is None or igdb != recorded_igdb:
            continue
        checked_at = aware_time(row.get("checked_at"))
        if checked_at is None or checked_at > now:
            continue
        ids = row.get("steam_appids")
        links = row.get("links")
        if source_id is None or not isinstance(ids, list) or not isinstance(links, list):
            continue
        declared = {decimal_id(aid) for aid in ids}
        if None in declared or not declared:
            continue
        linked = set()
        valid = True
        for link in links:
            if not isinstance(link, dict):
                valid = False
                break
            aid = decimal_id(link.get("steam_appid"))
            if (aid not in declared or decimal_id(link.get("uid")) != aid
                    or decimal_id(link.get("external_game_id")) is None
                    or decimal_id(link.get("external_game_source")) != source_id
                    or decimal_id(link.get("game")) != igdb):
                valid = False
                break
            linked.add(aid)
        if not valid or linked != declared:
            continue
        for aid in sorted(linked, key=int):
            proof = normalize_twitch_admission({
                "schema_version": 1, "method": METHOD, "appid": int(aid),
                "twitch_game_id": str(gid), "igdb_id": igdb,
                "checked_at": row["checked_at"], "source_frontend_commit": commit,
                "source_enrollment": enrollment,
            }, aid)
            if proof is not None and validate_twitch_snapshot(proof, tracking, discovery, now):
                # Multiple Twitch categories cannot manufacture duplicate Steam rows.
                existing = accepted.get(int(aid))
                if existing is None or aware_time(proof["checked_at"]) > aware_time(existing["checked_at"]):
                    accepted[int(aid)] = proof
    return sorted(accepted.items())


def build_candidate(appid: int, proof: dict, item: dict, details: dict,
                    followers: int, checked_at: str, now: datetime,
                    blocked: set[int]) -> tuple[dict | None, str]:
    """Use official Steam identity, exact Taiwan dates, and the full adult rule."""
    if (decimal_id(item.get("appid")) != str(appid) or item.get("success") not in (1, True)
            or item.get("visible") is False):
        return None, "steam_store_unavailable"
    if not isinstance(details, dict) or not details:
        return None, "steam_type_unavailable"
    if details.get("steam_appid") != appid:
        return None, "steam_identity_mismatch"
    if details.get("type") != "game":
        return None, "not_a_steam_game"
    descriptor_data = details.get("content_descriptors")
    if not isinstance(descriptor_data, dict) or not isinstance(descriptor_data.get("ids"), list):
        return None, "steam_content_descriptors_unavailable"
    descriptors = descriptor_data["ids"]
    audited = {"appid": appid, "content_descriptorids": descriptors}
    screening = dict(item)
    if not screening.get("tags") and isinstance(item.get("tagids"), list):
        screening["tags"] = [{"tagid": tagid} for tagid in item["tagids"]]
    if is_disallowed(audited, blocked) or is_explicit_sex_game(screening):
        return None, "adult_content"
    store_item = item
    release = item.get("release")
    visible_release = details.get("release_date")
    # Store Browse omits protobuf's default false after a game has released.
    # Never infer it from absence alone: corroborate with the same AppID's
    # official TW appdetails and its exact visible Taiwan date below.
    # Future titles still require Store Browse's date_full.
    if (isinstance(release, dict) and "is_coming_soon" not in release
            and item.get("is_coming_soon") is not True
            and isinstance(visible_release, dict) and visible_release.get("coming_soon") is False):
        store_item = deepcopy(item)
        store_item["release"]["is_coming_soon"] = False
    detail = parse_store_release_detail(store_item, today=now.astimezone(TAIPEI).date())
    if not isinstance(details.get("release_date"), dict):
        return None, "uncertain_taiwan_store_date"
    visible_raw = str(details["release_date"].get("date") or "").strip()
    chinese_date = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", visible_raw)
    if chinese_date:
        visible_raw = "-".join((chinese_date[1], chinese_date[2].zfill(2), chinese_date[3].zfill(2)))
    visible = parse_release_window(visible_raw)
    if visible.precision != "day" or visible.start is None or visible.end != visible.start:
        return None, "uncertain_taiwan_store_date"
    if detail.get("exact") is not True:
        # A released TW listing with an exact day can disagree with Browse's
        # future instant. Keep the genuine instant; never invent date_full in
        # the Browse response or replace the timestamp with the display day.
        raw_stamp = release.get("steam_release_date") if isinstance(release, dict) else None
        if (not isinstance(visible_release, dict) or visible_release.get("coming_soon") is not False
                or visible.start > now.astimezone(TAIPEI).date()
                or isinstance(raw_stamp, bool) or not isinstance(raw_stamp, (int, float, str))):
            return None, "uncertain_steam_date"
        try:
            instant = datetime.fromtimestamp(int(raw_stamp), timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            return None, "uncertain_steam_date"
        detail = {"release_time_utc": stamp(instant),
                  "release_display_provider": "Steam IStoreBrowseService/GetItems",
                  "release_date_basis": "steam_store_browse_release_timestamp"}
    normalized = resolve_store_release_day(visible.start.isoformat(), detail.get("release_time_utc"),
                                          allow_taiwan_store_authority=True)
    if normalized is None:
        return None, "steam_date_conflict"
    day = normalized["release_start"]
    timestamp_day = aware_time(detail["release_time_utc"]).astimezone(TAIPEI).date().isoformat()
    store_authority = normalized["release_date_normalization"] == TW_STORE_DATE_AUTHORITY
    today = now.astimezone(TAIPEI).date()
    taipei_day = datetime.fromisoformat(day).date()
    if not today - timedelta(days=30) <= taipei_day <= today + timedelta(days=365):
        return None, "outside_new_game_window"
    if type(followers) is not int or followers < 0 or aware_time(checked_at) is None:
        return None, "official_followers_unavailable"
    candidate = {
        "appid": appid, "name": item.get("name") or details.get("name") or f"Steam App {appid}",
        "name_en": item.get("name") or details.get("name") or f"Steam App {appid}",
        "release_raw": day, "release_start": day, "release_end": day,
        "release_precision": "day", "release_display_precision": "date_full",
        "release_display_provider": TW_STORE_DATE_PROVIDER if store_authority else detail["release_display_provider"],
        "release_date_basis": detail["release_date_basis"],
        "release_date_timezone": "Asia/Taipei", "release_time_utc": detail["release_time_utc"],
        "release_time_source": detail["release_display_provider"],
        "release_timestamp_taipei_date": timestamp_day, "release_date_conflict": day != timestamp_day,
        "release_store_date": normalized["release_store_date"],
        "release_date_normalization": normalized["release_date_normalization"],
        "release_date_verified_at": stamp(now), "post_followers_store_verified": True,
        "post_followers_store_verified_at": stamp(now),
        "followers": followers, "follower_checked_at": checked_at,
        "follower_source": "Steam Community XML memberCount", "official_ge5000": followers >= 5000,
        "sexual_content_screened": True, "steam_type": "game",
        "content_descriptorids": sorted(set(descriptors) | set(item.get("content_descriptorids") or [])),
        "store_url": f"https://store.steampowered.com/app/{appid}/",
        "community_url": f"https://steamcommunity.com/app/{appid}/",
        "twitch_admission": proof,
    }
    return (candidate, "accepted") if is_twitch_qualified(candidate) else (None, "invalid_admission")


def request(session, url: str, *, params: dict | None = None, timeout: float = 25,
            now: datetime | None = None, clock=None, prior_cooldown: dict | None = None):
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname == "steamcommunity.com" or hostname.endswith(".steamcommunity.com"):
        raise RuntimeError("Community requests belong to the official Followers queue")
    response = session.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        stage = "steam_store_browse" if url == STORE_BROWSE else "steam_appdetails"
        observed = clock() if clock is not None else (now or datetime.now(timezone.utc))
        policy = rate_limit_policy(stage, response.headers.get("Retry-After"), observed, prior_cooldown)
        raise RateLimited(stage, policy["retry_seconds"], policy=policy)
    response.raise_for_status()
    return response


def cached_follower(appid: int, documents: list[dict], now: datetime | None = None) -> tuple[int, str] | None:
    now = now or datetime.now(timezone.utc)
    choices = []
    for doc in documents:
        rows = doc.get("games") or doc.get("official_results") or doc.get("verified") or {}
        if not isinstance(rows, dict):
            continue
        row = rows.get(str(appid))
        if not isinstance(row, dict):
            continue
        count = row.get("followers", row.get("official_followers"))
        checked = row.get("checked_at") or row.get("official_checked_at_taipei")
        clock = aware_time(checked)
        if type(count) is int and count >= 0 and clock is not None and clock <= now:
            choices.append((clock, count, checked))
    if not choices:
        return None
    _, count, checked = max(choices, key=lambda x: x[0])
    return count, checked


def signature(row: dict) -> str:
    value = [row["appid"], row["followers"], row["release_start"], row["twitch_admission"]]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity_signature(proof: dict) -> str:
    # Hourly frontend commits do not reset a Steam retry/cooldown deadline.
    identity = {key: value for key, value in proof.items() if key != "source_frontend_commit"}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def follower_candidate(probe: dict) -> dict:
    """Store the metadata evidence without turning the probe's zero into a count."""
    metadata = deepcopy(probe)
    for key in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
        metadata.pop(key, None)
    return {
        "appid": metadata["appid"], "name": metadata["name"],
        "release_date": metadata["release_start"], "steam_url": metadata["store_url"],
        "group_id64": None, "queue_source": "twitch_steam_discovery",
        "twitch_admission": deepcopy(metadata["twitch_admission"]),
        "steam_candidate": metadata,
    }


def retained_follower_candidate(appid: int, proof: dict, prior: dict, now: datetime,
                                blocked: set[int]) -> dict | None:
    """Keep a verified pending row through temporary metadata failures only."""
    candidate = prior.get("follower_candidate")
    if (prior.get("status") == "excluded" or prior.get("reason") in {
            "adult_content", "not_a_steam_game", "outside_new_game_window",
            "uncertain_steam_date", "uncertain_taiwan_store_date", "steam_date_conflict",
            "steam_identity_mismatch", "invalid_admission",
        } or not isinstance(candidate, dict)
            or candidate.get("queue_source") != "twitch_steam_discovery"
            or decimal_id(candidate.get("appid")) != str(appid)
            or not isinstance(candidate.get("steam_candidate"), dict)):
        return None
    metadata = deepcopy(candidate["steam_candidate"])
    stored_proof = normalize_twitch_admission(candidate.get("twitch_admission"), appid)
    metadata_proof = normalize_twitch_admission(metadata.get("twitch_admission"), appid)
    if (stored_proof is None or metadata_proof is None
            or identity_signature(stored_proof) != identity_signature(proof)
            or identity_signature(metadata_proof) != identity_signature(proof)
            or decimal_id(metadata.get("appid")) != str(appid)
            or candidate.get("release_date") != metadata.get("release_start")
            or candidate.get("steam_url") != f"https://store.steampowered.com/app/{appid}/"
            or metadata.get("store_url") != candidate["steam_url"]
            or is_disallowed(metadata, blocked)):
        return None
    verified_at = aware_time(metadata.get("release_date_verified_at"))
    release_at = aware_time(metadata.get("release_time_utc"))
    if verified_at is None or verified_at > now or release_at is None:
        return None
    try:
        day = datetime.fromisoformat(metadata["release_start"]).date()
    except (KeyError, TypeError, ValueError):
        return None
    today = now.astimezone(TAIPEI).date()
    if not today - timedelta(days=30) <= day <= today + timedelta(days=365):
        return None
    # Reuse the exact metadata gate, supplying a placeholder only in memory.
    metadata.update(twitch_admission=proof, followers=0, follower_checked_at=stamp(verified_at))
    if not is_twitch_qualified(metadata):
        return None
    return follower_candidate(metadata)


def collect(frontend: Path, commit: str, master: dict, previous: dict, *,
            session=None, now: datetime | None = None, max_seconds: int = 900,
            caches: list[dict] | None = None, monotonic=time.monotonic,
            sleep=time.sleep, blocked: set[int] | None = None, clock=None) -> dict:
    fixed_now = now
    clock = clock or (lambda: fixed_now if fixed_now is not None else datetime.now(timezone.utc))
    now = now or clock()
    session = session or requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadarTwitchSteamImport/1.0"})
    discovery = read_json(frontend / "data/twitch_steam_discovery.json")
    tracking = read_json(frontend / "data/twitch_tracking.json")
    catalog = read_json(frontend / "data/steam_upcoming.json")
    candidates = validate_snapshot(discovery, tracking, catalog, commit, now)
    by_id = {int(row["appid"]): row for row in master["games"]}
    blocked = excluded_appids() if blocked is None else blocked
    batch = {"schema_version": 1, "generated_at": stamp(now), "source_frontend_commit": commit,
             "records": [], "state_updates": {}, "cooldown_updates": {}, "stop_reason": "complete",
             "active_twitch_appids": [appid for appid, _ in candidates]}
    queued = {}
    # Seed all active rows before bounded processing. A deadline or global
    # metadata cooldown must not drop candidates that were already verified.
    for appid, proof in candidates:
        retained = retained_follower_candidate(
            appid, proof, (previous.get("games") or {}).get(str(appid), {}), now, blocked,
        )
        if retained is not None:
            queued[appid] = retained
    cooldowns = deepcopy(previous.get("api_cooldowns") or {})
    deadline = monotonic() + max_seconds
    last_metadata_start = float("-inf")
    def get(url: str, *, params: dict):
        nonlocal last_metadata_start
        delay = max(0, 1.5 - (monotonic() - last_metadata_start))
        if delay:
            sleep(min(delay, max(0, deadline - monotonic())))
        last_metadata_start = monotonic()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError("deadline")
        stage = "steam_store_browse" if url == STORE_BROWSE else "steam_appdetails"
        response = request(session, url, params=params, timeout=min(25, remaining),
                           clock=clock, prior_cooldown=cooldowns.get(stage))
        prior_cooldown = cooldowns.get(stage) or {}
        if prior_cooldown.get("attempts"):
            recovered_at = stamp(clock())
            cooldowns[stage] = {**prior_cooldown, "attempts": 0,
                                "updated_at": recovered_at, "last_success_at": recovered_at}
            batch["cooldown_updates"][stage] = deepcopy(cooldowns[stage])
        return response
    for appid, proof in candidates:
        if monotonic() >= deadline:
            batch["stop_reason"] = "deadline"
            break
        prior = (previous.get("games") or {}).get(str(appid), {})
        record = by_id.get(appid)
        # Persistent membership outlives Twitch expiry. No Steam fetch is needed
        # on every subsequent import; the normal content reconciler owns refresh.
        if isinstance(record, dict) and is_twitch_qualified(record):
            queued.pop(appid, None)
            content_signature = signature(record)
            if (prior.get("status") == "accepted" and prior.get("retry_at") is None
                    and not prior.get("retry_attempts") and not prior.get("rate_limit_stage")
                    and not prior.get("rate_limit_attempts") and not prior.get("retry_source")
                    and prior.get("validation_version") == STATE_VALIDATION_VERSION
                    and prior.get("content_signature") == content_signature
                    and prior.get("twitch_admission") == record["twitch_admission"]
                    and not prior.get("follower_candidate")):
                continue
            batch["state_updates"][str(appid)] = {
                "status": "accepted", "updated_at": stamp(now),
                "validation_version": STATE_VALIDATION_VERSION,
                "twitch_admission": record["twitch_admission"],
                "content_signature": content_signature, "reason": "steam_verified",
                "retry_at": None, "retry_attempts": 0, "rate_limit_attempts": 0,
                "rate_limit_stage": None, "retry_source": None, "retry_after": None,
                "follower_candidate": None,
            }
            continue
        count = cached_follower(appid, caches or [], now)
        if count is None and isinstance(record, dict):
            count = cached_follower(appid, [{"games": {str(appid): {
                "followers": record.get("followers"), "checked_at": record.get("follower_checked_at"),
            }}}], now)
        retry_at = aware_time(prior.get("retry_at"))
        prior_proof = prior.get("twitch_admission")
        parser_migration = (prior.get("validation_version") != STATE_VALIDATION_VERSION
                             and prior.get("reason") in {"uncertain_steam_date", "uncertain_taiwan_store_date", "steam_date_conflict"})
        if (not parser_migration
                and retry_at is not None and retry_at > clock() and isinstance(prior_proof, dict)
                and identity_signature(prior_proof) == identity_signature(proof)):
            continue
        state = {"status": "pending", "updated_at": stamp(now), "twitch_admission": proof,
                 "validation_version": STATE_VALIDATION_VERSION,
                 "follower_candidate": deepcopy(queued.get(appid))}
        batch["state_updates"][str(appid)] = state
        metadata_blocked = next((stage for stage in ("steam_store_browse", "steam_appdetails")
            if aware_time((cooldowns.get(stage) or {}).get("retry_at")) is not None
            and aware_time(cooldowns[stage]["retry_at"]) > clock()), None)
        if metadata_blocked is not None:
            cooldown = cooldowns[metadata_blocked]
            state.update(reason="metadata_cooldown", rate_limit_stage=metadata_blocked,
                         retry_at=cooldown["retry_at"], retry_source=cooldown.get("retry_source"),
                         retry_after=cooldown.get("retry_after"), retry_attempts=0,
                         rate_limit_attempts=cooldown.get("attempts", 0))
            batch["stop_reason"] = "steam_metadata_cooldown"
            break
        try:
            payload = {"ids": [{"appid": appid}], "context": {
                "country_code": "TW", "language": "english", "steam_realm": 1,
            }, "data_request": {"include_release": True, "include_basic_info": True,
                                "include_tag_count": 20}}
            data = get(STORE_BROWSE, params={"input_json": json.dumps(payload)}).json()
            items = (data.get("response") or {}).get("store_items") or []
            item = next((x for x in items if isinstance(x, dict) and x.get("appid") == appid), {})
            data = get(APPDETAILS, params={"appids": appid, "cc": "TW", "l": "tchinese"}).json()
            envelope = data.get(str(appid)) or {}
            details = envelope.get("data") if envelope.get("success") is True else {}
            # Verify all metadata gates before adding work to the shared queue.
            probe, reason = build_candidate(appid, proof, item, details, 0, stamp(now), now, blocked)
            if probe is None:
                excluded = reason in {"adult_content", "not_a_steam_game", "outside_new_game_window"}
                retry = (transient_retry_policy(prior, clock()) if reason in {
                    "steam_store_unavailable", "steam_type_unavailable", "steam_content_descriptors_unavailable",
                } else {"retry_at": stamp(clock() + timedelta(hours=24))})
                state.update(reason=reason, **retry, status="excluded" if excluded else "pending",
                             rate_limit_stage=None, rate_limit_attempts=0, retry_after=None)
                if reason not in {"steam_store_unavailable", "steam_type_unavailable",
                                  "steam_content_descriptors_unavailable"}:
                    queued.pop(appid, None)
                    state["follower_candidate"] = None
                continue
            if count is None:
                queued[appid] = follower_candidate(probe)
                state.update(reason="queued_official_followers", rate_limit_stage=None,
                             retry_at=None, retry_source=None, retry_after=None,
                             retry_attempts=0, rate_limit_attempts=0,
                             follower_candidate=deepcopy(queued[appid]))
                continue
            row, reason = build_candidate(appid, proof, item, details, *count, now, blocked)
            if row is None:
                state.update(reason=reason, **transient_retry_policy(prior, clock()),
                             rate_limit_stage=None, rate_limit_attempts=0, retry_after=None)
                continue
            batch["records"].append(row)
            queued.pop(appid, None)
            state.update(status="accepted", reason="steam_verified", content_signature=signature(row),
                         followers=row["followers"], follower_checked_at=row["follower_checked_at"],
                         retry_at=None, retry_attempts=0, rate_limit_attempts=0,
                         rate_limit_stage=None, retry_source=None, retry_after=None)
            state["follower_candidate"] = None
        except (requests.RequestException, ValueError, TypeError, RuntimeError) as exc:
            state.update(reason=type(exc).__name__, **transient_retry_policy(prior, clock()),
                         rate_limit_stage=None, rate_limit_attempts=0, retry_after=None)
            if isinstance(exc, RateLimited):
                policy = exc.policy or rate_limit_policy(exc.stage, str(exc.retry_seconds), clock())
                state.update(rate_limit_stage=exc.stage, retry_at=policy["retry_at"],
                             retry_attempts=0, rate_limit_attempts=policy["attempts"],
                             retry_source=policy["retry_source"], retry_after=policy["retry_after"],
                             updated_at=policy["observed_at"])
                cooldown = {**policy, "updated_at": policy["observed_at"]}
                cooldowns[exc.stage] = cooldown
                batch["cooldown_updates"][exc.stage] = cooldown
                batch["stop_reason"] = "steam_rate_limited"
                break
    batch["follower_candidates"] = [queued[appid] for appid in sorted(queued)]
    return batch


def apply_batch(master: dict, state: dict, batch: dict) -> tuple[dict, dict]:
    """Merge only verified records and newer per-AppID updates against latest main."""
    if batch.get("schema_version") != 1 or not isinstance(batch.get("records"), list) or not isinstance(batch.get("state_updates"), dict):
        raise ValueError("Malformed import batch")
    if not isinstance(master.get("games"), list) or not isinstance(state.get("games", {}), dict):
        raise ValueError("Malformed master/import state")
    result = deepcopy(master)
    merged_state = deepcopy(state)
    by_id = {int(row["appid"]): deepcopy(row) for row in result["games"]}
    blocked = excluded_appids()
    for row in batch["records"]:
        if not is_twitch_qualified(row) or is_disallowed(row, blocked):
            raise ValueError("Batch contains unqualified Steam admission")
        appid = int(row["appid"])
        current = by_id.get(appid, {})
        if current and (is_disallowed(current, blocked) or current.get("sexual_content_screened") is False):
            continue
        candidate = preserve_twitch_admission(current, keep_newer_release(current, row))
        current_follower_time = aware_time(current.get("follower_checked_at"))
        if current_follower_time is not None and current_follower_time > aware_time(candidate["follower_checked_at"]):
            for key in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
                if key in current:
                    candidate[key] = current[key]
        merged = {**current, **candidate}
        if not is_twitch_qualified(merged) or is_disallowed(merged, blocked):
            # A concurrent authoritative date/adult update wins; do not overwrite it.
            continue
        by_id[appid] = merged
    if batch["records"]:
        result["games"] = sorted(by_id.values(), key=lambda row: (row.get("release_start") or "", int(row["appid"])))
    if result["games"] != master["games"]:
        result["updated_at"] = batch["generated_at"]
    merged_state.setdefault("schema_version", 1)
    updates = merged_state.setdefault("games", {})
    for aid, update in batch["state_updates"].items():
        if decimal_id(aid) is None or not isinstance(update, dict) or aware_time(update.get("updated_at")) is None:
            raise ValueError("Malformed per-AppID import state update")
        current = updates.get(aid, {})
        previous_time = aware_time(current.get("updated_at"))
        if previous_time is not None and previous_time > aware_time(update["updated_at"]):
            continue
        merged = {**current, **deepcopy(update)}
        if "content_dispatch" not in update and current.get("content_signature") != update.get("content_signature"):
            merged.pop("content_dispatch", None)
        updates[aid] = merged
    for stage, cooldown in batch.get("cooldown_updates", {}).items():
        if stage not in {"steam_store_browse", "steam_appdetails"}:
            raise ValueError("Malformed Steam cooldown stage")
        until, updated = aware_time(cooldown.get("retry_at")), aware_time(cooldown.get("updated_at"))
        if until is None or updated is None:
            raise ValueError("Malformed Steam cooldown deadline")
        current = merged_state.setdefault("api_cooldowns", {}).get(stage, {})
        previous_updated = aware_time(current.get("updated_at"))
        if previous_updated is None or updated >= previous_updated:
            # A concurrent longer cooldown must not be shortened by an older
            # batch, even when both were measured within the same timestamp.
            previous_until = aware_time(current.get("retry_at"))
            if previous_until is not None and previous_until > until:
                cooldown = {**cooldown, "retry_at": current["retry_at"]}
            merged_state["api_cooldowns"][stage] = deepcopy(cooldown)
    if merged_state != state:
        merged_state["updated_at"] = batch["generated_at"]
    return result, merged_state


def dispatch(master: dict, state: dict, *, token: str, target: str,
             session=None, now: datetime | None = None, max_seconds: int = 600,
             monotonic=time.monotonic) -> dict:
    session = session or requests.Session()
    now = now or datetime.now(timezone.utc)
    batch = {"schema_version": 1, "generated_at": stamp(now), "records": [],
             "state_updates": {}, "stop_reason": "complete"}
    deadline = monotonic() + max_seconds
    by_id = {str(row["appid"]): row for row in master["games"]}
    for aid, pending in (state.get("games") or {}).items():
        if monotonic() >= deadline:
            batch["stop_reason"] = "deadline"
            break
        row = by_id.get(aid)
        if pending.get("status") != "accepted" or not is_twitch_qualified(row):
            continue
        sig = signature(row)
        previous = pending.get("content_dispatch") or {}
        if previous.get("status") == "dispatched" and previous.get("signature") == sig:
            continue
        receipt = {"status": "pending", "signature": sig, "attempted_at": stamp(now),
                   "target_repository": target, "event_type": "steam_game_twitch_discovered"}
        update = {"status": "accepted", "updated_at": stamp(now),
                  "content_signature": sig, "content_dispatch": receipt}
        batch["state_updates"][aid] = update
        if not token or not target:
            receipt["reason"] = "content_dispatch_not_configured"
            continue
        payload = {"event_type": "steam_game_twitch_discovered", "client_payload": {
            "appid": int(aid), "official_followers": row["followers"],
            "release_date": row["release_start"], "official_checked_at_taipei": row["follower_checked_at"],
            "twitch_admission": row["twitch_admission"], "signature": sig,
            "source_repository": os.environ.get("GITHUB_REPOSITORY", "danielet087/game-trend-radar-backend"),
        }}
        try:
            response = session.post(f"https://api.github.com/repos/{target}/dispatches", json=payload,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"}, timeout=25)
            receipt["http"] = response.status_code
            receipt["status"] = "dispatched" if response.status_code == 204 else "pending"
        except requests.RequestException as exc:
            receipt["reason"] = type(exc).__name__
    return batch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["collect", "apply", "dispatch"])
    parser.add_argument("--frontend-path", type=Path)
    parser.add_argument("--frontend-commit")
    parser.add_argument("--master", type=Path, default=Path("data/steam_upcoming_master.json"))
    parser.add_argument("--state", type=Path, default=Path("data/twitch_steam_import_state.json"))
    parser.add_argument("--batch", type=Path, default=Path("output/twitch_steam_import_batch.json"))
    parser.add_argument("--max-seconds", type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= 900:
        parser.error("max-seconds must be 1..900")
    master = read_json(args.master)
    if not isinstance(master.get("games"), list):
        parser.error("master games must be an array")
    state = read_json(args.state, optional=True)
    if args.phase == "collect":
        if args.frontend_path is None or args.frontend_commit is None:
            parser.error("collect requires frontend-path and immutable frontend-commit")
        actual_commit = subprocess.check_output(
            ["git", "-C", str(args.frontend_path), "rev-parse", "HEAD"], text=True,
        ).strip()
        if actual_commit != args.frontend_commit:
            parser.error("frontend-commit differs from the checked-out immutable snapshot")
        cache_paths = [Path("data/steam_followers_cache.json"), Path("data/steam_followers_checkpoint.json"),
                      Path("experiments/steam_official_daily_catchup/checkpoint.json"),
                      Path("experiments/steam_official_followers_20260922/checkpoint.json"),
                      Path("experiments/steam_official_nearfirst_20260922/checkpoint.json")]
        caches = [read_json(path) for path in cache_paths if path.is_file()]
        batch = collect(args.frontend_path, args.frontend_commit, master, state,
                        max_seconds=args.max_seconds, caches=caches)
        write_json(args.batch, batch)
    elif args.phase == "apply":
        batch = read_json(args.batch)
        master, state = apply_batch(master, state, batch)
        write_json(args.master, master)
        write_json(args.state, state)
    else:
        batch = dispatch(master, state, token=os.environ.get("CONTENT_BACKEND_TOKEN", ""),
                         target=os.environ.get("CONTENT_BACKEND_REPOSITORY", "danielet087/game-trend-radar-content-backend"),
                         max_seconds=args.max_seconds)
        write_json(args.batch, batch)
    print("TWITCH_STEAM_IMPORT", args.phase, "records", len(batch["records"]),
          "states", len(batch["state_updates"]), "stop", batch.get("stop_reason", "complete"), flush=True)


if __name__ == "__main__":
    main()
