"""A supervisor may start a new batch only if GitHub and saved state agree."""
import base64
import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from scripts import steam_candidate_supervisor as sup


def state(days=150, phase="discovery"):
    anchor = date(2026, 9, 19)
    return {
        "mode": "two_phase_steam_year",
        "phase": phase,
        "days_scanned": days,
        "anchor_date": anchor.isoformat(),
        "end_date": (anchor + timedelta(days=364)).isoformat(),
        "next_date": (anchor + timedelta(days=days)).isoformat(),
        "next_follower_index": 0,
        "initial_complete": False,
        "last_attempt": {"followers_queried": 0, "new_catalog_total": 9772},
    }


def run(status="completed", conclusion="success"):
    return {"id": 1, "status": status, "conclusion": conclusion}


def test_busy_new_or_old_steam_workflow_prevents_dispatch():
    for new, old in [([run("in_progress", None)], []),
                     ([run()], [run("queued", None)]),
                     ([run("waiting", None)], [])]:
        with (
            patch.object(sup, "fetch_runs", side_effect=[new, old]),
            patch.object(sup, "fetch_state") as read,
            patch.object(sup, "api") as api,
        ):
            assert sup.check_once() == "busy"
            read.assert_not_called()
            api.assert_not_called()


def test_completed_success_dispatches_one_new_workflow():
    with (
        patch.object(sup, "fetch_runs", side_effect=[[run()], []]),
        patch.object(sup, "fetch_state", return_value=state()),
        patch.object(sup, "api", return_value=None) as api,
    ):
        assert sup.check_once() == "dispatched"
    api.assert_called_once_with(
        "POST", "actions/workflows/steam-two-phase.yml/dispatches",
        payload={"ref": "main"},
    )


def test_failed_latest_batch_never_dispatches_again():
    with (
        patch.object(sup, "fetch_runs", side_effect=[[run("completed", "failure")], []]),
        patch.object(sup, "fetch_state", return_value=state()),
        patch.object(sup, "api") as api,
    ):
        with pytest.raises(RuntimeError, match="did not succeed"):
            sup.check_once()
        api.assert_not_called()


def test_discovery_cursor_must_match_days_scanned():
    broken = state(days=150)
    broken["next_date"] = "2026-09-19"
    with pytest.raises(RuntimeError, match="inconsistent"):
        sup.validate_state(broken)


def test_discovery_requires_zero_follower_requests():
    broken = state(days=150)
    broken["last_attempt"]["followers_queried"] = 4
    with pytest.raises(RuntimeError, match="Unexpected Followers"):
        sup.validate_state(broken)


def test_completion_stops_automatically():
    done = state(days=365, phase="complete")
    done["initial_complete"] = True
    with (
        patch.object(sup, "fetch_runs", side_effect=[[run()], []]),
        patch.object(sup, "fetch_state", return_value=done),
        patch.object(sup, "api") as api,
    ):
        assert sup.check_once() == "done"
    api.assert_not_called()


def test_followers_begin_only_after_discovery():
    follower = state(days=365, phase="followers")
    follower["last_attempt"] = {"phase": "discovery", "followers_queried": 0}
    with (
        patch.object(sup, "fetch_runs", side_effect=[[run()], []]),
        patch.object(sup, "fetch_state", return_value=follower),
        patch.object(sup, "api") as api,
    ):
        assert sup.check_once() == "dispatched"
        api.assert_called_once()


def test_followers_failures_block_next_batch():
    follower = state(days=365, phase="followers")
    follower["last_attempt"] = {"failures": 1}
    with (
        patch.object(sup, "fetch_runs", side_effect=[[run()], []]),
        patch.object(sup, "fetch_state", return_value=follower),
        patch.object(sup, "api") as api,
    ):
        with pytest.raises(RuntimeError, match="Followers batch failed"):
            sup.check_once()
        api.assert_not_called()


def test_api_decodes_remote_state_without_logging_token():
    payload = state()
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    with patch.object(sup, "api", return_value={"encoding": "base64", "content": encoded}):
        assert sup.fetch_state() == payload


def test_poll_twice_three_minutes_apart(monkeypatch):
    calls = []
    monkeypatch.setattr(sup, "check_once", lambda: calls.append("checked") or "busy")
    monkeypatch.setattr(sup.time, "sleep", lambda seconds: calls.append(seconds))
    sup.main()
    assert calls == ["checked", 180, "checked"]
