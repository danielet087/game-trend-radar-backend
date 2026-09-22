"""V3: two independent, fresh Steam Store scans; never load project candidate files.

Pass A and B differ ONLY by ignore_preferences=1. Both request 100 rows,
filter=comingsoon, category1=998, TW region and English date strings.
Outputs are isolated GitHub Actions artifacts (no commit of collected games).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://store.steampowered.com/search/results/"
DETAILS = "https://store.steampowered.com/api/appdetails"
BASE = {
    "query": "", "start": 0, "count": 100, "dynamic_data": "",
    "sort_by": "Released_ASC", "category1": 998, "filter": "comingsoon",
    "infinite": 1, "cc": "tw", "l": "english", "ndl": 1,
}
DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b, %Y", "%d %B, %Y")


def exact_date(s):
    s = " ".join((s or "").replace("Sept ", "Sep ").split())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_json(session, url, params, label, events, deadline):
    for attempt in range(7):
        if time.monotonic() >= deadline:
            raise TimeoutError("Experiment time budget exhausted")
        try:
            resp = session.get(url, params=params, timeout=(12, 40))
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                delay = min(12 * (2 ** attempt), 120)
                events.append({"label": label, "status": resp.status_code, "sleep_s": delay})
                print(f"RATE_LIMIT_OR_SERVER label={label} status={resp.status_code} sleep={delay}", flush=True)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, ValueError) as err:
            delay = min(8 * (2 ** attempt), 90)
            events.append({"label": label, "error": type(err).__name__, "sleep_s": delay})
            print(f"REQUEST_RETRY label={label} error={type(err).__name__} sleep={delay}", flush=True)
            time.sleep(delay)
    raise RuntimeError("Steam request did not recover: " + label)


def search_pass(session, name, ignore_preferences, today, end, out, deadline):
    params = dict(BASE)
    if ignore_preferences:
        params["ignore_preferences"] = 1
    started = time.monotonic()
    counts = Counter()
    event_log = []
    game_by_id = {}
    all_seen = set()
    date_conflicts = []
    seen_html = set()
    pages = 0
    start = 0
    first_total = None
    last_total = None
    stop_reason = None
    error = None
    print(f"PASS_START {name} params={json.dumps(params,sort_keys=True)}", flush=True)
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Experiment time budget exhausted")
            params["start"] = start
            payload = get_json(session, URL, params, f"{name}:{start}", event_log, deadline)
            if not isinstance(payload, dict) or not isinstance(payload.get("results_html"), str):
                raise ValueError(f"Missing Steam results_html for {name}, start {start}")
            if payload.get("total_count") is None:
                raise ValueError(f"Missing Steam total_count for {name}, start {start}")
            total = int(payload["total_count"])
            first_total = total if first_total is None else first_total
            last_total = total
            html = payload["results_html"]
            digest = hashlib.sha256(html.encode()).hexdigest()
            if digest in seen_html:
                raise ValueError(f"Repeated full-page HTML: {name} start={start}")
            seen_html.add(digest)
            rows = BeautifulSoup(html, "html.parser").select("a.search_result_row")
            if not rows:
                if start >= total:
                    stop_reason = "empty_after_reported_total"
                    break
                raise ValueError(f"Unexpected empty page {name} start={start}, total={total}")
            pages += 1
            counts["raw_rows"] += len(rows)
            for row in rows:
                raw_id = row.get("data-ds-appid") or ""
                match = re.fullmatch(r"\d+", str(raw_id))
                if not match:
                    counts["missing_or_multivalued_appid"] += 1
                    continue
                appid = int(raw_id)
                raw_date_node = row.select_one(".search_released")
                title_node = row.select_one(".title")
                raw_date = raw_date_node.get_text(" ", strip=True) if raw_date_node else ""
                title = title_node.get_text(" ", strip=True) if title_node else ""
                day = exact_date(raw_date)
                if appid in all_seen:
                    counts["duplicate_appid_rows"] += 1
                all_seen.add(appid)
                if day is None:
                    counts["imprecise_rows"] += 1
                elif day < today:
                    counts["before_window_rows"] += 1
                elif day > end:
                    counts["after_window_rows"] += 1
                else:
                    counts["eligible_rows_including_duplicates"] += 1
                    item = {
                        "appid": appid, "name": title, "release_date": str(day),
                        "release_text": raw_date,
                        "steam_url": f"https://store.steampowered.com/app/{appid}/",
                        "steam_tagids": row.get("data-ds-tagids") or "",
                    }
                    if appid in game_by_id and game_by_id[appid]["release_date"] != str(day):
                        date_conflicts.append({
                            "appid": appid, "first": game_by_id[appid]["release_date"],
                            "later": str(day),
                        })
                    game_by_id[appid] = item
            start += len(rows)
            if pages == 1 or pages % 20 == 0 or len(rows) < params["count"]:
                print(
                    f"PASS_PROGRESS {name} pages={pages} rows={start} "
                    f"total_now={total} unique_any={len(all_seen)} "
                    f"exact_in_year={len(game_by_id)} elapsed={round(time.monotonic()-started,1)}s",
                    flush=True,
                )
            if pages % 25 == 0:
                save(out / (name + "_partial.json"), list(game_by_id.values()))
            if len(rows) < params["count"]:
                stop_reason = "last_partial_page"
                break
            if pages > 400:
                raise RuntimeError(f"Too many pages: {name}")
            time.sleep(0.75)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"PASS_INCOMPLETE {name} {error}", flush=True)

    games = sorted(game_by_id.values(), key=lambda g: (g["release_date"], g["appid"]))
    save(out / (name + "_fresh_candidates.json"), games)
    summary = {
        "pass": name, "status": "complete" if not error else "incomplete",
        "error": error, "stop_reason": stop_reason,
        "params": {k: v for k, v in params.items() if k != "start"},
        "pages": pages, "first_reported_total": first_total,
        "last_reported_total": last_total, "rows_processed": start,
        "unique_appids_all_dates": len(all_seen), "unique_exact_in_year": len(games),
        "counters": dict(counts), "date_conflicts": date_conflicts[:50],
        "date_conflict_total": len(date_conflicts),
        "request_backoff_events": event_log, "http_429_count": sum(e.get("status")==429 for e in event_log),
        "elapsed_seconds": round(time.monotonic()-started, 2),
    }
    save(out / (name + "_report.json"), summary)
    print("PASS_FINAL " + json.dumps({k: v for k,v in summary.items() if k not in ("request_backoff_events", "date_conflicts")}, ensure_ascii=False), flush=True)
    return games, summary


def verify(session, chosen, deadline):
    evidence = []
    errors = []
    # Representative samples from pass overlap, plus each pass-only collection.
    for item in chosen:
        if time.monotonic() >= deadline:
            break
        try:
            payload = get_json(session, DETAILS, {"appids":item["appid"],"cc":"tw","l":"english"}, f"details:{item['appid']}", errors, deadline)
            entry = payload.get(str(item["appid"]), {})
            data = entry.get("data") or {}
            release = data.get("release_date") or {}
            day = exact_date(release.get("date"))
            evidence.append({
                "appid": item["appid"], "search_date": item["release_date"],
                "details_success": bool(entry.get("success")),
                "details_type": data.get("type"),
                "details_coming_soon": release.get("coming_soon"),
                "details_date_text": release.get("date"),
                "details_date_agrees": bool(day and str(day)==item["release_date"]),
                "details_exact_date": str(day) if day else None,
            })
        except Exception as exc:
            evidence.append({"appid": item["appid"], "error_type": type(exc).__name__})
        time.sleep(0.8)
    return evidence, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="output/steam_fresh_year_v3")
    parser.add_argument("--max-minutes", type=int, default=40)
    args = parser.parse_args()
    out = Path(args.out)
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    end = today + timedelta(days=365)
    started = time.monotonic()
    deadline = started + args.max_minutes * 60
    session = requests.Session()
    session.headers.update({"User-Agent":"Mozilla/5.0 (compatible; FreshSteamStoreYearProbe/3.0)", "Accept": "application/json,text/html"})
    print(f"FRESH_V3_START {today} to {end}; old project data is NEVER checked out or read.", flush=True)
    a, ar = search_pass(session,"inclusive_100",True,today,end,out,deadline)
    b, br = search_pass(session,"standard_100",False,today,end,out,deadline)
    amap, bmap = ({x["appid"]:x for x in items} for items in (a,b))
    ak, bk = set(amap), set(bmap)
    both, onlya, onlyb = sorted(ak&bk), sorted(ak-bk), sorted(bk-ak)
    date_differences = [{
        "appid": id, "inclusive_date":amap[id]["release_date"],
        "standard_date":bmap[id]["release_date"],
    } for id in both if amap[id]["release_date"] != bmap[id]["release_date"]]
    union = [amap.get(id) or bmap[id] for id in sorted(ak|bk)]
    save(out / "union_of_two_fresh_v3_scans.json", union)
    chosen = []
    for ids, mapping, n in [(onlya,amap,12),(onlyb,bmap,12),(both,amap,18)]:
        if ids:
            sampled = sorted({ids[round(i*(len(ids)-1)/max(1,min(n,len(ids))-1))] for i in range(min(n,len(ids)))})
            chosen.extend(mapping[id] for id in sampled)
    verified, details_errors = verify(session,chosen,deadline)
    save(out / "appdetails_verification.json", verified)
    report = {
        "status": "complete" if ar["status"]=="complete" and br["status"]=="complete" else "incomplete",
        "date_window_inclusive": [str(today),str(end)],
        "timezone":"Asia/Taipei", "source":URL,
        "old_project_data_read":False,
        "passes": [ar,br], "pass_overlap_appids":len(both),
        "inclusive_only_appids":len(onlya), "standard_only_appids":len(onlyb),
        "fresh_union_unique_exact_in_year":len(union),
        "date_disagreements_in_overlap":date_differences[:30],
        "date_disagreements_total":len(date_differences),
        "appdetails_sample_count":len(verified),
        "appdetails_type_game":sum(x.get("details_type")=="game" for x in verified),
        "appdetails_both_exact_date_and_game":sum(x.get("details_type")=="game" and x.get("details_date_agrees") and x.get("details_coming_soon") is True for x in verified),
        "appdetails_errors":details_errors,
        "duration_seconds":round(time.monotonic()-started,2),
        "caveat":"Complete SEARCH pagination does not guarantee all games on a mutable Steam catalog. Exact Steam display dates are not verified for every game. Adult content is not filtered in this discovery-only experiment.",
    }
    save(out/"comparison_report.json", report)
    print("FRESH_V3_FINAL "+json.dumps({k:v for k,v in report.items() if k not in ("passes","appdetails_errors","date_disagreements_in_overlap")},ensure_ascii=False),flush=True)
    if report["status"]!="complete":
        sys.exit(2)


if __name__=="__main__":
    main()
