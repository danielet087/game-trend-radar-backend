"""Resume official Steam Followers from an existing *experimental* artifact.

Avoid requerying the previously verified 90 games. A 429 stops the run, and
every verified answer is saved for the next run. No production follower cache,
state or frontend data is touched.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
import requests

URL = "https://steamcommunity.com/games/{appid}/memberslistxml/?xml=1"


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--prior-dir", required=True)
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--max-minutes", type=float, default=7)
    p.add_argument("--interval", type=float, default=25)
    p.add_argument("--max-new", type=int, default=15)
    args = p.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if len(rows) != 92 or len({int(r["appid"]) for r in rows}) != 92:
        raise ValueError("Expected 92 unique independent priority candidates")
    cohort = {str(int(r["appid"])): r for r in rows}
    previous = Path(args.prior_dir)
    verified = {}
    for filename in ("official_5000_qualified.json", "official_below_5000.json"):
        for row in load_json(previous / filename, []):
            aid = str(int(row["appid"]))
            val = row.get("official_followers")
            if aid not in cohort or not isinstance(val, int) or val < 0:
                raise ValueError("Invalid previously verified official record")
            verified[aid] = row
    prior_count = len(verified)
    if prior_count != 90:
        raise ValueError(f"Previous artifact had {prior_count} official records, expected 90")

    checkpoint = Path(args.checkpoint)
    saved = load_json(checkpoint, {})
    if saved and saved.get("cohort") != "steam_fresh_20260922_first_source_92":
        raise ValueError("Unexpected official checkpoint cohort")
    for aid, record in saved.get("verified", {}).items():
        if aid not in cohort or not isinstance(record.get("official_followers"), int):
            raise ValueError("Invalid checkpoint record")
        verified[aid] = record

    out = Path(args.out)
    started = time.monotonic()
    deadline = started + args.max_minutes * 60
    pending = [aid for aid in cohort if aid not in verified]
    requests = 0

    def persist(status):
        now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
        checkpoint_value = {
            "cohort": "steam_fresh_20260922_first_source_92",
            "updated_at_taipei": now,
            "verified": verified,
        }
        write(checkpoint, checkpoint_value)
        qualified = sorted(
            [row for row in verified.values() if row["official_followers"] >= 5000],
            key=lambda x: (-x["official_followers"], int(x["appid"])),
        )
        below = sorted(
            [row for row in verified.values() if row["official_followers"] < 5000],
            key=lambda x: (-x["official_followers"], int(x["appid"])),
        )
        todo = [cohort[aid] for aid in cohort if aid not in verified]
        write(out / "official_5000_qualified.json", qualified)
        write(out / "official_below_5000.json", below)
        write(out / "official_pending.json", todo)
        report = {
            "status": status, "cohort_size": len(cohort),
            "prior_official_results_imported": prior_count,
            "total_official_verified": len(verified),
            "qualified_5000": len(qualified),
            "below_5000": len(below),
            "still_pending": len(todo),
            "requests_this_run": requests,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "old_production_follower_cache_read": False,
            "timestamp_taipei": now,
        }
        write(out / "report.json", report)
        print("OFFICIAL_RESUME " + json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return report

    persist("starting")
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (compatible; GameTrendRadar-OfficialCheckpoint/2.0)"})
    last_request = None
    stop = "complete" if not pending else "bounded_batch"

    for aid in pending[:args.max_new]:
        if time.monotonic() > deadline - 50:
            stop = "time_budget"
            break
        if last_request is not None:
            wait = args.interval - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)
        last_request = time.monotonic()
        requests += 1
        try:
            response = sess.get(URL.format(appid=aid), timeout=(8, 25))
            if response.status_code == 429:
                stop = "official_429_retry_later"
                print("OFFICIAL_429 stop_without_repeated_retries appid=" + aid, flush=True)
                break
            if response.status_code != 200:
                stop = f"official_http_{response.status_code}_retry_later"
                break
            root = ET.fromstring(response.content)
            raw = root.findtext(".//memberCount")
            if not raw or not raw.strip().replace(",", "").isdigit():
                stop = "missing_official_member_count"
                break
            followers = int(raw.strip().replace(",", ""))
            record = {
                **cohort[aid], "official_followers": followers,
                "official_group_id64": root.findtext(".//groupID64"),
                "official_checked_at_taipei": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
                "official_source": "Steam Community memberslistxml memberCount",
                "official_qualified_5000": followers >= 5000,
            }
            verified[aid] = record
            persist("progress")
        except (requests.RequestException, ET.ParseError):
            stop = "official_request_error_retry_later"
            break

    result = persist("complete" if len(verified) == len(cohort) else stop)
    print("OFFICIAL_RESUME_FINAL " + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
