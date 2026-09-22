"""Verify a fresh third-party >=4000 shortlist against official Steam Community Followers.

Input must come from the isolated fresh prefilter artifact.
No legacy follower cache/state is read.
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


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--input",required=True)
    p.add_argument("--out",default="output/steam_official_followers_verify")
    p.add_argument("--threshold",type=int,default=5000)
    p.add_argument("--start-interval",type=float,default=8.0)
    a=p.parse_args()

    rows=json.loads(Path(a.input).read_text(encoding="utf-8"))
    if not isinstance(rows,list): raise SystemExit("input must be list")
    if len({int(x["appid"]) for x in rows})!=len(rows): raise SystemExit("duplicate appids")

    sess=requests.Session()
    sess.headers.update({"User-Agent":"Mozilla/5.0 (compatible; GameTrendRadar-OfficialFollowersVerify/1.0)"})
    out=Path(a.out)
    started=time.monotonic()
    interval=max(5.0,a.start_interval)
    last_req=0.0
    success_streak=0
    stats={"input_count":len(rows),"official_threshold":a.threshold,"http_requests":0,"http_429":0,"retries":0,"start_interval_seconds":interval}
    verified=[]; failures=[]

    def wait_slot():
        nonlocal last_req
        now=time.monotonic()
        sleep=max(0,last_req+interval-now)
        if sleep: time.sleep(sleep)
        last_req=time.monotonic()

    print(f"OFFICIAL_VERIFY_START input={len(rows)} interval={interval}s threshold={a.threshold}",flush=True)
    for idx,row in enumerate(rows,1):
        appid=int(row["appid"])
        follower=None; gid=None; error=None; attempts=0
        for attempt in range(6):
            attempts+=1
            wait_slot()
            try:
                resp=sess.get(URL.format(appid=appid),timeout=(10,35))
                stats["http_requests"]+=1
                if resp.status_code==429:
                    stats["http_429"]+=1
                    success_streak=0
                    interval=min(30.0,interval+4.0)
                    retry_after=resp.headers.get("Retry-After")
                    try: extra=min(120,max(30,int(retry_after)))
                    except (TypeError,ValueError): extra=min(120,30*(attempt+1))
                    print(f"OFFICIAL_429 appid={appid} interval_now={interval}s backoff={extra}s",flush=True)
                    time.sleep(extra)
                    stats["retries"]+=1
                    continue
                if resp.status_code in (500,502,503,504):
                    success_streak=0
                    time.sleep(min(60,10*(attempt+1)))
                    stats["retries"]+=1
                    continue
                resp.raise_for_status()
                root=ET.fromstring(resp.content)
                raw=root.findtext(".//memberCount")
                raw_gid=root.findtext(".//groupID64")
                if raw and raw.strip().replace(",","").isdigit():
                    follower=int(raw.strip().replace(",",""))
                    gid=raw_gid if raw_gid and raw_gid.isdigit() else None
                    success_streak+=1
                    if success_streak>=12 and interval>6.0:
                        interval=max(6.0,interval-1.0)
                        success_streak=0
                    break
                error="missing_memberCount"
            except (requests.RequestException,ET.ParseError,ValueError) as exc:
                error=type(exc).__name__
                success_streak=0
                stats["retries"]+=1
                time.sleep(min(45,6*(attempt+1)))
        rec={
            **row,
            "official_followers":follower,
            "official_group_id64":gid,
            "official_checked_at_taipei":datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
            "official_source":"Steam Community memberslistxml memberCount",
            "official_qualified_5000": bool(follower is not None and follower>=a.threshold),
            "attempts":attempts,
        }
        if follower is None:
            rec["error"]=error or "unresolved"; failures.append(rec)
        else:
            rec["third_party_delta"]=follower-int(row["third_party_followers"])
            verified.append(rec)
        if idx==1 or idx%10==0 or idx==len(rows):
            q=sum(x["official_qualified_5000"] for x in verified)
            print(f"OFFICIAL_PROGRESS {idx}/{len(rows)} verified={len(verified)} qualified={q} failures={len(failures)} interval={interval}s elapsed={round(time.monotonic()-started,1)}s",flush=True)

    qualified=[x for x in verified if x["official_qualified_5000"]]
    rejected=[x for x in verified if not x["official_qualified_5000"]]
    qualified.sort(key=lambda x:(-x["official_followers"],x["appid"]))
    rejected.sort(key=lambda x:(-x["official_followers"],x["appid"]))
    save(out/"official_5000_qualified.json",qualified)
    save(out/"official_below_5000.json",rejected)
    save(out/"official_unresolved.json",failures)
    report={
        **stats,
        "verified_count":len(verified),
        "qualified_5000_count":len(qualified),
        "below_5000_count":len(rejected),
        "unresolved_count":len(failures),
        "final_interval_seconds":interval,
        "duration_seconds":round(time.monotonic()-started,2),
        "old_project_follower_data_read":False,
        "third_party_priority_input_count":len(rows),
    }
    save(out/"report.json",report)
    print("OFFICIAL_VERIFY_FINAL "+json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)
    if failures:
        raise SystemExit(2)


if __name__=="__main__":
    main()
