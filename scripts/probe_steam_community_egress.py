"""Read-only probe: is Steam Community refusing this runner's IP, or one endpoint?

Six spaced GET requests from one GitHub runner, then a JSON report. Nothing is
written to state, caches, the database or any public file, and the production
cooldown is not consulted or changed. Response bodies are not logged beyond a
short prefix of a refusal page.
"""
from __future__ import annotations

import json
import os
import time

import requests

GROUP = "103582791465851187"
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
PRODUCTION = "GameTrendRadarOfficialDailyCatchup/1.0"
CHECKS = (
    ("control_store_api", "https://store.steampowered.com/api/appdetails?appids=10&filters=basic", PRODUCTION),
    ("community_home", "https://steamcommunity.com/", BROWSER),
    ("group_xml_production_agent", f"https://steamcommunity.com/gid/{GROUP}/memberslistxml/?xml=1", PRODUCTION),
    ("group_xml_browser_agent", f"https://steamcommunity.com/gid/{GROUP}/memberslistxml/?xml=1", BROWSER),
    ("group_html", f"https://steamcommunity.com/gid/{GROUP}", BROWSER),
    ("games_xml", "https://steamcommunity.com/games/10/memberslistxml/?xml=1", BROWSER),
)
HEADERS = ("Retry-After", "Server", "Content-Type", "Cache-Control", "X-Cache", "Akamai-GRN")


def main() -> int:
    session = requests.Session()
    rows = []
    for index, (label, url, agent) in enumerate(CHECKS):
        if index:
            time.sleep(float(os.environ.get("PROBE_SPACING_SECONDS", "20")))
        row = {"check": label, "url": url, "agent": "browser" if agent == BROWSER else "production"}
        started = time.monotonic()
        try:
            response = session.get(url, timeout=(8, 24), headers={"User-Agent": agent, "Accept-Language": "en-US"})
            body = response.content
            row.update(http=response.status_code, bytes=len(body),
                       headers={key: response.headers.get(key)[:120] for key in HEADERS if response.headers.get(key)},
                       has_member_count=b"<memberCount>" in body)
            if response.status_code != 200:
                row["body_prefix"] = body[:160].decode("utf-8", errors="replace")
        except requests.RequestException as error:
            row["error"] = type(error).__name__
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        rows.append(row)
        print("STEAM_EGRESS_PROBE", json.dumps(row, ensure_ascii=False), flush=True)
    summary = {row["check"]: row.get("http", row.get("error")) for row in rows}
    print("STEAM_EGRESS_SUMMARY", json.dumps(summary), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("### Steam Community egress probe\n\n| Check | HTTP |\n|---|---|\n")
            stream.writelines(f"| {key} | {value} |\n" for key, value in summary.items())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
