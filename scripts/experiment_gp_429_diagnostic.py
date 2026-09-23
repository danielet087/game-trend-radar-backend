"""Two-request Games Popularity 429 diagnostic; never log key or response body."""
import json
import os
import requests

URL="https://games-popularity.com/swagger/api/game/latest/730"
KEY=os.environ.get("GAMES_POPULARITY_API_KEY","").strip()
if not KEY:
    raise SystemExit("Missing Games Popularity key")
S=requests.Session()
for label,params in [("with_key",{"apiKey":KEY}),("anonymous",{})]:
    try:
        resp=S.get(URL,params=params,timeout=(6,13),
                   headers={"User-Agent":"GameTrendRadar-RateLimitDiagnostic/1.0"})
        h=resp.headers
        selected={x:h.get(x) for x in
                  ("Retry-After","X-RateLimit-Limit","X-RateLimit-Remaining",
                   "X-RateLimit-Reset","X-RateLimit-Daily-Remaining")
                  if h.get(x) is not None}
        print("GP_DIAGNOSTIC",json.dumps({"mode":label,"http":resp.status_code,
              "retry_headers":selected,"content_type":h.get("Content-Type")}),flush=True)
    except requests.RequestException as e:
        print("GP_DIAGNOSTIC",json.dumps({"mode":label,"error_type":type(e).__name__}),flush=True)
