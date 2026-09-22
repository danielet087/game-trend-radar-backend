"""Probe Games Popularity as a second follower source.

Uses no API key and therefore stays within the documented anonymous daily quota.
Samples unresolved plus measured rows from the fresh prefilter artifact.
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path
import requests

BASE="https://games-popularity.com/swagger/api/game/latest/{appid}"

def save(p,o):
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(o,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--unresolved",required=True)
    p.add_argument("--priority",required=True)
    p.add_argument("--below",required=True)
    p.add_argument("--out",default="output/second_source_probe")
    a=p.parse_args()
    unresolved=json.loads(Path(a.unresolved).read_text(encoding="utf-8"))
    priority=json.loads(Path(a.priority).read_text(encoding="utf-8"))
    below=json.loads(Path(a.below).read_text(encoding="utf-8"))
    # 60 stratified unresolved + 20 measured reference rows = 80 anonymous requests.
    def strat(rows,n):
        if not rows:return []
        return [rows[round(i*(len(rows)-1)/(n-1))] for i in range(n)] if n>1 else [rows[0]]
    samples=[]
    for row in strat(unresolved,60):
        samples.append({**row,"sample_type":"unresolved"})
    measured=(priority[:10]+below[:10])
    for row in measured:
        samples.append({**row,"sample_type":"measured_reference"})
    sess=requests.Session()
    sess.headers.update({"User-Agent":"GameTrendRadar-SecondFollowerSourceProbe/1.0"})
    results=[]; start=time.monotonic(); http429=0
    for i,row in enumerate(samples,1):
        appid=int(row["appid"])
        rec={"appid":appid,"name":row.get("name"),"sample_type":row["sample_type"],"first_source_followers":row.get("third_party_followers")}
        try:
            r=sess.get(BASE.format(appid=appid),timeout=(10,30))
            rec["http"]=r.status_code
            if r.status_code==429:
                http429+=1
            if r.status_code==200:
                data=r.json()
                follower=(data.get("followers") or {}) if isinstance(data,dict) else {}
                rec["second_source_followers"]=follower.get("followers")
                rec["observed_at"]=follower.get("added")
            elif r.status_code==404:
                rec["second_source_status"]="not_in_dataset"
        except (requests.RequestException,ValueError) as e:
            rec["error_type"]=type(e).__name__
        results.append(rec)
        if i==1 or i%10==0 or i==len(samples):
            cov=sum(isinstance(x.get("second_source_followers"),int) for x in results)
            print(f"SECOND_SOURCE_PROGRESS {i}/{len(samples)} with_followers={cov} 429={http429} elapsed={round(time.monotonic()-start,1)}s",flush=True)
        time.sleep(0.15)
    unresolved_rows=[x for x in results if x["sample_type"]=="unresolved"]
    ref=[x for x in results if x["sample_type"]=="measured_reference"]
    compare=[x for x in ref if isinstance(x.get("second_source_followers"),int) and isinstance(x.get("first_source_followers"),int)]
    report={
        "requests":len(results),
        "unresolved_sample_count":len(unresolved_rows),
        "unresolved_second_source_coverage":sum(isinstance(x.get("second_source_followers"),int) for x in unresolved_rows),
        "unresolved_second_source_ge4000":sum(isinstance(x.get("second_source_followers"),int) and x["second_source_followers"]>=4000 for x in unresolved_rows),
        "reference_compared":len(compare),
        "reference_mean_abs_difference": round(sum(abs(x["second_source_followers"]-x["first_source_followers"]) for x in compare)/len(compare),2) if compare else None,
        "http_429":http429,
        "duration_seconds":round(time.monotonic()-start,2),
        "provider":"games-popularity.com",
        "api_key_used":False,
    }
    save(Path(a.out)/"results.json",results)
    save(Path(a.out)/"report.json",report)
    print("SECOND_SOURCE_FINAL "+json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)

if __name__=="__main__": main()
