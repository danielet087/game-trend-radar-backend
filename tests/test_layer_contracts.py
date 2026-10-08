"""Verify deployed entry points and keep domain imports free of runtime I/O."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = (
    ("steam_candidate_pipeline", "candidates", "--batch-days"),
    ("experiment_official_daily_catchup_250", "official_followers", "--max-requests"),
    ("collect_public_growth", "growth", "--interval"),
    ("persist_growth_checkpoint", "growth_checkpoint", "merge"),
)


@pytest.fixture
def offline_environment(tmp_path):
    # A broken entry must fail instead of silently making a source request.
    (tmp_path / "sitecustomize.py").write_text(
        "import socket\n"
        "def denied(*args, **kwargs):\n"
        "    raise AssertionError('CLI help attempted network access')\n"
        "socket.socket.connect = denied\n"
        "socket.create_connection = denied\n",
        encoding="utf-8",
    )
    return {**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + str(ROOT)}


@pytest.mark.parametrize("script,job,flag", ENTRIES)
@pytest.mark.parametrize("invocation", ("direct", "module", "job"))
def test_deployed_help_keeps_cli_contract(script, job, flag, invocation, offline_environment):
    if invocation == "direct":
        arguments = [str(ROOT / "scripts" / (script + ".py"))]
    else:
        module = "scripts." + script if invocation == "module" else "radar_backend.jobs." + job
        arguments = ["-m", module]
    result = subprocess.run(
        [sys.executable, *arguments, "--help"], cwd=ROOT,
        env=offline_environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert flag in result.stdout


def test_domain_can_load_without_runtime_io(offline_environment):
    modules = ["radar_backend.domain." + path.stem
               for path in sorted((ROOT / "radar_backend/domain").glob("*.py"))
               if path.stem != "__init__"]
    program = """
import builtins
import importlib
import pathlib
import subprocess
import sys

def denied(*args, **kwargs):
    raise AssertionError('Domain attempted runtime I/O')

builtins.open = denied
pathlib.Path.read_text = denied
pathlib.Path.write_text = denied
subprocess.run = denied
subprocess.Popen = denied
for name in sys.argv[1:]:
    importlib.import_module(name)
for name in sys.modules:
    assert name != 'requests' and not name.startswith('radar_backend.adapters.'), name
    assert not name.startswith(('radar_backend.state.', 'radar_backend.jobs.')), name
"""
    result = subprocess.run(
        [sys.executable, "-c", program, *modules], cwd=ROOT,
        env=offline_environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("direct", (True, False))
def test_external_schedule_entry_without_pythonpath(direct):
    arguments = ([str(ROOT / "scripts/external_schedule.py")] if direct
                 else ["-m", "scripts.external_schedule"])
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, *arguments, "--help"], cwd=ROOT,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--workflow" in result.stdout
