"""Probe official Steam Community group HTML member counts for unresolved followers."""
from __future__ import annotations
import argparse,json,re,time
from pathlib import Path
import requests
from bs4 import BeautifulSoup

BASE=103582791429521408

def strat(rows,n):
    if not rows:return []
    if n<=1:return [rows[0]]
    return [rows[round(i*(len(rows)-1)/(n-1))] for i in range(n)]

def parse_members(html):
    soup=BeautifulSoup(html,"html.parser")
    # Steam group pages currently expose a membercount element/link.
    for node in soup.select(".membercount, a.membercount"):
        text=node.get_text(" ",strip=True)
        m=re.search(r"([0-9][0-9,]*)\s+MEMBERS?",text,re.I)
        if m:return int(m.group(1).replace(",",""))
    # Conservative fallback: visible page text only.
    text=soup.get_text(" ",strip=True)
    m=re.search(r"\b([0-9][0-9,]*)\s+MEMBERS\b",text,re.I)
    return int(m.group(1).replace(",","")) if m else None

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--unresolved",required=True)
    p.add_argument("--priority",required=True)
    p.add_argument("--below",required=True)
    p.add_argument("--out",default="output/group_html_probe")
    a=p.parse_args()
    un=json.loads(Path(a.unresolved).read_text(encoding="utf-8"))
    pr=json.loads(Path(a.priority).read_text(encoding="utf-8"))
    bl=json.loads(Path(a.below).read_text(encoding="utf-8"))
    samples=[]
    for x in strat([r for r in un if isinstance(r.get("group_short_id"),int)],30):
        samples.append({**x,"sample_type":"unresolved"})
    for x in (pr[:5]+bl[:5]):
        samples.append({**x,"sample_type":"measured_reference"})
    s=requests.Session();s.headers.update({"User-Agent":"Mozilla/5.0 (compatible; GameTrendRadar-GroupHtmlProbe/1.0)"})
    started=time.monotonic();results=[];count429=0
    for i,row in enumerate(samples,1):
        gid64=BASE+int(row["group_short_id"])
        rec={"appid":row["appid"],"name":row.get("name"),"sample_type":row["sample_type"],"group_id64":str(gid64),"first_source_followers":row.get("third_party_followers")}
        try:
            r=s.get(f"https://steamcommunity.com/gid/{gid64}",timeout=(10,30),allow_redirects=True)
            rec["http"]=r.status_code;rec["final_url"]=r.url
            if r.status_code==429:count429+=1
            if r.status_code==200:
                rec["html_members"]=parse_members(r.text)
                rec["html_bytes"]=len(r.content)
        except requests.RequestException as e:
            rec["error_type"]=type(e).__name__
        results.append(rec)
        if i==1 or i%10==0 or i==len(samples):
            ok=sum(isinstance(x.get("html_members"),int) for x in results)
            print(f"HTML_PROBE_PROGRESS {i}/{len(samples)} members={ok} 429={count429} elapsed={round(time.monotonic()-started,1)}s",flush=True)
        time.sleep(0.8)
    unresolved=[x for x in results if x["sample_type"]=="unresolved"]
    refs=[x for x in results if x["sample_type"]=="measured_reference" and isinstance(x.get("html_members"),int) and isinstance(x.get("first_source_followers"),int)]
    report={
        "requests":len(results),"unresolved_sample":len(unresolved),
        "unresolved_html_coverage":sum(isinstance(x.get("html_members"),int) for x in unresolved),
        "unresolved_html_ge4000":sum(isinstance(x.get("html_members"),int) and x["html_members"]>=4000 for x in unresolved),
        "reference_compared":len(refs),
        "reference_exact_matches":sum(x["html_members"]==x["first_source_followers"] for x in refs),
        "reference_mean_abs_difference":round(sum(abs(x["html_members"]-x["first_source_followers"]) for x in refs)/len(refs),2) if refs else None,
        "http_429":count429,"duration_seconds":round(time.monotonic()-started,2),
        "source":"Steam Community group HTML MEMBERS display",
    }
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    (out/"results.json").write_text(json.dumps(results,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("HTML_PROBE_FINAL "+json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)

if __name__=="__main__":main()
