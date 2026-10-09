"""Local JSON I/O for the existing published-title refresh format."""
from __future__ import annotations

import json
from pathlib import Path


def read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def save_changed(path: Path, data: dict) -> bool:
    output = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == output:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output, encoding="utf-8")
    return True


def exists(path: Path) -> bool:
    return path.exists()
