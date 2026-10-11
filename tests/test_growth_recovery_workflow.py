"""Exercise the real receipt CLI and exact completion gate used by the Worker."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from radar_backend.jobs import growth_checkpoint
from scripts.external_schedule import growth_collection_complete, stamp_growth_publication
from tests.test_external_schedule import accounted_growth_report


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2026-10-11T07:15:00+08:00")


def workflow_steps():
    return yaml.safe_load((ROOT / ".github/workflows/steam-public-growth.yml").read_text())["jobs"]["observe"]["steps"]


def run_completion_gate(tmp_path, report):
    output = tmp_path / "output/public-growth.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report), encoding="utf-8")
    gate = next(step for step in workflow_steps() if step.get("name") == "Require complete growth coverage")
    # The production shell is executed unchanged apart from freezing its clock.
    command = gate["run"].replace("datetime.now(timezone.utc)", f"datetime.fromisoformat('{NOW.isoformat()}')")
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    return subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, text=True, capture_output=True)


def stamp_with_cli(path, *, persisted=True, published=True):
    args = ["growth-checkpoint", "stamp", "--report", str(path),
            "--state-persisted", str(persisted).lower(), "--published", str(published).lower(),
            "--target-slot", "2026-10-10T23:15:00Z", "--input-revision", "catalog-at-collection"]
    with patch.object(sys, "argv", args), patch.object(
        growth_checkpoint, "datetime", SimpleNamespace(now=lambda zone: NOW),
    ):
        growth_checkpoint.main()
    return json.loads(path.read_text(encoding="utf-8"))


def test_cli_records_actual_receipts_before_the_workflow_allows_daily_success(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(accounted_growth_report()), encoding="utf-8")
    complete = stamp_with_cli(path)
    assert growth_collection_complete(complete, NOW)
    assert complete["job_result"]["target_slot"] == "2026-10-10T23:15:00Z"
    assert complete["job_result"]["input_revision"] == "catalog-at-collection"
    result = run_completion_gate(tmp_path, complete)
    assert result.returncode == 0, result.stderr
    assert "DAILY_STEAM_GROWTH_COMPLETE 2 measured=1 unavailable=1" in result.stdout


@pytest.mark.parametrize("persisted,published", [(False, False), (False, True), (True, False)])
def test_cli_failed_delivery_cannot_reuse_a_prior_success_or_suppress_recovery(tmp_path, persisted, published):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(accounted_growth_report()), encoding="utf-8")
    stamp_with_cli(path)
    failed = stamp_with_cli(path, persisted=persisted, published=published)
    assert failed["job_result"]["status"] == "failed"
    assert not growth_collection_complete(failed, NOW)
    result = run_completion_gate(tmp_path, failed)
    assert result.returncode != 0
    assert "next six-hour check" in result.stderr


def test_delivered_retryable_group_failure_still_fails_the_real_completion_gate(tmp_path):
    pending = deepcopy(accounted_growth_report())
    pending.update(reason="awaiting_group_resolution", collection_complete=False,
                   unavailable=[], pending=[{"appid": 20, "reason": "group_missing_api_key"}])
    delivered = stamp_growth_publication(pending, NOW, state_persisted=True, published=True)
    result = run_completion_gate(tmp_path, delivered)
    assert result.returncode != 0
    assert "retryable=1" in result.stderr
    assert delivered["measurements"][0]["followers"] == 5001


def test_group_resolution_secret_and_exact_gate_name_remain_scheduler_contracts():
    steps = workflow_steps()
    collect = next(step for step in steps if step.get("id") == "collect")
    assert collect["env"]["STEAM_WEB_API_KEY"] == "${{ secrets.STEAM_WEB_API_KEY }}"
    names = [step.get("name") for step in steps]
    assert names.count("Require complete growth coverage") == 1
    assert names.index("Record durable state and publication acknowledgements") < names.index("Require complete growth coverage")

