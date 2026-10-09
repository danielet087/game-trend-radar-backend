"""Pure rules for authoritative Twitch-to-Steam intake evidence.

Observation times and rule collaborators are supplied by the caller. These
functions retain the existing admission, Taiwan date, and pending queue gates.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re


def stamp(now: datetime, *, timezone_type=timezone) -> str:
    return now.astimezone(timezone_type.utc).isoformat().replace("+00:00", "Z")


def validate_snapshot(
    discovery: dict, tracking: dict, catalog: dict, commit: str, now: datetime, *,
    decimal_id, aware_time, valid_enrollment, normalize_twitch_admission,
    validate_twitch_snapshot, method, regex_module=re,
) -> list[tuple[int, dict]]:
    """Cross-check links against the authoritative registry in one git commit."""
    if not isinstance(commit, str) or regex_module.fullmatch(r"[0-9a-f]{40}", commit) is None:
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
        if decimal_id(gid) != decimal_id(row.get("twitch_game_id")) or row.get("method") != method:
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
                "schema_version": 1, "method": method, "appid": int(aid),
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


def build_candidate(
    appid: int, proof: dict, item: dict, details: dict,
    followers: int, checked_at: str, now: datetime, blocked: set[int], *,
    decimal_id, is_disallowed, is_explicit_sex_game, parse_store_release_detail,
    parse_release_window, resolve_store_release_day, aware_time,
    is_twitch_qualified, stamp, taipei, tw_store_date_authority,
    tw_store_date_provider, datetime_type=datetime, timedelta_type=timedelta,
    timezone_type=timezone, regex_module=re, deepcopy=deepcopy,
) -> tuple[dict | None, str]:
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
    detail = parse_store_release_detail(store_item, today=now.astimezone(taipei).date())
    if not isinstance(details.get("release_date"), dict):
        return None, "uncertain_taiwan_store_date"
    visible_raw = str(details["release_date"].get("date") or "").strip()
    chinese_date = regex_module.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", visible_raw)
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
                or visible.start > now.astimezone(taipei).date()
                or isinstance(raw_stamp, bool) or not isinstance(raw_stamp, (int, float, str))):
            return None, "uncertain_steam_date"
        try:
            instant = datetime_type.fromtimestamp(int(raw_stamp), timezone_type.utc)
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
    timestamp_day = aware_time(detail["release_time_utc"]).astimezone(taipei).date().isoformat()
    store_authority = normalized["release_date_normalization"] == tw_store_date_authority
    today = now.astimezone(taipei).date()
    taipei_day = datetime_type.fromisoformat(day).date()
    if not today - timedelta_type(days=30) <= taipei_day <= today + timedelta_type(days=365):
        return None, "outside_new_game_window"
    if type(followers) is not int or followers < 0 or aware_time(checked_at) is None:
        return None, "official_followers_unavailable"
    candidate = {
        "appid": appid, "name": item.get("name") or details.get("name") or f"Steam App {appid}",
        "name_en": item.get("name") or details.get("name") or f"Steam App {appid}",
        "release_raw": day, "release_start": day, "release_end": day,
        "release_precision": "day", "release_display_precision": "date_full",
        "release_display_provider": tw_store_date_provider if store_authority else detail["release_display_provider"],
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


def cached_follower(
    appid: int, documents: list[dict], now: datetime, *, aware_time,
) -> tuple[int, str] | None:
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


def signature(row: dict, *, json_module=json, hashlib_module=hashlib) -> str:
    value = [row["appid"], row["followers"], row["release_start"], row["twitch_admission"]]
    return hashlib_module.sha256(json_module.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity_signature(proof: dict, *, json_module=json, hashlib_module=hashlib) -> str:
    identity = {key: value for key, value in proof.items() if key != "source_frontend_commit"}
    return hashlib_module.sha256(json_module.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def follower_candidate(probe: dict, *, deepcopy=deepcopy) -> dict:
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


def retained_follower_candidate(
    appid: int, proof: dict, prior: dict, now: datetime, blocked: set[int], *,
    decimal_id, normalize_twitch_admission, identity_signature, is_disallowed,
    aware_time, stamp, is_twitch_qualified, follower_candidate, taipei,
    datetime_type=datetime, timedelta_type=timedelta, deepcopy=deepcopy,
) -> dict | None:
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
        day = datetime_type.fromisoformat(metadata["release_start"]).date()
    except (KeyError, TypeError, ValueError):
        return None
    today = now.astimezone(taipei).date()
    if not today - timedelta_type(days=30) <= day <= today + timedelta_type(days=365):
        return None
    # Reuse the exact metadata gate, supplying a placeholder only in memory.
    metadata.update(twitch_admission=proof, followers=0, follower_checked_at=stamp(verified_at))
    if not is_twitch_qualified(metadata):
        return None
    return follower_candidate(metadata)
