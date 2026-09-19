"""Small, read-only Steam XML interval pilot, never touches follower cache/state."""
from __future__ import annotations

import json
import statistics
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

CACHE = Path("data/steam_followers_cache.json")
OUT = Path("output/steam_xml_interval_probe.json")
URL = "https://steamcommunity.com/games/{}/memberslistxml/"


def main():
    data = json.loads(CACHE.read_text(encoding="utf-8"))["games"]
    # Choose 50 existing known IDs (not any new unknown applications).
    selected = [int(x) for x in list(data)[:50]]
    session = requests.Session()
    session.headers.update({"User-Agent": "GameTrendRadarXmlIntervalProbe/1.0"})
    rows = []
    last_start = None
    for appid in selected:
        now = time.monotonic()
        if last_start is not None:
            time.sleep(max(0.0, 1.0 - (now - last_start)))
        last_start = time.monotonic()
        row = {"appid": appid, "ok": False, "status": None}
        try:
            response = session.get(URL.format(appid), timeout=15)
            row["seconds"] = round(time.monotonic() - last_start, 3)
            row["status"] = response.status_code
            if response.status_code != 200:
                print("XML", appid, "HTTP", response.status_code, flush=True)
                rows.append(row)
                # Rate-limit or server error -> stop, do not hammer Steam.
                if response.status_code == 429 or response.status_code >= 500:
                    print("STOP: server rate limit/error", flush=True)
                    break
                continue
            root = ET.fromstring(response.content)
            member_count = root.findtext(".//memberCount")
            gid = root.findtext(".//groupID64")
            if gid and gid.isdigit() and member_count is not None:
                row["group_id64"] = gid
                row["members"] = int(member_count.strip().replace(",", ""))
                row["ok"] = True
            else:
                row["error"] = "missing_expected_xml_fields"
            rows.append(row)
            print("XML", appid, "HTTP 200", "ok" if row["ok"] else "missing_fields",
                  row["seconds"], flush=True)
        except (requests.RequestException, ET.ParseError, ValueError) as error:
            row["error"] = type(error).__name__
            row["seconds"] = round(time.monotonic() - last_start, 3)
            rows.append(row)
            print("XML", appid, row["error"], flush=True)
            # Don't keep hammering when network is unhealthy.
            if isinstance(error, requests.RequestException):
                break

    duration = round(time.monotonic() - (last_start or time.monotonic()), 3)
    ok = [x for x in rows if x["ok"]]
    stats = {
        "attempted": len(rows),
        "success": len(ok),
        "http_429": sum(x["status"] == 429 for x in rows),
        "http_non_200": sum(x["status"] != 200 for x in rows),
        "mean_response_seconds": round(statistics.mean(x["seconds"] for x in ok), 3) if ok else None,
        "median_response_seconds": round(statistics.median(x["seconds"] for x in ok), 3) if ok else None,
        "sample_ended_without_429": bool(len(rows) == len(selected) and not any(x["status"] == 429 for x in rows)),
        "last_request_seconds": duration,
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": "50 old IDs, one request start per second, halt on HTTP 429 or HTTP 5xx",
        "statistics": stats,
        "samples": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("SUMMARY", json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
