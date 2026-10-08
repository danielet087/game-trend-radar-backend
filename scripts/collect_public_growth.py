"""Daily official Followers observations for accepted games, through release +30d.

Does not edit eligibility, the master or existing collection queues.
Shares official observations and persistent Community cooldown with the queue.
Only resolved Steam clan IDs are queried; unknown groups remain pending.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET
import requests
from radar_core.jobs import JobResult, JobStatus

try:
    from scripts.twitch_steam_admission import is_twitch_qualified
    from scripts.steam_official_followers import (
        CooldownStore, OfficialFollowerCache, OfficialFollowerClient, OfficialObservation,
        read_state, save_state, valid_group_id64,
    )
except ModuleNotFoundError:  # Direct script invocation from the repository root.
    from twitch_steam_admission import is_twitch_qualified
    from steam_official_followers import (
        CooldownStore, OfficialFollowerCache, OfficialFollowerClient, OfficialObservation,
        read_state, save_state, valid_group_id64,
    )

TAIPEI = timezone(timedelta(hours=8))


def read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def eligible(row, today):
    try:
        released = date.fromisoformat(row["release_start"])
        count, aid = row["followers"], row["appid"]
        return (isinstance(aid, int) and not isinstance(aid, bool) and aid > 0
                and isinstance(count, int) and not isinstance(count, bool)
                and (is_twitch_qualified(row) or count >= 5000 or count > 3000 and row.get("recent_source") in {"tracked_release", "direct_release"})
                and row.get("release_precision", "day") == "day"
                and today - timedelta(days=30) <= released <= today + timedelta(days=365))
    except (KeyError, TypeError, ValueError):
        return False


def parse_count(body):
    root = ET.fromstring(body)
    for path in ("memberCount", "groupDetails/memberCount", ".//memberCount"):
        text = root.findtext(path, "").replace(",", "").strip()
        if text.isdigit():
            return int(text)
    raise ValueError("Missing Steam Community memberCount")


def collect(data_dir, output, *, max_requests=120, interval=30, now=None, session=None,
            sleep=time.sleep, checkpoint_path=None, official_cache_path=None,
            legacy_checkpoint_path=None, group_state_path=None, original_checkpoint_path=None, clock=None):
    if clock is None:
        clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    now = clock()
    if now.tzinfo is None:
        raise ValueError("Growth collection needs an aware clock")
    today = now.astimezone(TAIPEI).date()
    catalog = read_state(data_dir / "catalog.json")
    rows = catalog.get("games")
    if not isinstance(rows, list) or type(catalog.get("count")) is not int or catalog["count"] != len(rows):
        raise ValueError("A complete public catalog is required")
    history = read_state(data_dir / "insights-state.json", optional=True).get("records", {})
    if not isinstance(history, dict):
        raise ValueError("Malformed insights history")
    checkpoint = read_state(checkpoint_path) if checkpoint_path is not None else {}
    sources = [read_state(path, optional=True) for path in
               (official_cache_path, legacy_checkpoint_path, group_state_path, original_checkpoint_path) if path is not None]
    cache = OfficialFollowerCache(checkpoint, *sources)
    legacy = read_state(legacy_checkpoint_path, optional=True) if legacy_checkpoint_path is not None else {}
    cooldown = CooldownStore(checkpoint, legacy)
    # Validate the retry state before considering any network operation.
    cooldown.deadline()
    rows = [row for row in rows if eligible(row, today)]

    def latest(row):
        record = history.get(str(row["appid"])) or {}
        if not isinstance(record, dict) or not isinstance(record.get("history", []), list):
            raise ValueError("Malformed per-game insights history")
        options = [dict(at=row.get("follower_checked_at"), followers=row.get("followers")),
                   *record.get("history", [])]
        shared = cache.latest(row["appid"], clock(), expected_group=cache.group_id(row["appid"]))
        if shared is not None:
            options.append({"at": shared.checked_at, "followers": shared.followers})
        options = [p for p in options if isinstance(p, dict) and timestamp(p.get("at"))
                   and timestamp(p["at"]) <= clock() and type(p.get("followers")) is int
                   and p["followers"] >= 0 and p.get("source", "steam_community") == "steam_community"]
        return max(options, key=lambda p: timestamp(p["at"]), default=None)

    rows.sort(key=lambda row: (latest(row) or {}).get("at") or "")
    http = session if session is not None else requests.Session()
    http.headers.update({"User-Agent": "GameTrendRadar/1.0 (+https://github.com/danielet087/game-trend-radar)"})
    client = OfficialFollowerClient(session=http, clock=clock, cooldown=cooldown)
    measurements, errors, pending, events = [], [], [], []
    attempts = reused = 0
    previous_start = None
    started = time.monotonic()
    reason = "completed"

    def persist():
        # Save Community retry state before publishing even a partial observation.
        if checkpoint_path is not None:
            save_state(checkpoint_path, checkpoint)
        save_state(output, {"measurements": measurements, "errors": errors,
                            "pending": pending, "reason": reason})

    for row in rows:
        aid = row["appid"]
        previous = latest(row)
        if previous and timestamp(previous["at"]).astimezone(TAIPEI).date() == today:
            measurements.append({"appid": aid, "followers": previous["followers"],
                                 "at": previous["at"], "source": "steam_community"})
            reused += 1
            continue
        gid = cache.group_id(aid)
        if gid is None:
            pending.append({"appid": aid, "reason": "awaiting_group_resolution"})
            continue
        if cooldown.blocked(clock()):
            reason = "rate_limited"
            pending.append({"appid": aid, "reason": "community_cooldown"})
            continue
        if attempts >= max_requests or time.monotonic() - started > 3600:
            reason = "bounded_run"
            pending.append({"appid": aid, "reason": "bounded_run"})
            continue
        if previous_start is not None:
            sleep(max(0, interval - (time.monotonic() - previous_start)))
        previous_start = time.monotonic()
        outcome = client.fetch(gid)
        if outcome.http is not None or outcome.error_type is not None:
            attempts += 1
        events.append({"appid": aid, "group_id64": gid, "status": outcome.status,
                       "http": outcome.http, "observed_at": outcome.observed_at.isoformat()})
        if outcome.status == "ok":
            at = outcome.observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            cache.remember(OfficialObservation(aid, gid, outcome.followers, at))
            measurements.append({"appid": aid, "followers": outcome.followers, "at": at, "source": "steam_community"})
        elif outcome.status in {"rate_limited", "cooldown_no_request"}:
            reason = "rate_limited"
            pending.append({"appid": aid, "reason": "community_cooldown"})
        else:
            errors.append(aid)
            pending.append({"appid": aid, "reason": outcome.status})
            reason = "source_unavailable"
        persist()
        # Invalid XML/unexpected HTTP also stop this run; they never imply zero.
        if outcome.status not in {"ok", "rate_limited", "cooldown_no_request"}:
            measured = {item["appid"] for item in measurements}
            accounted = {item["appid"] for item in pending}
            for later in rows:
                if later["appid"] not in measured | accounted:
                    recent = latest(later)
                    if recent and timestamp(recent["at"]).astimezone(TAIPEI).date() == today:
                        measurements.append({"appid": later["appid"], "followers": recent["followers"],
                                             "at": recent["at"], "source": "steam_community"})
                        reused += 1
                    else:
                        pending.append({"appid": later["appid"], "reason": "source_unavailable"})
            break
    if reason == "completed" and pending:
        reason = "awaiting_group_resolution"
    persist()
    result = {"generated_at": clock().isoformat(), "eligible": len(rows),
              "requests": attempts, "reused": reused, "errors": errors, "reason": reason,
              "pending": pending, "events": events, "measurements": measurements,
              "next_request_after_taipei": checkpoint.get("next_request_after_taipei"),
              "collection_complete": len(measurements) == len(rows) and not errors and not pending,
              "state_saved": checkpoint_path is not None}
    result["job_result"] = JobResult(
        job="steam_public_growth", status=(JobStatus.COOLING_DOWN if reason == "rate_limited" else JobStatus.PARTIAL),
        reason=reason, collection_complete=result["collection_complete"],
        state_persisted=False, published=False, requires_publication=True,
        target_slot=today.isoformat(),
    ).to_dict()
    save_state(output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"measurements", "events", "pending"}}))
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=Path("experiments/steam_official_daily_catchup/checkpoint.json"))
    parser.add_argument("--official-cache", type=Path, default=Path("data/steam_followers_cache.json"))
    parser.add_argument("--legacy-checkpoint", type=Path, default=Path("experiments/steam_official_nearfirst_20260922/checkpoint.json"))
    parser.add_argument("--group-state", type=Path, default=Path("data/steam_prefilter_state.json"))
    parser.add_argument("--original-checkpoint", type=Path, default=Path("experiments/steam_official_followers_20260922/checkpoint.json"))
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    if args.interval < 30 or not 0 <= args.max_requests <= 120:
        parser.error("Keep at least 30 seconds between requests and at most 120 requests")
    collect(args.data_dir, args.output, max_requests=args.max_requests, interval=args.interval,
            checkpoint_path=args.checkpoint, official_cache_path=args.official_cache,
            legacy_checkpoint_path=args.legacy_checkpoint, group_state_path=args.group_state,
            original_checkpoint_path=args.original_checkpoint)


if __name__ == "__main__":
    main()
