"""CLI composition for Steam snapshot publication and durable receipts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from radar_backend.publication.checkpoint_delivery import CHECKPOINT, publish_growth_checkpoint, publish_queue_batch
from radar_backend.publication.steam import publish_catalog, publish_growth, read_json, write_json


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render-queue-status", help="Render an offline queue snapshot with a frozen clock")
    render.add_argument("--observed-at", required=True)
    dispatch = commands.add_parser("apply-dispatch-batch", help="Apply only persisted content-dispatch receipts")
    dispatch.add_argument("--batch", type=Path, required=True)
    for command in ("catalog", "growth", "growth-checkpoint", "queue"):
        item = commands.add_parser(command)
        item.add_argument("--repo", type=Path, default=Path("frontend") if command in {"catalog", "growth"} else Path("."))
        item.add_argument("--input-revision", default="")
        item.add_argument("--receipt", type=Path, default=Path(f"output/steam-{command}-publication.json"))
        item.add_argument("--max-attempts", type=int, default=8 if command == "catalog" else 5)
        if command in {"catalog", "growth"}:
            item.add_argument("--auth-script", type=Path, default=Path("scripts/git_frontend_auth.sh"))
        if command == "catalog":
            item.add_argument("--input", type=Path, default=Path("data/steam_upcoming_master.json"))
            item.add_argument("--skip-missing-batch", type=Path)
        if command == "growth":
            item.add_argument("--report", type=Path, default=Path("output/public-growth.json"))
        if command == "growth-checkpoint":
            item.add_argument("--baseline", type=Path, default=Path("output/growth-checkpoint-baseline.json"))
            item.add_argument("--observed", type=Path, default=Path(CHECKPOINT))
            item.add_argument("--report", type=Path, default=Path("output/public-growth.json"))
        if command == "queue":
            item.add_argument("--batch", type=Path, required=True)
            item.add_argument("--kind", choices=("twitch", "groups", "dispatch"), required=True)
    return result


def validate_receipt_path(args):
    receipt = args.receipt.resolve()
    current = Path.cwd().resolve()
    target = args.repo.resolve()
    protected = [getattr(args, name, None) for name in (
        "input", "report", "baseline", "observed", "batch", "auth_script", "skip_missing_batch")]
    if any(path is not None and receipt == path.resolve() for path in protected):
        raise ValueError("Publication receipt must not overwrite an immutable input")
    if receipt.suffix != ".json":
        raise ValueError("Publication receipt must be a JSON artifact")
    if receipt.is_relative_to(current) and not receipt.is_relative_to(current / "output"):
        raise ValueError("In-checkout publication receipts belong in output artifacts")
    if args.command in {"catalog", "growth"} and receipt.is_relative_to(target):
        raise ValueError("Publication receipt must remain outside the public target checkout")
    if args.command in {"growth-checkpoint", "queue"} and receipt.is_relative_to(target):
        if not receipt.is_relative_to(target / "output"):
            raise ValueError("Backend publication receipt must remain outside persisted source data")


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "apply-dispatch-batch":
        from scripts.reconcile_twitch_official_queue import MASTER, STATE, CHECKPOINT as QUEUE_CHECKPOINT, apply_queue_batch
        batch = read_json(args.batch)
        if (not isinstance(batch, dict) or batch.get("records") != []
                or "follower_candidates" in batch):
            raise ValueError("Dispatch receipt must not modify admissions or queue candidates")
        master, state, checkpoint = read_json(MASTER), read_json(STATE, optional=True), read_json(QUEUE_CHECKPOINT)
        next_master, next_state, next_checkpoint = apply_queue_batch(master, state, checkpoint, batch)
        if next_master != master or next_checkpoint != checkpoint:
            raise ValueError("Dispatch receipt attempted to modify non-owned queue state")
        if next_state != state or not STATE.is_file():
            write_json(STATE, next_state)
        return 0
    if args.command == "render-queue-status":
        from scripts.export_scheduler_queue_status import export_status
        from radar_backend.domain.growth import timestamp
        observed = timestamp(args.observed_at)
        if observed is None:
            raise ValueError("Timezone-aware queue publication clock required")
        export_status(now=observed)
        return 0
    validate_receipt_path(args)
    # A report from an earlier invocation is never a fresh push receipt.
    args.receipt.unlink(missing_ok=True)
    if args.command == "catalog" and args.skip_missing_batch and not args.skip_missing_batch.is_file():
        print("STEAM_PUBLICATION_SKIPPED discovery_only")
        return 0
    revision = args.input_revision or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()
    if args.command in {"catalog", "growth"}:
        if not os.environ.get("FRONTEND_REPO_TOKEN"):
            raise ValueError("FRONTEND_REPO_TOKEN is required for public publication")
        common = dict(input_revision=revision, max_attempts=args.max_attempts, auth_script=args.auth_script)
        if args.command == "catalog":
            receipt = publish_catalog(args.repo.resolve(), read_json(args.input), **common)
        else:
            receipt = publish_growth(args.repo.resolve(), read_json(args.report), **common)
        acknowledgement = "published=true"
    elif args.command == "growth-checkpoint":
        receipt = publish_growth_checkpoint(args.repo.resolve(), read_json(args.baseline),
                    read_json(args.observed), read_json(args.report), input_revision=revision,
                    max_attempts=args.max_attempts)
        acknowledgement = "state_persisted=true"
    else:
        receipt = publish_queue_batch(args.repo.resolve(), read_json(args.batch), kind=args.kind,
                    input_revision=revision, max_attempts=args.max_attempts)
        acknowledgement = "state_persisted=true"
    write_json(args.receipt, receipt.to_dict())
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(acknowledgement + "\n")
    print("STEAM_PUBLICATION", json.dumps(receipt.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
