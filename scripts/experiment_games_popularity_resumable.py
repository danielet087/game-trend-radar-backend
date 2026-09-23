"""Resumable Games Popularity scan of the independent 2026-09-22 candidate cohort.

Only the fresh 3,238-game prefilter artifact and this experiment's own checkpoint
are read. No production Steam follower cache, publication or cursor is touched.

A 429/403 stops THIS run promptly; already completed HTTP 200/404 records survive.
The GitHub workflow commits the checkpoint only after this process returns.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import requests

ENDPOINT = "https://games-popularity.com/swagger/api/game/latest/{appid}"
COHORT_ID = "steam_fresh_20260922_post_adult_3238"
FINAL_STATES = {"measured", "not_in_dataset", "metric_missing"}


def write_json_atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_candidates(paths):
    by_id = {}
    for filename, band in paths:
        data = json.loads(Path(filename).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Input cohort file must be a list")
        for row in data:
            appid = int(row["appid"])
            if appid in by_id:
                raise ValueError(f"Duplicate cohort AppID {appid}")
            by_id[appid] = {
                "appid": appid, "name": row.get("name"),
                "release_date": row.get("release_date"),
                "steam_url": row.get("steam_url"),
                "first_source_status": band,
                "steam_groups_followers": row.get("third_party_followers"),
            }
    if len(by_id) != 3238:
        raise ValueError(f"Fresh experiment cohort changed: {len(by_id)} != 3238")
    return by_id


def lookup(appid, key):
    # Keep every request bounded, never echo requests' exceptions or URLs:
    # the key is present in the query string.
    try:
        with requests.Session() as session:
            response = session.get(
                ENDPOINT.format(appid=appid),
                params={"apiKey": key} if key else {},
                headers={"User-Agent": "GameTrendRadar-GamesPopularityCheckpoint/2.0"},
                timeout=(5, 12),
            )
            status = response.status_code
            if status == 429:
                return {"status": "rate_limited", "http": 429}
            if status in (401, 403):
                return {"status": "auth_or_quota_block", "http": status}
            if status == 404:
                return {"status": "not_in_dataset", "http": status}
            if status != 200:
                return {"status": "temporary_error", "http": status}
            body = response.json()
            if not isinstance(body, dict):
                return {"status": "temporary_error", "http": status}
            followers = body.get("followers")
            if not isinstance(followers, dict):
                return {"status": "metric_missing", "http": status}
            count = followers.get("followers")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                return {
                    "status": "measured", "http": status, "followers": count,
                    "observed_at": followers.get("added"),
                }
            return {"status": "metric_missing", "http": status}
    except (requests.RequestException, ValueError):
        return {"status": "temporary_error", "http": None}


def summarize(rows, results, status, started, threshold):
    resolved = {aid: x for aid, x in results.items() if x.get("status") in FINAL_STATES}
    measured = {aid: x for aid, x in resolved.items() if x.get("status") == "measured"}
    promoted = [
        aid for aid, record in measured.items()
        if record["followers"] >= threshold
        and (rows[int(aid)]["steam_groups_followers"] is None
             or rows[int(aid)]["steam_groups_followers"] < threshold)
    ]
    original_missing = [
        aid for aid, row in rows.items() if row["steam_groups_followers"] is None
    ]
    original_missing_resolved = [
        aid for aid in original_missing
        if str(aid) in measured
    ]
    priority = [
        aid for aid, row in rows.items()
        if isinstance(row["steam_groups_followers"], int)
        and row["steam_groups_followers"] >= threshold
        or str(aid) in measured and measured[str(aid)]["followers"] >= threshold
    ]
    return {
        "cohort": COHORT_ID, "status": status,
        "total": len(rows), "resolved": len(resolved), "measured": len(measured),
        "not_in_dataset": sum(x["status"] == "not_in_dataset" for x in resolved.values()),
        "metric_missing": sum(x["status"] == "metric_missing" for x in resolved.values()),
        "pending": len(rows) - len(resolved),
        "second_source_ge4000": sum(x["followers"] >= threshold for x in measured.values()),
        "combined_ge4000": len(priority),
        "rescued_by_second_source": len(promoted),
        "first_source_unresolved_total": len(original_missing),
        "first_source_unresolved_resolved_by_second": len(original_missing_resolved),
        "first_source_unresolved_still_without_any_measurement":
            sum(str(aid) not in measured for aid in original_missing),
        "elapsed_this_run_seconds": round(time.monotonic() - started, 2),
        "updated_at_taipei": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
        "counts_not_official": True,
        "anonymous_observations": sum(x.get("api_auth_mode") == "anonymous" for x in measured.values()),
        "authenticated_observations": sum(x.get("api_auth_mode") == "authenticated" for x in measured.values()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--priority", required=True)
    parser.add_argument("--below", required=True)
    parser.add_argument("--unresolved", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-minutes", type=float, default=12)
    parser.add_argument("--max-new", type=int, default=3238)
    parser.add_argument("--threshold", type=int, default=4000)
    parser.add_argument("--anonymous", action="store_true", help="Use only public anonymous quota; never transmit API key")
    args = parser.parse_args()
    if not 1 <= args.workers <= 6 or not 1 <= args.batch_size <= 50:
        raise ValueError("Unsafe batch or worker count")

    key = "" if args.anonymous else os.environ.get("GAMES_POPULARITY_API_KEY", "").strip()
    if not key and not args.anonymous:
        raise RuntimeError("GAMES_POPULARITY_API_KEY is missing")

    rows = read_candidates([
        (args.priority, "priority"),
        (args.below, "below_4000"),
        (args.unresolved, "unresolved"),
    ])
    checkpoint = Path(args.checkpoint)
    previous = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    if previous and previous.get("cohort") != COHORT_ID:
        raise ValueError("Refusing checkpoint belonging to another cohort")
    results = previous.get("results", {})
    if not isinstance(results, dict):
        raise ValueError("Invalid saved checkpoint")
    if any(int(k) not in rows for k in results):
        raise ValueError("Checkpoint AppID not in frozen input")

    out = Path(args.out)
    started = time.monotonic()
    deadline = started + args.max_minutes * 60
    def priority_order(aid):
        row = rows[aid]
        first = row["steam_groups_followers"]
        if isinstance(first, int) and 3000 <= first < args.threshold:
            return (0, -first, aid)
        if first is None:
            return (1, row.get("release_date") or "", aid)
        if isinstance(first, int) and first >= args.threshold:
            return (2, -first, aid)
        return (3, -(first or 0), aid)

    pending = [aid for aid in sorted(rows, key=priority_order) if results.get(str(aid), {}).get("status") not in FINAL_STATES]
    requests_this_run = 0
    successful_preflight = 0
    stop_reason = "complete" if not pending else "budget"
    print(f"RESUME_START cohort={COHORT_ID} total={len(rows)} "
          f"already_resolved={len(rows)-len(pending)} pending={len(pending)}", flush=True)

    def persist(reason):
        report = summarize(rows, results, reason, started, args.threshold)
        report["http_requests_this_run"] = requests_this_run
        write_json_atomic(checkpoint, {
            "version": 2, "cohort": COHORT_ID,
            "frozen_input_date": "2026-09-22",
            "updated_at_taipei": report["updated_at_taipei"],
            "results": results,
        })
        write_json_atomic(out / "report.json", report)
        priority_rows = []
        missing_rows = []
        for aid in sorted(rows):
            base = rows[aid]
            gp = results.get(str(aid), {})
            value = gp.get("followers") if gp.get("status") == "measured" else None
            first = base["steam_groups_followers"]
            max_value = max([v for v in (first, value) if isinstance(v, int)], default=None)
            record = {
                **base, "games_popularity_followers": value,
                "games_popularity_observed_at": gp.get("observed_at"),
                "games_popularity_status": gp.get("status", "pending"),
                "max_third_party_followers": max_value,
                "official_verified": False,
            }
            if max_value is not None and max_value >= args.threshold:
                priority_rows.append(record)
            if first is None and value is None:
                missing_rows.append(record)
        write_json_atomic(out / "combined_third_party_ge4000.json", priority_rows)
        write_json_atomic(out / "both_sources_no_followers.json", missing_rows)
        print("RESUME_PROGRESS " + json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return report

    persist("started")
    # Probe ONE app before starting worker batches. An account-level 429 must
    # not fan out to 25+ useless requests (the September 23 test proved this).
    if pending:
        first = pending[0]
        preflight = lookup(first, key)
        preflight["api_auth_mode"] = "anonymous" if args.anonymous else "authenticated"
        requests_this_run += 1
        if preflight.get("status") in FINAL_STATES:
            results[str(first)] = preflight
            pending = pending[1:]
            successful_preflight = 1
            persist("preflight_ok")
        elif preflight.get("status") in ("rate_limited", "auth_or_quota_block"):
            reason = preflight["status"]
            final = persist(reason)
            print("RESUME_STOP preflight_" + reason + " pending=" + str(final["pending"]), flush=True)
            print("RESUME_FINAL " + json.dumps(final, ensure_ascii=False, sort_keys=True), flush=True)
            return
    run_limit = min(len(pending), max(0, args.max_new - successful_preflight))
    for offset in range(0, run_limit, args.batch_size):
        if time.monotonic() > deadline - 30:
            stop_reason = "time_budget_reached"
            break
        chunk = pending[offset: min(offset + args.batch_size, run_limit)]
        throttled = False
        blocked = False
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(lookup, aid, key): aid for aid in chunk}
            for future in as_completed(futures):
                aid = futures[future]
                response = future.result()
                response["api_auth_mode"] = "anonymous" if args.anonymous else "authenticated"
                requests_this_run += 1
                status = response.get("status")
                if status in FINAL_STATES:
                    results[str(aid)] = response
                elif status == "rate_limited":
                    throttled = True
                elif status == "auth_or_quota_block":
                    blocked = True
                # All temporary failures stay pending; no zero follower fabrication.
        report = persist("rate_limited" if throttled else "auth_or_quota_block" if blocked else "scanning")
        if throttled or blocked:
            stop_reason = "rate_limited" if throttled else "auth_or_quota_block"
            print(f"RESUME_STOP reason={stop_reason} pending={report['pending']} "
                  f"retry_next_run=true", flush=True)
            break
        if report["pending"] == 0:
            stop_reason = "complete"
            break
    else:
        stop_reason = "complete" if not summarize(rows, results, "scanning", started, args.threshold)["pending"] else "batch_limit"

    final = persist(stop_reason)
    print("RESUME_FINAL " + json.dumps(final, ensure_ascii=False, sort_keys=True), flush=True)
    # A rate-limited partial run is an explicitly INCOMPLETE but safely persisted
    # benchmark, not a failed GitHub job that discards its progress.


if __name__ == "__main__":
    main()
