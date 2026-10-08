"""Atomic hourly files and frozen, acknowledged checkpoint persistence.

The rebase helper is retained for historical tools. Official jobs use the
shared publisher and replay validated three-way state with a recovery batch.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from radar_backend.state.official_followers import save_state as save
from radar_core.publication import PublicationError
from radar_backend.publication.official_checkpoint import CHECKPOINT, MASTER, OfficialCheckpointPersistence
from radar_backend.state.official_merge import strict_json_loads


def exists(path):
    return path.exists()


def read(path):
    return strict_json_loads(path.read_text(encoding="utf-8"))


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


def _root(checkpoint, master):
    # Resolve the checkout only after deriving it from lexical paths. Resolving
    # the checkpoint first could follow an attacker-controlled directory link.
    root = Path(checkpoint).absolute().parents[2]
    if Path(checkpoint).absolute() != root / CHECKPOINT or Path(master).absolute() != root / MASTER:
        raise ValueError("Official persistence requires the established source paths")
    return root


def begin_persistence(*, checkpoint, master, checkpoint_state, master_state, clock=None):
    """Capture the loaded inputs before the application mutates either dict."""
    root = _root(checkpoint, master)
    publisher = OfficialCheckpointPersistence(root, clock=clock)
    publisher.begin(checkpoint_state, master_state)
    return publisher


def git_push(*, checkpoint, master, output, export_status, rebase, publisher=None):
    """Make each 10-success checkpoint durable before a runner interruption."""
    try:
        # The arguments and legacy rebase helper remain import-compatible for
        # historical tools; official persistence exclusively uses frozen replay.
        root = _root(checkpoint, master)
        persistence = publisher or OfficialCheckpointPersistence(root)
        if persistence.root != root.resolve():
            raise ValueError("Checkpoint publisher belongs to a different checkout")
        persistence.persist()
        print("DAILY_CATCHUP_GIT_CHECKPOINT_OK", flush=True)
        return True
    except (PublicationError, OSError, ValueError, TypeError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("DAILY_CATCHUP_GIT_CHECKPOINT_FAILURE",
              type(exc).__name__, flush=True)
        return False
