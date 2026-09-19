"""Read-only shorter interval pilot. NEVER modifies official cache or site JSON.

10 seconds first (16 existing Steam XML group IDs), then 6 seconds (16
different already-known IDs). Stop immediately on any HTTP error, malformed
XML, request timeout, or rate limit. Store bounded report privately.
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
REPORT = Path("output/steam_xml_shorter_pilot.json")
URL = "https://steamcommunity.com/games/{appid}/memberslistxml/"
STAGES = ((10.0, 16), (6.0, 16))


def run() -> None:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))["games"]
    ids = [int(k) for k, value in cache.items()
           if value.get("followers") is not None][:sum(n for _, n in STAGES)]
    if len(ids) < sum(n for _, n in STAGES):
        raise RuntimeError("Not enough old cached IDs for disjoint test groups")

    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarShorterXmlRatePilot/1.0"
    results = []
    stages = []
    abort = False
    for interval, count in STAGES:
        if abort:
            break
        stage_rows = []
        last_start = None
        for appid in ids[len(results):len(results) + count]:
            if last_start is not None:
                time.sleep(max(0.0, interval - (time.monotonic() - last_start)))
            last_start = time.monotonic()
            row = {"appid": appid, "start_interval": interval, "http": None, "ok": False}
            try:
                response = session.get(URL.format(appid=appid), params={"xml": 1},
                                       timeout=16)
                row["http"] = response.status_code
                row["response_seconds"] = round(time.monotonic() - last_start, 3)
                if response.status_code != 200:
                    row["stop_reason"] = "HTTP_" + str(response.status_code)
                    abort = True
                else:
                    root = ET.fromstring(response.content)
                    members = root.findtext(".//memberCount")
                    group = root.findtext(".//groupID64")
                    if members is not None and group and group.isdigit():
                        row["ok"] = True
                    else:
                        row["stop_reason"] = "missing_required_xml_fields"
                        abort = True
            except (requests.RequestException, ET.ParseError, ValueError) as exc:
                row["stop_reason"] = type(exc).__name__
                row["response_seconds"] = round(time.monotonic() - last_start, 3)
                abort = True
            results.append(row)
            stage_rows.append(row)
            print("XML", "interval", interval, "appid", appid, "http",
                  row["http"], "ok", row["ok"], "seconds",
                  row.get("response_seconds"), flush=True)
            if abort:
                print("STOP", row["stop_reason"], "NO FURTHER XML REQUESTS", flush=True)
                break
        successful = [row for row in stage_rows if row["ok"]]
        stage = {
            "interval_seconds": interval,
            "expected": count,
            "attempted": len(stage_rows),
            "success": len(successful),
            "complete_without_error": len(successful) == count,
            "mean_response_seconds": round(
                statistics.mean(row["response_seconds"] for row in successful), 3)
                if successful else None,
            "last_error": stage_rows[-1].get("stop_reason") if stage_rows else None,
        }
        stages.append(stage)
        print("STAGE", json.dumps(stage), flush=True)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Steam Community XML (not third-party)",
        "read_only": True,
        "notes": "Short pilot, not a long-term IP-wide rate-limit guarantee",
        "stages": stages,
        "total_requests": len(results),
        "all_stages_success": len(stages) == len(STAGES) and all(
            s["complete_without_error"] for s in stages),
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print("SUMMARY", json.dumps({
        "stages": stages, "total_requests": len(results),
        "all_stages_success": report["all_stages_success"],
    }), flush=True)


if __name__ == "__main__":
    run()
