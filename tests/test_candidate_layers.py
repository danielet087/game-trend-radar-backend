"""Candidate layers preserve gates, restart state, and honest durability reports."""
from __future__ import annotations

import ast
import copy
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.application import candidates as application
from radar_backend.domain import candidates as domain
from radar_backend.jobs.candidates import build_parser
from radar_backend.state.candidate_store import load_json, save_json, write_json


REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)


class MemoryState:
    def __init__(self, values=None):
        self.values = copy.deepcopy(values or {})
        self.saved = []
        self.outputs = {}

    def load(self, path, default):
        return copy.deepcopy(self.values.get(str(path), default))

    def save(self, path, payload):
        self.values[str(path)] = copy.deepcopy(payload)
        self.saved.append(str(path))

    def write_output(self, payload, path):
        self.outputs[str(path)] = copy.deepcopy(payload)


def runtime(store, **operations):
    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected external source call")

    sources = {
        name: unexpected for name in application.CandidateSourcePort.__annotations__
    }
    sources.update(
        api_key=lambda: "offline-test-key",
        excluded_appids=lambda: set(),
        is_disallowed=lambda row, blocked: int(row["appid"]) in blocked,
        session_factory=lambda: SimpleNamespace(headers={}),
    )
    sources.update(operations)
    return application.CandidateRuntime(
        sources=SimpleNamespace(**sources), state=store,
        today=lambda: TODAY, utcnow=lambda: NOW, sleep=unexpected,
    )


def candidate(appid=101):
    return {
        "appid": appid, "name": f"Game {appid}",
        "release_start": "2026-11-01", "release_end": "2026-11-01",
        "release_raw": "2026-11-01", "release_precision": "day",
        "store_url": f"https://store.steampowered.com/app/{appid}/",
    }


def prepared_state(phase="prefilter"):
    state = domain.fresh_state(TODAY, 365)
    state.update(phase=phase, days_scanned=365)
    return state


def eligible_catalog():
    rows = [candidate()]
    return {"games": rows, "count": 1, "date_precision_complete": True,
            "date_precision_eligible": rows}


def test_domain_and_application_import_boundaries():
    """Rules cannot silently grow transport/state reads during later changes."""
    domain_tree = ast.parse(Path(domain.__file__).read_text())
    application_tree = ast.parse(Path(application.__file__).read_text())
    for tree in (domain_tree, application_tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(name.startswith(("requests", "scripts.", "collectors.",
                                             "radar_backend.adapters.",
                                             "radar_backend.state.")) for name in names)
    assert "open(" not in Path(domain.__file__).read_text()


def test_discovery_source_failure_keeps_restart_cursor_and_files_unchanged():
    state = domain.fresh_state(TODAY, 365)
    catalog = {"games": []}
    store = MemoryState({"state.json": state, "catalog.json": catalog})
    source = Mock(side_effect=RuntimeError("HTTP 429"))
    args = build_parser().parse_args([
        "--state", "state.json", "--catalog", "catalog.json", "--master", "master.json",
        "--batch-days", "1", "--search-interval", "0",
    ])
    with pytest.raises(RuntimeError, match="HTTP 429"):
        application.execute_pipeline(args, runtime(store, query_day=source))
    assert store.saved == []
    assert store.outputs == {}
    assert store.values["state.json"] == state
    assert store.values["catalog.json"] == catalog


def test_incomplete_display_date_gate_fails_before_loading_external_ledger():
    ledger = Mock(side_effect=RuntimeError("Ledger unavailable"))
    state = domain.fresh_state(TODAY, 365)
    with pytest.raises(RuntimeError, match="date gate incomplete"):
        application.active_candidate_rows(
            {"games": [candidate()]}, state,
            runtime(MemoryState(), excluded_appids=ledger),
        )
    ledger.assert_not_called()


def test_failed_prescreen_then_retry_persists_only_the_complete_window():
    state = prepared_state()
    catalog = eligible_catalog()
    original_pre = {"version": 1, "next_index": 0, "complete": False, "games": {}}
    store = MemoryState({"state.json": state, "catalog.json": catalog,
                         "pre.json": original_pre})
    args = build_parser().parse_args([
        "--state", "state.json", "--catalog", "catalog.json", "--master", "master.json",
        "--prefilter-state", "pre.json", "--prefilter-batch-size", "1",
        "--prefilter-request-interval", "0", "--output", "public.json",
    ])

    def failed_scan(rows, pre, **kwargs):
        pre["next_index"] = 1
        pre["games"]["101"] = {"third_party_followers": 4500}
        raise RuntimeError("Third-party response incomplete")

    with pytest.raises(RuntimeError, match="incomplete"):
        application.execute_pipeline(args, runtime(store, scan_batch=failed_scan))
    assert store.values["pre.json"] == original_pre
    assert store.values["state.json"] == state
    assert store.saved == []

    def successful_scan(rows, pre, **kwargs):
        assert pre["next_index"] == 0
        pre["next_index"] = 1
        pre["games"]["101"] = {"third_party_followers": 4500}
        return {"start_index": 0, "next_index": 1, "screened": 1,
                "priority": 1, "missing": 0}

    result = application.execute_pipeline(
        args, runtime(store, scan_batch=successful_scan,
                      filter_confirmed_master_games=lambda master, rows, **kw: master,
                      is_twitch_qualified=lambda row: False),
    )
    assert store.values["pre.json"]["complete"] is True
    assert store.values["pre.json"]["games"]["101"]["priority"] is True
    assert store.values["state.json"]["phase"] == "followers"
    assert result["job_result"]["status"] == "partial"
    assert result["job_result"]["state_persisted"] is False
    assert store.outputs["public.json"]["initialization"]["prefilter_complete"] is True
    assert store.outputs["public.json"]["filter"]["min_followers"] == 5000


def test_cached_completion_keeps_published_history_and_does_not_claim_git_success():
    state = prepared_state("followers")
    master = {"games": [candidate(), dict(candidate(99), release_start="2026-10-01"),
                        candidate(999)]}
    store = MemoryState({
        "state.json": state, "catalog.json": eligible_catalog(), "master.json": master,
        "pre.json": {"complete": True, "next_index": 1,
                     "games": {"101": {"priority": True, "third_party_followers": 4000}}},
        "cache.json": {"games": {"101": {"followers": 5100}}},
    })
    args = build_parser().parse_args([
        "--state", "state.json", "--catalog", "catalog.json", "--master", "master.json",
        "--prefilter-state", "pre.json", "--follower-cache", "cache.json",
        "--output", "public.json",
    ])
    result = application.execute_pipeline(
        args, runtime(store, filter_confirmed_master_games=lambda master, rows, **kw: master,
                      is_twitch_qualified=lambda row: False),
    )
    assert result["phase"] == "complete"
    assert result["job_result"]["collection_complete"] is True
    assert result["job_result"]["state_persisted"] is False
    assert result["job_result"]["successful"] is False
    assert [row["appid"] for row in store.outputs["public.json"]["games"]] == [101, 99]
    assert store.values["master.json"] == master


def test_local_state_write_failure_never_returns_successful_job_result():
    store = MemoryState({"state.json": dict(prepared_state("complete"), initial_complete=True)})
    store.save = Mock(side_effect=OSError("Disk full"))
    args = build_parser().parse_args([
        "--state", "state.json", "--catalog", "catalog.json", "--master", "master.json",
    ])
    with pytest.raises(OSError, match="Disk full"):
        application.execute_pipeline(args, runtime(store))
    assert store.outputs == {}


def test_unresolved_observation_and_boundary_counts_remain_distinct():
    games = {
        "1": {"third_party_followers": None, "priority": True},
        "2": {"third_party_followers": 3999, "priority": True},
        "3": {"third_party_followers": 4000, "priority": False},
    }
    domain.normalize_prefilter_entries(games)
    assert games["1"] == {"third_party_followers": None, "priority": False,
                          "scheduling_band": "unresolved"}
    assert [row["appid"] for row in domain.priority_rows(
        [candidate(1), candidate(2), candidate(3)], {"games": games},
    )] == [3]


def test_json_store_keeps_legacy_fallback_and_atomic_replace(tmp_path):
    path = tmp_path / "state.json"
    assert load_json(path, {"phase": "discovery"}) == {"phase": "discovery"}
    path.write_text("{malformed")
    assert load_json(path, {"phase": "discovery"}) == {"phase": "discovery"}
    payload = {"phase": "prefilter", "name": "繁體名稱"}
    save_json(path, payload)
    assert load_json(path, {}) == payload
    assert not path.with_suffix(".json.tmp").exists()
    public_path = tmp_path / "output" / "public.json"
    assert write_json(payload, public_path) == public_path
    assert load_json(public_path, {}) == payload


@pytest.mark.parametrize("command", [
    ["scripts/steam_candidate_pipeline.py"],
    ["-m", "scripts.steam_candidate_pipeline"],
    ["-m", "radar_backend.jobs.candidates"],
])
def test_compatible_cli_help_does_not_collect(command):
    result = subprocess.run(
        [sys.executable, *command, "--help"], cwd=REPO,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--prefilter-batch-size" in result.stdout
    assert "--max-fresh-requests-per-run" in result.stdout
