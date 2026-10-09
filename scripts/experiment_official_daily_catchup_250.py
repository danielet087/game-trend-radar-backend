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
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from radar_core.jobs import JobResult, JobStatus

from scripts.steam_master_date_gate import fetch_store_release_details
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.import_twitch_steam_discoveries import cached_follower
from scripts.twitch_official_queue import is_twitch_queue_candidate
from scripts.twitch_steam_admission import aware_time
from scripts.steam_official_followers import (
    GROUP_BASE, CooldownStore, FollowerOutcome, OfficialFollowerCache,
    OfficialFollowerClient, group_to_gid, steam_429_cooldown, valid_group_id64,
)

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
COHORT = "steam_official_daily_catchup_dynamic_v1"


def manual_cooldown_override(requested):
    """Only an explicitly manual dispatch may probe before a retry deadline."""
    if not requested:
        return False
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("SCHEDULE_TRIGGER_SOURCE") != "manual"):
        raise ValueError("Skipping cooldown requires an explicit manual workflow dispatch")
    return True


def preserve_cooldown_deadline(until, *previous):
    """An unsuccessful manual probe cannot shorten an existing retry deadline."""
    for value in previous:
        if (deadline := aware_time(value)) is not None:
            until = max(until, deadline)
    return until


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
        "release_timestamp_taipei_date": result.get("release_timestamp_taipei_date"),
        "release_date_conflict": result.get("release_date_conflict") is True,
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
        announced_day = result.get("release_date")
        timestamp_day = detail["release_start"]
        # The candidate queue's release_date came from the verified TW Store
        # full-date display. Store Browse's timestamp is secondary metadata and
        # may map to the following Taiwan day.
        result["release_date"] = announced_day if valid_date(announced_day) else timestamp_day
        result["release_timestamp_taipei_date"] = detail.get(
            "release_timestamp_taipei_date", timestamp_day
        )
        result["release_date_conflict"] = result["release_date"] != timestamp_day
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
        if result.get("queue_source") == "twitch_steam_discovery":
            continue
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
            announced_day = result.get("release_date")
            timestamp_day = detail["release_start"]
            result["release_date"] = announced_day if valid_date(announced_day) else timestamp_day
            result["release_timestamp_taipei_date"] = detail.get(
                "release_timestamp_taipei_date", timestamp_day
            )
            result["release_date_conflict"] = result["release_date"] != timestamp_day
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
        if result.get("queue_source") == "twitch_steam_discovery":
            continue
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


def rebase_checkpoint():
    """Regenerate only a dashboard-only conflict; never overwrite queue state."""
    from scripts.export_scheduler_queue_status import OUTPUT, export_status
    command = ["git", "pull", "--rebase", "origin", "main"]
    for _ in range(5):
        result = subprocess.run(command, timeout=45, stdout=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        conflicts = subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=U"], text=True, timeout=20,
        ).splitlines()
        if conflicts != [str(OUTPUT)]:
            raise subprocess.CalledProcessError(result.returncode, command)
        export_status()
        subprocess.run(["git", "add", str(OUTPUT)], check=True, timeout=20,
                       stdout=subprocess.DEVNULL)
        command = ["git", "-c", "core.editor=true", "rebase", "--continue"]
    raise subprocess.CalledProcessError(1, command)


def git_push():
    """Make each 10-success checkpoint durable before a runner interruption."""
    # The status exporter is read-only. Its failure must never reset or block
    # the established official queue; the previous snapshot stays visibly old.
    from scripts.export_scheduler_queue_status import OUTPUT, export_status
    try:
        export_status()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("SCHEDULER_QUEUE_STATUS_EXPORT_FAILED", type(exc).__name__, flush=True)
    try:
        files = [str(CHECKPOINT), str(MASTER)]
        if OUTPUT.is_file():
            files.append(str(OUTPUT))
        subprocess.run(["git", "add", *files], check=True, timeout=20,
                       stdout=subprocess.DEVNULL)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], timeout=20)
        if diff.returncode not in (0, 1):
            raise subprocess.CalledProcessError(diff.returncode, diff.args)
        if diff.returncode == 1:
            subprocess.run(["git", "commit", "-m",
                            "experiment: checkpoint dynamically queued official Followers"],
                           check=True, timeout=25, stdout=subprocess.DEVNULL)
        # A prior checkpoint may already be committed locally after its push
        # failed. A clean index does not prove that commit reached the remote.
        rebase_checkpoint()
        subprocess.run(["git", "push", "origin", "HEAD:main"],
                       check=True, timeout=50, stdout=subprocess.DEVNULL)
        print("DAILY_CATCHUP_GIT_CHECKPOINT_OK", flush=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("DAILY_CATCHUP_GIT_CHECKPOINT_FAILURE",
              type(exc).__name__, flush=True)
        return False


def official_job_result(report, *, state_persisted):
    """Keep an hourly checkpoint distinct from complete current-day coverage."""
    reason = report["stop_reason"]
    source_status = report.get("source_status", {})
    current_source_complete = (
        source_status.get("status") == "current_day_prefilter_complete"
    )
    awaiting_resolution = bool(
        source_status.get("parked_group_xml_appids")
        or report.get("awaiting_group_resolution")
        or report.get("unresolved_candidates")
    )
    collection_complete = (
        type(report.get("remaining_queue")) is int
        and report["remaining_queue"] == 0
        and current_source_complete
        and not awaiting_resolution
    )
    if reason in {"git_checkpoint_failure", "final_git_checkpoint_failure"} or not state_persisted:
        status = JobStatus.FAILED
    elif collection_complete:
        status = JobStatus.COMPLETE
    elif "cooldown" in reason or reason == "first_http_429":
        status = JobStatus.COOLING_DOWN
    else:
        status = JobStatus.PARTIAL
    return JobResult(
        job="official-followers", status=status, reason=reason,
        collection_complete=collection_complete, state_persisted=state_persisted,
        # Saving the master or dispatching content is not a frontend publication.
        published=False, requires_publication=False,
        target_slot=os.environ.get("SCHEDULE_TARGET_SLOT") or None,
    )


def make_queue(cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter, official_cache, other_official, *, now=None):
    observed = (now or clock()).astimezone(TZ)
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
        attempted_at = aware_time(event.get("when_taipei"))
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--max-seconds", type=int, default=3450)
    parser.add_argument("--save-every", type=int, default=10)
    parser.add_argument("--skip-cooldown", action="store_true",
                        help="Explicit manual dispatch only: probe one queued game during cooldown")
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 250 or args.interval < 8.0:
        raise ValueError("Max 250 single-file requests, min 8 seconds apart")
    if not 120 <= args.max_seconds <= 3500 or not 1 <= args.save_every <= 20:
        raise ValueError("Invalid duration/save interval")
    skip_cooldown = manual_cooldown_override(args.skip_cooldown)
    if skip_cooldown:
        args.max_requests = 1
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
    # Missing group IDs stay in the same queue. Resolve them in the existing
    # workflow before using Community XML; they must not consume XML attempts.
    follower_cache = OfficialFollowerCache(cp, official_cache, oldcp, other_official, prefilter)
    cooldown_store = CooldownStore(cp, oldcp)
    known_group_queue = [{**row, "group_id64": gid} for row in q
                         if (gid := valid_group_id64(row.get("group_id64"))
                             or follower_cache.group_id(row["appid"])) is not None]
    attempts = []
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_metadata = {"run_id": run_id} if run_id.isdecimal() else {}
    cp["scheduler_batch"] = {
        **run_metadata, "active": True, "status": "preparing",
        "manual_cooldown_override": skip_cooldown,
        "request_limit": args.max_requests,
        "started_at": started_at_taipei, "last_updated_at": clock().isoformat(),
        "requests_this_run": 0, "official_new_this_run": 0, "http_429_this_run": 0,
    }
    stop = "nothing_pending" if not q else "time_budget"
    last_start = None
    client = requests.Session()
    client.headers["User-Agent"] = "GameTrendRadarOfficialDailyCatchup/1.0"
    follower_client = OfficialFollowerClient(session=client, clock=clock, cooldown=cooldown_store)
    cooldown = cp.get("next_request_after_taipei")
    legacy_cooldown = oldcp.get("next_request_after_taipei")
    previous_deadlines = (cooldown, legacy_cooldown,
                          (cp.get("community_cooldown") or {}).get("retry_at"))
    if not q:
        stop = "nothing_pending"
    elif not skip_cooldown and CooldownStore(cp).blocked(clock()):
        stop = "official_429_cooldown_no_request"
    elif not skip_cooldown and CooldownStore(oldcp).blocked(clock()):
        stop = "legacy_official_429_cooldown_no_request"
    elif not known_group_queue:
        stop = "awaiting_group_resolution_no_request"
    else:
        stop = "batch_request_limit"
        for row in known_group_queue[:args.max_requests]:
            if time.monotonic() - started >= args.max_seconds - 40:
                stop = "hour_time_budget"
                break
            if last_start is not None:
                delay = args.interval - (time.monotonic() - last_start)
                if delay > 0:
                    time.sleep(delay)
            last_start = time.monotonic()
            aid = row["appid"]
            event = {
                "appid": aid, "when_taipei": clock().isoformat(),
                "release_date": row["release_date"],
                "queue_source": row["queue_source"], "http": None,
                "status": "request_started",
            }
            if run_id.isdecimal():
                event["github_run_id"] = run_id
            if skip_cooldown:
                event["manual_cooldown_override"] = True
            cached = follower_cache.latest(aid, clock(), today_only=True, expected_group=row["group_id64"])
            if cached is not None:
                outcome = FollowerOutcome("ok", datetime.fromisoformat(cached.checked_at.replace("Z", "+00:00")),
                                          followers=cached.followers)
                event["cache_reused"] = True
            else:
                outcome = follower_client.fetch(row["group_id64"], manual_override=skip_cooldown)
            event.update(status=outcome.status, http=outcome.http)
            if outcome.retry_after:
                event["retry_after_header"] = outcome.retry_after[:128]
            if outcome.error_type:
                event["error_type"] = outcome.error_type
            for key in ("content_type", "response_bytes", "response_prefix"):
                value = getattr(outcome, key)
                if value is not None:
                    event[key] = value
            if outcome.status == "ok":
                count = outcome.followers
                event["official_followers"] = count
                cp["official_results"][str(aid)] = {
                    **row, "group_id64": row["group_id64"],
                    "official_followers": count, "official_ge5000": count >= 5000,
                    "official_checked_at_taipei": outcome.observed_at.astimezone(TZ).isoformat(),
                    "official_source": "Steam Community XML memberCount",
                }
                # Twitch admissions still use the shared cache-only importer.
                if count >= 5000 and row.get("queue_source") != "twitch_steam_discovery":
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
            else:
                stop = {
                    "rate_limited": "first_http_429",
                    "access_or_server_error": "http_access_or_server_error",
                    "unexpected_http": "unexpected_http",
                    "missing_count_or_group_mismatch": "invalid_official_xml",
                    "transport_or_xml_error": "transport_or_xml_error",
                    "cooldown_no_request": "official_429_cooldown_no_request",
                }.get(outcome.status, "awaiting_group_resolution_no_request")
                if cp.get("next_request_after_taipei"):
                    event["next_request_after_taipei"] = cp["next_request_after_taipei"]
            attempts.append(event)
            cp["attempt_events"].append(event)
            success_count = len(cp["official_results"]) - start_count
            cp["scheduler_batch"].update(
                status="querying", last_updated_at=clock().isoformat(),
                last_appid=aid, last_name=row.get("name") or f"Steam App {aid}",
                requests_this_run=sum(not item.get("cache_reused") for item in attempts),
                official_new_this_run=success_count,
                http_429_this_run=sum(item.get("http") == 429 for item in attempts),
            )
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
                # A clean rebase can merge another producer's AppIDs. Reload
                # that merged state before the next in-memory save.
                latest_checkpoint, latest_master = read(CHECKPOINT), read(MASTER)
                cp.clear()
                cp.update(latest_checkpoint)
                master.clear()
                master.update(latest_master)
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
        "manual_cooldown_override": skip_cooldown,
        "request_interval_seconds": args.interval,
        "requests_this_run": sum(not item.get("cache_reused") for item in attempts),
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
        "awaiting_group_resolution": len(q) - len(known_group_queue),
        "http_429_this_run": sum(e["http"] == 429 for e in attempts),
        "twitch_priority_pending": source_status.get("twitch_priority_pending", 0),
        "twitch_official_success_this_run": sum(e.get("queue_source") == "twitch_steam_discovery"
                                                and e["status"] == "ok" for e in attempts),
        "next_request_after_taipei": cp.get("next_request_after_taipei"),
        "production_cache_modified": False,
        "github_actions_hourly_schedule": "03:00-23:00 Asia/Taipei",
    }
    # Calculate new qualified strictly from saved outcomes in this batch, not timestamps.
    ids = {str(e["appid"]) for e in attempts if e["status"] == "ok"}
    report["new_ge5000_this_run"] = sum(
        cp["official_results"][aid]["official_followers"] >= 5000 for aid in ids
    )
    cp["scheduler_batch"].update(
        active=False, status="finished", last_updated_at=report["finish_taipei"],
        finished_at=report["finish_taipei"], stop_reason=stop,
        requests_this_run=report["requests_this_run"], official_new_this_run=successful,
        http_429_this_run=report["http_429_this_run"],
    )
    save(CHECKPOINT, cp)
    save(MASTER, master)
    save(OUT / "report.json", report)
    save(OUT / "attempts.json", attempts)
    state_persisted = git_push()
    if not state_persisted:
        report["stop_reason"] = "final_git_checkpoint_failure"
        cp["scheduler_batch"]["stop_reason"] = report["stop_reason"]
        save(CHECKPOINT, cp)
    job_result = official_job_result(report, state_persisted=state_persisted)
    report["job_result"] = job_result.to_dict()
    save(OUT / "report.json", report)
    print("DAILY_CATCHUP_FINAL", json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    if job_result.status is JobStatus.FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
