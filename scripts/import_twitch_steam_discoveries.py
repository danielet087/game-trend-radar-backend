"""Import confirmed Twitch discoveries without changing Steam's normal queue.

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
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

from collectors.steam_upcoming import parse_follower_xml, parse_release_window
from scripts.public_catalog import keep_newer_release
from scripts.screen_steam_candidates_before_followers import is_explicit_sex_game
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.steam_master_date_gate import parse_store_release_detail
from scripts.twitch_steam_admission import (
    METHOD, aware_time, decimal_id, is_twitch_qualified,
    normalize_twitch_admission, preserve_twitch_admission, valid_enrollment, validate_twitch_snapshot,
    resolve_store_release_day,
)

TAIPEI = ZoneInfo("Asia/Taipei")
STORE_BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
FOLLOWERS = "https://steamcommunity.com/games/{appid}/memberslistxml/"
STATE_VALIDATION_VERSION = 3


class RateLimited(RuntimeError):
    def __init__(self, stage: str, retry_seconds: int):
        super().__init__("Steam HTTP 429")
        self.stage = stage
        self.retry_seconds = retry_seconds


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
    # official TW appdetails, then still require a past timestamp and matching
    # exact visible Taiwan date below. Future titles still require date_full.
    if (isinstance(release, dict) and "is_coming_soon" not in release
            and item.get("is_coming_soon") is not True
            and isinstance(visible_release, dict) and visible_release.get("coming_soon") is False):
        store_item = deepcopy(item)
        store_item["release"]["is_coming_soon"] = False
    detail = parse_store_release_detail(store_item, today=now.astimezone(TAIPEI).date())
    if detail.get("exact") is not True:
        return None, "uncertain_steam_date"
    if (isinstance(visible_release, dict) and visible_release.get("coming_soon") is False
            and aware_time(detail.get("release_time_utc")) > now):
        return None, "uncertain_steam_date"
    if not isinstance(details.get("release_date"), dict):
        return None, "uncertain_taiwan_store_date"
    visible_raw = str(details["release_date"].get("date") or "").strip()
    chinese_date = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", visible_raw)
    if chinese_date:
        visible_raw = "-".join((chinese_date[1], chinese_date[2].zfill(2), chinese_date[3].zfill(2)))
    visible = parse_release_window(visible_raw)
    if visible.precision != "day" or visible.start is None or visible.end != visible.start:
        return None, "uncertain_taiwan_store_date"
    normalized = resolve_store_release_day(visible.start.isoformat(), detail.get("release_time_utc"))
    if normalized is None:
        return None, "steam_date_conflict"
    day = normalized["release_start"]
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
        "release_display_provider": detail["release_display_provider"],
        "release_date_basis": detail["release_date_basis"],
        "release_date_timezone": "Asia/Taipei", "release_time_utc": detail["release_time_utc"],
        "release_time_source": detail["release_display_provider"],
        "release_timestamp_taipei_date": day, "release_date_conflict": False,
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


def request(session, url: str, *, params: dict | None = None, timeout: float = 25):
    response = session.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        stage = "steam_community" if url.startswith("https://steamcommunity.com/") else (
            "steam_store_browse" if url == STORE_BROWSE else "steam_appdetails"
        )
        default = 3600 if stage == "steam_community" else 900
        try:
            retry_seconds = int(response.headers.get("Retry-After"))
        except (ValueError, TypeError, AttributeError):
            retry_seconds = default
        if retry_seconds <= 0:
            retry_seconds = default
        # Do not shorten an explicit longer server cooldown.
        raise RateLimited(stage, max(60, retry_seconds))
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


def collect(frontend: Path, commit: str, master: dict, previous: dict, *,
            session=None, now: datetime | None = None, max_seconds: int = 900,
            caches: list[dict] | None = None, monotonic=time.monotonic,
            sleep=time.sleep, blocked: set[int] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    session = session or requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadarTwitchSteamImport/1.0"})
    discovery = read_json(frontend / "data/twitch_steam_discovery.json")
    tracking = read_json(frontend / "data/twitch_tracking.json")
    catalog = read_json(frontend / "data/steam_upcoming.json")
    candidates = validate_snapshot(discovery, tracking, catalog, commit, now)
    by_id = {int(row["appid"]): row for row in master["games"]}
    blocked = excluded_appids() if blocked is None else blocked
    batch = {"schema_version": 1, "generated_at": stamp(now), "source_frontend_commit": commit,
             "records": [], "state_updates": {}, "cooldown_updates": {}, "stop_reason": "complete"}
    cooldowns = deepcopy(previous.get("api_cooldowns") or {})
    # Legacy receipts did not record which endpoint returned 429. Preserve
    # their live retry deadline conservatively for XML while allowing normal
    # metadata checks and candidates backed by a real Followers cache.
    community_until = aware_time((cooldowns.get("steam_community") or {}).get("retry_at"))
    for prior in (previous.get("games") or {}).values():
        if not isinstance(prior, dict):
            continue
        retry = aware_time(prior.get("retry_at"))
        if (prior.get("reason") == "RateLimited" and prior.get("rate_limit_stage") in (None, "steam_community")
                and retry is not None and retry > now and (community_until is None or retry > community_until)):
            community_until = retry
    if community_until is not None and community_until > now:
        cooldowns["steam_community"] = {"retry_at": stamp(community_until), "updated_at": stamp(now)}
        batch["cooldown_updates"]["steam_community"] = cooldowns["steam_community"]
    deadline = monotonic() + max_seconds
    last_metadata_start = float("-inf")
    def get(url: str, *, params: dict):
        nonlocal last_metadata_start
        if not url.startswith("https://steamcommunity.com/"):
            delay = max(0, 1.5 - (monotonic() - last_metadata_start))
            if delay:
                sleep(min(delay, max(0, deadline - monotonic())))
            last_metadata_start = monotonic()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError("deadline")
        return request(session, url, params=params, timeout=min(25, remaining))
    last_follower_start = float("-inf")
    for appid, proof in candidates:
        if monotonic() >= deadline:
            batch["stop_reason"] = "deadline"
            break
        prior = (previous.get("games") or {}).get(str(appid), {})
        record = by_id.get(appid)
        # Persistent membership outlives Twitch expiry. No Steam fetch is needed
        # on every subsequent import; the normal content reconciler owns refresh.
        if isinstance(record, dict) and is_twitch_qualified(record):
            batch["state_updates"][str(appid)] = {
                "status": "accepted", "updated_at": stamp(now),
                "validation_version": STATE_VALIDATION_VERSION,
                "twitch_admission": record["twitch_admission"],
                "content_signature": signature(record),
            }
            continue
        retry_at = aware_time(prior.get("retry_at"))
        prior_proof = prior.get("twitch_admission")
        parser_migration = (prior.get("validation_version") != STATE_VALIDATION_VERSION
                             and prior.get("reason") in {"uncertain_steam_date", "uncertain_taiwan_store_date", "steam_date_conflict"})
        if (not parser_migration and retry_at is not None and retry_at > now and isinstance(prior_proof, dict)
                and identity_signature(prior_proof) == identity_signature(proof)):
            continue
        state = {"status": "pending", "updated_at": stamp(now), "twitch_admission": proof,
                 "validation_version": STATE_VALIDATION_VERSION}
        batch["state_updates"][str(appid)] = state
        metadata_blocked = next((stage for stage in ("steam_store_browse", "steam_appdetails")
            if aware_time((cooldowns.get(stage) or {}).get("retry_at")) is not None
            and aware_time(cooldowns[stage]["retry_at"]) > now), None)
        if metadata_blocked is not None:
            state.update(reason="metadata_cooldown", rate_limit_stage=metadata_blocked,
                         retry_at=cooldowns[metadata_blocked]["retry_at"])
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
            count = cached_follower(appid, caches or [], now)
            if count is None and isinstance(record, dict):
                count = cached_follower(appid, [{"games": {str(appid): {
                    "followers": record.get("followers"), "checked_at": record.get("follower_checked_at"),
                }}}], now)
            # Validate all non-Followers gates before spending a Community request.
            probe, reason = build_candidate(appid, proof, item, details, 0, stamp(now), now, blocked)
            if probe is None:
                state.update(reason=reason, retry_at=stamp(now + timedelta(hours=24)),
                             status="excluded" if reason in {"adult_content", "not_a_steam_game", "outside_new_game_window"} else "pending")
                continue
            if count is None:
                if community_until is not None and community_until > now:
                    state.update(reason="steam_community_cooldown", rate_limit_stage="steam_community",
                                 retry_at=stamp(community_until))
                    batch["stop_reason"] = "steam_community_cooldown"
                    continue
                delay = max(0, 8 - (monotonic() - last_follower_start))
                if monotonic() + delay + 25 >= deadline:
                    state.update(reason="deadline_before_followers")
                    batch["stop_reason"] = "deadline"
                    break
                if delay:
                    sleep(delay)
                last_follower_start = monotonic()
                response = get(FOLLOWERS.format(appid=appid), params={"xml": 1})
                count = parse_follower_xml(response.text), stamp(datetime.now(timezone.utc))
            row, reason = build_candidate(appid, proof, item, details, *count, now, blocked)
            if row is None:
                state.update(reason=reason, retry_at=stamp(now + timedelta(hours=6)))
                continue
            batch["records"].append(row)
            state.update(status="accepted", reason="steam_verified", content_signature=signature(row),
                         followers=row["followers"], follower_checked_at=row["follower_checked_at"], retry_at=None)
        except (requests.RequestException, ValueError, TypeError, RuntimeError, ET.ParseError) as exc:
            state.update(reason=type(exc).__name__, retry_at=stamp(now + timedelta(hours=6)))
            if isinstance(exc, RateLimited):
                until = now + timedelta(seconds=exc.retry_seconds)
                state.update(rate_limit_stage=exc.stage, retry_at=stamp(until))
                cooldown = {"retry_at": stamp(until), "updated_at": stamp(now)}
                cooldowns[exc.stage] = cooldown
                batch["cooldown_updates"][exc.stage] = cooldown
                if exc.stage == "steam_community":
                    community_until = until
                    batch["stop_reason"] = "steam_community_rate_limited"
                    continue
                batch["stop_reason"] = "steam_rate_limited"
                break
    return batch


def apply_batch(master: dict, state: dict, batch: dict) -> tuple[dict, dict]:
    """Merge only verified records and newer per-AppID updates against latest main."""
    if batch.get("schema_version") != 1 or not isinstance(batch.get("records"), list) or not isinstance(batch.get("state_updates"), dict):
        raise ValueError("Malformed import batch")
    if not isinstance(master.get("games"), list) or not isinstance(state.get("games", {}), dict):
        raise ValueError("Malformed master/import state")
    result, merged_state = deepcopy(master), deepcopy(state)
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
    merged_state["updated_at"] = batch["generated_at"]
    for stage, cooldown in batch.get("cooldown_updates", {}).items():
        if stage not in {"steam_store_browse", "steam_appdetails", "steam_community"}:
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
