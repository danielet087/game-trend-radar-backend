"""Dynamic near-release official Followers catch-up, one externally triggered hourly batch.

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
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path("experiments/steam_official_daily_catchup")
FROZEN = Path("experiments/steam_official_nearfirst_20260922")
ELIGIBLE = Path("data/steam_candidates_eligible.json")
PREFILTER = Path("data/steam_prefilter_state.json")
OFFICIAL_CACHE = Path("data/steam_followers_cache.json")
ORIGINAL_OFFICIAL = Path("experiments/steam_official_followers_20260922/checkpoint.json")
OUT = Path("output/steam_official_daily_catchup")
CHECKPOINT = ROOT / "checkpoint.json"
TZ = ZoneInfo("Asia/Taipei")
GROUP_BASE = 103582791429521408
COHORT = "steam_official_daily_catchup_dynamic_v1"


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


def git_push():
    """Make each 10-success checkpoint durable before a runner interruption."""
    try:
        subprocess.run(["git", "add", str(CHECKPOINT)], check=True, timeout=20,
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
    if utc_date_as_taipei(prefilter.get("updated_at")) == today:
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
    pending = [
        row for aid, row in cp["pending_candidates"].items()
        if aid not in existing_official
    ]
    pending.sort(key=lambda r: (
        r["release_date"] < today, r["release_date"], r["appid"]
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
                    hours = min(168, 48 * (2 ** min(cp["rate_limit_count"] - 1, 2)))
                    cp["next_request_after_taipei"] = (
                        clock() + timedelta(hours=hours)
                    ).isoformat()
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
                        cp["rate_limit_count"] = 0
                        cp["next_request_after_taipei"] = None
            except (requests.RequestException, ET.ParseError) as exc:
                event["status"] = "transport_or_xml_error"
                event["error_type"] = type(exc).__name__
                cp["next_request_after_taipei"] = (
                    clock() + timedelta(hours=12)
                ).isoformat()
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
        "no_github_cron": True,
    }
    # Calculate new qualified strictly from saved outcomes in this batch, not timestamps.
    ids = {str(e["appid"]) for e in attempts if e["status"] == "ok"}
    report["new_ge5000_this_run"] = sum(
        cp["official_results"][aid]["official_followers"] >= 5000 for aid in ids
    )
    save(CHECKPOINT, cp)
    save(OUT / "report.json", report)
    save(OUT / "attempts.json", attempts)
    if not git_push():
        report["stop_reason"] = "final_git_checkpoint_failure"
        save(OUT / "report.json", report)
    print("DAILY_CATCHUP_FINAL", json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
