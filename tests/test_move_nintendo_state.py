import json
import subprocess

import pytest

from radar_core.publication import SubprocessGitRepository
from radar_core.storage import documents, primary, primary_store
from radar_core.storage.sources import SOURCES, git_backed, source_id
from radar_core.storage.sql import SqliteClient, StorageError
from scripts import move_nintendo_state as move
from scripts import storage_project as project
from scripts import storage_sync as sync

LEDGER = (json.dumps({"schema_version": 1, "storage_format": "gzip-base64", "payload": "H4sI" * 64},
                     separators=(",", ":")) + "\n").encode()
FILES = {
    move.PATH: LEDGER,
    "data/nintendo_upcoming.json": b'{"games": []}\n',
    "data/another-collector.json": b'{"kept": true}\n',
}


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
                          cwd=root, check=True, capture_output=True).stdout


@pytest.fixture()
def system(tmp_path):
    database = SqliteClient(tmp_path / "primary.sqlite")
    documents.initialize(database)
    primary_store.bootstrap_primary(database, move.FRONTEND, FILES, "a" * 40)
    remote, work = tmp_path / "remote.git", tmp_path / "frontend"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "init", "--initial-branch=main", str(work))
    for path, raw in FILES.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_bytes(raw)
    git(work, "add", ".")
    git(work, "commit", "-m", "initial")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "origin", "HEAD:main")

    def repository():
        return SubprocessGitRepository(work, disposable_checkout=True,
                                       primary=primary.GitPrimary(move.FRONTEND, db=database))
    yield database, repository, remote
    database.close()


def remote_files(remote):
    names = git(remote, "ls-tree", "-r", "--name-only", "main").decode().split()
    return {name: git(remote, "show", "main:" + name) for name in names}


def test_the_state_source_is_database_only_and_holds_only_the_ledger():
    assert not git_backed(move.STATE_SPEC)
    assert move.STATE_SPEC["files"] == (move.PATH,) and move.STATE_SPEC["prefixes"] == ()
    assert primary._managed(move.STATE, move.PATH)
    assert not primary._managed(move.STATE, "data/nintendo_upcoming.json")
    assert [source_id(spec) for spec in SOURCES if not git_backed(spec)][-1] == move.STATE


def test_bootstrap_dry_run_writes_nothing_and_apply_copies_once(system):
    database, _, _ = system
    report = move.run(database, "bootstrap")
    assert report["status"] == "dry_run" and report["bytes"] == len(LEDGER)
    assert documents._head(database, move.STATE) is None
    report = move.run(database, "bootstrap", apply=True)
    assert report["status"] == "bootstrapped" and report["active"] is True and report["generation"] == 1
    _, copied = primary_store.read_files(database, move.STATE)
    assert copied == {move.PATH: LEDGER}
    # The frontend copy stays until the retire step.
    _, frontend = primary_store.read_files(database, move.FRONTEND)
    assert frontend == FILES
    again = move.run(database, "bootstrap", apply=True)
    assert again["status"] == "exists" and again["state_sha256"] == again["frontend_sha256"]


def test_bootstrap_needs_a_frontend_ledger(tmp_path):
    database = SqliteClient(tmp_path / "empty.sqlite")
    documents.initialize(database)
    primary_store.bootstrap_primary(database, move.FRONTEND, {"data/other.json": b"{}"}, "b" * 40)
    with pytest.raises(StorageError) as caught:
        move.run(database, "bootstrap", apply=True)
    assert caught.value.code == "NO_NINTENDO_STATE"


def test_retire_dry_run_compares_three_copies_and_changes_nothing(system):
    database, repository, remote = system
    move.run(database, "bootstrap", apply=True)
    head, _ = primary_store.read_files(database, move.FRONTEND)
    report = move.run(database, "retire", repository=repository())
    assert report["status"] == "identical" and report["bytes"] == len(LEDGER)
    assert report["state_sha256"] == report["frontend_database_sha256"] == report["frontend_git_sha256"]
    assert primary_store.read_files(database, move.FRONTEND)[0]["generation"] == head["generation"]
    assert remote_files(remote) == FILES


def test_retire_removes_the_frontend_copy_from_database_and_git(system):
    database, repository, remote = system
    move.run(database, "bootstrap", apply=True)
    report = move.run(database, "retire", apply=True, repository=repository())
    assert report["status"] == "retired" and report["changed"] is True
    _, frontend = primary_store.read_files(database, move.FRONTEND)
    assert frontend == {path: raw for path, raw in FILES.items() if path != move.PATH}
    assert remote_files(remote) == frontend
    assert primary_store.projection_pending(database, move.FRONTEND) == []
    assert primary_store.read_files(database, move.STATE)[1] == {move.PATH: LEDGER}
    again = move.run(database, "retire", apply=True, repository=repository())
    assert again["status"] == "already_retired"


@pytest.mark.parametrize("stale", ["state", "git"])
def test_retire_refuses_when_any_copy_differs(system, stale):
    database, repository, remote = system
    move.run(database, "bootstrap", apply=True)
    if stale == "state":
        head, _ = primary_store.read_files(database, move.STATE)
        primary_store.publish_patch(database, move.STATE, base_generation=head["generation"],
                                    changed={move.PATH: b'{"older": true}\n'}, operation_id="older-ledger",
                                    projection_required=False)
    else:
        # The database holds a newer ledger that is not projected to Git yet.
        head, _ = primary_store.read_files(database, move.FRONTEND)
        primary_store.publish_patch(database, move.FRONTEND, base_generation=head["generation"],
                                    changed={move.PATH: b'{"newer": true}\n'}, operation_id="unprojected")
        head, _ = primary_store.read_files(database, move.STATE)
        primary_store.publish_patch(database, move.STATE, base_generation=head["generation"],
                                    changed={move.PATH: b'{"newer": true}\n'}, operation_id="mirrored",
                                    projection_required=False)
    before = primary_store.read_files(database, move.FRONTEND)
    with pytest.raises(StorageError) as caught:
        move.run(database, "retire", apply=True, repository=repository())
    assert caught.value.code == "COPIES_DIFFER"
    assert primary_store.read_files(database, move.FRONTEND) == before
    assert remote_files(remote)[move.PATH] == LEDGER


def test_restore_returns_the_database_copy_to_the_frontend(system):
    database, repository, remote = system
    move.run(database, "bootstrap", apply=True)
    assert move.run(database, "restore", repository=repository())["status"] == "not_retired"
    move.run(database, "retire", apply=True, repository=repository())
    assert move.run(database, "restore", repository=repository())["status"] == "dry_run"
    report = move.run(database, "restore", apply=True, repository=repository())
    assert report["status"] == "restored"
    assert primary_store.read_files(database, move.FRONTEND)[1][move.PATH] == LEDGER
    assert remote_files(remote)[move.PATH] == LEDGER


def test_a_busy_lease_is_retried_and_other_errors_are_not(system, monkeypatch):
    database, _, _ = system
    calls = []

    def busy_then_ok(client, *, apply=False):
        calls.append(apply)
        if len(calls) < 3:
            raise StorageError("LEASE_BUSY", "busy")
        return {"status": "dry_run"}

    monkeypatch.setattr(move, "bootstrap", busy_then_ok)
    assert move.run(database, "bootstrap", sleep=lambda seconds: None)["attempts"] == 3
    monkeypatch.setattr(move, "bootstrap", lambda client, apply=False: (_ for _ in ()).throw(
        StorageError("NO_NINTENDO_STATE", "x")))
    with pytest.raises(StorageError):
        move.run(database, "bootstrap", sleep=lambda seconds: None)


def test_git_tools_never_touch_the_state_source(system, tmp_path):
    database, _, _ = system
    move.run(database, "bootstrap", apply=True)
    results, errors = sync.ingest(database, local_root=tmp_path / "unused")
    state = next(item for item in results if item["source_id"] == move.STATE)
    assert state["import_status"] == "database_only"
    assert not any(error["stage"] == move.STATE for error in errors)
    report = project.run(environment={"RADAR_DATABASE_PRIMARY": "1"}, database=database, sources=(move.STATE_SPEC,))
    assert report["status"] == "passed" and report["sources"] == [{"source_id": move.STATE, "status": "database_only"}]
