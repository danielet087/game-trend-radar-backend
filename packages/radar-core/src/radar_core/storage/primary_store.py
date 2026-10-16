"""Authoritative document writes, durable fencing, and scoped snapshot reads.

The global remote lease covers staging through publication and garbage
collection. Collection itself must run outside the lease. A namespace stays in
legacy import mode until explicitly activated; there is no automatic Git
fallback after activation.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import random
import time
import uuid

from .sql import StorageError

_LEASES = ContextVar("radar_storage_leases", default={})
_CLOCK = "CAST(strftime('%s','now') AS INTEGER)"
# Full snapshot reads and GC legitimately take longer than the old six-second
# retry window. Bound queueing independently of the renewable writer TTL, and
# always stop sooner when the HTTP client's overall operation budget expires.
LEASE_WAIT_SECONDS = 180
_PROOF_COLUMNS = {"writer_owner": "TEXT NOT NULL DEFAULT ''", "writer_fence": "INTEGER NOT NULL DEFAULT 0",
                  "writer_nonce": "TEXT NOT NULL DEFAULT ''"}


def _docs():
    from . import documents
    return documents


def _valid(prefix="NEW"):
    return ("EXISTS (SELECT 1 FROM radar_leases l WHERE l.name='global' "
            f"AND l.owner={prefix}.writer_owner AND l.fence={prefix}.writer_fence "
            f"AND l.expires_at>{_CLOCK})")


_ENFORCED = "EXISTS (SELECT 1 FROM radar_source_policy WHERE mode IN ('primary','replica'))"


def migrate_schema(client):
    """Add v2 metadata idempotently; old binaries fail their schema check."""
    client.execute("""CREATE TABLE IF NOT EXISTS radar_leases (
        name TEXT PRIMARY KEY, owner TEXT NOT NULL, fence INTEGER NOT NULL,
        expires_at INTEGER NOT NULL, purpose TEXT NOT NULL)""")
    with maintenance(client, purpose="migration"):
        version = client.execute("SELECT version FROM radar_schema WHERE singleton=1")[0]["version"]
        if version not in (1, 2):
            raise StorageError("UNSUPPORTED_SCHEMA", "An explicit storage migration is required.")
        for table in ("radar_chunks", "radar_objects", "radar_sources", "radar_outbox"):
            columns = dict(_PROOF_COLUMNS)
            if table in ("radar_chunks", "radar_objects", "radar_outbox"):
                columns.update(gc_owner="TEXT NOT NULL DEFAULT ''", gc_fence="INTEGER NOT NULL DEFAULT 0")
            if table == "radar_sources":
                columns.update(writer_kind="TEXT NOT NULL DEFAULT 'archive'", operation_id="TEXT NOT NULL DEFAULT ''",
                               payload_revision="TEXT NOT NULL DEFAULT ''", change_manifest_json="TEXT NOT NULL DEFAULT '{}'",
                               deleted_json="TEXT NOT NULL DEFAULT '[]'", delivery_target="TEXT NOT NULL DEFAULT 'git'")
            existing = {row["name"] for row in client.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    client.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        definitions = [
            """CREATE TABLE IF NOT EXISTS radar_source_policy (
                source_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('legacy','primary','replica')),
                activated_at TEXT, writer_owner TEXT NOT NULL DEFAULT '', writer_fence INTEGER NOT NULL DEFAULT 0,
                writer_nonce TEXT NOT NULL DEFAULT '')""",
            """CREATE TABLE IF NOT EXISTS radar_operations (
                source_id TEXT NOT NULL, operation_id TEXT NOT NULL, payload_revision TEXT NOT NULL,
                generation INTEGER NOT NULL, revision TEXT NOT NULL, manifest_digest TEXT NOT NULL,
                captured_at TEXT NOT NULL, changed INTEGER NOT NULL,
                change_manifest_json TEXT NOT NULL, deleted_json TEXT NOT NULL,
                delivery_target TEXT NOT NULL DEFAULT 'git',
                projection_git_revision TEXT, projection_generation INTEGER, projection_manifest_digest TEXT,
                writer_owner TEXT NOT NULL DEFAULT '', writer_fence INTEGER NOT NULL DEFAULT 0,
                writer_nonce TEXT NOT NULL DEFAULT '', PRIMARY KEY(source_id,operation_id))""",
            """CREATE TABLE IF NOT EXISTS radar_projections (
                source_id TEXT NOT NULL, git_revision TEXT NOT NULL, generation INTEGER NOT NULL,
                revision TEXT NOT NULL, manifest_digest TEXT NOT NULL, captured_at TEXT NOT NULL,
                writer_owner TEXT NOT NULL DEFAULT '', writer_fence INTEGER NOT NULL DEFAULT 0,
                writer_nonce TEXT NOT NULL DEFAULT '', PRIMARY KEY(source_id,git_revision))""",
            """CREATE TABLE IF NOT EXISTS radar_evidence (
                evidence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, origin_manifest_digest TEXT NOT NULL,
                generation INTEGER NOT NULL, revision TEXT NOT NULL, captured_at TEXT NOT NULL,
                manifest_digest TEXT NOT NULL, writer_owner TEXT NOT NULL DEFAULT '',
                writer_fence INTEGER NOT NULL DEFAULT 0, writer_nonce TEXT NOT NULL DEFAULT '')""",
        ]
        for sql in definitions:
            client.execute(sql)
        if "delivery_target" not in {row["name"] for row in client.execute("PRAGMA table_info(radar_operations)")}:
            client.execute("ALTER TABLE radar_operations ADD COLUMN delivery_target TEXT NOT NULL DEFAULT 'git'")
        # Old import SQL does not supply a fresh proof/nonce and is rejected even
        # if an old workflow initialized before the v2 migration or cutover.
        for event in ("INSERT", "UPDATE"):
            fresh = "" if event == "INSERT" else " OR NEW.writer_nonce=OLD.writer_nonce"
            client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_sources_{event.lower()}
                BEFORE {event} ON radar_sources WHEN {_ENFORCED}
                AND (NOT {_valid()} OR NEW.writer_nonce=''{fresh}
                  OR (EXISTS (SELECT 1 FROM radar_source_policy p WHERE p.source_id=NEW.source_id
                      AND p.mode='primary') AND NEW.writer_kind NOT IN ('primary','restore'))
                  OR (EXISTS (SELECT 1 FROM radar_source_policy p WHERE p.source_id=NEW.source_id
                      AND p.mode='replica') AND NEW.writer_kind NOT IN ('replica','restore')))
                BEGIN SELECT RAISE(ABORT,'radar_writer_fenced'); END""")
            client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_operation_{event.lower()}
                AFTER {event} ON radar_sources WHEN NEW.writer_kind='primary' AND NEW.operation_id<>''
                BEGIN INSERT INTO radar_operations
                    (source_id,operation_id,payload_revision,generation,revision,manifest_digest,
                     captured_at,changed,change_manifest_json,deleted_json,delivery_target,writer_owner,writer_fence,writer_nonce)
                    VALUES (NEW.source_id,NEW.operation_id,NEW.payload_revision,NEW.generation,NEW.revision,
                            NEW.manifest_digest,NEW.captured_at,1,NEW.change_manifest_json,NEW.deleted_json,NEW.delivery_target,
                            NEW.writer_owner,NEW.writer_fence,NEW.writer_nonce); END""")
        client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_sources_delete
            BEFORE DELETE ON radar_sources WHEN {_ENFORCED}
            BEGIN SELECT RAISE(ABORT,'radar_source_delete_forbidden'); END""")
        for table in ("radar_chunks", "radar_objects"):
            client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_{table}_insert
                BEFORE INSERT ON {table} WHEN {_ENFORCED} AND (NOT {_valid()} OR NEW.writer_nonce='')
                BEGIN SELECT RAISE(ABORT,'radar_writer_fenced'); END""")
            same = "NEW.digest=OLD.digest AND " + ("NEW.payload=OLD.payload" if table == "radar_chunks"
                    else "NEW.raw_bytes=OLD.raw_bytes AND NEW.chunk_digests_json=OLD.chunk_digests_json")
            gc = ("EXISTS (SELECT 1 FROM radar_leases l WHERE l.name='global' "
                  f"AND l.owner=NEW.gc_owner AND l.fence=NEW.gc_fence AND l.expires_at>{_CLOCK} "
                  "AND l.purpose IN ('gc','maintenance'))")
            client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_{table}_update
                BEFORE UPDATE ON {table} WHEN {_ENFORCED} AND NOT ({same} AND {gc})
                BEGIN SELECT RAISE(ABORT,'radar_immutable_object'); END""")
        for table in ("radar_chunks", "radar_objects", "radar_outbox"):
            gc = ("EXISTS (SELECT 1 FROM radar_leases l WHERE l.name='global' "
                  f"AND l.owner=OLD.gc_owner AND l.fence=OLD.gc_fence AND l.expires_at>{_CLOCK} "
                  "AND l.purpose IN ('gc','maintenance'))")
            client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_{table}_delete
                BEFORE DELETE ON {table} WHEN {_ENFORCED} AND NOT {gc}
                BEGIN SELECT RAISE(ABORT,'radar_gc_fenced'); END""")
        # Trigger-created outbox rows inherit authority from the just-published
        # source, so no rewrite of the v1 atomic event trigger is necessary.
        source_proof = ("EXISTS (SELECT 1 FROM radar_sources s JOIN radar_leases l ON l.name='global' "
                        "AND l.owner=s.writer_owner AND l.fence=s.writer_fence "
                        f"WHERE l.expires_at>{_CLOCK} AND s.source_id=NEW.source_id "
                        "AND s.generation=NEW.generation AND s.manifest_digest=NEW.manifest_digest)")
        client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_outbox_insert
            BEFORE INSERT ON radar_outbox WHEN {_ENFORCED} AND NOT {source_proof}
            BEGIN SELECT RAISE(ABORT,'radar_outbox_fenced'); END""")
        gc = ("EXISTS (SELECT 1 FROM radar_leases l WHERE l.name='global' "
              f"AND l.owner=NEW.gc_owner AND l.fence=NEW.gc_fence AND l.expires_at>{_CLOCK} "
              "AND l.purpose IN ('gc','maintenance'))")
        client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_outbox_update
            BEFORE UPDATE ON radar_outbox WHEN {_ENFORCED}
            AND NOT (({_valid()} AND NEW.writer_nonce<>OLD.writer_nonce) OR {gc})
            BEGIN SELECT RAISE(ABORT,'radar_outbox_fenced'); END""")
        for table in ("radar_source_policy", "radar_operations", "radar_projections", "radar_evidence"):
            for event in ("INSERT", "UPDATE"):
                enforced = f"({_ENFORCED} OR NEW.mode IN ('primary','replica'))" if table == "radar_source_policy" else _ENFORCED
                client.execute(f"""CREATE TRIGGER IF NOT EXISTS radar_v2_{table}_{event.lower()}
                    BEFORE {event} ON {table} WHEN {enforced} AND NOT {_valid()}
                    BEGIN SELECT RAISE(ABORT,'radar_metadata_fenced'); END""")
        client.execute("UPDATE radar_schema SET version=2 WHERE singleton=1")


class Lease:
    def __init__(self, client, purpose, ttl=120):
        self.client, self.purpose, self.ttl = client, purpose, ttl
        self.owner, self.fence = uuid.uuid4().hex, None
        self.last_renewed = 0.0
        self.clock = getattr(client, "clock", time.monotonic)
        self.sleep = getattr(client, "sleep", time.sleep)

    def _remaining_wait(self, deadline):
        now = self.clock()
        client_deadline = getattr(self.client, "deadline", None)
        if client_deadline is not None:
            if now >= client_deadline:
                raise StorageError("TIME_BUDGET_EXCEEDED", "The storage time budget was exhausted while waiting for its lease.")
            deadline = min(deadline, client_deadline)
        remaining = deadline - now
        if remaining <= 0:
            raise StorageError("LEASE_BUSY", "Storage lease contention exceeded the bounded wait budget.")
        return remaining

    def acquire(self):
        sql = f"""INSERT INTO radar_leases (name,owner,fence,expires_at,purpose)
            VALUES ('global',?,1,{_CLOCK}+?,?)
            ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,
              fence=radar_leases.fence+1,expires_at=excluded.expires_at,purpose=excluded.purpose
            WHERE radar_leases.expires_at<={_CLOCK}
            RETURNING owner,fence,expires_at"""
        deadline = self.clock() + LEASE_WAIT_SECONDS
        attempt = 0
        while True:
            self._remaining_wait(deadline)
            try:
                rows = self.client.execute(sql, (self.owner, self.ttl, self.purpose))
            except StorageError:
                rows = self.client.execute(f"SELECT owner,fence,expires_at FROM radar_leases "
                                           f"WHERE name='global' AND owner=? AND expires_at>{_CLOCK}", (self.owner,))
                if not rows:
                    raise
            if rows:
                self.fence = rows[0]["fence"]
                self.last_renewed = self.clock()
                return self
            owned = self.client.execute(f"SELECT owner,fence,expires_at FROM radar_leases "
                                        f"WHERE name='global' AND owner=? AND expires_at>{_CLOCK}", (self.owner,))
            if owned:
                self.fence = owned[0]["fence"]
                self.last_renewed = self.clock()
                return self
            # Jitter prevents independent repositories from retrying together;
            # no sleep or further SQL request may outlive the client's budget.
            delay = random.uniform(0.5, 1.0) * min(5.0, 2 ** min(attempt, 3))
            self.sleep(min(delay, self._remaining_wait(deadline)))
            attempt += 1

    def renew(self, force=False):
        if not force and self.clock() - self.last_renewed < self.ttl / 3:
            return
        rows = self.client.execute(f"""UPDATE radar_leases SET expires_at={_CLOCK}+?
            WHERE name='global' AND owner=? AND fence=? AND expires_at>{_CLOCK}
            RETURNING fence""", (self.ttl, self.owner, self.fence))
        if not rows:
            raise StorageError("LEASE_LOST", "The storage writer lease expired or was fenced by another writer.")
        self.last_renewed = self.clock()

    def release(self):
        self.client.execute("UPDATE radar_leases SET expires_at=0 WHERE name='global' AND owner=? AND fence=?",
                            (self.owner, self.fence))


@contextmanager
def maintenance(client, purpose="maintenance"):
    """Hold one remote lease; nesting on the same client reuses that lease."""
    existing = _LEASES.get().get(id(client))
    if existing:
        existing.renew()
        yield existing
        return
    lease = Lease(client, purpose).acquire()
    token = _LEASES.set({**_LEASES.get(), id(client): lease})
    try:
        yield lease
    finally:
        _LEASES.reset(token)
        # Failure to release must not conceal a successful commit or its error.
        # The database clock will expire this bounded lease automatically.
        try:
            lease.release()
        except StorageError:
            pass


def proof(client):
    lease = _LEASES.get().get(id(client))
    if not lease:
        raise StorageError("LEASE_REQUIRED", "Storage writes require the remote lease context.")
    lease.renew()
    return lease.owner, lease.fence, uuid.uuid4().hex


def heartbeat(client):
    lease = _LEASES.get().get(id(client))
    if lease:
        lease.renew()


def primary_status(client, source_id):
    rows = client.execute("SELECT mode,activated_at FROM radar_source_policy WHERE source_id=?", (source_id,))
    mode = rows[0]["mode"] if rows else "legacy"
    return {"source_id": source_id, "active": mode == "primary", "mode": mode,
            "activated_at": rows[0]["activated_at"] if rows else None}


def _set_policy(client, source_id, mode):
    owner, fence, nonce = proof(client)
    client.execute("""INSERT INTO radar_source_policy
        (source_id,mode,activated_at,writer_owner,writer_fence,writer_nonce) VALUES (?,?,?,?,?,?)
        ON CONFLICT(source_id) DO UPDATE SET mode=excluded.mode,activated_at=excluded.activated_at,
          writer_owner=excluded.writer_owner,writer_fence=excluded.writer_fence,writer_nonce=excluded.writer_nonce""",
        (source_id, mode, _docs()._now(), owner, fence, nonce))


def activate_primary(client, source_id, expected_generation=None):
    d = _docs()
    with maintenance(client, "activate"):
        head = d._head(client, source_id)
        if not head:
            raise StorageError("SOURCE_NOT_FOUND", "A complete verified source snapshot is required before activation.")
        if expected_generation is not None and head["generation"] != expected_generation:
            raise StorageError("STALE_GENERATION", "The source changed before activation.")
        if primary_status(client, source_id)["mode"] == "replica":
            raise StorageError("REPLICA_READ_ONLY", "Standby promotion requires a separate controlled procedure.")
        d._graph(client, [head["manifest_digest"]])
        _set_policy(client, source_id, "primary")
        return {**primary_status(client, source_id), **head}


def bootstrap_primary(client, source_id, files, revision, captured_at=None):
    d = _docs()
    with maintenance(client, "activate"):
        if d._head(client, source_id):
            raise StorageError("SOURCE_EXISTS", "Bootstrap only accepts a new explicit namespace.")
        d.publish_snapshot(client, source_id, revision, files, captured_at or d._now(), expected_generation=0)
        return activate_primary(client, source_id, expected_generation=1)


def _select(path, paths):
    return paths is None or any(path == scope.rstrip("/") or path.startswith(scope.rstrip("/") + "/") for scope in paths)


def resolve_git_revision(client, source_id, git_revision):
    d = _docs()
    rows = client.execute("SELECT source_id,generation,revision,manifest_digest,captured_at FROM radar_projections "
                          "WHERE source_id=? AND git_revision=?", (source_id, git_revision))
    if rows:
        return dict(rows[0])
    rows = client.execute("SELECT manifest_digest FROM radar_revisions WHERE source_id=? AND revision=?",
                          (source_id, git_revision))
    if rows:
        current = d._head(client, source_id)
        event = client.execute("SELECT source_id,generation,revision,manifest_digest,captured_at FROM radar_outbox "
                               "WHERE source_id=? AND revision=? ORDER BY generation DESC LIMIT 1", (source_id, git_revision))
        if event:
            return dict(event[0])
        if current and current["manifest_digest"] == rows[0]["manifest_digest"]:
            return current
        return {"source_id": source_id, "generation": None, "revision": git_revision,
                "manifest_digest": rows[0]["manifest_digest"], "captured_at": None}
    raise StorageError("REVISION_NOT_FOUND", "No acknowledged database snapshot matches this Git revision.")


def _load_manifest(client, source_id, revision=None, paths=None):
    d = _docs()
    head = d._head(client, source_id)
    if not head:
        raise StorageError("SOURCE_NOT_FOUND", "The source has no database snapshot.")
    root = head["manifest_digest"]
    if revision is not None and revision not in (head["revision"], root):
        mapped = client.execute("SELECT source_id,generation,revision,manifest_digest,captured_at FROM radar_projections "
                                "WHERE source_id=? AND git_revision=?", (source_id, revision))
        if mapped:
            head = dict(mapped[0])
            root = head["manifest_digest"]
        elif len(revision) == 64:
            root = revision
            events = client.execute("SELECT source_id,generation,revision,manifest_digest,captured_at FROM radar_outbox "
                                    "WHERE source_id=? AND manifest_digest=? ORDER BY generation DESC LIMIT 1", (source_id, root))
            if not events:
                events = client.execute("SELECT source_id,generation,revision,manifest_digest,captured_at FROM radar_projections "
                                        "WHERE source_id=? AND manifest_digest=? LIMIT 1", (source_id, root))
            if not events:
                evidence = client.execute("SELECT source_id,generation,revision,origin_manifest_digest AS manifest_digest,captured_at "
                                          "FROM radar_evidence WHERE source_id=? AND origin_manifest_digest=? LIMIT 1", (source_id, root))
                events = evidence
            if not events:
                historical = client.execute("SELECT revision FROM radar_revisions WHERE source_id=? AND manifest_digest=? LIMIT 1",
                                            (source_id, root))
                if not historical:
                    raise StorageError("REVISION_NOT_FOUND", "The requested manifest does not belong to this source.")
                head = {"source_id": source_id, "manifest_digest": root, "generation": None,
                        "revision": historical[0]["revision"], "captured_at": None}
            else:
                head = dict(events[0])
        else:
            head = resolve_git_revision(client, source_id, revision)
            root = head["manifest_digest"]
    objects = d._get_objects(client, [root], require=False)
    if root in objects:
        chunks = d._get_chunks(client, d._chunk_ids(objects))
        return head, d._manifest(d._decode(objects[root], chunks))
    evidence = client.execute("SELECT * FROM radar_evidence WHERE source_id=? AND origin_manifest_digest=?", (source_id, root))
    for item in evidence:
        obj = d._get_objects(client, [item["manifest_digest"]])
        chunks = d._get_chunks(client, d._chunk_ids(obj))
        manifest = d._manifest(d._decode(obj[item["manifest_digest"]], chunks))
        if paths is not None and all(any(_select(path, [scope]) for path in manifest) for scope in paths):
            return {"source_id": source_id, "generation": item["generation"], "revision": item["revision"],
                    "manifest_digest": root, "captured_at": item["captured_at"]}, manifest
    raise StorageError("HISTORY_PRUNED", "This historical snapshot is not retained by a live evidence reference.")


def read_files(client, source_id, paths=None, *, known_files=None, revision=None):
    d = _docs()
    if isinstance(paths, str):
        paths = (paths,)
    if paths is not None:
        paths = tuple(paths)
        for path in paths:
            d._path(path.rstrip("/"))
    with maintenance(client, "read"):
        head, manifest = _load_manifest(client, source_id, revision, paths)
        selected = {path: entry for path, entry in manifest.items() if _select(path, paths)}
        cached, needed = {}, {}
        for path, entry in selected.items():
            raw = (known_files or {}).get(path)
            if isinstance(raw, bytes) and len(raw) == entry["raw_bytes"] and d._sha(raw) == entry["digest"]:
                cached[path] = raw
            else:
                needed[path] = entry
        objects = d._get_objects(client, [entry["digest"] for entry in needed.values()])
        chunks = d._get_chunks(client, d._chunk_ids(objects))
        for path, entry in needed.items():
            raw = d._decode(objects[entry["digest"]], chunks)
            if len(raw) != entry["raw_bytes"]:
                raise StorageError("CORRUPT_MANIFEST", "Snapshot byte count differs from its object.")
            cached[path] = raw
        return {**head, "manifest": manifest}, cached


def _receipt(row):
    return {"source_id": row["source_id"], "generation": row["generation"],
            "manifest_digest": row["manifest_digest"], "revision": row["revision"],
            "operation_id": row["operation_id"], "changed": bool(row["changed"]),
            "projection_pending": row["delivery_target"] == "git" and row["projection_git_revision"] is None,
            "delivery_target": row["delivery_target"],
            "file_digests": json.loads(row["change_manifest_json"]), "deleted": json.loads(row["deleted_json"])}


def publish_patch(client, source_id, *, base_generation, changed, deleted=(), operation_id,
                  captured_at=None, payload_revision=None, projection_required=True):
    d = _docs()
    d._identifier(operation_id, "operation ID")
    payload_revision = payload_revision or operation_id
    d._identifier(payload_revision, "payload revision")
    if type(base_generation) is not int or base_generation < 1 or not isinstance(changed, dict) or type(projection_required) is not bool:
        raise StorageError("INVALID_PATCH", "A positive base generation and changed byte mapping are required.")
    deleted = tuple(sorted(set(deleted)))
    for path in (*changed, *deleted):
        d._path(path)
    if set(changed) & set(deleted) or any(not isinstance(raw, bytes) for raw in changed.values()):
        raise StorageError("INVALID_PATCH", "Changed byte strings and explicit deletions must not overlap.")
    with maintenance(client, "write"):
        if not primary_status(client, source_id)["active"]:
            raise StorageError("SOURCE_NOT_PRIMARY", "This namespace has not been activated for primary writes.")
        prior = client.execute("SELECT * FROM radar_operations WHERE source_id=? AND operation_id=?", (source_id, operation_id))
        if prior:
            if (prior[0]["payload_revision"] != payload_revision or
                    prior[0]["delivery_target"] != ("git" if projection_required else "database_only")):
                raise StorageError("OPERATION_CONFLICT", "An operation ID was reused for a different frozen payload.")
            return _receipt(prior[0])
        head, manifest = _load_manifest(client, source_id)
        if head["generation"] != base_generation:
            raise StorageError("STALE_GENERATION", "The source changed; merge the frozen batch against its current snapshot.")
        incoming, entries = {}, dict(manifest)
        changes = {}
        for path, raw in sorted(changed.items()):
            if len(raw) > d.MAX_OBJECT_BYTES:
                raise StorageError("INVALID_FILE_BYTES", "A changed document exceeds the object size limit.")
            digest = d._sha(raw)
            entries[path] = {"digest": digest, "raw_bytes": len(raw)}
            changes[path] = digest
            if entries[path] != manifest.get(path):
                incoming[digest] = raw
        for path in deleted:
            entries.pop(path, None)
        root_raw = d._json({"format": d.MANIFEST_FORMAT,
                           "files": [{"path": path, **entry} for path, entry in sorted(entries.items())]}).encode()
        root = d._sha(root_raw)
        op = {"operation_id": operation_id, "payload_revision": payload_revision,
              "change_manifest_json": d._json(changes), "deleted_json": d._json(deleted),
              "delivery_target": "git" if projection_required else "database_only"}
        if root != head["manifest_digest"]:
            incoming[root] = root_raw
            d._ensure_raw_objects(client, incoming)
            objects = d._get_objects(client, incoming)
            chunks = d._get_chunks(client, d._chunk_ids(objects))
            for obj in objects.values():
                d._decode(obj, chunks)
            when = d._time(captured_at or d._now())
            # Commit time orders snapshots; measurements keep their frozen clocks
            # inside file bytes and are never rewritten during retry.
            when = max(when, head["captured_at"])
            target = {"source_id": source_id, "generation": base_generation + 1,
                      "revision": "db:" + d._sha((source_id + "\0" + operation_id).encode()),
                      "manifest_digest": root, "captured_at": when}
            d._advance(client, target, base_generation, writer_kind="primary", operation=op)
        else:
            owner, fence, nonce = proof(client)
            client.execute("""INSERT OR IGNORE INTO radar_operations
                (source_id,operation_id,payload_revision,generation,revision,manifest_digest,captured_at,
                 changed,change_manifest_json,deleted_json,delivery_target,writer_owner,writer_fence,writer_nonce)
                VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?)""", (source_id, operation_id, payload_revision,
                head["generation"], head["revision"], root, head["captured_at"], op["change_manifest_json"],
                op["deleted_json"], op["delivery_target"], owner, fence, nonce))
        return _receipt(client.execute("SELECT * FROM radar_operations WHERE source_id=? AND operation_id=?",
                                       (source_id, operation_id))[0])


def projection_pending(client, source_id):
    return [_receipt(row) for row in client.execute("SELECT * FROM radar_operations WHERE source_id=? "
            "AND delivery_target='git' AND projection_git_revision IS NULL ORDER BY generation,operation_id", (source_id,))]


def acknowledge_projection(client, source_id, operation_id, git_revision, *, generation=None, manifest_digest=None):
    d = _docs()
    if not isinstance(git_revision, str) or len(git_revision) not in (40, 64) or any(c not in "0123456789abcdef" for c in git_revision):
        raise StorageError("INVALID_GIT_REVISION", "A real Git commit hash is required for publication acknowledgement.")
    with maintenance(client, "write"):
        rows = client.execute("SELECT * FROM radar_operations WHERE source_id=? AND operation_id=?", (source_id, operation_id))
        if not rows:
            raise StorageError("OPERATION_NOT_FOUND", "Only a committed operation can acknowledge publication.")
        row = rows[0]
        head = d._head(client, source_id)
        generation = head["generation"] if generation is None else generation
        manifest_digest = head["manifest_digest"] if manifest_digest is None else manifest_digest
        event = client.execute("SELECT source_id,generation,revision,manifest_digest,captured_at FROM radar_outbox "
                               "WHERE source_id=? AND generation=? AND manifest_digest=?", (source_id, generation, manifest_digest))
        if not event:
            event = client.execute("SELECT source_id,generation,revision,manifest_digest,captured_at FROM radar_operations "
                                   "WHERE source_id=? AND generation=? AND manifest_digest=? LIMIT 1", (source_id, generation, manifest_digest))
        if not event or generation < row["generation"]:
            raise StorageError("PROJECTION_MISMATCH", "The Git projection must identify a committed database generation.")
        projected = event[0]
        old = client.execute("SELECT manifest_digest,generation FROM radar_projections WHERE source_id=? AND git_revision=?",
                             (source_id, git_revision))
        if old and (old[0]["manifest_digest"], old[0]["generation"]) != (manifest_digest, generation):
            raise StorageError("PROJECTION_CONFLICT", "A Git revision already identifies a different database snapshot.")
        owner, fence, nonce = proof(client)
        client.execute("""INSERT OR IGNORE INTO radar_projections
            (source_id,git_revision,generation,revision,manifest_digest,captured_at,writer_owner,writer_fence,writer_nonce)
            VALUES (?,?,?,?,?,?,?,?,?)""", (source_id, git_revision, generation, projected["revision"],
            manifest_digest, projected["captured_at"], owner, fence, nonce))
        client.execute("""UPDATE radar_operations SET projection_git_revision=?,projection_generation=?,
            projection_manifest_digest=?,writer_owner=?,writer_fence=?,writer_nonce=?
            WHERE source_id=? AND operation_id=?""", (git_revision, generation, manifest_digest,
            owner, fence, nonce, source_id, operation_id))
        return {"source_id": source_id, "operation_id": operation_id, "git_revision": git_revision,
                "generation": generation, "manifest_digest": manifest_digest, "projection_pending": False}


def pin_evidence(client, source_id, *, revision, paths, evidence_id):
    d = _docs()
    d._identifier(evidence_id, "evidence ID")
    with maintenance(client, "write"):
        head, files = read_files(client, source_id, paths, revision=revision)
        if any(path.rstrip("/") not in files for path in paths):
            raise StorageError("INCOMPLETE_EVIDENCE", "Every exact evidence path must exist in the selected snapshot.")
        raw = d._json({"format": d.MANIFEST_FORMAT, "files": [
            {"path": path, "digest": d._sha(content), "raw_bytes": len(content)}
            for path, content in sorted(files.items())]}).encode()
        digest = d._sha(raw)
        existing = client.execute("SELECT * FROM radar_evidence WHERE evidence_id=?", (evidence_id,))
        if existing:
            if (existing[0]["source_id"], existing[0]["origin_manifest_digest"], existing[0]["manifest_digest"]) != (source_id, head["manifest_digest"], digest):
                raise StorageError("EVIDENCE_CONFLICT", "An evidence ID already names a different frozen snapshot.")
            return dict(existing[0])
        if head["generation"] is None:
            raise StorageError("HISTORY_PRUNED", "The historical generation cannot be established for new evidence.")
        d._ensure_raw_objects(client, {digest: raw})
        owner, fence, nonce = proof(client)
        client.execute("""INSERT OR IGNORE INTO radar_evidence
            (evidence_id,source_id,origin_manifest_digest,generation,revision,captured_at,manifest_digest,
             writer_owner,writer_fence,writer_nonce) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (evidence_id, source_id, head["manifest_digest"], head["generation"], head["revision"],
             head["captured_at"], digest, owner, fence, nonce))
        return dict(client.execute("SELECT * FROM radar_evidence WHERE evidence_id=?", (evidence_id,))[0])


def import_legacy_evidence(client, source_id, *, git_revision, files, captured_at=None):
    """Pin exact files fetched at a fixed Git commit before primary activation.

    The caller establishes their Git provenance. Generation zero explicitly
    identifies imported evidence rather than a canonical database generation.
    This does not publish a source head or invent a mapping to its current data.
    """
    d = _docs()
    d._identifier(source_id, "source ID")
    if (not isinstance(git_revision, str) or len(git_revision) not in (40, 64)
            or any(c not in "0123456789abcdef" for c in git_revision)):
        raise StorageError("INVALID_GIT_REVISION", "Legacy evidence requires a fixed Git commit hash.")
    required = {"data/twitch_tracking.json", "data/twitch_steam_discovery.json"}
    allowed = required | {"data/steam_upcoming.json", "data/twitch_steam_mapping.json"}
    if not isinstance(files, dict) or not required.issubset(files):
        raise StorageError("INCOMPLETE_EVIDENCE", "Legacy evidence requires tracking and discovery at the same Git commit.")
    if not set(files).issubset(allowed):
        raise StorageError("INVALID_EVIDENCE", "Only the selected legacy admission evidence paths may be imported.")
    for raw in files.values():
        if not isinstance(raw, bytes) or len(raw) > d.MAX_OBJECT_BYTES:
            raise StorageError("INVALID_FILE_BYTES", "Legacy evidence must contain bounded exact byte strings.")
        try:
            json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise StorageError("INVALID_EVIDENCE", "Every legacy evidence document must contain valid JSON.") from None
    when = d._time(captured_at or d._now())
    raw = d._json({"format": d.MANIFEST_FORMAT, "files": [
        {"path": path, "digest": d._sha(content), "raw_bytes": len(content)}
        for path, content in sorted(files.items())]}).encode()
    digest = d._sha(raw)
    evidence_id = "legacy-git:" + d._sha((source_id + "\0" + git_revision).encode())
    with maintenance(client, "write"):
        if primary_status(client, source_id)["mode"] != "legacy":
            raise StorageError("SOURCE_PRIMARY", "Legacy evidence must be imported before namespace activation.")
        head = d._head(client, source_id)
        if not head:
            raise StorageError("SOURCE_NOT_FOUND", "Seed the source before importing its historical evidence.")
        existing = client.execute("SELECT * FROM radar_evidence WHERE evidence_id=?", (evidence_id,))
        if existing:
            row = existing[0]
            if (row["source_id"], row["revision"], row["generation"], row["manifest_digest"]) != (source_id, git_revision, 0, digest):
                raise StorageError("EVIDENCE_CONFLICT", "This fixed Git commit already identifies different evidence bytes.")
            when = row["captured_at"]
        mapped = client.execute("SELECT generation,manifest_digest FROM radar_projections WHERE source_id=? AND git_revision=?",
                                (source_id, git_revision))
        if mapped and (mapped[0]["generation"], mapped[0]["manifest_digest"]) != (0, digest):
            raise StorageError("EVIDENCE_CONFLICT", "This Git commit already identifies a different database projection.")
        # A matching current Git revision must also match the imported bytes;
        # reads may legitimately resolve that revision through the full head.
        if head["revision"] == git_revision:
            _, current_files = read_files(client, source_id, tuple(files))
            if current_files != files:
                raise StorageError("EVIDENCE_CONFLICT", "Evidence bytes differ from the source at the same Git commit.")
        incoming = {d._sha(content): content for content in files.values()}
        incoming[digest] = raw
        d._ensure_raw_objects(client, incoming)
        d._graph(client, [digest])
        owner, fence, nonce = proof(client)
        # Persist the pin first. A failed mapping write can be retried without
        # exposing partial data or allowing GC to remove the imported objects.
        client.execute("""INSERT OR IGNORE INTO radar_evidence
            (evidence_id,source_id,origin_manifest_digest,generation,revision,captured_at,manifest_digest,
             writer_owner,writer_fence,writer_nonce) VALUES (?,?,?,0,?,?,?,?,?,?)""",
            (evidence_id, source_id, digest, git_revision, when, digest, owner, fence, nonce))
        client.execute("""INSERT OR IGNORE INTO radar_projections
            (source_id,git_revision,generation,revision,manifest_digest,captured_at,writer_owner,writer_fence,writer_nonce)
            VALUES (?,?,0,?,?,?,?,?,?)""", (source_id, git_revision, git_revision, digest, when, owner, fence, nonce))
        return {"evidence_id": evidence_id, "source_id": source_id, "git_revision": git_revision,
                "revision": git_revision, "generation": 0, "manifest_digest": digest,
                "origin_manifest_digest": digest, "captured_at": when}


def release_evidence(client, evidence_id):
    with maintenance(client, "write"):
        return client.execute("DELETE FROM radar_evidence WHERE evidence_id=? RETURNING evidence_id", (evidence_id,))


def retained_roots(client):
    roots = {row["manifest_digest"] for row in client.execute("SELECT manifest_digest FROM radar_evidence")}
    roots.update(row["manifest_digest"] for row in client.execute(
        "SELECT manifest_digest FROM radar_operations WHERE delivery_target='git' AND projection_git_revision IS NULL"))
    return roots


_METADATA_TABLES = ("radar_operations", "radar_projections", "radar_evidence", "radar_source_policy")
_METADATA_KEYS = {"radar_operations": ("source_id", "operation_id"),
                  "radar_projections": ("source_id", "git_revision"),
                  "radar_evidence": ("evidence_id",), "radar_source_policy": ("source_id",)}


def metadata_snapshot(client):
    """Caller must hold the primary lease while reading metadata and its roots."""
    return {table: [dict(row) for row in client.execute(f"SELECT * FROM {table}")]
            for table in _METADATA_TABLES}


def install_metadata(client, metadata, *, replica=False):
    """Copy semantic metadata with target-local lease proofs, policy last."""
    d = _docs()
    with maintenance(client, "replicate" if replica else "restore"):
        for table in _METADATA_TABLES:
            keys = _METADATA_KEYS[table]
            existing = {tuple(row[key] for key in keys): row for row in client.execute(f"SELECT * FROM {table}")}
            statements = []
            for original in metadata.get(table, []):
                row = {key: value for key, value in original.items() if key not in _PROOF_COLUMNS}
                if replica and table == "radar_source_policy" and row["mode"] == "primary":
                    row["mode"] = "replica"
                old = existing.get(tuple(row[key] for key in keys))
                if old and all(old.get(key) == value for key, value in row.items()):
                    continue
                owner, fence, nonce = proof(client)
                row.update(writer_owner=owner, writer_fence=fence, writer_nonce=nonce)
                columns = tuple(row)
                updates = ",".join(f"{key}=excluded.{key}" for key in columns if key not in keys)
                statements.append((f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                                   f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}", tuple(row.values())))
            d._run(client, statements)
        # Explicitly released pins must not accumulate forever in the standby.
        if replica:
            live = {row["evidence_id"] for row in metadata.get("radar_evidence", [])}
            for row in client.execute("SELECT evidence_id FROM radar_evidence"):
                if row["evidence_id"] not in live:
                    client.execute("DELETE FROM radar_evidence WHERE evidence_id=?", (row["evidence_id"],))
