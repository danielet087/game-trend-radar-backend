"""Read and write the existing compact catalog JSON file."""
from __future__ import annotations

import json
from pathlib import Path


def exists(path: Path) -> bool:
    return path.exists()


def read_revision(path: Path, *, json_module=json):
    try:
        return json_module.loads(path.read_text(encoding='utf-8')).get('revision')
    except (OSError, ValueError, TypeError):
        return None


def write_payload(path: Path, payload: dict, *, json_module=json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json_module.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )
