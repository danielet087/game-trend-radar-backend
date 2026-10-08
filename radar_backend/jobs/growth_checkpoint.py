"""CLI composition for checkpoint merging and delivery receipt persistence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from radar_backend.publication.growth import stamp_growth_publication
from radar_backend.state.growth_checkpoint import merge_growth_checkpoint
from radar_backend.state.official_followers import read_state, save_state

def stamp_report(path, *, state_persisted, published, target_slot=None, input_revision=None, now=None):
    result = read_state(path) if path.exists() else {"reason": "missing_collection_result"}
    stamped = stamp_growth_publication(
        result, now or datetime.now(timezone.utc), state_persisted=state_persisted,
        published=published, target_slot=target_slot or None, input_revision=input_revision or None,
    )
    save_state(path, stamped)
    return stamped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("merge")
    for flag in ("latest", "baseline", "observed", "report", "output"):
        merge.add_argument(f"--{flag}", type=Path, required=True)
    stamp = commands.add_parser("stamp")
    stamp.add_argument("--report", type=Path, required=True)
    stamp.add_argument("--state-persisted", choices=("true", "false"), required=True)
    stamp.add_argument("--published", choices=("true", "false"), required=True)
    stamp.add_argument("--target-slot", default="")
    stamp.add_argument("--input-revision", default="")
    args = parser.parse_args()
    if args.command == "merge":
        merged = merge_growth_checkpoint(
            read_state(args.latest), read_state(args.baseline), read_state(args.observed), read_state(args.report),
        )
        save_state(args.output, merged)
    else:
        stamp_report(args.report, state_persisted=args.state_persisted == "true",
                     published=args.published == "true", target_slot=args.target_slot,
                     input_revision=args.input_revision)


if __name__ == "__main__":
    main()
