"""Validate this run's daily Steam batch before its public update.

Stage 2 (third-party prescreen) must never query Steam XML, and every public
game must have at least 5,000 official Followers or a valid Twitch admission.
A Twitch-admitted game may have unknown (null) Followers, so the admission is
checked before the count. A run that produced no batch (discovery already
handed off to the hourly Followers queue) has nothing to validate; its
publication step skips the same missing batch as discovery-only.
"""
from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
from pathlib import Path

from scripts.twitch_steam_admission import is_twitch_qualified

STATE = Path("data/steam_candidate_state.json")
BATCH = Path("output/steam_upcoming.json")


def publicly_qualified(game) -> bool:
    if is_twitch_qualified(game):
        return True
    followers = game.get("followers")
    return followers is not None and int(followers) >= 5000


def validate(state: dict, batch: Path) -> str:
    attempt = state.get("last_attempt") or {}
    phase = attempt.get("phase")
    if phase not in {"prefilter", "followers"}:
        return "Steam discovery/initialization completion; no new XML batch."
    if not batch.is_file():
        return f"No Steam batch was produced in this run (last attempt: {phase}); discovery only."
    payload = json.loads(batch.read_text(encoding="utf-8"))
    unqualified = [game.get("appid") for game in payload["games"] if not publicly_qualified(game)]
    assert not unqualified, f"Games below 5,000 Followers without Twitch admission: {unqualified[:10]}"
    if phase == "prefilter":
        assert int(attempt.get("fresh_follower_requests", -1)) == 0, "Stage 2 must NEVER query Steam XML"
        assert 0 <= int(state["prefilter_next_index"]) <= int(attempt["candidate_count"])
        assert payload["initialization"]["prefilter_next_index"] == state["prefilter_next_index"]
        return (f"Third-party >=4000 prescreen: {state['prefilter_screened_count']} "
                f"/{attempt['candidate_count']}; Steam XML queries: 0")
    fresh = int(attempt.get("fresh_follower_requests", -1))
    assert 0 <= fresh <= 50, f"Unexpected fresh Followers requests: {fresh}"
    start, end = int(attempt["start_index"]), int(attempt["next_index"])
    assert end > start or state.get("initial_complete"), "No new verified priority games or completion"
    assert payload["initialization"]["prefilter_complete"] is True
    assert payload["initialization"]["verified_priority_count"] == end
    assert payload["initialization"]["days_scanned"] == 365
    assert payload["count"] == len(payload["games"])
    return f"Steam XML verification: {fresh} new, shortlisted {start}->{end}; officially qualified {payload['count']}"


def main() -> int:
    print(validate(json.loads(STATE.read_text(encoding="utf-8")), BATCH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
