"""Atomic hourly checkpoint files and acknowledged Git persistence.

The dashboard regeneration callback is injected by the composition root. Only
its derived output may be regenerated during a rebase; queue conflicts fail.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from radar_backend.state.official_followers import save_state as save


def exists(path):
    return path.exists()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rebase_checkpoint(*, output, export_status):
    """Regenerate only a dashboard-only conflict; never overwrite queue state."""
    command = ["git", "pull", "--rebase", "origin", "main"]
    for _ in range(5):
        result = subprocess.run(command, timeout=45, stdout=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        conflicts = subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=U"], text=True, timeout=20,
        ).splitlines()
        if conflicts != [str(output)]:
            raise subprocess.CalledProcessError(result.returncode, command)
        export_status()
        subprocess.run(["git", "add", str(output)], check=True, timeout=20,
                       stdout=subprocess.DEVNULL)
        command = ["git", "-c", "core.editor=true", "rebase", "--continue"]
    raise subprocess.CalledProcessError(1, command)


def git_push(*, checkpoint, master, output, export_status, rebase):
    """Make each 10-success checkpoint durable before a runner interruption."""
    # The status exporter is read-only. Its failure must never reset or block
    # the established official queue; the previous snapshot stays visibly old.
    try:
        export_status()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("SCHEDULER_QUEUE_STATUS_EXPORT_FAILED", type(exc).__name__, flush=True)
    try:
        files = [str(checkpoint), str(master)]
        if output.is_file():
            files.append(str(output))
        subprocess.run(["git", "add", *files], check=True, timeout=20,
                       stdout=subprocess.DEVNULL)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], timeout=20)
        if diff.returncode not in (0, 1):
            raise subprocess.CalledProcessError(diff.returncode, diff.args)
        if diff.returncode == 1:
            subprocess.run(["git", "commit", "-m",
                            "experiment: checkpoint dynamically queued official Followers"],
                           check=True, timeout=25, stdout=subprocess.DEVNULL)
        # A prior checkpoint may already be committed locally after its push
        # failed. A clean index does not prove that commit reached the remote.
        rebase()
        subprocess.run(["git", "push", "origin", "HEAD:main"],
                       check=True, timeout=50, stdout=subprocess.DEVNULL)
        print("DAILY_CATCHUP_GIT_CHECKPOINT_OK", flush=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("DAILY_CATCHUP_GIT_CHECKPOINT_FAILURE",
              type(exc).__name__, flush=True)
        return False
