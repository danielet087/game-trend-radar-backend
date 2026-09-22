"""Independent one-year upcoming Steam discovery experiment.

This script makes fresh requests to Steam Store Search. It intentionally never reads
the repository's existing data/ files or Steam followers cache/checkpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SOURCE = "https://store.steampowered.com/search/results/"
PARAMS = {
    "query": "", "start": 0, "count": 50, "dynamic_data": "",
    "sort_by": "Released_ASC", "force_infinite": 1, "filter": "comingsoon",
    "category1": 998, "ignore_preferences": 1, "cc": "tw",
    "l": "english", "infinite": 1,
}
DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b, %Y", "%d %B, %Y")


def parse_exact_date(value: str):
    value = " ".join(value.replace("Sept ", "Sep ").split())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def fetch_page(session, start):
    params = dict(PARAMS, start=start)
    for attempt in range(9):
        try:
            response = session.get(SOURCE, params=params, timeout=(12, 45))
            if response.status_code in (429, 500, 502, 503, 504):
                delay = min(10 * 2 ** attempt, 150)
                print(f"RETRY start={start} HTTP={response.status_code} pause={delay}s", flush=True)
                time.sleep(delay)
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results_html"), str):
                raise ValueError("Unexpected Steam search JSON response")
            return data
        except (requests.ConnectionError, requests.Timeout, ValueError) as exc:
            delay = min(8 * 2 ** attempt, 120)
            print(f"RETRY start={start} {type(exc).__name__} pause={delay}s", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Steam search failed after retries, start={start}")


def parse_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for row in soup.select("a.search_result_row"):
        raw_id = row.get("data-ds-appid", "")
        if not raw_id:
            match = re.search(r"/app/(\d+)", row.get("href", ""))
            raw_id = match.group(1) if match else ""
        match = re.search(r"\d+", str(raw_id))
        if not match:
            continue
        appid = int(match.group())
        title_node = row.select_one(".search_name .title") or row.select_one(".title")
        date_node = row.select_one(".search_released")
        name = title_node.get_text(" ", strip=True) if title_node else ""
        raw_date = date_node.get_text(" ", strip=True) if date_node else ""
        if not name:
            continue
        items.append({
            "appid": appid, "name": name, "release_text": raw_date,
            "release_date": str(parse_exact_date(raw_date)) if parse_exact_date(raw_date) else None,
            "steam_url": f"https://store.steampowered.com/app/{appid}/",
        })
    return items


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_samples(session, games):
    if not games:
        return []
    indexes = sorted(set(round(i * (len(games) - 1) / 19) for i in range(min(20, len(games)))))
    results = []
    for i in indexes:
        game = games[i]
        try:
            resp = session.get(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": game["appid"], "cc": "tw", "l": "english"},
                timeout=(10, 30),
            )
            resp.raise_for_status()
            entry = resp.json().get(str(game["appid"]), {})
            info = entry.get("data") or {}
            rd = info.get("release_date") or {}
            results.append({
                "appid": game["appid"], "search_date": game["release_date"],
                "appdetails_success": entry.get("success", False),
                "appdetails_type": info.get("type"),
                "appdetails_coming_soon": rd.get("coming_soon"),
                "appdetails_release_text": rd.get("date"),
            })
        except (requests.RequestException, ValueError) as exc:
            results.append({"appid": game["appid"], "error_type": type(exc).__name__})
        time.sleep(0.7)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="output/steam_fresh_year_experiment")
    parser.add_argument("--max-minutes", type=float, default=53)
    args = parser.parse_args()
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    since = datetime.now(ZoneInfo("Asia/Taipei")).date()
    through = since + timedelta(days=365)
    began = time.monotonic()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SteamUpcomingResearch/1.0)", "Accept": "application/json,text/html;q=0.9"})
    start = 0
    pages = 0
    reported_total = None
    all_ids = set()
    exact = {}
    dates = Counter()
    excluded = Counter()
    error = None
    last_sha = None

    print(f"FRESH_SOURCE Steam search/results start={since} end={through}", flush=True)
    print("FRESH_ONLY existing data/ JSON, follower cache, master and checkpoint NOT read", flush=True)
    try:
        while True:
            if time.monotonic() - began > args.max_minutes * 60:
                raise TimeoutError(f"Experiment exceeded {args.max_minutes} minutes")
            payload = fetch_page(session, start)
            count_raw = payload.get("total_count")
            total = int(count_raw) if count_raw is not None else None
            if total is None:
                raise ValueError("Steam search response missing total_count")
            if reported_total is None:
                reported_total = total
            rows = parse_rows(payload["results_html"])
            if not rows:
                if start >= total:
                    break
                raise RuntimeError(f"Empty/unparseable page start={start}, reported total={total}")
            sha = hashlib.sha256(payload["results_html"].encode("utf-8")).hexdigest()
            if sha == last_sha:
                raise RuntimeError(f"Duplicate consecutive page at start={start}, Steam pagination unreliable")
            last_sha = sha
            page_new = 0
            for item in rows:
                appid = item["appid"]
                if appid in all_ids:
                    excluded["duplicate_appid"] += 1
                    continue
                all_ids.add(appid)
                parsed = item["release_date"]
                if not parsed:
                    excluded["imprecise_date"] += 1
                elif parsed < str(since):
                    excluded["before_window"] += 1
                elif parsed > str(through):
                    excluded["after_window"] += 1
                else:
                    exact[appid] = item
                    dates[parsed[:7]] += 1
                    page_new += 1
            pages += 1
            start += len(rows)
            if pages == 1 or pages % 10 == 0 or start >= total:
                print(
                    f"PROGRESS pages={pages} processed={start} total_now={total} "
                    f"unique={len(all_ids)} exact_in_year={len(exact)} "
                    f"imprecise={excluded['imprecise_date']} "
                    f"elapsed_s={round(time.monotonic()-began,1)}",
                    flush=True,
                )
            if pages % 20 == 0:
                write(output / "progress.json", {
                    "completed": False, "pages": pages, "processed_rows": start,
                    "search_reported_total": reported_total,
                    "unique_appids": len(all_ids), "exact_in_year": len(exact),
                    "excluded": dict(excluded), "elapsed_seconds": round(time.monotonic() - began, 2),
                })
                write(output / "fresh_candidates_partial.json", list(exact.values()))
            if start >= total:
                break
            if pages > 1800:
                raise RuntimeError("Exceeded 1800 pages; unsafe pagination")
            time.sleep(0.55)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"INCOMPLETE: {error}", flush=True)

    games = sorted(exact.values(), key=lambda row: (row["release_date"], row["appid"]))
    write(output / "fresh_candidates.json", games)
    samples = verify_samples(session, games) if not error else []
    write(output / "appdetails_sample_verification.json", samples)
    report = {
        "status": "complete" if not error else "incomplete",
        "error": error,
        "date_window_inclusive": [str(since), str(through)],
        "timezone": "Asia/Taipei",
        "source": SOURCE,
        "source_params_without_start": {k: v for k, v in PARAMS.items() if k != "start"},
        "fresh_search_requests_completed": pages,
        "search_reported_total_at_start": reported_total,
        "rows_processed": start,
        "unique_appids_seen": len(all_ids),
        "exact_date_games_in_year": len(games),
        "excluded": dict(excluded),
        "by_month": dict(sorted(dates.items())),
        "sample_appdetails_requests": len(samples),
        "sample_appdetails_type_game": sum(x.get("appdetails_type") == "game" for x in samples),
        "sample_appdetails_unavailable": sum("error_type" in x for x in samples),
        "duration_seconds": round(time.monotonic() - began, 2),
        "old_data_read": False,
        "notes": [
            "New HTTP search/results requests to Steam; no preexisting project candidate files read.",
            "Release dates here are Steam Search display dates, not individually verified timestamps.",
            "AppDetails is cross-checked only for a stratified sample and is not used to inflate discovery count.",
            "Steam Search category1=998 is used as the official game category, but full per-app type verification is not claimed.",
        ],
    }
    write(output / "report.json", report)
    print("FINAL_FRESH_YEAR_EXPERIMENT " + json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    if error:
        sys.exit(2)


if __name__ == "__main__":
    main()
