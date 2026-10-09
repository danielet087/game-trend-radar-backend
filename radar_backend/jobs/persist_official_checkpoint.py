"""Replay a saved hourly checkpoint after process failure, without collection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path

from radar_core.publication import PublicationReceipt
from radar_backend.publication.official_checkpoint import (
    CHECKPOINT, MASTER, PENDING, RECEIPT, _validate_pending, baseline_from_head,
    artifact_paths, capture_pending, pending_revision, publish_pending, validate_source_locations,
)
from radar_backend.publication.steam import read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--pending", type=Path, default=Path(PENDING))
    parser.add_argument("--receipt", type=Path, default=Path(RECEIPT))
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args(argv)
    root = args.repo.resolve()
    validate_source_locations(root)
    pending, receipt_path = artifact_paths(root, args.pending, args.receipt)
    if pending.is_file():
        batch = read_json(pending)
        _validate_pending(batch)
    else:
        observed = {"checkpoint": read_json(root / CHECKPOINT, optional=True),
                    "master": read_json(root / MASTER, optional=True)}
        baseline, present = baseline_from_head(root, return_presence=True)
        batch = capture_pending(root, baseline, observed,
                                now=datetime.now(timezone.utc), baseline_present=present)
        receipt_path.unlink(missing_ok=True)
        write_json(pending, batch)
    if not batch["recovery_eligible"]:
        receipt = PublicationReceipt.from_dict(read_json(receipt_path))
        if receipt.published_revision != batch["acknowledged_revision"]:
            raise ValueError("Recovery acknowledgement does not match its publication receipt")
        if receipt.payload_revision != pending_revision(batch) or receipt.input_revision != batch["input_revision"]:
            raise ValueError("Acknowledged recovery batch differs from its frozen publication receipt")
        baseline, present = baseline_from_head(root, revision=receipt.published_revision,
                                               return_presence=True)
        observed = {"checkpoint": read_json(root / CHECKPOINT),
                    "master": read_json(root / MASTER)}
        if observed != baseline:
            # The runner may stop after observation eleven, between ten-success
            # acknowledgements. Use the actual acknowledged merged commit as
            # baseline, preserving every newer local observation before reset.
            batch = capture_pending(root, baseline, observed,
                now=datetime.now(timezone.utc), baseline_present=present,
                input_revision=receipt.published_revision)
            receipt_path.unlink(missing_ok=True)
            write_json(pending, batch)
        else:
            print("OFFICIAL_CHECKPOINT_ALREADY_ACKNOWLEDGED", receipt.published_revision)
    if batch["recovery_eligible"]:
        receipt = publish_pending(root, batch, receipt_path=receipt_path,
                                  max_attempts=args.max_attempts)
        batch.update(recovery_eligible=False, acknowledged_revision=receipt.published_revision)
        write_json(pending, batch)
        print("OFFICIAL_CHECKPOINT_RECOVERED", receipt.published_revision)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("state_persisted=true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
