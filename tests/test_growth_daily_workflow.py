"""Daily capacity and fresh JSON inputs for queued growth workflow runs."""
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

from radar_backend.state.official_followers import read_state
from radar_backend.publication.checkpoint_delivery import publish_growth_checkpoint


ROOT = Path(__file__).resolve().parents[1]
STATE_PATHS = (
    "experiments/steam_official_daily_catchup/checkpoint.json",
    "experiments/steam_official_nearfirst_20260922/checkpoint.json",
    "experiments/steam_official_followers_20260922/checkpoint.json",
    "data/steam_followers_cache.json",
    "data/steam_prefilter_state.json",
)
CODE_PATH = "radar_backend/jobs/growth.py"
FROZEN_PATHS = {
    STATE_PATHS[1]: "output/growth-inputs/legacy-checkpoint.json",
    STATE_PATHS[2]: "output/growth-inputs/original-checkpoint.json",
    STATE_PATHS[3]: "output/growth-inputs/official-cache.json",
    STATE_PATHS[4]: "output/growth-inputs/group-state.json",
}


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/steam-public-growth.yml").read_text())


def step(step_id):
    return next(item for item in workflow()["jobs"]["observe"]["steps"] if item.get("id") == step_id)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def queued_checkout(tmp_path):
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   capture_output=True, check=True)
    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], capture_output=True, check=True)
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.com")
    for name in STATE_PATHS:
        write(writer / name, json.dumps({"marker": "queued-old", "path": name}))
    write(writer / CODE_PATH, "PINNED_CODE = 'queued-version'\n")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "queued workflow input")
    git(writer, "push", "origin", "HEAD:main")
    runner = tmp_path / "runner"
    subprocess.run(["git", "clone", str(remote), str(runner)], capture_output=True, check=True)
    return writer, runner, git(runner, "rev-parse", "HEAD")


def refresh(runner):
    output = runner.parent / "github-output.txt"
    result = subprocess.run(["bash", "-c", step("growth_state")["run"]], cwd=runner,
                            env={**os.environ, "GITHUB_OUTPUT": str(output)},
                            capture_output=True, text=True)
    return result, output.read_text() if output.exists() else ""


def advance_state(writer, *, broken=None, removed=None):
    for name in STATE_PATHS:
        if name == removed:
            (writer / name).unlink()
        else:
            write(writer / name, "{malformed" if name == broken else
                  json.dumps({"marker": "latest-after-lock", "path": name}))
    # A newer main revision can contain code changes too: the run must keep
    # executing the immutable code that passed its own dependency checks.
    write(writer / CODE_PATH, "PINNED_CODE = 'new-main-code'\n")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "official state advanced while growth waited")
    git(writer, "push", "origin", "HEAD:main")
    return git(writer, "rev-parse", "HEAD")


def test_daily_budget_covers_the_cold_cohort_without_holding_a_slot_for_cooldown():
    command = step("collect")["run"].replace("\\\n", " ")
    argv = shlex.split(next(line for line in command.splitlines()
                           if line.strip().startswith("python -m radar_backend.jobs.growth")))
    option = lambda flag: argv[argv.index(flag) + 1]
    requests = int(option("--max-requests"))
    seconds = int(option("--max-seconds"))
    interval = int(option("--interval"))
    cooldown_wait = int(option("--max-cooldown-wait"))
    # October 12 grew to 138 eligible games, above the old 120/3600 budget.
    cold_cohort = 138
    assert requests == 250 and requests >= cold_cohort
    assert interval >= 30
    # Keep the resumable report wrapper, but defer an active shared cooldown
    # to the next six-hour check instead of sleeping while holding the slot.
    assert "--wait-for-cooldown" in argv and cooldown_wait == 0
    assert seconds == 9000 and (cold_cohort - 1) * interval + cooldown_wait < seconds
    assert workflow()["jobs"]["observe"]["timeout-minutes"] * 60 >= seconds + 15 * 60
    assert workflow()["concurrency"] == {
        "group": "isolated-official-nearfirst-frozen-20260922", "cancel-in-progress": False,
    }


def test_latest_inputs_are_frozen_before_baseline_without_changing_unowned_tracked_files(tmp_path):
    writer, runner, pinned_revision = queued_checkout(tmp_path)
    latest_revision = advance_state(writer)
    result, acknowledgement = refresh(runner)
    assert result.returncode == 0, result.stderr
    assert acknowledgement == f"revision={latest_revision}\n"
    assert f"code={pinned_revision} state={latest_revision}" in result.stdout
    assert git(runner, "rev-parse", "HEAD") == pinned_revision
    assert git(runner, "rev-parse", "refs/remotes/origin/main") == latest_revision
    assert (runner / CODE_PATH).read_text() == "PINNED_CODE = 'queued-version'\n"
    assert git(runner, "diff", "--cached", "--name-only") == ""
    assert git(runner, "diff", "--name-only") == STATE_PATHS[0]
    assert read_state(runner / STATE_PATHS[0]) == {"marker": "latest-after-lock", "path": STATE_PATHS[0]}
    for tracked, frozen in FROZEN_PATHS.items():
        assert read_state(runner / tracked) == {"marker": "queued-old", "path": tracked}
        assert read_state(runner / frozen) == {"marker": "latest-after-lock", "path": tracked}
    argv = shlex.split(step("collect")["run"].split("python -m radar_backend.jobs.growth", 1)[1].replace("\\\n", " "))
    for flag, frozen in (
        ("--official-cache", FROZEN_PATHS[STATE_PATHS[3]]),
        ("--legacy-checkpoint", FROZEN_PATHS[STATE_PATHS[1]]),
        ("--group-state", FROZEN_PATHS[STATE_PATHS[4]]),
        ("--original-checkpoint", FROZEN_PATHS[STATE_PATHS[2]]),
    ):
        assert argv[argv.index(flag) + 1] == frozen
    collect_setup = step("collect")["run"].split("python -m radar_backend.jobs.growth", 1)[0]
    prepared = subprocess.run(["bash", "-c", collect_setup], cwd=runner,
                              capture_output=True, text=True)
    assert prepared.returncode == 0, prepared.stderr
    assert read_state(runner / "output/growth-checkpoint-baseline.json")["marker"] == "latest-after-lock"
    ids = [item.get("id") for item in workflow()["jobs"]["observe"]["steps"]]
    assert ids.index("catalog") < ids.index("growth_state") < ids.index("collect")


def test_refreshed_inputs_allow_real_checkpoint_publication_with_its_original_owned_scope(tmp_path):
    writer, runner, pinned_revision = queued_checkout(tmp_path)
    advance_state(writer)
    result, _ = refresh(runner)
    assert result.returncode == 0, result.stderr
    baseline = read_state(runner / STATE_PATHS[0])
    observed = deepcopy(baseline)
    observation = {
        "appid": 1, "group_id64": "103582791429521531", "official_followers": 5001,
        "official_checked_at_taipei": "2026-10-12T01:20:00Z",
        "official_source": "Steam Community XML memberCount",
    }
    observed["official_growth_observations"] = {"1": observation}
    write(runner / STATE_PATHS[0], json.dumps(observed))
    report = {"reason": "completed", "errors": [], "eligible": 1,
              "measurements": [{"appid": 1, "followers": 5001, "at": "2026-10-12T01:20:00Z"}],
              "events": [{"status": "ok", "observed_at": "2026-10-12T01:20:00Z"}]}
    frozen_contents = {name: (runner / name).read_text() for name in FROZEN_PATHS.values()}
    receipt = publish_growth_checkpoint(
        runner, baseline, observed, report, input_revision=pinned_revision,
        now=datetime(2026, 10, 12, 2, tzinfo=timezone.utc),
    )
    persisted = json.loads(git(writer.parent / "origin.git", "show", f"main:{STATE_PATHS[0]}"))
    assert persisted["official_growth_observations"]["1"] == observation
    assert persisted["marker"] == "latest-after-lock"
    assert receipt.published_revision == git(writer.parent / "origin.git", "rev-parse", "main")
    for tracked, frozen in FROZEN_PATHS.items():
        remote = json.loads(git(writer.parent / "origin.git", "show", f"main:{tracked}"))
        assert remote == {"marker": "latest-after-lock", "path": tracked}
        assert (runner / frozen).read_text() == frozen_contents[frozen]
    assert git(runner, "diff", "--name-only") == ""


@pytest.mark.parametrize("state_path", STATE_PATHS)
def test_corrupt_new_state_is_preserved_and_rejected_instead_of_using_stale_progress(tmp_path, state_path):
    writer, runner, pinned_revision = queued_checkout(tmp_path)
    latest_revision = advance_state(writer, broken=state_path)
    result, acknowledgement = refresh(runner)
    assert result.returncode == 0, result.stderr
    assert acknowledgement == f"revision={latest_revision}\n"
    assert git(runner, "rev-parse", "HEAD") == pinned_revision
    current_input = runner / FROZEN_PATHS.get(state_path, state_path)
    assert current_input.read_text() == "{malformed"
    with pytest.raises(json.JSONDecodeError):
        read_state(current_input)


def test_missing_latest_state_path_stops_collection_without_a_freshness_acknowledgement(tmp_path):
    writer, runner, pinned_revision = queued_checkout(tmp_path)
    advance_state(writer, removed=STATE_PATHS[-1])
    result, acknowledgement = refresh(runner)
    assert result.returncode != 0
    assert acknowledgement == ""
    assert "fatal:" in result.stderr and STATE_PATHS[-1] in result.stderr
    assert git(runner, "rev-parse", "HEAD") == pinned_revision


def test_full_coverage_gate_and_durable_delivery_order_are_preserved():
    steps = workflow()["jobs"]["observe"]["steps"]
    names = [item.get("name") for item in steps]
    assert names.count("Require complete growth coverage") == 1
    assert names.index("Record durable state and publication acknowledgements") < names.index("Require complete growth coverage")
    gate = next(item for item in steps if item.get("name") == "Require complete growth coverage")
    assert "growth_collection_complete(result, datetime.now(timezone.utc))" in gate["run"]
    assert "raise SystemExit" in gate["run"]
    assert "next six-hour check" in gate["run"]
