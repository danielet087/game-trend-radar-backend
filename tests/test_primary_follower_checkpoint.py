"""Primary follower checkpoints use database authority and acknowledged CAS."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from collectors import steam_upcoming as legacy
from radar_backend.adapters import github_contents_checkpoint as transport
from radar_backend.application.follower_checkpoint import DatabaseFollowerCheckpointAcknowledgement
from radar_backend.domain.follower_checkpoint import CheckpointConflict, merge_follower_records
from radar_backend.state.follower_checkpoint import checkpoint_bytes
from radar_core import storage
from radar_core.storage.sql import StorageError


REPOSITORY = "fixture/backend"
MAIN = REPOSITORY + "@main"
STATE = REPOSITORY + "@steam-state"
CACHE = "data/steam_followers_cache.json"
CHECKPOINT = "data/steam_followers_checkpoint.json"
NOW = datetime(2026, 10, 15, 1, tzinfo=timezone.utc)
STAMP = "2026-10-15T01:00:00Z"
OLDER = "2026-10-15T00:00:00Z"
NEWER = "2026-10-15T02:00:00Z"


def record(count, when=OLDER):
    return {"followers": count, "checked_at": when}


def envelope(games, **extra):
    return {"version": 1, "updated_at": STAMP, "games": games, **extra}


class Database:
    def __init__(self):
        self.files = {
            MAIN: {CACHE: checkpoint_bytes(envelope({"1": record(10)}))},
            STATE: {CHECKPOINT: checkpoint_bytes(envelope({"2": record(20)}, keep="metadata")),
                    "data/unrelated.json": b'{"keep":"exact bytes"}\n'},
        }
        self.generations = {MAIN: 1, STATE: 1}
        self.active = {MAIN: True, STATE: True}
        self.reads, self.writes = [], []
        self.failure = None
        self.before_write = None
        self.corrupt_receipt = None

    def primary_status(self, client, source_id):
        assert client is self
        if self.failure == "status":
            raise StorageError("API_ERROR", "Primary unavailable.")
        return {"active": self.active[source_id], "mode": "primary"}

    def read_files(self, client, source_id, paths=None):
        assert client is self
        self.reads.append((source_id, paths))
        if self.failure == "read":
            raise StorageError("API_ERROR", "Primary unavailable.")
        return ({"generation": self.generations[source_id]},
                {path: self.files[source_id][path] for path in paths if path in self.files[source_id]})

    def publish_patch(self, client, source_id, **kwargs):
        assert client is self
        self.writes.append((source_id, deepcopy(kwargs)))
        if self.failure == "write":
            raise StorageError("API_ERROR", "Primary unavailable.")
        if self.before_write:
            self.before_write(self)
        if not self.active[source_id]:
            raise StorageError("PRIMARY_NOT_ACTIVE", "Primary ownership changed.")
        if kwargs["base_generation"] != self.generations[source_id]:
            raise StorageError("STALE_GENERATION", "Competing write.")
        self.files[source_id].update(kwargs["changed"])
        self.generations[source_id] += 1
        receipt = {
            "source_id": source_id, "generation": self.generations[source_id],
            "revision": "database-revision", "manifest_digest": "a" * 64,
            "operation_id": kwargs["operation_id"], "changed": True,
            "projection_pending": True,
            "file_digests": {path: hashlib.sha256(raw).hexdigest()
                             for path, raw in kwargs["changed"].items()},
            "deleted": kwargs["deleted"],
        }
        if self.corrupt_receipt:
            self.corrupt_receipt(receipt)
        return receipt


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.delenv("STEAM_CHECKPOINT_TOKEN", raising=False)
    monkeypatch.setattr(legacy, "_utc_now", lambda: NOW)
    value = Database()
    monkeypatch.setattr(storage, "primary_store", value, raising=False)
    monkeypatch.setattr(transport, "_database_client", lambda: value)
    def no_github(*args, **kwargs):
        pytest.fail("Database primary must not read or write GitHub Contents")
    monkeypatch.setattr(legacy.requests, "get", no_github)
    monkeypatch.setattr(legacy.requests, "put", no_github)
    return value


def collector():
    return legacy.SteamUpcomingCollector(follower_request_interval=0, search_request_interval=0)


def test_initial_cache_comes_from_both_database_sources_not_checkout(database, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / CACHE).write_bytes(checkpoint_bytes(envelope({"999": record(999, NEWER)})))
    (tmp_path / CHECKPOINT).write_text("stale invalid Git artifact")
    current = collector()
    assert current.follower_cache == {"1": record(10), "2": record(20)}
    assert database.reads == [(MAIN, [CACHE]), (STATE, [CHECKPOINT])]
    assert json.loads((tmp_path / CACHE).read_bytes())["games"] == current.follower_cache
    assert json.loads((tmp_path / CHECKPOINT).read_bytes())["games"] == current.follower_cache


@pytest.mark.parametrize("failure", ["status", "read"])
def test_primary_unavailable_during_initial_read_aborts_without_local_fallback(database, failure):
    database.failure = failure
    with pytest.raises(StorageError, match="Primary unavailable"):
        collector()
    assert not database.writes


@pytest.mark.parametrize("source", [MAIN, STATE])
def test_inactive_database_source_aborts_initialization(database, source):
    database.active[source] = False
    with pytest.raises(StorageError) as error:
        collector()
    assert error.value.code == "PRIMARY_NOT_ACTIVE"
    assert not database.writes


@pytest.mark.parametrize("raw", [None, b"[]", b'{"games":{"1":1}}',
                                  b'{"games":{"1":{"followers":1e999}}}',
                                  b'{"games":{},"games":{}}'])
def test_missing_or_corrupt_primary_checkpoint_never_becomes_empty_state(database, raw):
    if raw is None:
        del database.files[STATE][CHECKPOINT]
    else:
        database.files[STATE][CHECKPOINT] = raw
    with pytest.raises((StorageError, ValueError)):
        collector()
    assert not database.writes


def test_primary_write_needs_no_github_token_and_preserves_remote_metadata_and_other_files(database):
    current = collector()
    current.follower_cache["3"] = record(30, NEWER)
    current._checkpoint_dirty_count = 5
    unrelated = database.files[STATE]["data/unrelated.json"]
    current._persist_checkpoint_remote(reason="periodic")
    saved = json.loads(database.files[STATE][CHECKPOINT])
    assert saved["games"] == current.follower_cache == {"1": record(10), "2": record(20), "3": record(30, NEWER)}
    assert saved["keep"] == "metadata"
    assert database.files[STATE]["data/unrelated.json"] == unrelated
    assert current._checkpoint_dirty_count == 0
    assert len(database.writes) == 1


def test_conflict_rereads_primary_and_remerges_same_frozen_observations(database):
    current = collector()
    current.follower_cache["3"] = record(30)
    current._checkpoint_dirty_count = 5
    def competing(db):
        if len(db.writes) == 1:
            db.files[STATE][CHECKPOINT] = checkpoint_bytes(envelope({"3": record(300, NEWER), "4": record(40)}))
            db.generations[STATE] += 1
            current.follower_cache["99"] = record(999)
    database.before_write = competing
    current._persist_checkpoint_remote(reason="periodic")
    assert len(database.writes) == 2
    first, second = [call[1] for call in database.writes]
    assert first["operation_id"] == second["operation_id"]
    assert first["payload_revision"] == second["payload_revision"]
    assert current.follower_cache == {"1": record(10), "2": record(20), "3": record(300, NEWER), "4": record(40)}
    assert current._checkpoint_dirty_count == 0


@pytest.mark.parametrize("failure", ["read", "write", "conflicts", "inactive"])
def test_failed_primary_persistence_raises_and_preserves_pending_local_observations(database, failure, tmp_path):
    current = collector()
    current.follower_cache["3"] = record(30)
    current._checkpoint_dirty_count = 5
    expected = deepcopy(current.follower_cache)
    if failure in {"read", "write"}:
        database.failure = failure
    elif failure == "inactive":
        database.active[STATE] = False
    else:
        database.before_write = lambda db: db.generations.__setitem__(STATE, db.generations[STATE] + 1)
    with pytest.raises((StorageError, CheckpointConflict)):
        current._persist_checkpoint_remote(reason="periodic")
    assert current._checkpoint_dirty_count == 5
    assert current.follower_cache == expected
    assert json.loads((tmp_path / CHECKPOINT).read_bytes())["games"] == expected
    if failure == "conflicts":
        assert len(database.writes) == 3


@pytest.mark.parametrize("field,value", [
    ("source_id", MAIN), ("generation", True), ("revision", ""),
    ("manifest_digest", "bad"), ("operation_id", "other"),
    ("file_digests", {CHECKPOINT: "b" * 64}), ("deleted", [CHECKPOINT]),
])
def test_invalid_primary_ack_never_clears_dirty(database, field, value):
    current = collector()
    current._checkpoint_dirty_count = 5
    database.corrupt_receipt = lambda receipt: receipt.__setitem__(field, value)
    with pytest.raises(ValueError, match="acknowledgement"):
        current._persist_checkpoint_remote(reason="periodic")
    assert current._checkpoint_dirty_count == 5


def test_database_ack_has_database_identity_without_fabricating_git_sha(database):
    result = transport.persist_database_follower_checkpoint(
        envelope({"3": record(30)}), repository=REPOSITORY, path=CHECKPOINT,
        branch="steam-state", merge_records=merge_follower_records,
    )
    ack = result.acknowledgement
    assert isinstance(ack, DatabaseFollowerCheckpointAcknowledgement)
    assert ack.source_id == STATE and ack.generation == 2
    assert ack.revision == "database-revision"
    assert ack.payload_revision == hashlib.sha256(database.files[STATE][CHECKPOINT]).hexdigest()
    assert not hasattr(ack, "commit_sha") and not hasattr(ack, "blob_sha")


def test_database_ack_cannot_claim_an_older_generation(database):
    current = collector()
    current._checkpoint_dirty_count = 5
    database.generations[STATE] = 3
    database.corrupt_receipt = lambda receipt: receipt.__setitem__("generation", 1)
    with pytest.raises(ValueError, match="acknowledgement"):
        current._persist_checkpoint_remote(reason="periodic")
    assert current._checkpoint_dirty_count == 5


def test_primary_threshold_and_forced_checkpoint_keep_existing_batch_behavior(database):
    current = collector()
    current._checkpoint_dirty_count = 4
    current._persist_checkpoint_remote(reason="periodic")
    assert not database.writes
    current._checkpoint_dirty_count = 0
    current._persist_checkpoint_remote(reason="segment-end", force=True)
    assert len(database.writes) == 1


def test_primary_path_outside_checkout_is_rejected_before_database_read(database, tmp_path):
    with pytest.raises(ValueError, match="inside the checkout"):
        transport.load_database_follower_checkpoint(
            repository=REPOSITORY, path=tmp_path.parent / "outside.json", branch="steam-state",
        )
    assert not database.reads


def test_invalid_primary_mode_never_silently_reverts_to_git(database, monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "true")
    with pytest.raises(StorageError) as error:
        collector()
    assert error.value.code == "INVALID_CONFIG"
    assert not database.reads


def test_429_checkpoint_failure_aborts_before_sleep_or_the_next_game(database, monkeypatch):
    current = collector()
    database.failure = "write"
    requests = []
    def limited(*args, **kwargs):
        requests.append(args)
        return SimpleNamespace(status_code=429, headers={})
    monkeypatch.setattr(current.session, "get", limited)
    monkeypatch.setattr(legacy.time, "sleep", lambda *_: pytest.fail("Do not wait after a primary failure"))
    games = [legacy.UpcomingGame(appid=aid, name=str(aid), release_raw="2027-01-01",
             release_start="2027-01-01", release_end="2027-01-01", release_precision="day",
             capsule_image=None, store_url=f"https://store.steampowered.com/app/{aid}")
             for aid in (3, 4)]
    with pytest.raises(transport.DatabaseCheckpointError):
        current.qualify(games)
    assert len(requests) == 1
    assert current.follower_failures == 0


def test_real_sqlite_primary_read_merge_write_and_idempotent_ack(tmp_path, monkeypatch):
    from radar_core.storage import documents, primary_store
    from radar_core.storage.sql import SqliteClient
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.delenv("STEAM_CHECKPOINT_TOKEN", raising=False)
    monkeypatch.setattr(legacy, "_utc_now", lambda: NOW)
    def no_github(*args, **kwargs):
        pytest.fail("SQLite primary integration must not use GitHub")
    monkeypatch.setattr(legacy.requests, "get", no_github)
    monkeypatch.setattr(legacy.requests, "put", no_github)
    db = SqliteClient()
    try:
        documents.initialize(db)
        originals = Database().files
        for source, files in originals.items():
            documents.publish_snapshot(db, source, "a" * 40, files, STAMP, expected_generation=0)
        for source in originals:
            primary_store.activate_primary(db, source, expected_generation=1)
        monkeypatch.setattr(transport, "_database_client", lambda: db)
        current = collector()
        current.follower_cache["3"] = record(30, NEWER)
        current._checkpoint_dirty_count = 5
        current._persist_checkpoint_remote(reason="periodic")
        first_head, first_files = primary_store.read_files(db, STATE)
        assert first_head["generation"] == 2
        assert first_head["revision"].startswith("db:")
        assert first_files["data/unrelated.json"] == originals[STATE]["data/unrelated.json"]
        assert json.loads(first_files[CHECKPOINT])["games"] == current.follower_cache
        assert json.loads(first_files[CHECKPOINT])["keep"] == "metadata"
        # A retry of the same frozen batch acknowledges its original operation.
        current._persist_checkpoint_remote(reason="segment-end", force=True)
        second_head, second_files = primary_store.read_files(db, STATE)
        assert second_head == first_head and second_files == first_files
        assert current._checkpoint_dirty_count == 0
        assert len(db.execute("SELECT * FROM radar_operations WHERE source_id=?", (STATE,))) == 1
    finally:
        db.close()
