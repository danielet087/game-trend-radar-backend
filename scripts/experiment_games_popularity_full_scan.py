"""Full Games Popularity follower scan for the fresh 3,238-candidate experiment.

Fresh input comes from the just-created third-party prefilter artifact.
This script never reads the legacy production follower cache/state.

Security: API key is accepted only from the environment and is never logged.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import random
import threading
import time
from zoneinfo import ZoneInfo

import requests

URL = "https://games-popularity.com/swagger/api/game/latest/{appid}"
_tls = threading.local()


def save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_session():
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "GameTrendRadar-GamesPopularityFullScan/1.0"})
        _tls.session = s
    return s


def lookup(appid: int, api_key: str):
    last_error = None
    attempts = 0
    for attempt, base_delay in enumerate((0, 1, 2, 5, 12, 25, 45), start=1):
        attempts = attempt
        if base_delay:
            time.sleep(base_delay + random.random() * 0.4)
        try:
            r = get_session().get(
                URL.format(appid=appid),
                params={"apiKey": api_key},
                timeout=(8, 28),
            )
            code = r.status_code
            if code == 404:
                return {
                    "appid": appid,
                    "status": "not_in_dataset",
                    "followers": None,
                    "observed_at": None,
                    "attempts": attempts,
                    "http_status": code,
                }
            if code in (401, 403):
                return {
                    "appid": appid,
                    "status": "auth_error",
                    "followers": None,
                    "observed_at": None,
                    "attempts": attempts,
                    "http_status": code,
                }
            if code == 429 or 500 <= code < 600:
                last_error = f"http_{code}"
                continue
            r.raise_for_status()
            body = r.json()
            followers = body.get("followers") if isinstance(body, dict) else None
            value = followers.get("followers") if isinstance(followers, dict) else None
            observed = followers.get("added") if isinstance(followers, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return {
                    "appid": appid,
                    "status": "measured",
                    "followers": value,
                    "observed_at": observed,
                    "attempts": attempts,
                    "http_status": code,
                }
            return {
                "appid": appid,
                "status": "metric_missing",
                "followers": None,
                "observed_at": observed,
                "attempts": attempts,
                "http_status": code,
            }
        except (requests.ConnectionError, requests.Timeout, ValueError):
            last_error = "transport_or_json"
    return {
        "appid": appid,
        "status": "request_failed",
        "followers": None,
        "observed_at": None,
        "attempts": attempts,
        "error_type": last_error,
    }


def load_rows(paths):
    rows = {}
    source_band = {}
    for path, band in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise SystemExit(f"{path}: expected JSON list")
        for row in data:
            appid = int(row["appid"])
            if appid in rows:
                raise SystemExit(f"duplicate AppID across inputs: {appid}")
            rows[appid] = row
            source_band[appid] = band
    return rows, source_band


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--priority", required=True)
    p.add_argument("--below", required=True)
    p.add_argument("--unresolved", required=True)
    p.add_argument("--out", default="output/games_popularity_full_scan")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--threshold", type=int, default=4000)
    args = p.parse_args()

    key = os.environ.get("GAMES_POPULARITY_API_KEY", "").strip()
    if not key:
        raise SystemExit("GAMES_POPULARITY_API_KEY missing")

    rows, source_band = load_rows([
        (args.priority, "first_source_ge4000"),
        (args.below, "first_source_below4000"),
        (args.unresolved, "first_source_unresolved"),
    ])
    if len(rows) != 3238:
        raise SystemExit(f"Expected 3238 fresh candidates, got {len(rows)}")

    started = time.monotonic()
    print(f"GP_FULL_START input={len(rows)} workers={args.workers} threshold={args.threshold}", flush=True)

    results = {}
    counts = {
        "measured": 0,
        "not_in_dataset": 0,
        "metric_missing": 0,
        "request_failed": 0,
        "auth_error": 0,
        "extra_retry_attempts": 0,
        "http_429_or_retryable_seen": 0,
    }

    # First request synchronously: fail fast if the newly-added key is invalid.
    first_id = min(rows)
    first = lookup(first_id, key)
    if first.get("status") == "auth_error":
        raise SystemExit("Games Popularity API key was rejected (401/403)")
    results[first_id] = first
    counts[first["status"]] = counts.get(first["status"], 0) + 1
    counts["extra_retry_attempts"] += max(0, int(first.get("attempts", 1)) - 1)

    remaining = [appid for appid in rows if appid != first_id]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_appid = {pool.submit(lookup, appid, key): appid for appid in remaining}
        completed = 1
        for future in as_completed(future_to_appid):
            rec = future.result()
            appid = rec["appid"]
            results[appid] = rec
            completed += 1
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            counts["extra_retry_attempts"] += max(0, int(rec.get("attempts", 1)) - 1)
            if rec.get("attempts", 1) > 1:
                counts["http_429_or_retryable_seen"] += 1
            if completed == 1 or completed % 250 == 0 or completed == len(rows):
                gp_ge = sum(
                    isinstance(x.get("followers"), int) and x["followers"] >= args.threshold
                    for x in results.values()
                )
                rescued = sum(
                    isinstance(x.get("followers"), int)
                    and x["followers"] >= args.threshold
                    and source_band.get(aid) != "first_source_ge4000"
                    for aid, x in results.items()
                )
                print(
                    f"GP_FULL_PROGRESS {completed}/{len(rows)} measured={counts.get('measured',0)} "
                    f"gp_ge4000={gp_ge} rescued={rescued} "
                    f"elapsed={round(time.monotonic()-started,1)}s",
                    flush=True,
                )

    generated = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    all_rows = []
    gp_ge4000 = []
    gp_below4000 = []
    gp_unresolved = []
    combined_priority = []
    rescued = []
    near_3000_3999 = []

    for appid in sorted(rows):
        original = rows[appid]
        gp = results[appid]
        first = original.get("third_party_followers")
        first_val = int(first) if isinstance(first, int) and not isinstance(first, bool) else None
        gp_val = gp.get("followers") if isinstance(gp.get("followers"), int) else None
        measured_values = [v for v in (first_val, gp_val) if isinstance(v, int)]
        max_value = max(measured_values) if measured_values else None
        rec = {
            "appid": appid,
            "name": original.get("name"),
            "release_date": original.get("release_date"),
            "steam_url": original.get("steam_url"),
            "first_source_band": source_band[appid],
            "steam_groups_followers": first_val,
            "games_popularity_followers": gp_val,
            "games_popularity_observed_at": gp.get("observed_at"),
            "games_popularity_status": gp.get("status"),
            "max_third_party_followers": max_value,
            "screened_at_taipei": generated,
        }
        rec["combined_ge4000"] = bool(max_value is not None and max_value >= args.threshold)
        all_rows.append(rec)

        if gp_val is None:
            gp_unresolved.append(rec)
        elif gp_val >= args.threshold:
            gp_ge4000.append(rec)
        else:
            gp_below4000.append(rec)

        if rec["combined_ge4000"]:
            combined_priority.append(rec)
            if first_val is None or first_val < args.threshold:
                rescued.append(rec)
        elif max_value is not None and 3000 <= max_value < args.threshold:
            near_3000_3999.append(rec)

    combined_priority.sort(key=lambda x: (-(x["max_third_party_followers"] or -1), x["appid"]))
    rescued.sort(key=lambda x: (-(x["games_popularity_followers"] or -1), x["appid"]))
    near_3000_3999.sort(key=lambda x: (-(x["max_third_party_followers"] or -1), x["appid"]))
    gp_ge4000.sort(key=lambda x: (-(x["games_popularity_followers"] or -1), x["appid"]))
    gp_below4000.sort(key=lambda x: (-(x["games_popularity_followers"] or -1), x["appid"]))

    out = Path(args.out)
    save(out / "games_popularity_all_3238.json", all_rows)
    save(out / "games_popularity_ge4000.json", gp_ge4000)
    save(out / "games_popularity_below4000.json", gp_below4000)
    save(out / "games_popularity_unresolved.json", gp_unresolved)
    save(out / "combined_third_party_ge4000.json", combined_priority)
    save(out / "rescued_by_games_popularity.json", rescued)
    save(out / "near_3000_3999_safety_band.json", near_3000_3999)

    original_unresolved = [a for a in rows if source_band[a] == "first_source_unresolved"]
    original_unresolved_gp_measured = [
        a for a in original_unresolved if isinstance(results[a].get("followers"), int)
    ]
    original_unresolved_gp_ge4000 = [
        a for a in original_unresolved
        if isinstance(results[a].get("followers"), int)
        and results[a]["followers"] >= args.threshold
    ]
    remaining_both_unresolved = [
        a for a in original_unresolved if not isinstance(results[a].get("followers"), int)
    ]

    report = {
        "input_count": len(rows),
        "threshold": args.threshold,
        "workers": args.workers,
        "games_popularity_measured_count": len([r for r in all_rows if isinstance(r["games_popularity_followers"], int)]),
        "games_popularity_ge4000_count": len(gp_ge4000),
        "games_popularity_below4000_count": len(gp_below4000),
        "games_popularity_unresolved_count": len(gp_unresolved),
        "combined_two_source_ge4000_count": len(combined_priority),
        "rescued_by_second_source_count": len(rescued),
        "near_3000_3999_safety_band_count": len(near_3000_3999),
        "first_source_unresolved_count": len(original_unresolved),
        "first_source_unresolved_resolved_by_games_popularity": len(original_unresolved_gp_measured),
        "first_source_unresolved_games_popularity_ge4000": len(original_unresolved_gp_ge4000),
        "still_unresolved_in_both_sources": len(remaining_both_unresolved),
        "request_stats": counts,
        "duration_seconds": round(time.monotonic() - started, 2),
        "provider": "Games Popularity",
        "provider_is_third_party": True,
        "old_project_follower_data_read": False,
        "next_stage": "Official Steam verification should use combined_two_source_ge4000_count, plus optional near-3000 safety-band policy.",
    }
    save(out / "report.json", report)
    print("GP_FULL_FINAL " + json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
