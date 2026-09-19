"""GitHub Actions Steam collector supervisor (checks at cron + 180 seconds).

GitHub cron cannot run more frequently than five minutes. One supervisor job
checks on its five-minute start AND after a three-minute wait, giving an
approximately 2–3 minute checking gap while allowing GitHub scheduling delays.
The supervisor never scrapes Steam or reads API keys for Steam.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

LOG = logging.getLogger(__name__)
COLLECTOR_WORKFLOW = "steam-two-phase.yml"
LEGACY_WORKFLOW = "update-steam.yml"
ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}


def api(
    method: str, endpoint: str, *, payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        raise RuntimeError("GH_TOKEN and GITHUB_REPOSITORY are required")
    url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/") + (
        f"/repos/{repo}/{endpoint.lstrip('/')}"
    )
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "game-trend-radar-steam-supervisor",
    }
    request = urllib.request.Request(
        url, data=data, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as result:
            raw = result.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Do not include request URL, headers, or API token in logs.
        raise RuntimeError(
            f"GitHub API {method} {endpoint.split('?')[0]} returned "
            f"HTTP {error.code}"
        ) from None


def fetch_runs(workflow_name: str) -> list[dict[str, Any]]:
    response = api(
        "GET", f"actions/workflows/{workflow_name}/runs?per_page=20"
    )
    if not isinstance(response, dict) or not isinstance(response.get("workflow_runs"), list):
        raise RuntimeError(f"Workflow run list was unavailable: {workflow_name}")
    return response["workflow_runs"]


def fetch_state() -> dict[str, Any]:
    response = api("GET", "contents/data/steam_candidate_state.json?ref=main")
    if not isinstance(response, dict) or response.get("encoding") != "base64":
        raise RuntimeError("Steam candidate state could not be read")
    content = response.get("content")
    if not isinstance(content, str):
        raise RuntimeError("Steam candidate state has no content")
    state = json.loads(base64.b64decode(content))
    if not isinstance(state, dict):
        raise RuntimeError("Steam candidate state is not JSON object")
    return state


def validate_state(state: dict[str, Any]) -> None:
    if state.get("mode") != "two_phase_steam_year":
        raise RuntimeError("Steam state is not two-phase; refusing to dispatch")
    phase = state.get("phase")
    days = int(state.get("days_scanned", -1))
    if phase not in {"discovery", "followers", "complete"} or not 0 <= days <= 365:
        raise RuntimeError(f"Steam state has invalid phase or day count: {phase}, {days}")
    if phase == "discovery":
        anchor = date.fromisoformat(state["anchor_date"])
        next_date = date.fromisoformat(state["next_date"])
        end_date = date.fromisoformat(state["end_date"])
        if next_date != anchor + timedelta(days=days) or end_date != anchor + timedelta(days=364):
            raise RuntimeError("Steam discovery cursor/date range inconsistent; not dispatching")
        if days >= 365:
            raise RuntimeError("365 dates scanned but still in discovery phase")
        if int((state.get("last_attempt") or {}).get("followers_queried") or 0) != 0:
            raise RuntimeError("Unexpected Followers lookups during discovery")


def check_once() -> str:
    """Read live workflow/state and dispatch only when no collector is active."""
    new_runs = fetch_runs(COLLECTOR_WORKFLOW)
    old_runs = fetch_runs(LEGACY_WORKFLOW)
    active = [
        (run.get("id"), run.get("status"))
        for run in new_runs + old_runs
        if run.get("status") in ACTIVE_STATUSES
    ]
    if active:
        LOG.info("Steam collector still active or queued (%s); skip.", active)
        return "busy"
    state = fetch_state()
    validate_state(state)
    phase = state["phase"]
    days = int(state["days_scanned"])
    if phase == "complete" and state.get("initial_complete"):
        LOG.info("Steam two-stage initialization fully complete; no new dispatch.")
        return "done"
    if new_runs and (
        new_runs[0].get("status") != "completed"
        or new_runs[0].get("conclusion") != "success"
    ):
        raise RuntimeError(
            "Most recent Steam collector run did not succeed; "
            "supervisor stopped to avoid repeated failures"
        )
    last_attempt = state.get("last_attempt") or {}
    if phase == "followers" and int(last_attempt.get("failures") or 0) > 0:
        raise RuntimeError("Previous Followers batch failed; inspect logs before retry")
    if phase == "discovery":
        LOG.info(
            "Steam candidate discovery %s/365 days; next=%s; candidates=%s. "
            "Dispatching all remaining release dates up to day 365 (no Followers).",
            days, state.get("next_date"),
            last_attempt.get("new_catalog_total", "unknown"),
        )
    else:
        LOG.info(
            "Steam Followers after 365-day discovery, next index=%s. "
            "Dispatching one up-to-50-new-Followers batch.",
            state.get("next_follower_index"),
        )
    api(
        "POST",
        f"actions/workflows/{COLLECTOR_WORKFLOW}/dispatches",
        payload={"ref": "main"},
    )
    LOG.info("GitHub workflow_dispatch for %s accepted.", COLLECTOR_WORKFLOW)
    return "dispatched"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    first = check_once()
    if first == "done":
        return
    LOG.info("Next GitHub supervisor check in 180 seconds, if still running.")
    time.sleep(180)
    check_once()


if __name__ == "__main__":
    main()
