"""Growth use-case ports preserve evidence, bounded work and delivery semantics."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import ast
import subprocess
import sys

import pytest

from radar_backend.application.growth import collect_observations
from radar_backend.domain.growth import eligible, growth_collection_complete, latest_measurement
from radar_backend.domain.official_queue import GROUP_BASE
from radar_backend.publication.growth import stamp_growth_publication
from radar_backend.state.growth import GrowthProgressWriter
from radar_backend.state.official_followers import CooldownStore, OfficialFollowerCache
from scripts import collect_public_growth as legacy

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def row(aid):
    return {"appid": aid, "followers": 6000, "release_start": "2026-10-08"}


def context(*ids):
    checkpoint = {"pending_candidates": {str(aid): {"appid": aid, "group_id64": str(GROUP_BASE + aid)}
                                         for aid in ids}, "other_job": {"preserved": True}}
    return checkpoint, OfficialFollowerCache(checkpoint), CooldownStore(checkpoint)


class Source:
    def __init__(self):
        self.groups = []

    def fetch(self, gid):
        self.groups.append(gid)
        return SimpleNamespace(status="ok", http=200, error_type=None,
                               observed_at=NOW, followers=6500)


def execute(rows, checkpoint, cache, cooldown, source, persist, **kwargs):
    return collect_observations(rows, {}, checkpoint, client=source, cache=cache,
                                cooldown=cooldown, clock=lambda: NOW,
                                sleep=lambda seconds: None, monotonic=lambda: 0,
                                persist=persist, **kwargs)


def test_application_uses_injected_ports_and_waits_for_actual_delivery_receipts():
    checkpoint, cache, cooldown = context(1, 2)
    source = Source()
    saves = []
    result = execute([row(1), row(2), row(3)], checkpoint, cache, cooldown, source,
                     lambda state, report: saves.append((deepcopy(state), deepcopy(report))))
    assert source.groups == [str(GROUP_BASE + 1), str(GROUP_BASE + 2)]
    assert result["pending"] == [{"appid": 3, "reason": "awaiting_group_resolution"}]
    assert {item["appid"] for item in result["measurements"]} == {1, 2}
    assert saves[0][0]["official_growth_observations"]["1"]["official_followers"] == 6500
    assert saves[0][0]["other_job"] == {"preserved": True}
    assert not result["job_result"]["state_persisted"]
    assert not result["job_result"]["published"]
    assert not result["job_result"]["successful"]
    stamped = stamp_growth_publication(result, NOW, state_persisted=True, published=True)
    assert stamped["job_result"]["status"] == "partial"
    assert not growth_collection_complete(stamped, NOW)


def test_failed_progress_persistence_stops_before_another_request():
    checkpoint, cache, cooldown = context(1, 2)
    source = Source()

    def reject(*_):
        raise OSError("checkpoint write rejected")

    with pytest.raises(OSError, match="checkpoint write rejected"):
        execute([row(1), row(2)], checkpoint, cache, cooldown, source, reject)
    assert source.groups == [str(GROUP_BASE + 1)]


def test_bounded_run_does_not_query_or_invent_a_count_for_remaining_games():
    checkpoint, cache, cooldown = context(1, 2)
    source = Source()
    result = execute([row(1), row(2)], checkpoint, cache, cooldown, source,
                     lambda *_: None, max_requests=1)
    assert result["requests"] == 1
    assert result["reason"] == "bounded_run"
    assert result["pending"] == [{"appid": 2, "reason": "bounded_run"}]
    assert result["measurements"] == [{"appid": 1, "followers": 6500,
                                        "at": "2026-10-08T12:00:00Z", "source": "steam_community"}]


def test_clock_and_sleep_are_injected_without_a_real_thirty_second_wait():
    checkpoint, cache, cooldown = context(1, 2)
    source = Source()
    delays = []
    collect_observations([row(1), row(2)], {}, checkpoint, client=source, cache=cache,
                         cooldown=cooldown, clock=lambda: NOW, sleep=delays.append,
                         monotonic=lambda: 0, persist=lambda *_: None)
    assert delays == [30]


def test_progress_writer_saves_checkpoint_before_public_report(tmp_path, monkeypatch):
    from radar_backend.state import growth
    calls = []
    monkeypatch.setattr(growth, "save_state", lambda path, value: calls.append(path.name))
    GrowthProgressWriter(tmp_path / "report.json", tmp_path / "checkpoint.json").save({}, {})
    assert calls == ["checkpoint.json", "report.json"]


def test_legacy_collect_forwards_the_old_client_patch_point(tmp_path, monkeypatch):
    import json
    (tmp_path / "catalog.json").write_text(json.dumps({"count": 1, "games": [row(1)]}))
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps(context(1)[0]))
    source = Source()
    monkeypatch.setattr(legacy, "OfficialFollowerClient", lambda **kwargs: source)
    result = legacy.collect(tmp_path, tmp_path / "report.json", checkpoint_path=checkpoint,
                            now=NOW, session=SimpleNamespace(headers={}))
    assert source.groups == [str(GROUP_BASE + 1)]
    assert result["collection_complete"]
    assert not result["job_result"]["successful"]
    delivered = stamp_growth_publication(result, NOW, state_persisted=True, published=True)
    assert growth_collection_complete(delivered, NOW)


def test_growth_policies_reject_other_sources_and_future_evidence():
    other = {"at": "2026-10-08T11:00:00Z", "followers": 7000, "source": "SteamDB"}
    future = {"at": "2026-10-08T13:00:00Z", "followers": 9000}
    assert latest_measurement(row(1), {"history": [other, future]}, None, NOW) is None
    assert legacy.eligible is eligible


def test_new_layers_have_no_reverse_imports_from_scripts():
    paths = [ROOT / "radar_backend" / layer / name for layer, name in (
        ("domain", "growth.py"), ("application", "growth.py"),
        ("state", "growth.py"), ("state", "growth_checkpoint.py"),
        ("publication", "growth.py"), ("jobs", "growth.py"), ("jobs", "growth_checkpoint.py"))]
    for path in paths:
        tree = ast.parse(path.read_text())
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(name == "scripts" or name.startswith("scripts.") for name in imports), path
    domain_imports = [node.module or "" for node in ast.walk(ast.parse(paths[0].read_text()))
                      if isinstance(node, ast.ImportFrom)]
    assert not any(name.startswith(("requests", "radar_backend.state", "radar_backend.adapters"))
                   for name in domain_imports)


@pytest.mark.parametrize("args", [
    ["scripts/collect_public_growth.py", "--help"],
    ["-m", "scripts.collect_public_growth", "--help"],
    ["scripts/persist_growth_checkpoint.py", "--help"],
    ["-m", "scripts.persist_growth_checkpoint", "--help"],
    ["-m", "radar_backend.jobs.growth", "--help"],
    ["-m", "radar_backend.jobs.growth_checkpoint", "--help"],
])
def test_legacy_and_layer_cli_help_remain_offline(args):
    result = subprocess.run([sys.executable, *args], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
