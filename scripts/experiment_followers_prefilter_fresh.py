"""Fresh candidate third-party Followers >=4000 prescreen experiment.

Input: freshly generated post-adult-filter candidates.
Never reads legacy follower cache/candidate state.

Process:
1. Map AppID -> Steam game group short ID using Steam official ResolveVanityURL (url_type=3).
2. Query steam-groups.com bulk API for public group member counts.
3. Classify measured >=4000 as priority, measured <4000 as below threshold,
   and missing/unmapped as unresolved (never invented as zero).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import random
import threading
import time
from zoneinfo import ZoneInfo

import requests

VANITY = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
BULK = "https://api.steam-groups.com/api/groups/bulk"
BASE = 103582791429521408


def save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_tls = threading.local()


def session():
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent":"GameTrendRadar-FreshFollowersPrefilter/1.0"})
        _tls.s = s
    return s


def resolve_one(appid: int, key: str):
    delays = [0, 1, 3, 8, 20, 45]
    last = None
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay + random.random() * 0.5)
        try:
            r = session().get(
                VANITY,
                params={"key": key, "vanityurl": str(appid), "url_type": 3},
                timeout=(8, 22),
            )
            if r.status_code in (429,500,502,503,504):
                last = f"http_{r.status_code}"
                continue
            r.raise_for_status()
            payload = r.json().get("response") or {}
            if payload.get("success") != 1:
                return {"appid":appid,"group_short_id":None,"status":"unmapped","attempts":attempt+1}
            gid = int(payload["steamid"])
            short = gid - BASE if gid > BASE else None
            return {"appid":appid,"group_short_id":short,"status":"mapped" if short is not None else "unmapped","attempts":attempt+1}
        except (requests.RequestException,ValueError,KeyError,TypeError) as e:
            last = type(e).__name__
    return {"appid":appid,"group_short_id":None,"status":"error","error":last,"attempts":len(delays)}


def bulk_lookup(ids, stats):
    counts={}
    for offset in range(0,len(ids),200):
        chunk=ids[offset:offset+200]
        ok=False
        for attempt,delay in enumerate([0,2,6,15,40]):
            if delay: time.sleep(delay)
            try:
                r=requests.post(BULK,json={"ids":chunk,"limit":len(chunk)},timeout=(10,40),headers={"User-Agent":"GameTrendRadar-FreshFollowersPrefilter/1.0"})
                stats["third_party_http_requests"]+=1
                if r.status_code in (429,500,502,503,504):
                    if r.status_code==429: stats["third_party_429"]+=1
                    continue
                r.raise_for_status()
                data=r.json()
                rows=data.get("data") if isinstance(data,dict) else None
                if not isinstance(rows,list): raise ValueError("unexpected bulk response")
                for item in rows:
                    if not isinstance(item,dict): continue
                    try: gid=int(item.get("id"))
                    except (TypeError,ValueError): continue
                    members=item.get("members")
                    if isinstance(members,int) and not isinstance(members,bool) and members>=0 and gid in chunk:
                        counts[gid]=members
                ok=True
                break
            except (requests.RequestException,ValueError):
                continue
        if not ok:
            stats["failed_bulk_chunks"]+=1
        if offset and offset%1000==0:
            print(f"BULK_PROGRESS ids={offset}/{len(ids)} measured={len(counts)}",flush=True)
        time.sleep(0.25)
    return counts


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--input",required=True)
    p.add_argument("--out",default="output/steam_followers_prefilter_fresh")
    p.add_argument("--workers",type=int,default=8)
    p.add_argument("--threshold",type=int,default=4000)
    a=p.parse_args()
    key=os.environ.get("STEAM_WEB_API_KEY","").strip()
    if not key: raise SystemExit("STEAM_WEB_API_KEY missing")

    games=json.loads(Path(a.input).read_text(encoding="utf-8"))
    if not isinstance(games,list): raise SystemExit("input must be list")
    if len({int(x["appid"]) for x in games})!=len(games): raise SystemExit("duplicate appids")
    started=time.monotonic()
    print(f"PREFILTER_START input={len(games)} workers={a.workers} threshold={a.threshold}",flush=True)

    stats={
        "input_count":len(games),"workers":a.workers,"threshold":a.threshold,
        "resolve_requests_logical":len(games),"mapped":0,"unmapped":0,"resolve_errors":0,
        "resolve_retry_extra_attempts":0,"third_party_http_requests":0,"third_party_429":0,
        "failed_bulk_chunks":0,
    }
    mapped={}
    unmapped=[]
    errors=[]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futures={ex.submit(resolve_one,int(g["appid"]),key):int(g["appid"]) for g in games}
        done=0
        for f in as_completed(futures):
            rec=f.result(); done+=1
            stats["resolve_retry_extra_attempts"] += max(0,int(rec.get("attempts",1))-1)
            if rec["status"]=="mapped":
                mapped[rec["appid"]]=rec["group_short_id"]; stats["mapped"]+=1
            elif rec["status"]=="unmapped":
                unmapped.append(rec["appid"]); stats["unmapped"]+=1
            else:
                errors.append(rec); stats["resolve_errors"]+=1
            if done==1 or done%250==0 or done==len(games):
                print(f"RESOLVE_PROGRESS {done}/{len(games)} mapped={stats['mapped']} unmapped={stats['unmapped']} errors={stats['resolve_errors']} elapsed={round(time.monotonic()-started,1)}s",flush=True)

    unique_groups=sorted(set(mapped.values()))
    print(f"RESOLVE_DONE unique_groups={len(unique_groups)} elapsed={round(time.monotonic()-started,1)}s",flush=True)
    counts=bulk_lookup(unique_groups,stats)

    by_app={int(g["appid"]):g for g in games}
    measured=[]; priority=[]; below=[]; unresolved=[]
    now=datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    for appid in sorted(by_app):
        gid=mapped.get(appid)
        members=counts.get(gid) if gid is not None else None
        row={
            "appid":appid,"name":by_app[appid].get("name"),"release_date":by_app[appid].get("release_date"),
            "steam_url":by_app[appid].get("steam_url"),"group_short_id":gid,
            "third_party_followers":members,"third_party_source":"steam-groups.com",
            "checked_at_taipei":now,
        }
        if members is None:
            row["prefilter_status"]="unresolved"; unresolved.append(row)
        else:
            row["prefilter_status"]="priority" if members>=a.threshold else "below_threshold"
            measured.append(row)
            (priority if members>=a.threshold else below).append(row)

    priority.sort(key=lambda x:(-x["third_party_followers"],x["appid"]))
    below.sort(key=lambda x:(-x["third_party_followers"],x["appid"]))
    unresolved.sort(key=lambda x:x["appid"])

    out=Path(a.out)
    save(out/"third_party_4000_priority.json",priority)
    save(out/"third_party_below_4000.json",below)
    save(out/"third_party_unresolved.json",unresolved)
    save(out/"resolve_errors.json",errors)
    report={
        **stats,
        "unique_group_ids":len(unique_groups),
        "third_party_measured_count":len(measured),
        "third_party_priority_4000_count":len(priority),
        "third_party_below_4000_count":len(below),
        "third_party_unresolved_count":len(unresolved),
        "duration_seconds":round(time.monotonic()-started,2),
        "input_source":str(a.input),
        "old_project_follower_data_read":False,
        "next_stage":"Verify priority list against Steam official Followers; unresolved must not be treated as below threshold.",
    }
    save(out/"report.json",report)
    print("PREFILTER_FINAL "+json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)
    # Mapping hard failures indicate the benchmark itself was incomplete. Normal unmapped/missing are valid unresolved outcomes.
    if errors or stats["failed_bulk_chunks"]:
        raise SystemExit(2)


if __name__=="__main__":
    main()
