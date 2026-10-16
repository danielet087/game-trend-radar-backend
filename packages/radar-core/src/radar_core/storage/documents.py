"""Lossless, content-addressed snapshots with a single authoritative writer.

Files are immutable byte strings, including unknown JSON fields and large IDs.
Only a source head is mutable. Its CAS statement and an AFTER trigger atomically
publish a complete manifest and its replication event. Transport batches are
never assumed to be transactions.

Publishing, replication, export and GC acquire the same database-backed lease.
Schema v2 fences expired writers and blocks legacy Git imports after a source
is explicitly activated as a database primary. No transport batch is assumed
to be transactional.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
from functools import wraps
from typing import Iterable

from .sql import SqlClient, SqliteClient, StorageError


CHUNK_BYTES = 35 * 1024  # Base64 plus digest stays below 48 KiB per row.
MAX_BATCH_BYTES = 440 * 1024  # Leave room for provider-specific wire wrappers.
MAX_OBJECT_BYTES = 1024 * 1024 * 1024
MANIFEST_FORMAT = "radar-source-manifest-v1"
SCHEMA_VERSION = 2
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _leased(purpose):
    def decorate(function):
        @wraps(function)
        def guarded(client, *args, **kwargs):
            from .primary_store import maintenance
            with maintenance(client, purpose):
                return function(client, *args, **kwargs)
        return guarded
    return decorate

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS radar_chunks (
        digest TEXT PRIMARY KEY, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS radar_objects (
        digest TEXT PRIMARY KEY, raw_bytes INTEGER NOT NULL,
        chunk_digests_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS radar_sources (
        source_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
        manifest_digest TEXT NOT NULL, generation INTEGER NOT NULL,
        captured_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS radar_revisions (
        source_id TEXT NOT NULL, revision TEXT NOT NULL,
        manifest_digest TEXT NOT NULL,
        PRIMARY KEY (source_id, revision))""",
    """CREATE TABLE IF NOT EXISTS radar_outbox (
        source_id TEXT NOT NULL, generation INTEGER NOT NULL,
        revision TEXT NOT NULL, manifest_digest TEXT NOT NULL,
        captured_at TEXT NOT NULL, acked_at TEXT,
        PRIMARY KEY (source_id, generation))""",
    """CREATE INDEX IF NOT EXISTS radar_outbox_pending
        ON radar_outbox (acked_at, captured_at, source_id, generation)""",
]
for _operation in ("INSERT", "UPDATE"):
    _SCHEMA.extend([
        f"""CREATE TRIGGER IF NOT EXISTS radar_revision_guard_{_operation.lower()}
        BEFORE {_operation} ON radar_sources
        WHEN EXISTS (SELECT 1 FROM radar_revisions
          WHERE source_id = NEW.source_id AND revision = NEW.revision
            AND manifest_digest <> NEW.manifest_digest)
        BEGIN SELECT RAISE(ABORT, 'radar_revision_conflict'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS radar_source_event_{_operation.lower()}
        AFTER {_operation} ON radar_sources
        BEGIN
          INSERT OR IGNORE INTO radar_revisions
            (source_id, revision, manifest_digest)
            VALUES (NEW.source_id, NEW.revision, NEW.manifest_digest);
          INSERT INTO radar_outbox
            (source_id, generation, revision, manifest_digest, captured_at)
            VALUES (NEW.source_id, NEW.generation, NEW.revision,
                    NEW.manifest_digest, NEW.captured_at);
        END""",
    ])


def _error(code, message):
    raise StorageError(code, message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, AttributeError):
        _error("INVALID_CAPTURE_TIME", "Snapshot capture time must include a timezone.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value, name):
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        _error("INVALID_IDENTIFIER", f"A nonempty {name} of at most 512 characters is required.")


def _path(value):
    if (not isinstance(value, str) or not value or "\x00" in value or "\\" in value
            or PurePosixPath(value).is_absolute() or ".." in value.split("/")
            or str(PurePosixPath(value)) != value):
        _error("INVALID_PATH", "Snapshot paths must be canonical relative POSIX paths.")


def _run(client, statements: Iterable[tuple], *, max_statements=25):
    """Bound both statement count and approximate request size, not atomicity."""
    result, batch, size = [], [], 0
    for sql, params in statements:
        params = tuple(params)
        item_size = len(_json([sql, params]).encode("utf-8")) + len(params) * 80 + 300
        if item_size > MAX_BATCH_BYTES:
            _error("OBJECT_METADATA_TOO_LARGE", "A snapshot metadata row exceeds the bounded request size.")
        if batch and (len(batch) >= max_statements or size + item_size > MAX_BATCH_BYTES):
            from .primary_store import heartbeat
            heartbeat(client)
            result.extend(client.batch(batch))
            batch, size = [], 0
        batch.append((sql, params))
        size += item_size
    if batch:
        from .primary_store import heartbeat
        heartbeat(client)
        result.extend(client.batch(batch))
    return result


def initialize(client: SqlClient):
    """Install/migrate v1 to v2, refusing unknown future versions."""
    client.execute("""CREATE TABLE IF NOT EXISTS radar_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)""")
    client.execute("INSERT OR IGNORE INTO radar_schema (singleton,version) VALUES (1,?)",
                   (1,))
    versions = client.execute("SELECT version FROM radar_schema WHERE singleton=1")
    if len(versions) != 1 or versions[0]["version"] not in (1, SCHEMA_VERSION):
        _error("UNSUPPORTED_SCHEMA", "The storage schema version requires an explicit migration.")
    if versions[0]["version"] == SCHEMA_VERSION:
        return
    _run(client, ((sql, ()) for sql in _SCHEMA))
    from .primary_store import migrate_schema
    migrate_schema(client)


def _rows(client, table, columns, digests, *, payload=False):
    # Table/column arguments are internal constants, never user input.
    digests = sorted(set(digests))
    statements = []
    for offset in range(0, len(digests), 50):
        group = digests[offset:offset + 50]
        statements.append((f"SELECT {columns} FROM {table} WHERE digest IN "
                           f"({','.join('?' for _ in group)})", group))
    # At most ~10 MiB of payload per response, below the transport's 32 MiB cap.
    return [row for rows in _run(client, statements, max_statements=4 if payload else 25)
            for row in rows]


def _object(row):
    try:
        digest, length = row["digest"], row["raw_bytes"]
        chunks = json.loads(row["chunk_digests_json"])
        if (not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
                or type(length) is not int or not 0 <= length <= MAX_OBJECT_BYTES
                or not isinstance(chunks, list) or not chunks
                or not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in chunks)):
            raise ValueError
        return {"digest": digest, "raw_bytes": length, "chunk_digests_json": _json(chunks)}
    except (KeyError, ValueError, TypeError):
        _error("CORRUPT_OBJECT", "Stored object metadata is invalid.")


def _get_objects(client, digests, *, require=True):
    wanted = set(digests)
    objects = {row["digest"]: _object(row) for row in _rows(
        client, "radar_objects", "digest,raw_bytes,chunk_digests_json", wanted)}
    if require and set(objects) != wanted:
        _error("MISSING_OBJECT", "A snapshot references a missing object.")
    return objects


def _get_chunks(client, digests):
    wanted = set(digests)
    chunks = {}
    for row in _rows(client, "radar_chunks", "digest,payload", wanted, payload=True):
        try:
            digest, payload = row["digest"], row["payload"]
            if not isinstance(payload, str) or len(payload) > 48 * 1024 - 512:
                raise ValueError
            raw = base64.b64decode(payload, validate=True)
            if len(raw) > CHUNK_BYTES or _sha(raw) != digest:
                raise ValueError
            chunks[digest] = raw
        except (KeyError, ValueError, TypeError):
            _error("CORRUPT_CHUNK", "A stored chunk failed its content hash check.")
    if set(chunks) != wanted:
        _error("MISSING_CHUNK", "A snapshot references a missing chunk.")
    return chunks


def _chunk_ids(objects):
    return {digest for obj in objects.values() for digest in json.loads(obj["chunk_digests_json"])}


def _decode(obj, chunks):
    try:
        compressed = b"".join(chunks[digest] for digest in json.loads(obj["chunk_digests_json"]))
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(obj["raw_bytes"] + 1)
        if len(raw) != obj["raw_bytes"] or _sha(raw) != obj["digest"]:
            raise ValueError
        return raw
    except (ValueError, OSError, EOFError, KeyError):
        _error("CORRUPT_OBJECT", "A stored object failed its byte count or content hash check.")


def _encode(raw):
    compressed = gzip.compress(raw, mtime=0)
    chunks, ids = {}, []
    for offset in range(0, len(compressed), CHUNK_BYTES):
        piece = compressed[offset:offset + CHUNK_BYTES]
        digest = _sha(piece)
        ids.append(digest)
        chunks[digest] = piece
    return {"digest": _sha(raw), "raw_bytes": len(raw), "chunk_digests_json": _json(ids)}, chunks


@_leased("write")
def _put_graph(client, objects, chunks):
    from .primary_store import proof
    owner, fence, nonce = proof(client)
    known = {row["digest"] for row in _rows(client, "radar_chunks", "digest", chunks)}
    _run(client, (("INSERT OR IGNORE INTO radar_chunks (digest,payload,writer_owner,writer_fence,writer_nonce) VALUES (?,?,?,?,?)",
                   (digest, base64.b64encode(raw).decode("ascii"), owner, fence, nonce))
                  for digest, raw in sorted(chunks.items()) if digest not in known))
    known_objects = _get_objects(client, objects, require=False)
    _run(client, (("INSERT OR IGNORE INTO radar_objects "
                   "(digest,raw_bytes,chunk_digests_json,writer_owner,writer_fence,writer_nonce) VALUES (?,?,?,?,?,?)",
                   (digest, obj["raw_bytes"], obj["chunk_digests_json"], owner, fence, nonce))
                  for digest, obj in sorted(objects.items()) if digest not in known_objects))


def _ensure_raw_objects(client, raw_objects):
    objects = _get_objects(client, raw_objects, require=False)
    new_objects, chunks = {}, {}
    for digest, raw in raw_objects.items():
        if digest not in objects:
            obj, encoded = _encode(raw)
            new_objects[digest] = obj
            chunks.update(encoded)
    _put_graph(client, new_objects, chunks)


def _manifest(raw):
    try:
        value = json.loads(raw)
        if value.get("format") != MANIFEST_FORMAT or not isinstance(value.get("files"), list):
            raise ValueError
        files = {}
        for entry in value["files"]:
            path, digest, length = entry["path"], entry["digest"], entry["raw_bytes"]
            _path(path)
            if (path in files or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                    or type(length) is not int or not 0 <= length <= MAX_OBJECT_BYTES):
                raise ValueError
            files[path] = {"digest": digest, "raw_bytes": length}
        return files
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        _error("CORRUPT_MANIFEST", "A source manifest is invalid.")


def _graph(client, roots):
    """Download and verify each reachable chunk once across all supplied heads."""
    roots = set(roots)
    objects = _get_objects(client, roots)
    chunks = _get_chunks(client, _chunk_ids(objects))
    manifests = {digest: _manifest(_decode(objects[digest], chunks)) for digest in roots}
    needed = {entry["digest"] for files in manifests.values() for entry in files.values()}
    objects.update(_get_objects(client, needed - set(objects)))
    chunks.update(_get_chunks(client, _chunk_ids(objects) - set(chunks)))
    # A hash identifies exact raw bytes; check each shared object only once.
    for obj in objects.values():
        _decode(obj, chunks)
    for files in manifests.values():
        for entry in files.values():
            if objects[entry["digest"]]["raw_bytes"] != entry["raw_bytes"]:
                _error("CORRUPT_MANIFEST", "A manifest byte count disagrees with its object.")
    return manifests, objects, chunks


def _head(client, source_id):
    rows = client.execute("SELECT source_id,revision,manifest_digest,generation,captured_at FROM radar_sources WHERE source_id=?", (source_id,))
    return dict(rows[0]) if rows else None


@_leased("write")
def _advance(client, head, expected, *, writer_kind="archive", operation=None):
    """One statement; the source head, revision guard and outbox are atomic."""
    sql = """INSERT INTO radar_sources
        (source_id,revision,manifest_digest,generation,captured_at,
         writer_owner,writer_fence,writer_nonce,writer_kind,operation_id,payload_revision,change_manifest_json,deleted_json,delivery_target)
        SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,? WHERE ?=0 OR EXISTS
          (SELECT 1 FROM radar_sources WHERE source_id=? AND generation=?)
        ON CONFLICT(source_id) DO UPDATE SET
          revision=excluded.revision, manifest_digest=excluded.manifest_digest,
          generation=excluded.generation, captured_at=excluded.captured_at,
          writer_owner=excluded.writer_owner,writer_fence=excluded.writer_fence,writer_nonce=excluded.writer_nonce,
          writer_kind=excluded.writer_kind,operation_id=excluded.operation_id,payload_revision=excluded.payload_revision,
          change_manifest_json=excluded.change_manifest_json,deleted_json=excluded.deleted_json,delivery_target=excluded.delivery_target
        WHERE radar_sources.generation=?
          AND radar_sources.captured_at<=excluded.captured_at
          AND radar_sources.generation<excluded.generation
        RETURNING source_id,revision,manifest_digest,generation,captured_at"""
    params = tuple(head[key] for key in ("source_id", "revision", "manifest_digest", "generation", "captured_at"))
    from .primary_store import proof
    op = operation or {}
    params += (*proof(client), writer_kind, op.get("operation_id", ""), op.get("payload_revision", ""),
               op.get("change_manifest_json", "{}"), op.get("deleted_json", "[]"), op.get("delivery_target", "git"))
    try:
        result = client.execute(sql, params + (expected, head["source_id"], expected, expected))
    except StorageError:
        # A response can be lost after commit. Confirm the immutable target head.
        if _head(client, head["source_id"]) == head:
            return head
        raise
    if result:
        return dict(result[0])
    actual = _head(client, head["source_id"])
    if actual == head:
        return actual
    _error("STALE_GENERATION", "Another snapshot advanced this source; retry from its current head.")


@_leased("write")
def publish_snapshot(client: SqlClient, source_id: str, revision: str,
                     files: dict[str, bytes], captured_at: str,
                     expected_generation: int | None = None) -> dict:
    """Stage exact file bytes, verify the stored graph, then CAS its source head."""
    from .primary_store import primary_status
    if primary_status(client, source_id)["mode"] != "legacy":
        _error("SOURCE_PRIMARY", "Git imports cannot overwrite an activated database namespace.")
    _identifier(source_id, "source ID")
    _identifier(revision, "revision")
    captured_at = _time(captured_at)
    if not isinstance(files, dict):
        _error("INVALID_FILES", "Snapshot files must map relative paths to bytes.")
    current = _head(client, source_id)
    expected = (current["generation"] if current else 0) if expected_generation is None else expected_generation
    if type(expected) is not int or expected < 0:
        _error("INVALID_GENERATION", "The expected generation must be a nonnegative integer.")
    raw_objects, entries = {}, []
    for path, raw in sorted(files.items()):
        _path(path)
        if not isinstance(raw, bytes) or len(raw) > MAX_OBJECT_BYTES:
            _error("INVALID_FILE_BYTES", "Snapshot files must be byte strings within the object size limit.")
        digest = _sha(raw)
        raw_objects[digest] = raw
        entries.append({"path": path, "digest": digest, "raw_bytes": len(raw)})
    manifest = _json({"format": MANIFEST_FORMAT, "files": entries}).encode("utf-8")
    root = _sha(manifest)
    history = client.execute("SELECT manifest_digest FROM radar_revisions WHERE source_id=? AND revision=?",
                             (source_id, revision))
    if history and history[0]["manifest_digest"] != root:
        _error("REVISION_CONFLICT", "A source revision was already recorded with different file bytes.")
    if current and current["revision"] == revision:
        if current["manifest_digest"] != root:
            _error("REVISION_CONFLICT", "The current revision contains different file bytes.")
        _graph(client, [root])
        return current
    if expected != (current["generation"] if current else 0):
        _error("STALE_GENERATION", "The expected source generation is no longer current.")
    if current and captured_at < current["captured_at"]:
        _error("STALE_CAPTURE", "An older capture cannot replace a newer source snapshot.")
    raw_objects[root] = manifest
    _ensure_raw_objects(client, raw_objects)
    _graph(client, [root])  # Verify actual server bytes before making them visible.
    return _advance(client, {"source_id": source_id, "revision": revision,
                    "manifest_digest": root, "generation": expected + 1,
                    "captured_at": captured_at}, expected)


@_leased("read")
def read_snapshot(client: SqlClient, source_id: str) -> tuple[dict, dict[str, bytes]]:
    """Read one immutable complete generation, even if the head advances later."""
    current = _head(client, source_id)
    if not current:
        _error("SOURCE_NOT_FOUND", "The requested source has no published snapshot.")
    manifests, objects, chunks = _graph(client, [current["manifest_digest"]])
    files = {path: _decode(objects[entry["digest"]], chunks)
             for path, entry in manifests[current["manifest_digest"]].items()}
    return current, files


def read_local_snapshot(client: SqliteClient, source_id: str) -> tuple[dict, dict[str, bytes]]:
    """Verify a private read-only SQLite backup, including schema-v1 archives."""
    if not isinstance(client, SqliteClient):
        _error("LOCAL_READ_ONLY_REQUIRED", "Lease-free reads are restricted to a local SQLite backup.")
    rows = client.execute("PRAGMA query_only")
    if not rows or next(iter(rows[0].values())) != 1:
        _error("LOCAL_READ_ONLY_REQUIRED", "Enable SQLite query_only before lease-free backup reads.")
    return read_snapshot.__wrapped__(client, source_id)


@_leased("write")
def _ack(client, source_id, generation):
    from .primary_store import proof
    return client.execute("""UPDATE radar_outbox SET acked_at=?,writer_owner=?,writer_fence=?,writer_nonce=?
        WHERE source_id=? AND generation<=? AND acked_at IS NULL
        RETURNING source_id,generation""", (_now(), *proof(client), source_id, generation))


def sync_replica(primary: SqlClient, replica: SqlClient, max_events=100) -> dict:
    """Replay a bounded capture; later publications belong to the next pass.

    The target heads and event selection share one primary lease. A collector
    may publish while D1 is being copied, so pending events after this capture
    must not turn an otherwise verified pass into a moving-target failure.
    """
    if type(max_events) is not int or not 1 <= max_events <= 10000:
        _error("INVALID_EVENT_LIMIT", "Replication requires an event limit between 1 and 10000.")
    from .primary_store import maintenance
    with maintenance(primary, "read"):
        target_heads = _heads(primary)
        events_at_start = primary.execute(
            "SELECT COUNT(*) AS count FROM radar_outbox WHERE acked_at IS NULL")[0]["count"]
        events = primary.execute("""SELECT * FROM radar_outbox WHERE acked_at IS NULL
            ORDER BY captured_at,source_id,generation LIMIT ?""", (max_events,))
    latest = {}
    for event in events:
        if event["generation"] > latest.get(event["source_id"], {}).get("generation", 0):
            latest[event["source_id"]] = event
    report = {"status": "passed", "sources_synced": 0, "events_acked": 0, "errors": [],
              "target_heads": target_heads, "events_pending_at_start": events_at_start,
              "events_selected": len(events)}
    for source_id, event in latest.items():
        target = {key: event[key] for key in
                  ("source_id", "revision", "manifest_digest", "generation", "captured_at")}
        try:
            with maintenance(primary, "read"):
                _, objects, chunks = _graph(primary, [target["manifest_digest"]])
            with maintenance(replica, "replicate"):
                target = _replicate_one(primary, replica, source_id, target, objects, chunks)
            # Acknowledgement only follows verified server readback and head CAS.
            report["events_acked"] += len(_ack(primary, source_id, target["generation"]))
            report["sources_synced"] += 1
        except StorageError as error:
            report["errors"].append({"source_id": source_id, "code": error.code})
    # Pins, activation and projection ACKs can change without a new source head.
    # They therefore need an independent durable maintenance sweep.
    try:
        from .primary_store import maintenance, retained_roots, metadata_snapshot, install_metadata
        with maintenance(primary, "read"):
            metadata = metadata_snapshot(primary)
            roots = retained_roots(primary)
            _, objects, chunks = _graph(primary, roots)
        with maintenance(replica, "replicate"):
            _put_graph(replica, objects, chunks)
            _graph(replica, roots)
            install_metadata(replica, metadata, replica=True)
    except StorageError as error:
        report["errors"].append({"source_id": "metadata", "code": error.code})
    # Count each captured source boundary independently of wall-clock times:
    # source capture clocks can be reused and are not queue arrival timestamps.
    boundaries = " OR ".join("(source_id=? AND generation<=?)" for _ in target_heads) or "0"
    parameters = tuple(value for head in target_heads for value in (head["source_id"], head["generation"]))
    pending = primary.execute(f"""SELECT COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN {boundaries} THEN 1 ELSE 0 END),0) AS captured
        FROM radar_outbox WHERE acked_at IS NULL""", parameters)[0]
    report["events_pending"] = pending["total"]
    report["events_pending_in_target"] = pending["captured"]
    report["events_deferred"] = pending["total"] - pending["captured"]
    if report["errors"] or report["events_pending_in_target"]:
        report["status"] = "partial" if report["sources_synced"] else "failed"
    return report


def _replicate_one(primary, replica, source_id, target, objects, chunks):
    existing = _head(replica, source_id)
    if existing and existing["generation"] >= target["generation"]:
        primary_head = _head(primary, source_id)
        if existing["generation"] > primary_head["generation"]:
            _error("REPLICA_AHEAD", "Replica generation exceeds the authoritative source.")
        if existing["generation"] == target["generation"] and existing != target:
            _error("REPLICA_CONFLICT", "Replica generation refers to a different source snapshot.")
        if existing["generation"] > target["generation"]:
            evidence = primary.execute("SELECT source_id,revision,manifest_digest,generation,captured_at "
                                       "FROM radar_outbox WHERE source_id=? AND generation=?",
                                       (source_id, existing["generation"]))
            if not evidence:
                # An already acknowledged outbox row can be reclaimed while
                # another pass is copying D1. Primary operation receipts retain
                # the complete canonical head, including generation and clock.
                evidence = primary.execute("SELECT source_id,revision,manifest_digest,generation,captured_at "
                                           "FROM radar_operations WHERE source_id=? AND generation=? LIMIT 1",
                                           (source_id, existing["generation"]))
            authoritative = dict(evidence[0]) if evidence else primary_head
            if authoritative != existing:
                _error("REPLICA_CONFLICT", "Replica history differs from the authoritative event.")
        _graph(replica, [existing["manifest_digest"]])
        return existing
    _put_graph(replica, objects, chunks)
    _graph(replica, [target["manifest_digest"]])
    _advance(replica, target, existing["generation"] if existing else 0, writer_kind="replica")
    return target


def _heads(client):
    return [dict(row) for row in client.execute("SELECT source_id,revision,manifest_digest,generation,captured_at FROM radar_sources ORDER BY source_id")]


@_leased("gc")
def garbage_collect(client: SqlClient, *, max_delete=10000, replica_mode=False,
                    exclusive=False) -> dict:
    """Reclaim unreachable data under the shared remote lease.

    ``exclusive=True`` is retained as an explicit maintenance request. The
    database lease enforces exclusion across repositories and active uploaders.
    On the primary retain every unacknowledged manifest; on the replica its local
    trigger events are not an upstream queue and only current heads are roots.
    """
    if exclusive is not True:
        _error("EXCLUSIVE_ACCESS_REQUIRED", "Garbage collection requires the explicit maintenance request.")
    if type(max_delete) is not int or max_delete < 1:
        _error("INVALID_DELETE_LIMIT", "Garbage collection requires a positive delete limit.")
    before = _heads(client)
    roots = {head["manifest_digest"] for head in before}
    from .primary_store import retained_roots, proof
    roots.update(retained_roots(client))
    if not replica_mode:
        roots.update(row["manifest_digest"] for row in client.execute(
            "SELECT manifest_digest FROM radar_outbox WHERE acked_at IS NULL"))
    _, reachable, _ = _graph(client, roots)
    if _heads(client) != before:
        _error("STALE_GENERATION", "Source heads changed during garbage collection planning.")
    outbox_condition = "1=1" if replica_mode else "acked_at IS NOT NULL"
    owner, fence, _ = proof(client)
    client.execute(f"""UPDATE radar_outbox SET gc_owner=?,gc_fence=?
        WHERE {outbox_condition} AND NOT EXISTS
        (SELECT 1 FROM radar_sources s WHERE s.source_id=radar_outbox.source_id
          AND s.generation=radar_outbox.generation)
        AND (source_id,generation) IN
          (SELECT source_id,generation FROM radar_outbox WHERE {outbox_condition} LIMIT ?)""", (owner, fence, max_delete))
    removed_events = client.execute(f"""DELETE FROM radar_outbox
        WHERE {outbox_condition} AND NOT EXISTS
        (SELECT 1 FROM radar_sources s WHERE s.source_id=radar_outbox.source_id
          AND s.generation=radar_outbox.generation)
        AND (source_id,generation) IN
          (SELECT source_id,generation FROM radar_outbox WHERE {outbox_condition} LIMIT ?)
        RETURNING source_id,generation""", (max_delete,))
    all_objects = {row["digest"] for row in client.execute("SELECT digest FROM radar_objects")}
    doomed_objects = sorted(all_objects - set(reachable))[:max_delete]
    _delete_digests(client, "radar_objects", doomed_objects)
    # Keep chunks referenced even by objects awaiting a later bounded GC pass.
    remaining = {row["digest"]: _object(row) for row in client.execute("SELECT * FROM radar_objects")}
    protected_chunks = _chunk_ids(remaining)
    all_chunks = {row["digest"] for row in client.execute("SELECT digest FROM radar_chunks")}
    doomed_chunks = sorted(all_chunks - protected_chunks)[:max_delete]
    _delete_digests(client, "radar_chunks", doomed_chunks)
    return {"status": "passed", "objects_deleted": len(doomed_objects),
            "chunks_deleted": len(doomed_chunks), "events_deleted": len(removed_events),
            "objects_retained": len(remaining), "chunks_retained": len(all_chunks) - len(doomed_chunks)}


def _delete_digests(client, table, digests):
    from .primary_store import proof
    owner, fence, _ = proof(client)
    statements = []
    for offset in range(0, len(digests), 50):
        group = digests[offset:offset + 50]
        statements.append((f"UPDATE {table} SET gc_owner=?,gc_fence=? WHERE digest IN ({','.join('?' for _ in group)})",
                           (owner, fence, *group)))
        statements.append((f"DELETE FROM {table} WHERE digest IN ({','.join('?' for _ in group)})", group))
    _run(client, statements)


@_leased("read")
def export_sqlite(client: SqlClient, path) -> dict:
    """Export verified current heads and their complete graph as portable SQLite.

    Historical orphan blobs are omitted. Each current source retains its outbox
    acknowledgement; one full current snapshot supersedes older replay events.
    A successful export is independently reopened and all raw bytes revalidated.
    """
    destination = Path(path)
    if destination.exists():
        _error("EXPORT_EXISTS", "Refusing to overwrite an existing SQLite export.")
    heads = _heads(client)
    from .primary_store import retained_roots, metadata_snapshot, install_metadata
    metadata = metadata_snapshot(client)
    roots = {head["manifest_digest"] for head in heads} | retained_roots(client)
    _, objects, chunks = _graph(client, roots)
    revisions = client.execute("SELECT * FROM radar_revisions ORDER BY source_id,revision")
    current_events = {}
    for head in heads:
        rows = client.execute("SELECT * FROM radar_outbox WHERE source_id=? AND generation=?",
                              (head["source_id"], head["generation"]))
        if len(rows) != 1:
            _error("MISSING_EVENT", "A source head has no atomic publication event.")
        current_events[head["source_id"]] = rows[0]
    if _heads(client) != heads:
        _error("STALE_GENERATION", "Source heads changed during export; retry the complete export.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = None
    try:
        output = SqliteClient(destination)
        initialize(output)
        _put_graph(output, objects, chunks)
        for head in heads:
            _advance(output, head, 0)
            acked = current_events[head["source_id"]]["acked_at"]
            if acked is not None:
                output.execute("UPDATE radar_outbox SET acked_at=? WHERE source_id=? AND generation=?",
                               (acked, head["source_id"], head["generation"]))
        _run(output, (("INSERT OR IGNORE INTO radar_revisions (source_id,revision,manifest_digest) VALUES (?,?,?)",
                       (row["source_id"], row["revision"], row["manifest_digest"])) for row in revisions))
        install_metadata(output, metadata)
        output.close()
        output = SqliteClient(destination)
        if _heads(output) != heads:
            _error("EXPORT_VERIFICATION_FAILED", "Restored source heads differ from the export capture.")
        manifests, _, _ = _graph(output, roots)
        integrity = output.execute("PRAGMA integrity_check")
        if not integrity or next(iter(integrity[0].values())) != "ok":
            _error("EXPORT_VERIFICATION_FAILED", "SQLite integrity verification failed.")
        output.close()
        output = None
        return {"status": "passed", "sources": len(heads),
                "files": sum(len(manifests[head["manifest_digest"]]) for head in heads),
                "objects": len(objects), "chunks": len(chunks),
                "raw_bytes": sum(obj["raw_bytes"] for obj in objects.values()),
                "sqlite_bytes": destination.stat().st_size}
    except Exception:
        if output is not None:
            output.close()
        destination.unlink(missing_ok=True)
        raise
