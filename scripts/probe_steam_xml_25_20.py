"""Read-only interval experiment on 50 ALREADY verified Steam AppIDs.

Test 25 seconds for 25 consecutive XML requests, cool down, then 20 seconds
for 25 different cached games. Abort immediately on first HTTP error, invalid
XML or connection issue. NEVER write to official follower cache, candidate
state, public JSON, or Steam checkpoint.
"""
from __future__ import annotations

import json
import statistics
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

CACHE = Path("data/steam_followers_cache.json")
OUTPUT = Path("output/steam_xml_25_20_pilot.json")
URL = "https://steamcommunity.com/games/{appid}/memberslistxml/"
STAGES = ((25.0, 25), (20.0, 25))
COOLDOWN_BETWEEN_STAGES = 180


def main() -> None:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))["games"]
    ids = [int(appid) for appid, item in cache.items()
           if isinstance(item.get("followers"), int)]
    required = sum(n for _, n in STAGES)
    if len(ids) < required:
        raise RuntimeError("Not enough existing verified Steam AppIDs for this experiment")
    ids = ids[:required]
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarReadOnlyXml25vs20/1.0"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Compare 25s and 20s request-start intervals after 30s production",
        "disjoint_old_cached_appids": True,
        "no_official_cache_or_public_writes": True,
        "cooldown_between_stages_seconds": COOLDOWN_BETWEEN_STAGES,
        "stages": [],
        "stopped_on_error": False,
        "sample": [],
    }
    position = 0
    for stage_number, (interval, count) in enumerate(STAGES):
        if stage_number:
            print("COOLDOWN", COOLDOWN_BETWEEN_STAGES, "seconds", flush=True)
            time.sleep(COOLDOWN_BETWEEN_STAGES)
        rows = []
        previous_start = None
        for appid in ids[position:position + count]:
            if previous_start is not None:
                time.sleep(max(0., interval - (time.monotonic() - previous_start)))
            previous_start = time.monotonic()
            row = {"appid": appid, "interval_seconds": interval,
                   "http": None, "ok": False}
            try:
                response = session.get(
                    URL.format(appid=appid), params={"xml": 1}, timeout=16,
                )
                row["http"] = response.status_code
                row["response_seconds"] = round(
                    time.monotonic() - previous_start, 3)
                if response.status_code != 200:
                    row["stop_reason"] = "HTTP_" + str(response.status_code)
                else:
                    root = ET.fromstring(response.content)
                    members = root.findtext(".//memberCount")
                    group = root.findtext(".//groupID64")
                    if members is None or not group or not group.isdigit():
                        row["stop_reason"] = "invalid_group_XML"
                    else:
                        amount = int(members.strip().replace(",", ""))
                        if amount < 0:
                            row["stop_reason"] = "invalid_member_count"
                        else:
                            row["ok"] = True
            except (requests.RequestException, ET.ParseError, ValueError) as exc:
                row["stop_reason"] = type(exc).__name__
                row["response_seconds"] = round(
                    time.monotonic() - previous_start, 3)
            rows.append(row)
            report["sample"].append(row)
            print("XML", "interval", interval, "appid", appid,
                  "http", row["http"], "ok", row["ok"],
                  "seconds", row.get("response_seconds"), flush=True)
            if not row["ok"]:
                report["stopped_on_error"] = True
                print("STOP", row["stop_reason"],
                      "NO FURTHER XML QUERIES", flush=True)
                break
        position += count
        successful = [row for row in rows if row["ok"]]
        result = {
            "interval_seconds": interval,
            "planned": count,
            "attempted": len(rows),
            "success": len(successful),
            "http_429": sum(x["http"] == 429 for x in rows),
            "complete_without_error": len(successful) == count,
            "mean_response_seconds": round(statistics.mean(
                x["response_seconds"] for x in successful), 3)
                if successful else None,
            "first_error": rows[-1].get("stop_reason") if rows else None,
        }
        report["stages"].append(result)
        print("STAGE", json.dumps(result), flush=True)
        if report["stopped_on_error"]:
            break
    report["all_stages_completed"] = (
        len(report["stages"]) == len(STAGES)
        and all(row["complete_without_error"] for row in report["stages"])
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print("SUMMARY", json.dumps({
        "stages": report["stages"],
        "all_stages_completed": report["all_stages_completed"],
        "total_requests": len(report["sample"]),
        "stopped_on_error": report["stopped_on_error"],
    }), flush=True)


if __name__ == "__main__":
    main()
