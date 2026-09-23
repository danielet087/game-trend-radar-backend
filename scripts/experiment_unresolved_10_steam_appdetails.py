"""One-off: seeded random sample of 10 from the frozen 1,317 unresolved apps.

Test Steam Store's appdetails endpoint. Does not read production files, does not
query the slow Steam Community XML endpoint, and does not write production data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import random
import re
import time

import requests

INPUT = Path("input/first_source_missing_both_unmeasured.json")
OUTPUT = Path("output/steam_official_appdetails_unresolved_10")
URL = "https://store.steampowered.com/api/appdetails"
SEED = 20260923

def follower_like_keys(obj, path=""):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if re.search(r"follow|member(count|s)?|wishlist|subscriber", k, re.I):
                found.append({"path": p, "value_type": type(v).__name__})
            found.extend(follower_like_keys(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:25]):
            found.extend(follower_like_keys(v, f"{path}[{i}]"))
    return found

def content_ids(d):
    data=(d.get("content_descriptors") or {}).get("ids") or []
    return sorted({int(x) for x in data if isinstance(x,(int,str)) and str(x).isdigit()})

def run():
    cohort=json.loads(INPUT.read_text(encoding="utf-8"))
    assert isinstance(cohort, list) and len(cohort)==1317, len(cohort)
    assert len({int(row["appid"]) for row in cohort})==1317
    assert all(row.get("steam_groups_followers") is None and row.get("games_popularity_followers") is None for row in cohort)
    sampled=random.Random(SEED).sample(cohort,10)
    selected=[int(x["appid"]) for x in sampled]
    print("OFFICIAL_10_SELECTION seed=",SEED,"appids=",selected,flush=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    session=requests.Session()
    session.headers.update({"User-Agent":"Mozilla/5.0 (compatible; GameTrendRadar-SteamAppDetailsExperiment/1.0)"})
    results=[]
    started=time.monotonic()
    for index, source in enumerate(sampled, 1):
        aid=int(source["appid"])
        rec={
            "appid":aid,
            "candidate_name":source.get("name"),
            "candidate_release_date":source.get("release_date"),
            "candidate_official_group_short_id":source.get("group_short_id"),
            "candidate_steam_url":source.get("steam_url"),
            "steam_appdetails_success":False,
            "http_status":None,
            "attempts":0,
        }
        for attempt in range(1,4):
            rec["attempts"]=attempt
            try:
                response=session.get(URL,params={"appids":str(aid),"cc":"tw","l":"english","ndl":"1"},timeout=(8,25))
                rec["http_status"]=response.status_code
                if response.status_code==429:
                    rec["status"]="rate_limited"
                    print(f"OFFICIAL_10_429 appid={aid} attempt={attempt}",flush=True)
                    if attempt<3:time.sleep(8*attempt)
                    continue
                response.raise_for_status()
                body=response.json()
                entry=body.get(str(aid)) if isinstance(body,dict) else None
                if not isinstance(entry,dict) or not entry.get("success") or not isinstance(entry.get("data"),dict):
                    rec["status"]="steam_success_false"
                    break
                d=entry["data"]
                release=d.get("release_date") or {}
                rec.update({
                    "steam_appdetails_success":True,
                    "status":"ok",
                    "official_store_name":d.get("name"),
                    "steam_appid_response":d.get("steam_appid"),
                    "type":d.get("type"),
                    "release_date":release.get("date"),
                    "coming_soon":release.get("coming_soon"),
                    "supported_languages":d.get("supported_languages"),
                    "header_image":d.get("header_image"),
                    "capsule_image":d.get("capsule_image"),
                    "screenshots_count":len(d.get("screenshots") or []),
                    "categories":[v.get("description") for v in (d.get("categories") or []) if isinstance(v,dict)],
                    "genres":[v.get("description") for v in (d.get("genres") or []) if isinstance(v,dict)],
                    "content_descriptors":content_ids(d),
                    "developers":d.get("developers"),
                    "short_description_available":bool(d.get("short_description")),
                    "returned_top_level_keys":sorted(d),
                    "follower_or_group_member_like_fields":follower_like_keys(d),
                })
                break
            except (requests.RequestException,ValueError) as e:
                rec["status"]="error"
                rec["error_type"]=type(e).__name__
                if attempt<3:time.sleep(3*attempt)
        results.append(rec)
        print("OFFICIAL_10_PROGRESS",index,"/10",json.dumps({
            "appid":aid,"http":rec["http_status"],"status":rec.get("status"),
            "type":rec.get("type"),"follower_fields":rec.get("follower_or_group_member_like_fields"),
        },ensure_ascii=False),flush=True)
        time.sleep(0.8)
    success=[r for r in results if r["steam_appdetails_success"]]
    report={
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "frozen_cohort":"steam_fresh_20260922_post_adult_3238",
        "frozen_unresolved_count":len(cohort),
        "random_sample_seed":SEED,
        "selected_appids":selected,
        "sample_size":len(results),
        "official_appdetails_success":len(success),
        "official_appdetails_failed":len(results)-len(success),
        "official_store_detail_types":{t:sum(x.get("type")==t for x in success) for t in sorted({x.get("type") for x in success})},
        "with_official_release_date":sum(bool(x.get("release_date")) for x in success),
        "with_header_image":sum(bool(x.get("header_image")) for x in success),
        "with_languages":sum(bool(x.get("supported_languages")) for x in success),
        "with_categories":sum(bool(x.get("categories")) for x in success),
        "with_genres":sum(bool(x.get("genres")) for x in success),
        "with_any_followers_member_field":sum(bool(x.get("follower_or_group_member_like_fields")) for x in success),
        "total_runtime_s":round(time.monotonic()-started,2),
        "official_source":"Steam Store api/appdetails; NOT Steam Community XML memberCount",
        "important":"AppDetails metadata availability does not mean official game Followers is available. Never substitute reviews/recommendations for Followers.",
    }
    (OUTPUT/"sampled_games.json").write_text(json.dumps(results,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (OUTPUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("OFFICIAL_10_FINAL",json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)

if __name__=="__main__":
    run()
