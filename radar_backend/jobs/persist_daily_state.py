"""CLI for frozen private daily progress and acknowledged reset publication."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from radar_backend.publication.daily_state import (
    SOURCE_PATHS, capture_daily_baseline, freeze_daily_state,
    publish_daily_reset, publish_daily_state,
)
from radar_backend.publication.steam import read_json, write_json


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("reset", "capture", "publish"):
        item = commands.add_parser(command)
        item.add_argument("--repo", type=Path, default=Path("."))
        if command == "capture":
            item.add_argument("--output", type=Path, default=Path("output/daily-state-baseline.json"))
        else:
            item.add_argument("--receipt", type=Path,
                              default=Path("output/daily-reset-publication.json" if command == "reset"
                                           else "output/daily-state-publication.json"))
            item.add_argument("--max-attempts", type=int, default=5)
        if command == "publish":
            item.add_argument("--baseline", type=Path, default=Path("output/daily-state-baseline.json"))
            item.add_argument("--pending", type=Path, default=Path("output/daily-state-pending.json"))
    return result


def _artifact(path: Path, root: Path, protected=()):
    literal = path.absolute()
    for destination in (literal, literal.with_suffix(literal.suffix + ".tmp")):
        for item in (destination, *destination.parents):
            if item.is_symlink():
                raise ValueError("Daily publication artifact paths must not contain symlinks")
    target = literal.resolve()
    if target.suffix != ".json":
        raise ValueError("Daily publication artifacts must be JSON")
    if literal.is_relative_to(root) and not target.is_relative_to(root / "output"):
        raise ValueError("Daily publication artifact escapes the checkout output directory")
    if target.is_relative_to(root) and not target.is_relative_to(root / "output"):
        raise ValueError("Daily publication artifacts belong outside persisted source data")
    forbidden = [root / name for name in SOURCE_PATHS]
    forbidden.append(root / "output/steam_upcoming.json")
    forbidden.extend(protected)
    if any(target == item.resolve() for item in forbidden):
        raise ValueError("Daily publication artifact must not overwrite an immutable input")
    return target


def main(argv=None):
    args = parser().parse_args(argv)
    root = args.repo.resolve()
    if args.command == "capture":
        output = _artifact(args.output, root)
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        write_json(output, capture_daily_baseline(root, input_revision=revision))
        print("DAILY_STATE_BASELINE_CAPTURED", revision)
        return 0
    protected = [args.baseline, args.pending] if args.command == "publish" else []
    receipt_path = _artifact(args.receipt, root, protected)
    receipt_path.unlink(missing_ok=True)
    if args.command == "reset":
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        receipt = publish_daily_reset(
            root, input_revision=revision, max_attempts=args.max_attempts,
            event_name=os.environ.get("SCHEDULE_EVENT", ""),
            trigger_source=os.environ.get("SCHEDULE_TRIGGER_SOURCE", ""),
            target_slot=os.environ.get("SCHEDULE_TARGET_SLOT", ""),
            refresh_today=os.environ.get("SCHEDULE_REFRESH_TODAY", "false").lower() == "true")
    else:
        pending = _artifact(args.pending, root, (args.baseline, args.receipt))
        baseline_path = _artifact(args.baseline, root, (args.pending, args.receipt))
        baseline = read_json(baseline_path)
        if pending.exists():
            frozen = read_json(pending)
            if frozen.get("baseline") != baseline:
                raise ValueError("Pending daily progress belongs to another baseline")
        else:
            frozen = freeze_daily_state(root, baseline)
            # This artifact is durable before the publication engine's first
            # reset. A rejected push cannot erase the worker's observations.
            write_json(pending, frozen)
        receipt = publish_daily_state(root, frozen, max_attempts=args.max_attempts)
    write_json(receipt_path, receipt.to_dict())
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write("state_persisted=true\n")
    print("DAILY_STATE_PUBLICATION", json.dumps(receipt.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
