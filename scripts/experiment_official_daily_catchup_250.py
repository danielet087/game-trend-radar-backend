"""Dynamic near-release official Followers catch-up, one GitHub-scheduled hourly batch.

Inputs: persisted September 22 missing-source cohort plus eligible candidates
from a genuinely fresh daily follower prefilter. A stale/disabled daily scan is
reported honestly; never represented as the current day's full coverage.

Uses Steam Community official XML memberCount, NOT Store appdetails,
wishlist count, Community online count or third-party estimates.
Does NOT edit the production follower cache, candidate progress or frontend.
"""
from __future__ import annotations

import argparse
import json
import os
from email.utils import parsedate_to_datetime
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from scripts.steam_master_date_gate import fetch_store_release_details
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed

ROOT = Path("experiments/steam_official_daily_catchup")
FROZEN = Path("experiments/steam_official_nearfirst_20260922")
ELIGIBLE = Path("data/steam_candidates_eligible.json")
PREFILTER = Path("data/steam_prefilter_state.json")
OFFICIAL_CACHE = Path("data/steam_followers_cache.json")
ORIGINAL_OFFICIAL = Path("experiments/steam_official_followers_20260922/checkpoint.json")
OUT = Path("output/steam_official_daily_catchup")
CHECKPOINT = ROOT / "checkpoint.json"
MASTER = Path("data/steam_upcoming_master.json")
TZ = ZoneInfo("Asia/Taipei")
GROUP_BASE = 103582791429521408
COHORT = "steam_official_daily_catchup_dynamic_v1"


def steam_429_cooldown(response, failure_count, now):
    """15m, 30m, 1h, 2h, 4h ... capped at 24h; honor longer Retry-After."""
    minutes = min(24 * 60, 15 * (2 ** min(max(failure_count - 1, 0), 7)))
    cooldown_until = now + timedelta(minutes=minutes)
    header = response.headers.get("Retry-After")
    if header:
        try:
            value = header.strip()
            if value.isdigit():
                server_until = now + timedelta(seconds=int(value))
            else:
                server_until = parsedate_to_datetime(value)
                if server_until.tzinfo is None:
                    server_until = server_until.replace(tzinfo=timezone.utc)
                server_until = server_until.astimezone(TZ)
            cooldown_until = max(cooldown_until, server_until)
        except (TypeError, ValueError, OverflowError):
            pass  # Invalid Retry-After: retain locally configured backoff.
    return cooldown_until


def clock():
    return datetime.now(TZ)


def utc_date_as_taipei(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TZ).date().isoformat()
    except (TypeError, ValueError):
        return None


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def checked_numeric(data):
    return isinstance(data, int) and not isinstance(data, bool) and data >= 0


def group_to_gid(short):
    if short is None:
        return None
    if isinstance(short, bool) or not isinstance(short, int) or short < 0:
        raise ValueError("Bad Steam short Group ID; no fabricated IDs")
    return str(GROUP_BASE + short)


def valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).date().isoformat() == value and len(value) == 10
    except ValueError:
        return False


def upsert_qualified_master(master, result):
    """Persist only official >=5000 + post-Followers Store-exact games."""
    if (
        not checked_numeric(result.get("official_followers"))
        or result["official_followers"] < 5000
        or result.get("store_date_exact") is not True
        or result.get("release_display_precision") != "date_full"
        or not valid_date(result.get("release_date"))
    ):
        return False
    blocked = excluded_appids()
    candidate = {
        "appid": int(result["appid"]),
        "name": result.get("name") or f"Steam App {int(result['appid'])}",
        "name_en": result.get("name") or f"Steam App {int(result['appid'])}",
        "release_raw": result["release_date"],
        "release_start": result["release_date"],
        "release_end": result["release_date"],
        "release_precision": "day",
        "release_display_precision": "date_full",
        "release_display_provider": result.get("release_display_provider"),
        "release_date_basis": result.get("release_date_basis"),
        "release_date_timezone": result.get("release_date_timezone") or "Asia/Taipei",
        "release_time_utc": result.get("release_time_utc"),
        "release_time_source": result.get("release_display_provider"),
        "release_date_verified_at": datetime.now(timezone.utc).isoformat(),
        "post_followers_store_verified": True,
        "post_followers_store_verified_at": datetime.now(timezone.utc).isoformat(),
        "followers": int(result["official_followers"]),
        "follower_checked_at": result.get("official_checked_at_taipei"),
        "follower_source": result.get("official_source") or "Steam Community XML memberCount",
        "official_ge5000": True,
        "store_url": result.get("steam_url") or f"https://store.steampowered.com/app/{int(result['appid'])}/",
    }
    if is_disallowed(candidate, blocked):
        return False
    games = master.get("games")
    if not isinstance(games, list):
        games = []
    by_id = {int(x["appid"]): dict(x) for x in games if isinstance(x, dict) and x.get("appid") is not None}
    prior = by_id.get(candidate["appid"], {})
    merged = dict(prior)
    merged.update({k: v for k, v in candidate.items() if v is not None})
    changed = merged != prior
    by_id[candidate["appid"]] = merged
    master["games"] = sorted(
        by_id.values(),
        key=lambda x: (
            str(x.get("release_start") or "9999-12-31"),
            -int(x.get("followers") or 0),
            int(x.get("appid") or 0),
        ),
    )
    if changed:
        now = datetime.now(timezone.utc).isoformat()
        master["updated_at"] = now
        master["post_followers_store_gate_version"] = 1
        master["post_followers_store_gate_checked_at"] = now
    return changed


def content_dispatch_signature(result):
    return (
        f"{int(result['appid'])}:"
        f"{int(result['official_followers'])}:"
        f"{result['release_date']}:store-v2"
    )


def dispatch_content_event(cp, result):
    """Notify the independent content backend after official >=5000 verification.

    Today the receiver lives as a logically separate backend/workflow in this
    repository. CONTENT_BACKEND_REPOSITORY can later point at a dedicated repo
    without changing the Followers scanner.
    """
    if not checked_numeric(result.get("official_followers")) or result["official_followers"] < 5000:
        return "not_qualified"
    if not valid_date(result.get("release_date")):
        return "invalid_release_date"
    if (
        result.get("store_date_exact") is not True
        or result.get("release_display_precision") != "date_full"
    ):
        return "store_date_not_verified"

    target = (os.environ.get("CONTENT_BACKEND_REPOSITORY") or
              os.environ.get("GITHUB_REPOSITORY") or "").strip()
    token = (os.environ.get("CONTENT_BACKEND_TOKEN") or
             os.environ.get("GITHUB_TOKEN") or "").strip()
    if not target or not token:
        return "not_configured"

    aid = str(int(result["appid"]))
    signature = content_dispatch_signature(result)
    registry = cp.setdefault("content_dispatches", {})
    prior = registry.get(aid) or {}
    if prior.get("signature") == signature and prior.get("status") == "dispatched":
        return "already_dispatched"

    payload = {
        "event_type": "steam_game_qualified",
        "client_payload": {
            "appid": int(aid),
            "official_followers": int(result["official_followers"]),
            "release_date": result["release_date"],
            "official_checked_at_taipei": result.get("official_checked_at_taipei"),
            "official_source": result.get("official_source"),
            "source_repository": os.environ.get("GITHUB_REPOSITORY"),
            "signature": signature,
        },
    }
    attempted = clock().isoformat()
    try:
        response = requests.post(
            f"https://api.github.com/repos/{target}/dispatches",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "GameTrendRadarFollowersDispatch/1.0",
            },
            json=payload,
            timeout=20,
        )
        if response.status_code == 204:
            registry[aid] = {
                "signature": signature,
                "status": "dispatched",
                "target_repository": target,
                "event_type": "steam_game_qualified",
                "dispatched_at_taipei": attempted,
            }
            print("CONTENT_DISPATCH_OK", aid, signature, flush=True)
            return "dispatched"
        registry[aid] = {
            "signature": signature,
            "status": "failed",
            "target_repository": target,
            "http": response.status_code,
            "attempted_at_taipei": attempted,
        }
        print("CONTENT_DISPATCH_FAILED", aid, response.status_code, flush=True)
        return "failed"
    except requests.RequestException as exc:
        registry[aid] = {
            "signature": signature,
            "status": "failed",
            "target_repository": target,
            "error_type": type(exc).__name__,
            "attempted_at_taipei": attempted,
        }
        print("CONTENT_DISPATCH_FAILED", aid, type(exc).__name__, flush=True)
        return "failed"


def verify_store_date_for_result(result, session):
    """Second date gate: run only after official Followers >= 5000."""
    detail = fetch_store_release_details(
        session, [int(result["appid"])], today=clock().date(), interval=0.0,
    ).get(int(result["appid"])) or {"exact": False, "status": "unavailable"}
    result["store_date_checked_at_taipei"] = clock().isoformat()
    result["store_date_exact"] = detail.get("exact") is True
    result["store_date_status"] = detail.get("status")
    if result["store_date_exact"]:
        result["release_date"] = detail["release_start"]
        result["release_display_precision"] = "date_full"
        result["release_display_provider"] = detail.get("release_display_provider")
        result["release_date_basis"] = detail.get("release_date_basis")
        result["release_date_timezone"] = detail.get("release_date_timezone")
        result["release_time_utc"] = detail.get("release_time_utc")
    return result["store_date_exact"]


def reverify_pending_store_dates(cp, master, limit=25):
    """Recheck already-official >=5000 games when Store date was not exact yet.

    This lets an unannounced game become publishable later without re-querying
    Steam Community Followers.
    """
    today = clock().date().isoformat()
    selected = []
    for result in sorted(
        cp.get("official_results", {}).values(),
        key=lambda x: (x.get("release_date", "9999-12-31"), int(x.get("appid", 0))),
    ):
        if len(selected) >= limit:
            break
        if not checked_numeric(result.get("official_followers")) or result["official_followers"] < 5000:
            continue
        checked = str(result.get("store_date_checked_at_taipei") or "")
        if result.get("store_date_exact") is True or checked.startswith(today):
            continue
        selected.append(result)
    if not selected:
        return 0

    session = requests.Session()
    details = fetch_store_release_details(
        session, [int(x["appid"]) for x in selected], today=clock().date(), interval=0.5,
    )
    exact_count = 0
    for result in selected:
        detail = details.get(int(result["appid"])) or {"exact": False, "status": "unavailable"}
        result["store_date_checked_at_taipei"] = clock().isoformat()
        result["store_date_exact"] = detail.get("exact") is True
        result["store_date_status"] = detail.get("status")
        if result["store_date_exact"]:
            exact_count += 1
            result["release_date"] = detail["release_start"]
            result["release_display_precision"] = "date_full"
            result["release_display_provider"] = detail.get("release_display_provider")
            result["release_date_basis"] = detail.get("release_date_basis")
            result["release_date_timezone"] = detail.get("release_date_timezone")
            result["release_time_utc"] = detail.get("release_time_utc")
            upsert_qualified_master(master, result)
            dispatch_content_event(cp, result)
    return len(selected)


def retry_pending_content_dispatches(cp, limit=25):
    """Retry qualified outcomes that were saved before an event was delivered."""
    retried = 0
    for result in sorted(
        cp.get("official_results", {}).values(),
        key=lambda x: (x.get("release_date", "9999-12-31"), int(x.get("appid", 0))),
    ):
        if retried >= limit:
            break
        if not checked_numeric(result.get("official_followers")) or result["official_followers"] < 5000:
            continue
        aid = str(int(result["appid"]))
        prior = (cp.get("content_dispatches") or {}).get(aid) or {}
        signature = content_dispatch_signature(result)
        if prior.get("signature") == signature and prior.get("status") == "dispatched":
            continue
        dispatch_content_event(cp, result)
        retried += 1
    return retried


def git_push():
    """Make each 10-success checkpoint durable before a runner interruption."""
    try:
        subprocess.run(["git", "add", str(CHECKPOINT), str(MASTER)], check=True, timeout=20,
                       stdout=subprocess.DEVNULL)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], timeout=20)
        if diff.returncode == 0:
            return True
        subprocess.run(["git", "commit", "-m",
                        "experiment: checkpoint dynamically queued official Followers"],
                       check=True, timeout=25, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                       check=True, timeout=45, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "push", "origin", "HEAD:main"],
                       check=True, timeout=50, stdout=subprocess.DEVNULL)
        print("DAILY_CATCHUP_GIT_CHECKPOINT_OK", flush=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("DAILY_CATCHUP_GIT_CHECKPOINT_FAILURE",
              type(exc).__name__, flush=True)
        return False


def make_queue(cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter, official_cache, other_official):
    if len(frozen_rows) != 1317 or len(old_group_rows) != 1358:
        raise ValueError("Frozen original input changed")
    if legacy_cp.get("cohort") != "steam_fresh_20260922_post_adult_1317_near_release":
        raise ValueError("Legacy official checkpoint cohort mismatch")
    existing_official = set(legacy_cp["official_results"]) | set(cp["official_results"])
    existing_official |= {
        str(k) for k, v in official_cache.get("games", {}).items()
        if isinstance(v, dict) and checked_numeric(v.get("followers"))
    }
    existing_official |= {
        str(k) for k, v in other_official.get("verified", {}).items()
        if isinstance(v, dict) and checked_numeric(v.get("official_followers"))
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
    }
    today = clock().date().isoformat()
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
                checked_numeric(followers) and followers >= 4000
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
        if aid in existing_official:
            continue
        if aid in bad_missing_group and row.get("group_id64") is None:
            cp.setdefault("unresolved_candidates", {})[aid] = {
                **row,
                "status": "official_xml_fallback_returned_html",
                "resolution": "retry when a valid official group ID is available",
                "official_followers": None,
            }
            continue
        cp.setdefault("unresolved_candidates", {}).pop(aid, None)
        pending.append(row)
    pending.sort(key=lambda r: (
        r["release_date"] < today,
        r["release_date"] if r["release_date"] >= today
        else -datetime.fromisoformat(r["release_date"]).date().toordinal(),
        r["appid"],
    ))
    daily["combined_pending_now"] = len(pending)
    daily["completed_legacy"] = len(legacy_cp["official_results"])
    daily["completed_new"] = len(cp["official_results"])
    return pending, daily


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--max-seconds", type=int, default=3450)
    parser.add_argument("--save-every", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 250 or args.interval < 8.0:
        raise ValueError("Max 250 single-file requests, min 8 seconds apart")
    if not 120 <= args.max_seconds <= 3500 or not 1 <= args.save_every <= 20:
        raise ValueError("Invalid duration/save interval")
    started = time.monotonic()
    started_at_taipei = clock().isoformat()
    original = read(FROZEN / "source_queue.json")
    oldgroups = read(FROZEN / "source_unresolved.json")
    oldcp = read(FROZEN / "checkpoint.json")
    eligible = read(ELIGIBLE)
    prefilter = read(PREFILTER)
    official_cache = read(OFFICIAL_CACHE)
    other_official = read(ORIGINAL_OFFICIAL)

    if CHECKPOINT.exists():
        cp = read(CHECKPOINT)
        if cp.get("cohort") != COHORT:
            raise ValueError("Dynamic cohort checkpoint version mismatch")
    else:
        cp = {
            "version": 1,
            "cohort": COHORT,
            "pending_candidates": {},
            "official_results": {},
            "attempt_events": [],
            "next_request_after_taipei": None,
            "rate_limit_count": 0,
            "created_at_taipei": clock().isoformat(),
        }
    cp.setdefault("content_dispatches", {})
    master = read(MASTER) if MASTER.exists() else {"version": 1, "games": []}
    store_rechecks = reverify_pending_store_dates(cp, master)
    retried_dispatches = retry_pending_content_dispatches(cp)
    if store_rechecks or retried_dispatches:
        save(CHECKPOINT, cp)
        save(MASTER, master)

    q, source_status = make_queue(
        cp, original, oldcp, oldgroups, eligible, prefilter,
        official_cache, other_official
    )
    start_count = len(cp["official_results"])
    attempts = []
    stop = "nothing_pending" if not q else "time_budget"
    last_start = None
    client = requests.Session()
    client.headers["User-Agent"] = "GameTrendRadarOfficialDailyCatchup/1.0"
    cooldown = cp.get("next_request_after_taipei")
    if not q:
        stop = "nothing_pending"
    elif cooldown and clock() < datetime.fromisoformat(cooldown):
        stop = "official_429_cooldown_no_request"
    elif oldcp.get("next_request_after_taipei") and clock() < datetime.fromisoformat(
        oldcp["next_request_after_taipei"]
    ):
        stop = "legacy_official_429_cooldown_no_request"
    else:
        stop = "batch_request_limit"
        for row in q[:args.max_requests]:
            if time.monotonic() - started >= args.max_seconds - 40:
                stop = "hour_time_budget"
                break
            if last_start is not None:
                delay = args.interval - (time.monotonic() - last_start)
                if delay > 0:
                    time.sleep(delay)
            last_start = time.monotonic()
            aid = row["appid"]
            url = (f'https://steamcommunity.com/gid/{row["group_id64"]}/memberslistxml/?xml=1'
                   if row["group_id64"] is not None
                   else f"https://steamcommunity.com/games/{aid}/memberslistxml/?xml=1")
            event = {
                "appid": aid, "when_taipei": clock().isoformat(),
                "release_date": row["release_date"],
                "queue_source": row["queue_source"], "http": None,
                "status": "request_started",
            }
            try:
                response = client.get(url, timeout=(8, 24))
                event["http"] = response.status_code
                if response.status_code == 429:
                    event["status"] = "rate_limited"
                    cp["rate_limit_count"] += 1
                    if response.headers.get("Retry-After"):
                        event["retry_after_header"] = response.headers["Retry-After"][:128]
                    cp["next_request_after_taipei"] = steam_429_cooldown(
                        response, cp["rate_limit_count"], clock()
                    ).isoformat()
                    event["next_request_after_taipei"] = cp["next_request_after_taipei"]
                    stop = "first_http_429"
                elif response.status_code in (401, 403) or response.status_code >= 500:
                    event["status"] = "access_or_server_error"
                    cp["next_request_after_taipei"] = (
                        clock() + timedelta(hours=24)
                    ).isoformat()
                    stop = "http_access_or_server_error"
                elif response.status_code != 200:
                    event["status"] = "unexpected_http"
                    stop = "unexpected_http"
                else:
                    root = ET.fromstring(response.content)
                    raw_count = root.findtext(".//memberCount")
                    got_gid = root.findtext(".//groupID64")
                    if (
                        not raw_count or not raw_count.replace(",", "").strip().isdigit()
                        or not got_gid or not got_gid.isdigit()
                        or (
                            row["group_id64"] is not None
                            and row["group_id64"] != got_gid
                        )
                    ):
                        event["status"] = "missing_count_or_group_mismatch"
                        stop = "invalid_official_xml"
                    else:
                        count = int(raw_count.replace(",", "").strip())
                        event["status"] = "ok"
                        event["official_followers"] = count
                        cp["official_results"][str(aid)] = {
                            **row,
                            "group_id64": got_gid,
                            "official_followers": count,
                            "official_ge5000": count >= 5000,
                            "official_checked_at_taipei": clock().isoformat(),
                            "official_source": "Steam Community XML memberCount",
                        }
                        if count >= 5000:
                            official = cp["official_results"][str(aid)]
                            exact = verify_store_date_for_result(official, client)
                            event["store_date_exact"] = exact
                            event["store_date_status"] = official.get("store_date_status")
                            if exact:
                                event["release_date"] = official["release_date"]
                                upsert_qualified_master(master, official)
                                save(MASTER, master)
                                event["content_dispatch"] = dispatch_content_event(cp, official)
                            else:
                                event["content_dispatch"] = "store_date_not_verified"
                        cp["rate_limit_count"] = 0
                        cp["temporary_error_count"] = 0
                        cp["next_request_after_taipei"] = None
            except (requests.RequestException, ET.ParseError) as exc:
                event["status"] = "transport_or_xml_error"
                event["error_type"] = type(exc).__name__
                if isinstance(exc, ET.ParseError):
                    event["content_type"] = response.headers.get("Content-Type", "")[:100]
                    event["response_bytes"] = len(response.content)
                    event["response_prefix"] = response.content[:180].decode(
                        "utf-8", errors="replace"
                    )
                cp["temporary_error_count"] = min(
                    8, cp.get("temporary_error_count", 0) + 1
                )
                minutes = min(
                    24 * 60, 15 * (2 ** (cp["temporary_error_count"] - 1))
                )
                cp["next_request_after_taipei"] = (
                    clock() + timedelta(minutes=minutes)
                ).isoformat()
                event["next_request_after_taipei"] = cp["next_request_after_taipei"]
                stop = "transport_or_xml_error"
            attempts.append(event)
            cp["attempt_events"].append(event)
            success_count = len(cp["official_results"]) - start_count
            if len(cp["attempt_events"]) > 2000:
                cp["attempt_events"] = cp["attempt_events"][-1500:]
            if len(attempts) <= 4 or len(attempts) % 10 == 0 or event["status"] != "ok":
                print("DAILY_CATCHUP_ITEM", json.dumps({
                    "appid": aid,
                    "status": event["status"],
                    "followers": event.get("official_followers"),
                    "attempted": len(attempts),
                    "successes": success_count,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }, ensure_ascii=False), flush=True)
            save(CHECKPOINT, cp)
            if success_count and success_count % args.save_every == 0 or event["status"] != "ok":
                if not git_push():
                    stop = "git_checkpoint_failure"
                    break
            if event["status"] != "ok":
                break

    successful = len(cp["official_results"]) - start_count
    pending_left = max(0, len(q) - successful)
    report = {
        "source_status": source_status,
        "start_taipei": started_at_taipei,
        "finish_taipei": clock().isoformat(),
        "stop_reason": stop,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "request_limit": args.max_requests,
        "request_interval_seconds": args.interval,
        "requests_this_run": len(attempts),
        "official_new_this_run": successful,
        "store_date_rechecks_before_followers": store_rechecks,
        "new_ge5000_this_run": sum(
            checked_numeric(x.get("official_followers")) and x["official_followers"] >= 5000
            for x in cp["official_results"].values()
            if x.get("official_checked_at_taipei") in {
                e.get("when_taipei") for e in attempts if e["status"] == "ok"
            }
        ),
        "completed_legacy": len(oldcp["official_results"]),
        "completed_dynamic": len(cp["official_results"]),
        "combined_official_completed": len(oldcp["official_results"]) + len(cp["official_results"]),
        "remaining_queue": pending_left,
        "http_429_this_run": sum(e["http"] == 429 for e in attempts),
        "next_request_after_taipei": cp.get("next_request_after_taipei"),
        "production_cache_modified": False,
        "github_actions_hourly_schedule": "03:00-23:00 Asia/Taipei",
    }
    # Calculate new qualified strictly from saved outcomes in this batch, not timestamps.
    ids = {str(e["appid"]) for e in attempts if e["status"] == "ok"}
    report["new_ge5000_this_run"] = sum(
        cp["official_results"][aid]["official_followers"] >= 5000 for aid in ids
    )
    save(CHECKPOINT, cp)
    save(MASTER, master)
    save(OUT / "report.json", report)
    save(OUT / "attempts.json", attempts)
    if not git_push():
        report["stop_reason"] = "final_git_checkpoint_failure"
        save(OUT / "report.json", report)
    print("DAILY_CATCHUP_FINAL", json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
