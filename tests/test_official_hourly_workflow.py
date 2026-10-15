"""The hourly queue must leave time to persist and release its shared lock."""
from pathlib import Path
import shlex

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_hourly_collection_leaves_time_for_all_bounded_queue_phases():
    workflow = yaml.safe_load((ROOT / ".github/workflows/steam-official-daily-catchup-250.yml").read_text())
    job = workflow["jobs"]["follower-catchup"]
    steps = job["steps"]
    collect = next(step for step in steps
                   if "scripts.experiment_official_daily_catchup_250" in step.get("run", ""))
    commands = collect["run"].replace("\\\n", " ")
    argv = shlex.split(next(line for line in commands.splitlines()
                           if "python -u -m scripts.experiment_official_daily_catchup_250" in line))
    option = lambda name: argv[argv.index(name) + 1]
    assert int(collect["env"]["REQUEST_LIMIT"]) == 90
    assert option("--max-requests") == "$REQUEST_LIMIT"
    assert float(option("--interval")) >= 30
    collection_budget = int(option("--max-seconds"))
    assert 120 <= collection_budget <= 2700
    auxiliary_budgets = []
    for step in steps:
        for line in step.get("run", "").replace("\\\n", " ").splitlines():
            if "--max-seconds" not in line or "scripts.experiment_official_daily_catchup_250" in line:
                continue
            args = shlex.split(line)
            auxiliary_budgets.append(int(args[args.index("--max-seconds") + 1]))
    assert sorted(auxiliary_budgets) == [120, 300, 300, 300]
    assert job["timeout-minutes"] == 60
    # The rest covers setup, test validation and acknowledged Git publication.
    assert job["timeout-minutes"] * 60 - collection_budget - sum(auxiliary_budgets) >= 8 * 60
    assert collect["if"] == "steps.query_window.outputs.proceed == 'true'"
    guards = [step["id"] for step in steps
              if "scripts.external_schedule --workflow official-followers" in step.get("run", "")]
    assert guards == ["window", "query_window"]
    assert workflow["concurrency"] == {
        "group": "isolated-official-nearfirst-frozen-20260922", "cancel-in-progress": False,
    }
