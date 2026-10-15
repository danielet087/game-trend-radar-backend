"""Live legacy proofs retain original Git evidence through activation and GC."""
import base64
from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock, patch

import pytest

from radar_core.storage import documents, primary_store
from radar_core.storage.sql import SqliteClient, StorageError
from scripts import storage_legacy_evidence as migration
from tests.test_twitch_intake_rules import proof, snapshot, SHA


def raw(value):
    return json.dumps(value, sort_keys=True).encode()


def evidence():
    discovery, tracking, _ = snapshot()
    return {migration.EVIDENCE_PATHS[0]: raw(tracking), migration.EVIDENCE_PATHS[1]: raw(discovery)}


class GitHubObjects:
    def __init__(self, files=None):
        self.calls = []
        self.responses = {"/git/commits/" + SHA: {"sha": SHA, "tree": {"sha": "b" * 40}}}
        tree = []
        for path, content in (files or evidence()).items():
            digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            tree.append({"path": path, "type": "blob", "mode": "100644", "sha": digest, "size": len(content)})
            self.responses["/git/blobs/" + digest] = {
                "sha": digest, "encoding": "base64", "content": base64.b64encode(content).decode(), "size": len(content)}
        self.responses["/git/trees/" + "b" * 40 + "?recursive=1"] = {"sha": "b" * 40, "truncated": False, "tree": tree}

    def call(self, method, repository, path):
        assert method == "GET" and repository == "game-trend-radar"
        self.calls.append(path)
        return deepcopy(self.responses[path])


@pytest.fixture
def database():
    client = SqliteClient()
    documents.initialize(client)
    row = {"appid": 123, "twitch_admission": proof()}
    files = {
        migration.MASTER: raw({"games": [row]}),
        migration.INTAKE: raw({"games": {"123": {"status": "accepted", "twitch_admission": proof()}}}),
        migration.CHECKPOINT: raw({"pending_candidates": {}, "official_results": {}, "unresolved_candidates": {}}),
    }
    documents.publish_snapshot(client, migration.BACKEND, "c" * 40, files, "2026-10-15T00:00:00Z")
    documents.publish_snapshot(client, migration.FRONTEND, "d" * 40, {
        "data/steam_upcoming.json": raw({"games": [row]}), "data/catalog.json": raw({"games": []}),
        "data/games/123.json": raw(row), **evidence()}, "2026-10-15T00:00:00Z")
    yield client
    client.close()


def update_snapshot(client, source, changes):
    head, files = primary_store.read_files(client, source)
    documents.publish_snapshot(client, source, "e" * 40, {**files, **changes}, "2026-10-15T01:00:00Z",
                               expected_generation=head["generation"])


def test_complete_migration_retains_exact_git_bytes_without_advancing_head(database):
    before = documents.read_snapshot(database, migration.FRONTEND)
    github = GitHubObjects()
    result = migration.migrate_legacy_evidence(database, github)
    assert result["git_revisions"] == result["imported"] == 1
    assert result["references"] == 4
    assert len(github.calls) == 4
    assert documents.read_snapshot(database, migration.FRONTEND) == before
    head, files = primary_store.read_files(database, migration.FRONTEND, migration.EVIDENCE_PATHS, revision=SHA)
    assert head["generation"] == 0
    assert files == evidence()
    again = migration.migrate_legacy_evidence(database, Mock())
    assert again["github_requests"] == 0
    primary_store.activate_primary(database, migration.FRONTEND)
    retry = migration.migrate_legacy_evidence(database, Mock())
    assert retry["retained"] == 1 and retry["imported"] == 0
    documents.garbage_collect(database, max_delete=10000, exclusive=True)
    assert primary_store.read_files(database, migration.FRONTEND, migration.EVIDENCE_PATHS, revision=SHA)[1] == evidence()


def test_current_revision_is_also_pinned_before_it_becomes_history(database):
    current = documents.read_snapshot(database, migration.FRONTEND)[0]["revision"]
    changed = dict(proof(), source_frontend_commit=current)
    with patch.object(migration, "inventory", return_value=({current: [changed]}, 1)):
        migration.migrate_legacy_evidence(database, Mock())
    assert primary_store.resolve_git_revision(database, migration.FRONTEND, current)["generation"] == 0
    primary_store.activate_primary(database, migration.FRONTEND)
    primary_store.publish_patch(database, migration.FRONTEND, base_generation=1,
        changed={migration.EVIDENCE_PATHS[0]: b'{"new":true}'}, operation_id="advance")
    documents.garbage_collect(database, max_delete=10000, exclusive=True)
    assert primary_store.read_files(database, migration.FRONTEND, migration.EVIDENCE_PATHS, revision=current)[1] == evidence()


def test_inventory_includes_pending_and_nested_candidates_but_ignores_history_and_excluded(database):
    pending = dict(proof(), source_frontend_commit="f" * 40)
    ignored = dict(proof(), source_frontend_commit="1" * 40)
    update_snapshot(database, migration.BACKEND, {
        migration.INTAKE: raw({"games": {
            "123": {"status": "pending", "follower_candidate": {"appid": 123, "steam_candidate": {
                "appid": 123, "twitch_admission": pending}}},
            "999": {"status": "excluded", "twitch_admission": ignored}}}),
        migration.CHECKPOINT: raw({"pending_candidates": {"123": {"appid": 123, "twitch_admission": pending}},
                                  "attempt_events": [{"appid": 123, "twitch_admission": ignored}]}),
        "reports/old-proof.json": raw({"appid": 123, "twitch_admission": ignored})})
    proofs, _ = migration.inventory(database)
    assert set(proofs) == {SHA, "f" * 40}
    assert len(proofs["f" * 40]) == 1


def test_database_proofs_are_not_reinterpreted_as_git_revisions(database):
    modern = {key: value for key, value in proof().items() if key != "source_frontend_commit"}
    modern.update(source_database_revision="9" * 64, source_database_generation=2,
                  source_database_namespace=migration.FRONTEND)
    update_snapshot(database, migration.BACKEND, {migration.INTAKE: raw({"games": {
        "123": {"status": "pending", "twitch_admission": modern}}})})
    proofs, _ = migration.inventory(database)
    assert set(proofs) == {SHA}


@pytest.mark.parametrize("change,code", [
    (lambda responses: responses["/git/commits/" + SHA].update(sha="f" * 40), "INVALID_GIT_EVIDENCE"),
    (lambda responses: responses["/git/trees/" + "b" * 40 + "?recursive=1"].update(truncated=True), "INCOMPLETE_GIT_EVIDENCE_TREE"),
    (lambda responses: responses["/git/trees/" + "b" * 40 + "?recursive=1"]["tree"][0].update(mode="120000"), "INVALID_GIT_EVIDENCE"),
    (lambda responses: responses["/git/trees/" + "b" * 40 + "?recursive=1"]["tree"].pop(), "INCOMPLETE_GIT_EVIDENCE"),
    (lambda responses: next(value for key, value in responses.items() if "/git/blobs/" in key).update(content=base64.b64encode(b"wrong").decode()), "GIT_EVIDENCE_DIGEST_MISMATCH"),
])
def test_wrong_commit_truncated_tree_symlink_missing_or_corrupt_blob_never_imports(database, change, code):
    github = GitHubObjects()
    change(github.responses)
    with pytest.raises(StorageError) as caught:
        migration.migrate_legacy_evidence(database, github)
    assert caught.value.code == code
    assert database.execute("SELECT * FROM radar_evidence") == []


def test_mismatching_historical_admission_is_rejected_without_current_fallback(database):
    files = evidence()
    value = json.loads(files[migration.EVIDENCE_PATHS[1]])
    value["games"]["22"]["active"] = False
    files[migration.EVIDENCE_PATHS[1]] = raw(value)
    with pytest.raises(StorageError) as caught:
        migration.migrate_legacy_evidence(database, GitHubObjects(files))
    assert caught.value.code == "LEGACY_ADMISSION_EVIDENCE_MISMATCH"
    assert database.execute("SELECT * FROM radar_evidence") == []


def test_active_source_cannot_import_unknown_git_evidence(database):
    primary_store.activate_primary(database, migration.FRONTEND)
    github = Mock()
    with pytest.raises(StorageError) as caught:
        migration.migrate_legacy_evidence(database, github)
    assert caught.value.code == "PRIMARY_LEGACY_EVIDENCE_MISSING"
    github.call.assert_not_called()


@pytest.mark.parametrize("code", ["MISSING_OBJECT", "MISSING_CHUNK"])
def test_legacy_pruned_objects_recover_only_from_exact_git_revision(database, code):
    read = primary_store.read_files
    def partial_gc(client, source, paths=None, **kwargs):
        if kwargs.get("revision") == SHA:
            raise StorageError(code, "Missing historical object")
        return read(client, source, paths, **kwargs)
    with patch.object(primary_store, "read_files", side_effect=partial_gc):
        result = migration.migrate_legacy_evidence(database, GitHubObjects())
    assert result["imported"] == 1
    assert read(database, migration.FRONTEND, migration.EVIDENCE_PATHS, revision=SHA)[1] == evidence()


def test_deadline_inventory_and_revision_limits_fail_before_network(database):
    github = Mock()
    with pytest.raises(StorageError) as caught:
        migration.migrate_legacy_evidence(database, github, max_seconds=0)
    assert caught.value.code == "LEGACY_EVIDENCE_TIME_LIMIT"
    with patch.object(migration, "MAX_REVISIONS", 0), pytest.raises(StorageError) as caught:
        migration.migrate_legacy_evidence(database, github)
    assert caught.value.code == "LEGACY_EVIDENCE_REVISION_LIMIT"
    with patch.object(migration, "MAX_INPUT_BYTES", 1), pytest.raises(StorageError) as caught:
        migration.migrate_legacy_evidence(database, github)
    assert caught.value.code == "ADMISSION_INVENTORY_LIMIT"
    github.call.assert_not_called()


def test_missing_source_inventory_and_invalid_live_proof_fail_closed(database):
    update_snapshot(database, migration.BACKEND, {migration.MASTER: raw({"games": [
        {"appid": 999, "twitch_admission": proof()}]})})
    with pytest.raises(StorageError) as caught:
        migration.inventory(database)
    assert caught.value.code == "INVALID_LIVE_ADMISSION"

