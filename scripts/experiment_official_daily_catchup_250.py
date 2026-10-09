"""Dynamic near-release official Followers catch-up, one GitHub-scheduled hourly batch.

Inputs: persisted September 22 missing-source cohort plus eligible candidates
from a genuinely fresh daily follower prefilter. A stale/disabled daily scan is
reported honestly; never represented as the current day's full coverage.

Uses Steam Community official XML memberCount, NOT Store appdetails,
wishlist count, Community online count or third-party estimates.
Does NOT edit the production follower cache, candidate progress or frontend.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from radar_core.jobs import JobResult, JobStatus

# Permit the historical direct-script entry alongside python -m.
if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_backend.application.official_followers import (
    OfficialBatchServices, OfficialPaths, official_job_result as _official_job_result,
)
from radar_backend.domain.official_queue import (
    make_queue as _make_queue, preserve_cooldown_deadline,
    queue_checked_numeric as checked_numeric, utc_date_as_taipei, valid_date,
)
from radar_backend.jobs.official_followers import manual_cooldown_override, run_job
from radar_backend.state import official_checkpoint as _checkpoint_state

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


def clock():
    return datetime.now(TZ)


def read(path):
    return _checkpoint_state.read(path)


def save(path, data):
    return _checkpoint_state.save(path, data)


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
    from scripts.export_scheduler_queue_status import OUTPUT, export_status
    return _checkpoint_state.rebase_checkpoint(output=OUTPUT, export_status=export_status)


def git_push():
    from scripts.export_scheduler_queue_status import OUTPUT, export_status
    return _checkpoint_state.git_push(
        checkpoint=CHECKPOINT, master=MASTER, output=OUTPUT,
        export_status=export_status, rebase=rebase_checkpoint,
    )


def official_job_result(report, *, state_persisted):
    return _official_job_result(
        report, state_persisted=state_persisted,
        target_slot=os.environ.get("SCHEDULE_TARGET_SLOT") or None,
    )


def make_queue(cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter, official_cache, other_official, *, now=None):
    return _make_queue(
        cp, frozen_rows, legacy_cp, old_group_rows, eligible, prefilter,
        official_cache, other_official, now=now or clock(),
        is_twitch_queue_candidate=is_twitch_queue_candidate,
        cached_follower=cached_follower,
    )


def main():
    """Historical composition root; evaluate callbacks at invocation for callers.

    Store date verification, master promotion and content dispatch are explicit
    legacy ports until their cross-job migration can be reviewed separately.
    Queue, Community transport, cache/cooldown, persistence and batch coordination
    already live in their layers rather than behind renamed script modules.
    """
    paths = OfficialPaths(
        frozen=FROZEN, eligible=ELIGIBLE, prefilter=PREFILTER,
        official_cache=OFFICIAL_CACHE, original_official=ORIGINAL_OFFICIAL,
        checkpoint=CHECKPOINT, master=MASTER, output=OUT, cohort=COHORT,
    )
    services = OfficialBatchServices(
        read=read, save=save, exists=_checkpoint_state.exists,
        clock=clock, monotonic=time.monotonic,
        sleep=time.sleep, session_factory=requests.Session, make_queue=make_queue,
        git_push=git_push, reverify_pending_store_dates=reverify_pending_store_dates,
        retry_pending_content_dispatches=retry_pending_content_dispatches,
        verify_store_date_for_result=verify_store_date_for_result,
        upsert_qualified_master=upsert_qualified_master,
        dispatch_content_event=dispatch_content_event,
        follower_client_factory=OfficialFollowerClient,
        follower_cache_factory=OfficialFollowerCache, cooldown_factory=CooldownStore,
    )
    run_job(paths=paths, services=services, cooldown_override=manual_cooldown_override)


if __name__ == "__main__":
    main()
