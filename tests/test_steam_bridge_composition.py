"""Canonical Steam bridges exercised with fake HTTP and temporary files only."""

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
STAMP = "2026-10-09T10:00:00Z"
GROUP_BASE = 103582791429521408


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def candidate(appid, **extras):
    return {
        "appid": appid, "name": f"Game {appid}", "release_start": "2026-10-09",
        "release_end": "2026-10-09", "release_precision": "day",
        "release_display_precision": "date_full", "sexual_content_screened": True,
        "store_url": f"https://store.steampowered.com/app/{appid}/", **extras,
    }


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        return deepcopy(self.payload)


def test_formal_composition_imports_and_default_services_work_with_all_scripts_blocked():
    program = """
import importlib
import importlib.abc
import sys
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'scripts' or fullname.startswith('scripts.'):
            raise AssertionError('legacy script dependency: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
for name in (
    'radar_backend.jobs.candidates', 'radar_backend.jobs.official_followers',
    'radar_backend.jobs.resolve_official_groups',
    'radar_backend.jobs.reconcile_twitch_official_queue',
    'radar_backend.publication.daily_state', 'radar_backend.publication.steam',
    'radar_backend.publication.official_checkpoint',
    'radar_backend.adapters.queue_inputs', 'radar_backend.adapters.official_groups',
    'radar_backend.adapters.scheduler_status', 'radar_backend.adapters.follower_prefilter',
    'radar_backend.adapters.partial_catalog', 'radar_backend.domain.daily_schedule',
):
    importlib.import_module(name)
from radar_backend.jobs import candidates, official_followers
from radar_backend.adapters import follower_prefilter, partial_catalog, queue_inputs
runtime = candidates.default_runtime()
assert runtime.sources.scan_batch is follower_prefilter.scan_batch
assert runtime.sources.merge_partial_segment is partial_catalog.merge_partial_segment
services = official_followers.default_services(official_followers.default_paths())
assert callable(services.make_queue) and callable(services.git_push)
assert not any(name == 'scripts' or name.startswith('scripts.') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_canonical_queue_inputs_share_the_formal_official_paths_and_job_owners():
    from radar_backend.adapters import queue_inputs, official_groups, scheduler_status
    from radar_backend.jobs import official_followers, resolve_official_groups, reconcile_twitch_official_queue
    from radar_backend.adapters import twitch_intake

    paths = official_followers.default_paths()
    for field, constant in (
        ("checkpoint", "CHECKPOINT"), ("frozen", "FROZEN"), ("eligible", "ELIGIBLE"),
        ("prefilter", "PREFILTER"), ("official_cache", "OFFICIAL_CACHE"),
        ("original_official", "ORIGINAL_OFFICIAL"), ("master", "MASTER"),
    ):
        assert getattr(paths, field) == getattr(queue_inputs, constant)
    assert official_groups.worker is queue_inputs
    assert scheduler_status.worker is queue_inputs
    assert resolve_official_groups.worker is queue_inputs
    assert resolve_official_groups.collect is official_groups.collect
    assert reconcile_twitch_official_queue.collect is twitch_intake.collect
    assert reconcile_twitch_official_queue.apply_queue_batch is twitch_intake.apply_queue_batch


def install_queue_inputs(tmp_path, monkeypatch):
    from radar_backend.adapters import queue_inputs

    monkeypatch.chdir(tmp_path)
    for name, value in {
        "ROOT": tmp_path / "current", "FROZEN": tmp_path / "frozen",
        "CHECKPOINT": tmp_path / "current/checkpoint.json",
        "ELIGIBLE": tmp_path / "data/eligible.json",
        "PREFILTER": tmp_path / "data/prefilter.json",
        "OFFICIAL_CACHE": tmp_path / "data/cache.json",
        "ORIGINAL_OFFICIAL": tmp_path / "other/checkpoint.json",
        "MASTER": tmp_path / "data/master.json",
    }.items():
        monkeypatch.setattr(queue_inputs, name, value)
    monkeypatch.setattr(queue_inputs, "clock", lambda: NOW)
    frozen = [
        {"appid": 10000 + index, "name": f"Frozen {index}", "release_date": "2026-09-22"}
        for index in range(1317)
    ]
    old_groups = [{"appid": 10000 + index, "group_short_id": None} for index in range(1358)]
    legacy = {
        "cohort": "steam_fresh_20260922_post_adult_1317_near_release",
        "official_results": {str(row["appid"]): {"official_followers": 0} for row in frozen},
    }
    checkpoint = {"pending_candidates": {}, "official_results": {}, "attempt_events": []}
    values = {
        queue_inputs.CHECKPOINT: checkpoint,
        queue_inputs.FROZEN / "source_queue.json": frozen,
        queue_inputs.FROZEN / "checkpoint.json": legacy,
        queue_inputs.FROZEN / "source_unresolved.json": old_groups,
        queue_inputs.ELIGIBLE: {"screened_at": STAMP, "games": [candidate(1), candidate(2), candidate(3)]},
        queue_inputs.PREFILTER: {
            "updated_at": STAMP, "complete": True,
            "games": {"1": {"third_party_followers": 4000, "group_short_id": None},
                      "2": {"third_party_followers": None, "group_short_id": None},
                      "3": {"third_party_followers": 3999, "group_short_id": None}},
        },
        queue_inputs.OFFICIAL_CACHE: {"games": {}},
        queue_inputs.ORIGINAL_OFFICIAL: {"verified": {}},
        tmp_path / "data/steam_candidate_state.json": {"updated_at": STAMP},
        tmp_path / "data/twitch_steam_import_state.json": {"updated_at": STAMP},
    }
    for path, value in values.items():
        write(path, value)
    return queue_inputs, checkpoint


def test_fake_group_receipts_apply_to_fresh_queue_and_dashboard_only_writes_destination(tmp_path, monkeypatch):
    from radar_backend.adapters import official_groups, scheduler_status

    worker, checkpoint = install_queue_inputs(tmp_path, monkeypatch)
    projected, candidates = official_groups.current_queue(checkpoint)
    assert [row["appid"] for row in candidates] == [1, 2]
    assert checkpoint == {"pending_candidates": {}, "official_results": {}, "attempt_events": []}

    class Session:
        def __init__(self):
            self.headers = {}
            self.calls = []

        def get(self, url, **options):
            self.calls.append((url, options))
            appid = int(options["params"]["vanityurl"])
            return Response({"response": {"success": 1, "steamid": str(GROUP_BASE + appid)}})

    client = Session()
    elapsed = [0.0]
    sleeps = []

    def sleep(value):
        sleeps.append(value)
        elapsed[0] += value

    before = deepcopy(projected)
    batch = official_groups.collect(
        projected, candidates, api_key="fake offline key", session=client, now=NOW,
        monotonic=lambda: elapsed[0], sleep=sleep,
    )
    assert projected == before
    assert batch["requests_this_run"] == 2 and batch["stop_reason"] == "complete"
    assert sleeps == [1.0]
    assert "fake offline key" not in json.dumps(batch)
    for url, options in client.calls:
        assert url == official_groups.API_URL
        assert options["headers"] == {"x-webapi-key": "fake offline key"}
        assert "key" not in options["params"]
        assert options["allow_redirects"] is False

    changed = deepcopy(projected)
    changed["pending_candidates"]["1"]["release_date"] = "2026-10-10"
    stale = official_groups.apply_batch(changed, batch, eligible_appids=[1, 2])
    assert stale["pending_candidates"]["1"]["group_id64"] is None
    assert stale["pending_candidates"]["2"]["group_id64"] == str(GROUP_BASE + 2)
    excluded = official_groups.apply_batch(projected, batch, eligible_appids=[])
    assert excluded["pending_candidates"]["1"]["group_id64"] is None

    applied = official_groups.apply_batch(projected, batch, eligible_appids=[1, 2])
    assert projected == before
    assert applied["pending_candidates"]["1"]["group_id64"] == str(GROUP_BASE + 1)
    assert applied["pending_candidates"]["2"]["group_id64"] == str(GROUP_BASE + 2)
    assert official_groups.apply_batch(applied, batch, eligible_appids=[1, 2]) == applied
    worker.save(worker.CHECKPOINT, applied)
    source_bytes = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}

    refreshed, rows = official_groups.current_queue(worker.read(worker.CHECKPOINT))
    assert [row["group_id64"] for row in rows] == [str(GROUP_BASE + 1), str(GROUP_BASE + 2)]
    assert refreshed["pending_candidates"]["1"]["group_resolution"]["status"] == "resolved"
    destination = tmp_path / "dashboard/status.json"
    status = scheduler_status.export_status(output=destination, now=NOW)
    assert json.loads(destination.read_text(encoding="utf-8")) == status
    assert status["summary"]["ready_pending"] == 2
    assert status["summary"]["followers_ready_pending"] == 2
    assert status["summary"]["awaiting_group_pending"] == 0
    assert [row["appid"] for row in status["queue"]] == [1, 2]
    assert all(row["state"] == "waiting" for row in status["queue"])
    assert status["source"]["queue_projection"] == "official_collector_make_queue"
    assert status["source"]["candidate_state_updated_at"] == STAMP
    assert status["source"]["twitch_import_updated_at"] == STAMP
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes
    assert set(tmp_path.rglob("*.json")) == set(source_bytes) | {destination}


@pytest.mark.parametrize("failure", ["mapping", "bulk"])
def test_prefilter_http_failure_keeps_entire_window_then_real_partial_merge_preserves_evidence(tmp_path, monkeypatch, failure):
    from radar_backend.adapters import follower_prefilter, partial_catalog, public_catalog
    from radar_backend.state.adult_exclusions import excluded_appids

    monkeypatch.chdir(tmp_path)
    sleeps = []
    monkeypatch.setattr(follower_prefilter, "time", SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(follower_prefilter, "_now", lambda: STAMP)

    class Session:
        def __init__(self, failed=False):
            self.failed = failed
            self.headers = {}
            self.calls = []

        def get(self, url, **options):
            self.calls.append(("GET", url, options))
            appid = int(options["params"]["vanityurl"])
            if self.failed and failure == "mapping" and appid == 2:
                return Response({}, 403)
            if appid == 3:
                return Response({"response": {"success": 42}})
            return Response({"response": {"success": 1, "steamid": str(GROUP_BASE + 100 + appid)}})

        def post(self, url, **options):
            self.calls.append(("POST", url, options))
            if self.failed and failure == "bulk":
                return Response({}, 403)
            return Response({"data": [{"id": "101", "members": 4000}, {"id": "102", "members": 3999}]})

    catalog = [candidate(1), candidate(2), candidate(3)]
    prefilter = {"version": 1, "bulk_parser_version": 2, "next_index": 0,
                 "games": {"99": {"third_party_followers": None, "priority": True}}}
    original = deepcopy(prefilter)
    with pytest.raises(RuntimeError, match="priority cursor unchanged"):
        follower_prefilter.scan_batch(
            catalog, prefilter, steam_api_key="fake offline key", initial_index=0,
            session=Session(failed=True),
        )
    assert prefilter == original
    result = follower_prefilter.scan_batch(
        catalog, prefilter, steam_api_key="fake offline key", initial_index=0, session=Session(),
    )
    assert result == {"start_index": 0, "next_index": 3, "screened": 3,
                      "priority": 1, "missing": 1, "complete": True}
    assert prefilter["threshold"] == 4000
    assert prefilter["games"]["1"]["priority"] is True
    assert prefilter["games"]["2"]["priority"] is False
    assert prefilter["games"]["3"]["third_party_followers"] is None
    assert prefilter["games"]["99"]["priority"] is False
    priority = follower_prefilter.pending_priorities(catalog, prefilter, {}, min_start_index=0)
    assert priority == [catalog[0]]
    assert priority[0] is catalog[0]
    assert follower_prefilter.pending_priorities(catalog, prefilter, {"1": {}}, min_start_index=0) == []
    assert all("followers" not in game for game in catalog)

    ledger = tmp_path / "data/adult.json"
    write(ledger, {"criteria": {"exclude_content_descriptor_ids": [3, 4]},
                   "games": [{"appid": 9, "excluded_descriptor_ids": [3]}]})
    monkeypatch.setattr(partial_catalog, "excluded_appids", lambda: excluded_appids(ledger))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(public_catalog, "datetime", FrozenDateTime)
    proof = {"schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 1,
             "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-09T08:00:00Z",
             "source_frontend_commit": "a" * 40,
             "source_enrollment": {"source": "igdb_first_release_date", "observed_at": "2026-10-08T08:00:00Z",
                                   "viewer_count": 8000, "min_viewers": 7000}}
    existing = [candidate(1, followers=6000, twitch_admission=proof, steam_type="game",
                          categories=[{"id": 1, "description": "Multi-player"}],
                          categories_source="Steam IStoreBrowseService/GetItems supported_player_categoryids",
                          categories_checked_at="2026-10-09T09:00:00Z"),
                candidate(4, followers=8000, release_start="2000-01-01"),
                candidate(9, followers=9000)]
    incoming = [{**priority[0], "followers": 7000}]
    before = deepcopy((existing, incoming))
    merged = partial_catalog.merge_partial_segment(existing, incoming, today=date(2026, 10, 9))
    assert [game["appid"] for game in merged] == [4, 1]
    assert merged[0]["release_start"] == "2000-01-01"
    assert merged[1]["followers"] == 7000
    assert merged[1]["twitch_admission"] == proof
    assert merged[1]["categories"] == existing[0]["categories"]
    assert merged[1]["categories"] is existing[0]["categories"]
    assert (existing, incoming) == before
