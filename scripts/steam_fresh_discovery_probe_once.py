"""One-off, fresh Steam Store discovery. Does not read or mutate existing project JSON."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ).date()
END = TODAY + timedelta(days=365)
URL = "https://store.steampowered.com/search/results/"
APP_URL = "https://store.steampowered.com/api/appdetails"
OUT = Path("output")
OUT.mkdir(exist_ok=True)
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; GameTrendRadar-FreshDiscoveryProbe/1.0)", "Accept": "application/json,text/javascript,*/*;q=0.8"})
started = time.monotonic()
stat = {"start_date_taipei": str(TODAY), "end_date_taipei": str(END), "started_at_taipei": datetime.now(TZ).isoformat(), "source": "Live Steam Store search/results?infinite=1 and sample appdetails", "uses_existing_project_data": False, "request_pages": 0, "search_results_reported": None, "raw_rows_seen": 0, "distinct_ids_seen": 0, "valid_day_in_window": 0, "outside_window": 0, "missing_exact_day": 0, "missing_id": 0, "request_errors": [], "http_429_count": 0, "appdetails_sample_checked": 0, "appdetails_sample_agreed": 0, "appdetails_sample_disagreed": 0, "sample_failures": [], "stop_reason": None}
games = {}
max_pages = 600
max_seconds = 52 * 60

def request_json(url, params, retries=4):
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=28)
            if response.status_code == 429:
                stat["http_429_count"] += 1
                delay = min(90, 10 * (attempt + 1))
                print(f"Steam HTTP 429; backoff {delay}s", flush=True)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as err:
            stat["request_errors"].append({"type": type(err).__name__, "status": getattr(getattr(err, "response", None), "status_code", None), "at_page": stat["request_pages"]})
            print(f"request error: {type(err).__name__}, attempt={attempt + 1}", flush=True)
            time.sleep(min(20, 2 * (attempt + 1)))
    return None

def exact_date(raw):
    if isinstance(raw, dict):
        raw = raw.get("date")
    if not isinstance(raw, str):
        return None
    raw = re.sub(r"\s+", " ", raw).strip()
    patterns = ("%b %d, %Y", "%B %d, %Y", "%d %b, %Y", "%d %B, %Y", "%Y-%m-%d", "%Y/%m/%d", "%Y 年 %m 月 %d 日")
    for pattern in patterns:
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            pass
    return None

for page in range(max_pages):
    if time.monotonic() - started > max_seconds:
        stat["stop_reason"] = "max_seconds"
        break
    params = {"query": "", "start": page * 100, "count": 100, "dynamic_data": "", "sort_by": "Released_ASC", "category1": 998, "filter": "comingsoon", "infinite": 1, "cc": "tw", "l": "english", "ndl": 1}
    payload = request_json(URL, params)
    if not isinstance(payload, dict):
        stat["stop_reason"] = f"search_request_failed_page_{page}"
        break
    if stat["request_pages"] == 0:
        stat["search_results_reported"] = payload.get("total_count")
        stat["first_response_keys"] = list(payload.keys())
        stat["first_item_keys"] = ["data-ds-appid", "search_name", "search_released"]
        print("LIVE_START " + json.dumps({"date": str(TODAY), "end": str(END), "reported_total": stat["search_results_reported"], "response_keys": stat["first_response_keys"], "item_keys": stat["first_item_keys"]}), flush=True)
    html = payload.get("results_html")
    if not isinstance(html, str):
        stat["stop_reason"] = f"unexpected_schema_page_{page}"
        break
    items = BeautifulSoup(html, "html.parser").select("a.search_result_row")
    stat["request_pages"] += 1
    if not items:
        stat["stop_reason"] = "empty_search_page"
        break
    for item in items:
        stat["raw_rows_seen"] += 1
        appid = item.get("data-ds-appid")
        if not appid or not str(appid).isdigit():
            stat["missing_id"] += 1
            continue
        appid = int(appid)
        release_node = item.select_one(".search_released")
        release_raw = release_node.get_text(" ", strip=True) if release_node else ""
        name_node = item.select_one(".title")
        day = exact_date(release_raw)
        if day is None:
            stat["missing_exact_day"] += 1
        elif day < TODAY or day > END:
            stat["outside_window"] += 1
        elif appid not in games:
            games[appid] = {"appid": appid, "name": name_node.get_text(" ", strip=True) if name_node else "", "release_date": str(day), "release_date_raw": release_raw, "steam_url": f"https://store.steampowered.com/app/{appid}/", "source": "Steam Store live search results_html", "appdetails_checked": False}
    stat["distinct_ids_seen"] = len(games)
    stat["valid_day_in_window"] = len(games)
    if page < 3 or (page + 1) % 20 == 0:
        print("LIVE_PAGE " + json.dumps({"page": page + 1, "rows": stat["raw_rows_seen"], "valid_day_in_window": len(games), "excluded_no_exact_day": stat["missing_exact_day"], "excluded_outside_window": stat["outside_window"], "seconds": int(time.monotonic()-started)}), flush=True)
    time.sleep(0.7)
    if len(items) < 100:
        stat["stop_reason"] = "last_partial_search_page"
        break
else:
    stat["stop_reason"] = "max_pages"

# Cross-check sampled results on a distinct Steam official endpoint. Do not pass off sample validation as full validation.
for appid in list(games)[:10]:
    if time.monotonic() - started > max_seconds + 90:
        break
    payload = request_json(APP_URL, {"appids": appid, "cc": "tw", "l": "english"}, retries=2)
    stat["appdetails_sample_checked"] += 1
    info = payload.get(str(appid), {}) if isinstance(payload, dict) else {}
    data = info.get("data") or {}
    release = data.get("release_date") or {}
    day = exact_date(release.get("date"))
    item = games[appid]
    item["appdetails_checked"] = bool(info.get("success") and data)
    if item["appdetails_checked"]:
        item["official_type"] = data.get("type")
        item["official_coming_soon"] = release.get("coming_soon")
        item["appdetails_release_date_raw"] = release.get("date")
    agreed = bool(item["appdetails_checked"] and data.get("type") == "game" and release.get("coming_soon") is True and day == exact_date(item["release_date"]))
    if agreed:
        stat["appdetails_sample_agreed"] += 1
    else:
        stat["appdetails_sample_disagreed"] += 1
        stat["sample_failures"].append({"appid": appid, "name": item.get("name"), "search_date": item.get("release_date_raw"), "details_date": release.get("date"), "details_type": data.get("type"), "details_coming_soon": release.get("coming_soon"), "available": item["appdetails_checked"]})
    time.sleep(0.75)

stat["finished_at_taipei"] = datetime.now(TZ).isoformat()
stat["duration_seconds"] = round(time.monotonic()-started, 1)
stat["valid_day_in_window"] = len(games)
stat["full_scan"] = stat["stop_reason"] in ("last_partial_search_page", "empty_search_page")
stat["candidate_definition"] = "Official Steam Store comingsoon + category1=998 + exact YYYY-MM-DD in Taiwan-date interval, deduped by AppID. Appdetails verification only on first 10."
stat["no_existing_data_files_accessed"] = True
(OUT / "steam_fresh_discovery_probe_report.json").write_text(json.dumps(stat, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / "steam_fresh_discovery_probe_games.json").write_text(json.dumps({"metadata": stat, "games": list(games.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
print("LIVE_FINAL_REPORT " + json.dumps(stat, ensure_ascii=False), flush=True)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
        out.write(f"## Fresh Steam Store discovery probe — {TODAY} Taipei\n\n")
        out.write(f"- New, live Store search pages: **{stat['request_pages']}**\n- Raw rows fetched: **{stat['raw_rows_seen']}**\n- Unique exact-date games within {TODAY}…{END}: **{len(games)}**\n- Store search total (unfiltered by exact date): **{stat['search_results_reported']}**\n- Appdetails sample checked / fully agreed: **{stat['appdetails_sample_checked']} / {stat['appdetails_sample_agreed']}**\n- Complete pagination: **{stat['full_scan']}**\n- Stop reason: **{stat['stop_reason']}**\n- Duration seconds: **{stat['duration_seconds']}**\n- No existing project data was read or altered.\n")
