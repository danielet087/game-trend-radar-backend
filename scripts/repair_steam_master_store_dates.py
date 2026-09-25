"""One-off repair of the qualified Steam master using the post-Followers Store gate."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from collectors.steam_upcoming import taiwan_today
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.steam_master_date_gate import (
    apply_store_release_detail,
    fetch_store_release_details,
)
from scripts.update_steam_daily import load_json, save_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", default="data/steam_upcoming_master.json")
    parser.add_argument("--eligible", default="data/steam_candidates_eligible.json")
    parser.add_argument(
        "--report",
        default="reports/steam_master_post_followers_date_recheck_20260925.json",
    )
    args = parser.parse_args()

    master_path = Path(args.master)
    eligible_path = Path(args.eligible)
    report_path = Path(args.report)
    master = load_json(master_path, {"games": []})
    eligible = load_json(eligible_path, {"games": []})
    games = master.get("games")
    eligible_rows = eligible.get("games")
    if not isinstance(games, list) or not isinstance(eligible_rows, list):
        raise RuntimeError("Steam master/eligible source malformed")
    today = taiwan_today()
    eligible_ids = {int(x["appid"]) for x in eligible_rows}
    blocked = excluded_appids()

    official = []
    for row in games:
        if not isinstance(row, dict) or is_disallowed(row, blocked):
            continue
        try:
            if int(row["followers"]) >= 5000:
                official.append(row)
        except (KeyError, TypeError, ValueError):
            continue

    stale_future_before = [
        row for row in official
        if row.get("release_start", "") > today.isoformat()
        and int(row["appid"]) not in eligible_ids
    ]
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarPostFollowersDateRepair/1.0"
    details = fetch_store_release_details(
        session, [int(x["appid"]) for x in official], today=today, interval=1.5,
    )

    retained = []
    rejected = []
    changed_dates = []
    recovered_stale = []
    historical_verified = 0
    for row in official:
        appid = int(row["appid"])
        old_day = str(row.get("release_start") or "")
        detail = details.get(appid) or {"exact": False, "status": "unavailable"}
        if detail.get("exact") is not True:
            if old_day <= today.isoformat():
                raise RuntimeError(
                    f"Historical qualified AppID {appid} could not be reverified; refusing destructive repair"
                )
            rejected.append({
                "appid": appid,
                "name": row.get("name"),
                "old_release_start": old_day,
                "store_status": detail.get("status"),
                "followers": row.get("followers"),
            })
            continue
        updated = apply_store_release_detail(row, detail)
        if old_day != updated["release_start"]:
            changed_dates.append({
                "appid": appid,
                "name": row.get("name"),
                "from": old_day,
                "to": updated["release_start"],
            })
        if old_day <= today.isoformat():
            historical_verified += 1
        if appid not in eligible_ids and old_day > today.isoformat():
            recovered_stale.append({
                "appid": appid,
                "name": row.get("name"),
                "release_start": updated["release_start"],
                "followers": updated.get("followers"),
                "follower_checked_at": updated.get("follower_checked_at"),
            })
        retained.append(updated)

    # The existing public set proves there should be many valid records. Abort
    # rather than committing a suspicious mass deletion after a bad Store read.
    if len(retained) < 80:
        raise RuntimeError(f"Suspicious Store recheck result: only {len(retained)} exact records")

    stamp = datetime.now(timezone.utc).isoformat()
    master["games"] = retained
    master["updated_at"] = stamp
    master["post_followers_store_gate_version"] = 1
    master["post_followers_store_gate_checked_at"] = stamp
    report = {
        "version": 1,
        "checked_at": stamp,
        "today_taipei": today.isoformat(),
        "before_master_count": len(games),
        "official_ge5000_checked": len(official),
        "stale_future_before": len(stale_future_before),
        "retained_exact": len(retained),
        "rejected_non_exact": len(rejected),
        "historical_verified": historical_verified,
        "recovered_from_stale_set": len(recovered_stale),
        "date_changed": len(changed_dates),
        "rejected": rejected,
        "recovered": recovered_stale,
        "date_changes": changed_dates,
        "followers_cache_modified": False,
        "official_checkpoint_modified": False,
    }
    save_json(master_path, master)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(report_path, report)
    print("STEAM_MASTER_STORE_DATE_RECHECK", json.dumps({
        k: v for k, v in report.items()
        if k not in {"rejected", "recovered", "date_changes"}
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
