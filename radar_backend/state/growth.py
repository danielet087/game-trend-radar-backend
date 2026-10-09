"""Load growth inputs and atomically save private progress before its report."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from radar_backend.state.official_followers import read_state, save_state


@dataclass(frozen=True)
class GrowthInputs:
    rows: list
    history: dict
    checkpoint: dict
    sources: tuple
    legacy: dict


def read(path, default):
    """Compatibility JSON reader; optional defaults never hide malformed JSON."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def load_growth_inputs(data_dir, *, checkpoint_path=None, official_cache_path=None,
                       legacy_checkpoint_path=None, group_state_path=None,
                       original_checkpoint_path=None):
    catalog = read_state(data_dir / "catalog.json")
    rows = catalog.get("games")
    if not isinstance(rows, list) or type(catalog.get("count")) is not int or catalog["count"] != len(rows):
        raise ValueError("A complete public catalog is required")
    history = read_state(data_dir / "insights-state.json", optional=True).get("records", {})
    if not isinstance(history, dict):
        raise ValueError("Malformed insights history")
    checkpoint = read_state(checkpoint_path) if checkpoint_path is not None else {}
    paths = (official_cache_path, legacy_checkpoint_path, group_state_path, original_checkpoint_path)
    sources = tuple(read_state(path, optional=True) for path in paths if path is not None)
    legacy = read_state(legacy_checkpoint_path, optional=True) if legacy_checkpoint_path is not None else {}
    return GrowthInputs(rows, history, checkpoint, sources, legacy)


class GrowthProgressWriter:
    """Maintain the existing JSON layout and checkpoint-before-output ordering."""
    def __init__(self, output, checkpoint_path=None):
        self.output = output
        self.checkpoint_path = checkpoint_path

    def save(self, checkpoint, report):
        if self.checkpoint_path is not None:
            save_state(self.checkpoint_path, checkpoint)
        save_state(self.output, report)
