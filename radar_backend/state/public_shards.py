"""Local JSON and path operations for the existing public shard format."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any, *, json_module=json) -> Any:
    try:
        return json_module.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_if_changed(
    path: Path,
    payload: Any,
    *,
    load_json=None,
    json_module=json,
) -> bool:
    # Timestamps are not content changes. Leave the prior timestamp intact
    # when a shard's records have not changed.
    if isinstance(payload, dict) and "generated_at" in payload:
        if load_json is None:
            prior = globals()["load_json"](path, {}, json_module=json_module)
        else:
            prior = load_json(path, {})
        if isinstance(prior, dict):
            comparable_old = {k: v for k, v in prior.items() if k != "generated_at"}
            comparable_new = {k: v for k, v in payload.items() if k != "generated_at"}
            if comparable_old == comparable_new:
                return False
    text = json_module.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def exists(path: Path) -> bool:
    return path.exists()


def glob(path: Path, pattern: str):
    return path.glob(pattern)


def unlink(path: Path) -> None:
    path.unlink()
