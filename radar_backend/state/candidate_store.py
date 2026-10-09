"""Local candidate JSON persistence, separate from workflow Git durability.

Missing or malformed private files retain the original loader fallback behavior.
Writes preserve atomic replacement and the public writer's existing format.
A successful local write never acknowledges a remote Git push or publication.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else default
    except (OSError, ValueError, TypeError):
        LOG.warning("Could not read %s; using default state", path)
        return default


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    """Serialize the existing local output; remote publication is a later step."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class CandidateStateStore:
    load: Callable = load_json
    save: Callable = save_json
    write_output: Callable = write_json
