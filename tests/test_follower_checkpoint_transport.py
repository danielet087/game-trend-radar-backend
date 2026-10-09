"""Offline Contents API races and legacy collector compatibility contracts."""
from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
import requests

from collectors import steam_upcoming as legacy
from radar_backend.adapters.github_contents_checkpoint import load_remote_checkpoint, save_remote_checkpoint
from radar_backend.application.follower_checkpoint import persist_follower_checkpoint
from radar_backend.domain.follower_checkpoint import CheckpointConflict, merge_follower_records
from radar_backend.state.follower_checkpoint import (
    RemoteFollowerCheckpoint, checkpoint_bytes, git_blob_sha, load_checkpoint_file,
    save_checkpoint_file, FollowerCheckpointAcknowledgement,
)

NOW = datetime(2026, 10, 9, 2, 0, 0, 98765, tzinfo=timezone.utc)
STAMP = "2026-10-09T02:00:00Z"
OLD = "2026-10-09T00:00:00Z"
NEW = "2026-10-09T03:00:00Z"


def record(followers, checked_at=OLD, **extra):
    return {"followers": followers, "checked_at": checked_at, **extra}


def envelope(games, **extra):
    return {"version": 1, "updated_at": STAMP, "games": games, **extra}


class Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.payload = payload

    def json(self):
        return deepcopy(self.payload)


class ContentsServer:
    """A fake CAS server: each accepted PUT is tied to the current blob SHA."""
    def __init__(self, path, payload=None):
        self.path = str(path)
        self.raw = checkpoint_bytes(payload) if payload is not None else None
        self.gets = []
        self.puts = []
        self.get_response = None
        self.put_response = None
        self.before_put = None

    def metadata(self, raw):
        return {"type": "file", "path": self.path, "sha": git_blob_sha(raw), "size": len(raw)}

    def read_response(self):
        if self.raw is None:
            return Response(404)
        return Response(200, {**self.metadata(self.raw), "encoding": "base64", "content": base64.b64encode(self.raw).decode()})

    def get(self, url, **kwargs):
        self.gets.append((url, deepcopy(kwargs)))
        return self.get_response if self.get_response is not None else self.read_response()

    def put(self, url, **kwargs):
        body = kwargs["json"]
        raw = base64.b64decode(body["content"], validate=True)
        self.puts.append((url, deepcopy(kwargs), json.loads(raw)))
        if self.before_put is not None:
            self.before_put(self)
        expected = git_blob_sha(self.raw) if self.raw is not None else None
        if body.get("sha") != expected:
            return Response(409)
        if self.put_response is not None:
            return self.put_response(raw)
        created = self.raw is None
        self.raw = raw
        return Response(201 if created else 200, {"content": self.metadata(raw), "commit": {"sha": "c" * 40}})


@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("STEAM_CHECKPOINT_TOKEN", raising=False)
    result = legacy.SteamUpcomingCollector(
        follower_cache_path=tmp_path / "cache.json",
        checkpoint_path=tmp_path / "checkpoint.json",
        follower_request_interval=0, search_request_interval=0,
    )
    result.github_repository = "fixture/offline-only"
    result.checkpoint_token = "fake-token-never-sent"
    result.follower_cache = {"1": record(10)}
    result._checkpoint_dirty_count = 5
    monkeypatch.setattr(legacy, "_utc_now", lambda: NOW)
    return result


def wire(collector, monkeypatch, payload=None):
    server = ContentsServer(collector.checkpoint_path.as_posix(), payload)
    monkeypatch.setattr(legacy.requests, "get", server.get)
    monkeypatch.setattr(legacy.requests, "put", server.put)
    return server


def test_remote_records_and_newer_evidence_survive_in_shared_cache(collector, monkeypatch):
    collector.follower_cache = {"1": record(10), "2": record(20, NEW), "3": record(30), "4": record(40)}
    reference = collector.follower_cache
    server = wire(collector, monkeypatch, envelope({
        "1": record(999, NEW, remote_proof={"source": "community"}),
        "2": record(9), "3": record(333), "5": record(50),
    }, producer_metadata={"keep": True}))
    assert collector._persist_checkpoint_remote(reason="periodic") is None
    written = json.loads(server.raw)
    assert written["games"] == {
        "1": record(999, NEW, remote_proof={"source": "community"}),
        "2": record(20, NEW), "3": record(333), "4": record(40), "5": record(50),
    }
    assert written["producer_metadata"] == {"keep": True}
    assert reference is collector.follower_cache
    assert reference == written["games"] == load_checkpoint_file(collector.checkpoint_path)
    assert collector._checkpoint_dirty_count == 0
    assert server.puts[0][1]["json"]["branch"] == "steam-state"
    assert server.gets[0][1]["params"] == {"ref": "steam-state"}


def test_conflict_rereads_remote_with_one_frozen_clock_and_no_new_collection(collector, monkeypatch):
    server = wire(collector, monkeypatch, envelope({"2": record(20)}))
    calls = []
    monkeypatch.setattr(legacy, "_utc_now", lambda: calls.append("clock") or NOW)
    monkeypatch.setattr(collector, "fetch_followers", lambda *_: pytest.fail("Persistence must not recollect"))
    def competing_write(state):
        if len(state.puts) == 1:
            state.raw = checkpoint_bytes(envelope({"2": record(200, NEW), "3": record(30)}))
    server.before_put = competing_write
    collector._persist_checkpoint_remote(reason="steam-429", force=True)
    assert calls == ["clock"]
    assert len(server.gets) == len(server.puts) == 2
    assert all(item[2]["updated_at"] == STAMP for item in server.puts)
    assert server.puts[0][2]["games"]["1"] == server.puts[1][2]["games"]["1"] == record(10)
    assert json.loads(server.raw)["games"] == {"1": record(10), "2": record(200, NEW), "3": record(30)}
    assert collector._checkpoint_dirty_count == 0


def test_three_conflicts_keep_local_observations_and_dirty_count(collector, monkeypatch):
    server = wire(collector, monkeypatch, envelope({}))
    def competing_write(state):
        state.raw = checkpoint_bytes(envelope({str(100 + len(state.puts)): record(len(state.puts))}))
    server.before_put = competing_write
    collector._persist_checkpoint_remote(reason="periodic")
    assert len(server.puts) == len(server.gets) == 3
    assert collector._checkpoint_dirty_count == 5
    assert load_checkpoint_file(collector.checkpoint_path) == collector.follower_cache == {"1": record(10)}


@pytest.mark.parametrize("status", [403, 422, 429, 500])
def test_failed_read_never_puts_or_clears_dirty(collector, monkeypatch, status):
    server = wire(collector, monkeypatch)
    server.get_response = Response(status)
    collector._persist_checkpoint_remote(reason="periodic")
    assert not server.puts
    assert collector._checkpoint_dirty_count == 5
    assert load_checkpoint_file(collector.checkpoint_path) == {"1": record(10)}


@pytest.mark.parametrize("status", [202, 204, 403, 404, 422, 429, 500])
def test_failed_put_is_not_a_checkpoint_acknowledgement(collector, monkeypatch, status):
    server = wire(collector, monkeypatch)
    server.put_response = lambda raw: Response(status)
    collector._persist_checkpoint_remote(reason="periodic")
    assert len(server.gets) == len(server.puts) == 1
    assert collector._checkpoint_dirty_count == 5
    assert load_checkpoint_file(collector.checkpoint_path) == {"1": record(10)}


@pytest.mark.parametrize("corruption", [
    "invalid-base64", "no-content", "wrong-encoding", "wrong-size", "wrong-blob", "invalid-sha",
    "wrong-path", "wrong-type", "duplicate-json", "nan", "infinity", "overflow", "array",
    "games-array", "record-number", "unsupported-version", "boolean-version", "invalid-utf8",
])
def test_invalid_remote_checkpoint_cannot_be_overwritten(collector, monkeypatch, corruption):
    server = wire(collector, monkeypatch, envelope({"2": record(20)}))
    document = server.read_response().payload
    raw_cases = {
        "duplicate-json": b'{"games":{"2":{},"2":{}}}',
        "nan": b'{"games":{"2":{"followers":NaN}}}',
        "infinity": b'{"games":{"2":{"followers":Infinity}}}',
        "overflow": b'{"games":{"2":{"followers":1e999}}}',
        "array": b'[]', "games-array": b'{"games":[]}',
        "record-number": b'{"games":{"2":20}}',
        "unsupported-version": b'{"version":2,"games":{"2":{}}}',
        "boolean-version": b'{"version":true,"games":{"2":{}}}',
        "invalid-utf8": b'\xff',
    }
    if corruption in raw_cases:
        raw = raw_cases[corruption]
        document = {**server.metadata(raw), "encoding": "base64", "content": base64.b64encode(raw).decode()}
    else:
        key, value = {
            "invalid-base64": ("content", "not base64!"), "no-content": ("content", ""),
            "wrong-encoding": ("encoding", "none"), "wrong-size": ("size", 0),
            "wrong-blob": ("sha", "a" * 40), "invalid-sha": ("sha", "bad-sha"),
            "wrong-path": ("path", "different.json"), "wrong-type": ("type", "dir"),
        }[corruption]
        document[key] = value
    server.get_response = Response(200, document)
    assert collector._load_remote_checkpoint() == {}
    collector._persist_checkpoint_remote(reason="periodic")
    assert not server.puts
    assert collector._checkpoint_dirty_count == 5
    assert load_checkpoint_file(collector.checkpoint_path) == {"1": record(10)}


@pytest.mark.parametrize("corruption", ["missing-content", "missing-commit", "commit-sha", "blob-sha", "blob-other", "path", "type", "size", "boolean-size"])
def test_malformed_success_response_does_not_clear_dirty(collector, monkeypatch, corruption):
    server = wire(collector, monkeypatch)
    def malformed_ack(raw):
        result = {"content": server.metadata(raw), "commit": {"sha": "c" * 40}}
        if corruption == "missing-content":
            del result["content"]
        elif corruption == "missing-commit":
            del result["commit"]
        elif corruption == "commit-sha":
            result["commit"]["sha"] = "unverified"
        else:
            key, value = {
                "blob-sha": ("sha", "unverified"), "blob-other": ("sha", "a" * 40),
                "path": ("path", "different.json"), "type": ("type", "symlink"),
                "size": ("size", len(raw) + 1), "boolean-size": ("size", True),
            }[corruption]
            result["content"][key] = value
        return Response(201, result)
    server.put_response = malformed_ack
    collector._persist_checkpoint_remote(reason="periodic")
    assert len(server.puts) == 1
    assert collector._checkpoint_dirty_count == 5
    assert load_checkpoint_file(collector.checkpoint_path) == {"1": record(10)}


@pytest.mark.parametrize("disabled", ["token", "repository", "url", "below-threshold"])
def test_local_save_survives_disabled_remote_transport(collector, monkeypatch, disabled):
    server = wire(collector, monkeypatch)
    if disabled == "token":
        collector.checkpoint_token = ""
    elif disabled == "repository":
        collector.github_repository = ""
    elif disabled == "url":
        monkeypatch.setattr(collector, "_checkpoint_api_url", lambda: None)
    else:
        collector._checkpoint_dirty_count = 4
    dirty = collector._checkpoint_dirty_count
    collector._persist_checkpoint_remote(reason="periodic")
    assert not server.gets and not server.puts
    assert collector._checkpoint_dirty_count == dirty
    assert load_checkpoint_file(collector.checkpoint_path) == {"1": record(10)}


def test_forced_noop_still_requires_real_remote_ack(collector, monkeypatch):
    collector._checkpoint_dirty_count = 0
    server = wire(collector, monkeypatch, envelope(collector.follower_cache))
    collector._persist_checkpoint_remote(reason="segment-end", force=True)
    assert len(server.puts) == len(server.gets) == 1
    assert server.puts[0][2] == envelope({"1": record(10)})
    assert collector._checkpoint_dirty_count == 0


def test_normal_zero_dirty_does_not_make_remote_request(collector, monkeypatch):
    collector._checkpoint_dirty_count = 0
    server = wire(collector, monkeypatch)
    collector._persist_checkpoint_remote(reason="periodic")
    assert not server.gets and not server.puts


def test_real_creation_uses_no_expected_sha_and_validates_returned_commit(collector, monkeypatch):
    server = wire(collector, monkeypatch)
    collector._persist_checkpoint_remote(reason="segment-end", force=True)
    assert "sha" not in server.puts[0][1]["json"]
    assert collector._checkpoint_dirty_count == 0


def test_transport_ack_ties_commit_and_blob_to_exact_frozen_payload(collector):
    server = ContentsServer(collector.checkpoint_path.as_posix())
    payload = envelope({"1": record(10)})
    acknowledged = save_remote_checkpoint(
        payload, previous=RemoteFollowerCheckpoint({}), url="https://offline.invalid/contents",
        path=server.path, branch="steam-state", headers={}, timeout=20,
        reason="segment-end", put=server.put,
    )
    assert acknowledged == FollowerCheckpointAcknowledgement(
        "c" * 40, git_blob_sha(server.raw), hashlib.sha256(server.raw).hexdigest(), STAMP,
    )
    loaded = load_remote_checkpoint(
        url="https://offline.invalid/contents", path=server.path,
        branch="steam-state", headers={}, timeout=20, get=server.get,
    )
    assert loaded.blob_sha == acknowledged.blob_sha
    assert loaded.games == payload["games"]


@pytest.mark.parametrize("raw", [b'{"7":{"followers":1}}', b'{"games":{"7":{"followers":1}}}'])
def test_flat_and_historical_wrapped_checkpoint_remain_readable(collector, monkeypatch, raw):
    server = wire(collector, monkeypatch)
    server.raw = raw
    assert collector._load_remote_checkpoint() == {"7": {"followers": 1}}
    collector._persist_checkpoint_remote(reason="periodic")
    assert json.loads(server.raw)["games"]["7"] == {"followers": 1}


@pytest.mark.parametrize("kind", ["source", "temporary", "ancestor"])
def test_symlinks_never_receive_local_observations(collector, monkeypatch, tmp_path, kind):
    server = wire(collector, monkeypatch)
    outside = tmp_path / "outside.json"
    outside.write_text("outside must remain unchanged")
    if kind == "source":
        collector.checkpoint_path.symlink_to(outside)
    elif kind == "temporary":
        collector.checkpoint_path.with_suffix(".tmp").symlink_to(outside)
    else:
        directory = tmp_path / "outside-directory"
        directory.mkdir()
        link = tmp_path / "linked-directory"
        link.symlink_to(directory, target_is_directory=True)
        collector.checkpoint_path = link / "checkpoint.json"
    with pytest.raises(ValueError, match="symlink"):
        collector._persist_checkpoint_remote(reason="periodic")
    assert outside.read_text() == "outside must remain unchanged"
    assert not server.gets and not server.puts
    assert collector._checkpoint_dirty_count == 5
    assert not hasattr(collector, "_checkpoint_frozen_payload")
    if kind == "ancestor":
        assert not (directory / "checkpoint.json").exists()


def test_atomic_local_save_keeps_previous_file_when_serialization_fails(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("old bytes")
    with pytest.raises(ValueError):
        save_checkpoint_file(path, envelope({"1": record(float("nan"))}))
    assert path.read_text() == "old bytes"
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("operation", ["get", "put"])
def test_http_exception_logs_never_leak_token_or_request_url(collector, monkeypatch, caplog, operation):
    server = wire(collector, monkeypatch)
    secret = "private-token-in-error-url"
    def failed(*args, **kwargs):
        raise requests.ConnectionError("https://example.test/?token=" + secret)
    monkeypatch.setattr(legacy.requests, operation, failed)
    collector._persist_checkpoint_remote(reason="periodic")
    assert secret not in caplog.text
    assert "example.test" not in caplog.text
    assert "ConnectionError" in caplog.text
    assert collector._checkpoint_dirty_count == 5


def test_load_http_failure_is_best_effort_and_does_not_leak_details(collector, monkeypatch, caplog):
    monkeypatch.setattr(legacy.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError("token=private-secret")))
    assert collector._load_remote_checkpoint() == {}
    assert "private-secret" not in caplog.text


def test_legacy_helpers_and_payload_headers_are_resolved_at_call_time(collector, monkeypatch):
    server = wire(collector, monkeypatch)
    calls = []
    frozen = envelope({"42": record(420)})
    monkeypatch.setattr(collector, "_checkpoint_payload", lambda: calls.append("payload") or frozen)
    monkeypatch.setattr(collector, "_checkpoint_headers", lambda: {"changed-at-call-time": str(len(calls))})
    original_save = collector._save_checkpoint_local
    monkeypatch.setattr(collector, "_save_checkpoint_local", lambda: calls.append("save") or original_save())
    collector._persist_checkpoint_remote(reason="periodic")
    assert calls == ["payload", "save"]
    assert server.gets[0][1]["headers"] == server.puts[0][1]["headers"] == {"changed-at-call-time": "2"}
    assert collector.follower_cache == {"42": record(420)}
    timestamps = []
    monkeypatch.setattr(legacy, "_parse_iso_datetime", lambda value: timestamps.append(value) or None)
    assert legacy.merge_follower_records({"1": record(10)}, {"1": record(20, NEW)}) == {"1": record(10)}
    assert timestamps == [NEW, OLD]


def test_application_freezes_input_before_any_conflict_retry():
    source = envelope({"1": record(10)})
    attempts = []
    def read():
        source["games"]["1"]["followers"] = 999
        return RemoteFollowerCheckpoint({"2": record(20)})
    def write(previous, payload):
        attempts.append(deepcopy(payload))
        if len(attempts) == 1:
            raise CheckpointConflict()
        raw = checkpoint_bytes(payload)
        return FollowerCheckpointAcknowledgement(
            "c" * 40, git_blob_sha(raw), hashlib.sha256(raw).hexdigest(), payload["updated_at"],
        )
    result = persist_follower_checkpoint(source, read_latest=read, write_merged=write)
    assert source["games"]["1"]["followers"] == 999
    assert attempts[0] == attempts[1] == result.payload == envelope({"1": record(10), "2": record(20)})
    assert result.attempts == 2


@pytest.mark.parametrize("changes", [{"version": True}, {"version": 2}, {"updated_at": "2026-10-09T02:00:00"}, {"updated_at": "invalid"}])
def test_invalid_frozen_envelope_makes_no_remote_request(changes):
    source = envelope({"1": record(10)})
    source.update(changes)
    with pytest.raises(ValueError):
        persist_follower_checkpoint(source, read_latest=lambda: pytest.fail("No HTTP for invalid frozen batch"), write_merged=lambda *a: pytest.fail("No write"))


def test_new_domain_merge_keeps_equal_and_unknown_timestamp_first_source():
    remote = {"1": record(10), "2": record(20, "invalid"), "3": record(30, "invalid")}
    observed = {"1": record(100), "2": record(200, "invalid"), "3": record(300, NEW)}
    assert merge_follower_records(remote, observed) == {"1": record(10), "2": record(20, "invalid"), "3": record(300, NEW)}
