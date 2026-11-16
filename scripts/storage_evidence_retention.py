"""Release database evidence pins that no current document references.

Every hourly Steam intake pins the frontend snapshot it reads
(``steam-twitch-<manifest digest>``) before it requests anything. A pin is
needed again only while a current document still names that manifest, for
example an accepted admission's ``source_database_revision`` or a queued intake
record. Every other pin only keeps old document versions alive in Turso and D1.

The scan reads every current document of every database source, except the
Twitch metric archives, which never carry admission proofs. Any 64-character
hexadecimal token counts as a reference, so the rule leans towards keeping a
pin. Imported legacy Git pins and pins inside the grace window are always kept.
Each source is read in one call, so its documents come from one generation.

Releasing only deletes ``radar_evidence`` rows. The hourly storage sync then
garbage-collects the unreachable objects on Turso and, after installing the
same metadata, on D1. Daily SQL backups keep the released snapshots restorable.

The scheduled run caps how many pins one run may release. About one pin is
added per hour, so a much larger batch means the scan missed references and
the run refuses instead of deleting.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

from radar_core.storage import documents, primary_store
from radar_core.storage.sources import SOURCES, source_id
from radar_core.storage.sql import SqliteClient, StorageError, TursoClient

TOKEN = re.compile(rb"[0-9a-f]{64}")
SKIPPED_PREFIXES = ("data/twitch_history/", "data/twitch_series/")
LEGACY_PREFIX = "legacy-git:"
DELETE_BATCH = 50


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise StorageError("INVALID_EVIDENCE", "Evidence capture times must be timezone-aware.")
    return parsed


def referenced_tokens(client, *, skipped=SKIPPED_PREFIXES):
    """Every 64-hex token in the current documents, read one source at a time."""
    present = {row["source_id"] for row in client.execute("SELECT source_id FROM radar_sources")}
    if present - {source_id(spec) for spec in SOURCES}:
        # Its documents could reference a pin; never release without reading them.
        raise StorageError("UNSCANNED_SOURCE", "A database source is outside the reference scan.")
    tokens, scanned = set(), {"sources": 0, "files": 0, "raw_bytes": 0, "skipped_files": 0}
    for spec in SOURCES:
        identity = source_id(spec)
        if identity not in present:
            continue
        head, _ = primary_store.read_files(client, identity, paths=())
        wanted = sorted(path for path in head["manifest"] if not path.startswith(skipped))
        scanned["sources"] += 1
        scanned["skipped_files"] += len(head["manifest"]) - len(wanted)
        if not wanted:
            continue
        # A document deleted after the manifest read is simply absent here.
        _, files = primary_store.read_files(client, identity, paths=wanted)
        for raw in files.values():
            tokens.update(TOKEN.findall(raw))
            scanned["files"] += 1
            scanned["raw_bytes"] += len(raw)
    return {token.decode("ascii") for token in tokens}, scanned


def evidence_rows(client):
    return [dict(row) for row in client.execute(
        "SELECT evidence_id,source_id,origin_manifest_digest,manifest_digest,generation,captured_at "
        "FROM radar_evidence ORDER BY captured_at,evidence_id")]


def plan(rows, tokens, *, now, grace):
    """Keep legacy, recent and referenced pins; everything else may be released."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = now - grace
    keep = {"legacy": [], "recent": [], "referenced": []}
    release = []
    for row in rows:
        if row["evidence_id"].startswith(LEGACY_PREFIX):
            keep["legacy"].append(row)
        elif _time(row["captured_at"]) > cutoff:
            keep["recent"].append(row)
        elif row["origin_manifest_digest"] in tokens or row["manifest_digest"] in tokens:
            keep["referenced"].append(row)
        else:
            release.append(row)
    return keep, release


def estimate_release(client, release):
    """Encoded chunk bytes that become unreachable once these pins are gone."""
    released = {row["evidence_id"] for row in release}
    with primary_store.maintenance(client, "read"):
        roots = {head["manifest_digest"] for head in documents._heads(client)}
        roots.update(row["manifest_digest"] for row in client.execute(
            "SELECT manifest_digest FROM radar_operations WHERE delivery_target='git' AND projection_git_revision IS NULL"))
        roots.update(row["manifest_digest"] for row in client.execute(
            "SELECT manifest_digest FROM radar_outbox WHERE acked_at IS NULL"))
        pins = client.execute("SELECT evidence_id,manifest_digest FROM radar_evidence")
        kept_roots = roots | {row["manifest_digest"] for row in pins if row["evidence_id"] not in released}
        all_roots = roots | {row["manifest_digest"] for row in pins}
        manifests, _, _ = documents._read_manifests(client, all_roots)
        kept = documents._manifest_objects(manifests, kept_roots)
        freed = documents._manifest_objects(manifests, all_roots) - kept
        objects = documents._get_objects(client, kept | freed)
        sizes = {row["digest"]: row["bytes"] for row in client.execute(
            "SELECT digest,LENGTH(payload) AS bytes FROM radar_chunks")}
    kept_chunks = documents._chunk_ids({digest: objects[digest] for digest in kept})
    freed_chunks = documents._chunk_ids({digest: objects[digest] for digest in freed}) - kept_chunks
    return {"objects": len(freed), "chunks": len(freed_chunks),
            "encoded_chunk_bytes": sum(sizes.get(digest, 0) for digest in freed_chunks),
            "retained_encoded_chunk_bytes": sum(sizes.values())}


def release_pins(client, evidence_ids, *, batch=DELETE_BATCH):
    """Delete pins in short leases so collectors never wait long behind them."""
    released = []
    for offset in range(0, len(evidence_ids), batch):
        group = evidence_ids[offset:offset + batch]
        with primary_store.maintenance(client, "write"):
            rows = client.execute(
                "DELETE FROM radar_evidence WHERE evidence_id IN (" + ",".join("?" for _ in group) + ") RETURNING evidence_id",
                group)
        released.extend(row["evidence_id"] for row in rows)
    return released


def run(client, *, apply=False, grace_days=3, now=None, max_release=None):
    if type(grace_days) is not int or grace_days < 1:
        raise ValueError("grace_days must be a positive integer")
    if max_release is not None and (type(max_release) is not int or max_release < 1):
        raise ValueError("max_release must be a positive integer")
    now = now or datetime.now(timezone.utc)
    tokens, scanned = referenced_tokens(client)
    rows = evidence_rows(client)
    keep, release = plan(rows, tokens, now=now, grace=timedelta(days=grace_days))
    report = {
        "schema_version": 1, "mode": "apply" if apply else "dry_run", "checked_at": now.isoformat(),
        "grace_days": grace_days, "scanned": scanned, "evidence_total": len(rows),
        "kept": {reason: len(items) for reason, items in keep.items()},
        "release_candidates": len(release),
        "oldest_candidate": release[0]["captured_at"] if release else None,
        "newest_candidate": release[-1]["captured_at"] if release else None,
        "estimated_release": estimate_release(client, release),
        "released": 0,
    }
    if apply and max_release is not None and len(release) > max_release:
        report.update(status="refused", error="RELEASE_LIMIT_EXCEEDED", max_release=max_release)
        return report
    if apply and release:
        released = release_pins(client, [row["evidence_id"] for row in release])
        report["released"] = len(released)
        report["released_evidence_ids"] = released
    report["status"] = "passed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Release the planned pins; the default only reports.")
    parser.add_argument("--grace-days", type=int, default=3)
    parser.add_argument("--max-release", type=int, help="Refuse to release more pins than this in one run.")
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--sqlite", type=Path, help="Use a local SQLite database instead of Turso.")
    args = parser.parse_args(argv)
    client = None
    try:
        client = SqliteClient(args.sqlite) if args.sqlite else TursoClient.from_env(budget_seconds=900)
        report = run(client, apply=args.apply, grace_days=args.grace_days, max_release=args.max_release)
    except (StorageError, ValueError) as error:
        report = {"status": "failed", "error": getattr(error, "code", "INVALID_ARGUMENT")}
    finally:
        if isinstance(client, SqliteClient):
            client.close()
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {key: value for key, value in report.items() if key != "released_evidence_ids"}
    args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("## Database evidence retention\n\n```json\n" + json.dumps(summary, ensure_ascii=False, indent=2) + "\n```\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
