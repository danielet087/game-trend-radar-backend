"""Load growth inputs and atomically save private progress before its report."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
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
    def __init__(self, output, checkpoint_path=None, *, primary=None):
        self.output = output
        self.checkpoint_path = checkpoint_path
        if primary is None:
            from radar_core.storage import primary
        self.primary = primary if primary.enabled() else None
        if self.primary is not None and checkpoint_path is None:
            raise ValueError("Database growth progress requires its checkpoint path")
        self.baseline = deepcopy(read_state(checkpoint_path)) if self.primary is not None else None

    def save(self, checkpoint, report):
        if self.checkpoint_path is not None:
            save_state(self.checkpoint_path, checkpoint)
        save_state(self.output, report)
        if self.primary is not None:
            self._commit(checkpoint, report)

    def _commit(self, checkpoint, report):
        from radar_core.publication import snapshot_revision
        from radar_backend.publication.database_intents import CHECKPOINT, REPOSITORY, _replace
        from radar_backend.state.growth_checkpoint import merge_growth_checkpoint
        from radar_backend.state.official_merge import strict_json_loads
        observed, frozen_report = deepcopy(checkpoint), deepcopy(report)
        baseline = deepcopy(self.baseline)
        frozen = {"baseline": baseline, "observed": observed, "report": frozen_report}
        digest = snapshot_revision(frozen)
        pending = Path(self.output).with_name("growth-database-pending.json")
        save_state(pending, {"schema_version": 1, "kind": "growth-database-progress",
                            "recovery_eligible": True, "payload_revision": digest, **frozen})
        for attempt in range(5):
            head, files = self.primary.read_files(REPOSITORY, paths=(CHECKPOINT,))
            latest = strict_json_loads(files[CHECKPOINT].decode("utf-8"))
            merged = merge_growth_checkpoint(latest, baseline, observed, frozen_report)
            raw = (json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
            try:
                receipt = self.primary.write_files(REPOSITORY, {CHECKPOINT: raw},
                    base_generation=head["generation"], operation_id="steam-growth-" + digest,
                    payload_revision=digest)
            except Exception as error:
                if getattr(error, "code", None) != "STALE_GENERATION" or attempt == 4:
                    raise
                continue
            if (receipt.get("source_id") != REPOSITORY + "@main"
                    or receipt.get("operation_id") != "steam-growth-" + digest
                    or receipt.get("file_digests") != {CHECKPOINT: hashlib.sha256(raw).hexdigest()}
                    or receipt.get("deleted")):
                raise ValueError("Growth database acknowledgement differs from its frozen write")
            _replace(checkpoint, merged)
            self.baseline = deepcopy(merged)
            save_state(self.checkpoint_path, checkpoint)
            save_state(pending, {"schema_version": 1, "kind": "growth-database-progress",
                "recovery_eligible": False, "payload_revision": digest, "database_receipt": receipt})
            return
        raise RuntimeError("Growth progress exhausted its database CAS retries")
