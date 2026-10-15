"""Pending database writes are acknowledged only after a real public Git push."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from radar_core.storage import documents, primary, primary_store
from radar_core.storage.sql import SqliteClient, StorageError
from scripts import storage_project as project

SPEC = project.SOURCES[0]
SOURCE = project.source_id(SPEC)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.decode().strip()


@pytest.fixture
def system(tmp_path, monkeypatch):
    database = SqliteClient(tmp_path / "primary.sqlite")
    documents.initialize(database)
    primary_store.bootstrap_primary(database, SOURCE, {"data/value.json": b'{"value":"database"}\n'}, "a" * 40)
    seed, remote = tmp_path / "seed", tmp_path / "remote.git"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "data").mkdir()
    (seed / "data/value.json").write_text('{"value":"git"}\n')
    (seed / "README.md").write_text("Current code and curated configuration\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "initial")
    git(tmp_path, "clone", "--bare", str(seed), str(remote))
    monkeypatch.setattr(primary, "client", lambda: database)
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    original_git = project._git
    events = []
    def local_git(args, **kwargs):
        events.append(args[0])
        args = list(args)
        if args[0] == "clone":
            assert args[-2] == "https://github.com/" + SPEC["repository"] + ".git"
            args[-2] = str(remote)
        return original_git(args, **kwargs)
    monkeypatch.setattr(project, "_git", local_git)
    def hydrate(root, source, **kwargs):
        events.append("hydrate")
        return primary.hydrate(root, source, clean_baseline=True)
    monkeypatch.setattr(project, "_hydrate", hydrate)
    environment = {**os.environ, "RADAR_DATABASE_PRIMARY": "1", "FRONTEND_REPO_TOKEN": "frontend-test-secret",
                   "GH_TOKEN": "backend-test-secret"}
    yield database, remote, environment, events
    database.close()


def change(database, operation, value, *, projection_required=True):
    head, _ = primary_store.read_files(database, SOURCE)
    return primary_store.publish_patch(database, SOURCE, base_generation=head["generation"],
        changed={"data/value.json": json.dumps({"value": value}).encode() + b"\n"},
        operation_id=operation, projection_required=projection_required)


def test_real_git_projection_repairs_commit_and_repeated_run_is_noop(system):
    database, remote, environment, events = system
    change(database, "pending-1", "committed-before-crash")
    report = project.run(database=database, environment=environment, sources=(SPEC,))
    row = report["sources"][0]
    assert report["status"] == "passed" and row["acknowledged"] == 1
    revision = git(remote, "rev-parse", "main")
    assert revision == row["git_revision"]
    assert json.loads(git(remote, "show", "main:data/value.json")) == {"value": "committed-before-crash"}
    assert git(remote, "show", "main:README.md") == "Current code and curated configuration"
    stored = database.execute("SELECT * FROM radar_operations WHERE operation_id='pending-1'")[0]
    assert stored["projection_git_revision"] == revision
    assert stored["projection_generation"] == row["generation"]
    assert stored["projection_manifest_digest"] == row["manifest_digest"]
    assert events.index("hydrate") < events.index("push")
    events.clear()
    repeated = project.run(database=database, environment=environment, sources=(SPEC,))
    assert repeated["sources"][0]["status"] == "up_to_date" and events == []


def test_rejected_pushes_are_bounded_and_never_acknowledged(system, monkeypatch):
    database, _, environment, events = system
    change(database, "pending", "retained")
    original = project._git
    pushes = []
    def rejected(args, **kwargs):
        if args[0] == "push":
            pushes.append(1)
            return subprocess.CompletedProcess([], 1, b"", b"secret remote response")
        return original(args, **kwargs)
    monkeypatch.setattr(project, "_git", rejected)
    report = project.run(database=database, environment=environment, sources=(SPEC,))
    assert report["status"] == "degraded" and len(pushes) == 3
    assert report["errors"][0]["code"] == "GIT_PUSH_REJECTED"
    assert len(primary_store.projection_pending(database, SOURCE)) == 1
    assert events.count("hydrate") == 3 and events.count("reset") == 2
    assert "secret" not in json.dumps(report)


def test_real_nonfastforward_refreshes_git_and_database_without_backfeeding(system, monkeypatch, tmp_path):
    database, remote, environment, _ = system
    change(database, "pending-old", "first-database-value")
    original = project._git
    pushed = []
    def concurrent(args, **kwargs):
        if args[0] == "push" and not pushed:
            pushed.append(1)
            racer = tmp_path / "racer"
            git(tmp_path, "clone", str(remote), str(racer))
            git(racer, "config", "user.email", "racer@example.com")
            git(racer, "config", "user.name", "Racer")
            (racer / "README.md").write_text("New code was published concurrently\n")
            (racer / "data/value.json").write_text('{"value":"untrusted-git-value"}\n')
            git(racer, "add", ".")
            git(racer, "commit", "-m", "concurrent code")
            git(racer, "push", "origin", "main")
            change(database, "pending-new", "newest-database-value")
        return original(args, **kwargs)
    monkeypatch.setattr(project, "_git", concurrent)
    report = project.run(database=database, environment=environment, sources=(SPEC,))
    row = report["sources"][0]
    assert row["attempts"] == 2 and row["acknowledged"] == 2
    assert json.loads(git(remote, "show", "main:data/value.json"))["value"] == "newest-database-value"
    assert git(remote, "show", "main:README.md") == "New code was published concurrently"
    assert json.loads(primary_store.read_files(database, SOURCE)[1]["data/value.json"])["value"] == "newest-database-value"


def test_operations_after_hydration_remain_pending(system, monkeypatch):
    database, remote, environment, _ = system
    change(database, "before-hydrate", "project-this")
    original = project._hydrate
    def later(*args, **kwargs):
        head = original(*args, **kwargs)
        change(database, "after-hydrate", "next-cycle")
        return head
    monkeypatch.setattr(project, "_hydrate", later)
    report = project.run(database=database, environment=environment, sources=(SPEC,))
    assert report["sources"][0]["acknowledged"] == 1
    assert report["sources"][0]["pending"] == 1
    assert json.loads(git(remote, "show", "main:data/value.json"))["value"] == "project-this"
    assert [r["operation_id"] for r in primary_store.projection_pending(database, SOURCE)] == ["after-hydrate"]


def test_database_only_and_inactive_sources_never_need_git_or_credentials(system):
    database, _, environment, events = system
    change(database, "private-cache", "durable-only", projection_required=False)
    environment.pop("FRONTEND_REPO_TOKEN")
    report = project.run(database=database, environment=environment, sources=(SPEC, project.SOURCES[-1]))
    assert [r["status"] for r in report["sources"]] == ["up_to_date", "inactive"]
    assert events == []


def test_source_failure_does_not_stop_other_sources_and_errors_are_safe(monkeypatch):
    calls = []
    def source(database, spec, **kwargs):
        calls.append(spec)
        if len(calls) == 1:
            raise ValueError("token=never-emit-this")
        return {"source_id": project.source_id(spec), "status": "up_to_date"}
    monkeypatch.setattr(project, "project_source", source)
    report = project.run(database=object(), environment={"RADAR_DATABASE_PRIMARY": "1"}, sources=project.SOURCES[:2])
    assert len(calls) == 2 and report["status"] == "degraded"
    assert report["errors"] == [{"source_id": SOURCE, "code": "PROJECTION_FAILED"}]
    assert "never-emit" not in json.dumps(report)


def test_git_credentials_are_env_only_and_redirects_traces_helpers_are_disabled(tmp_path, monkeypatch):
    askpass = project._askpass(tmp_path)
    environment = project._git_environment({"PATH": os.environ["PATH"], "GH_TOKEN": "backend-secret",
        "FRONTEND_REPO_TOKEN": "frontend-secret", "TURSO_AUTH_TOKEN": "database-secret",
        "GIT_TRACE": "1", "GIT_CONFIG_COUNT": "4", "GIT_CONFIG_KEY_0": "http.extraHeader"}, askpass, "selected-secret")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or subprocess.CompletedProcess(argv, 0, b"", b""))
    project._git(["push", "origin", "HEAD:refs/heads/main"], environment=environment, deadline=project.Deadline())
    argv, options = calls[0]
    assert "selected-secret" not in " ".join(argv) and "selected-secret" not in askpass.read_text()
    assert options["env"]["RADAR_STORAGE_GIT_TOKEN"] == "selected-secret"
    assert not any(key in options["env"] for key in ("GIT_TRACE", "GIT_CONFIG_KEY_0", "GH_TOKEN", "FRONTEND_REPO_TOKEN", "TURSO_AUTH_TOKEN"))
    assert "http.followRedirects=false" in argv and "credential.helper=" in argv
    assert 0 < options["timeout"] <= 60


def test_missing_credentials_and_expired_global_budget_leave_operations_pending(system):
    database, _, environment, events = system
    change(database, "pending", "durable")
    environment.pop("FRONTEND_REPO_TOKEN")
    report = project.run(database=database, environment=environment, sources=(SPEC,))
    assert report["errors"][0]["code"] == "MISSING_GIT_CREDENTIAL" and not events
    report = project.run(database=database, environment=environment, deadline=project.Deadline(seconds=0), sources=project.SOURCES)
    assert len(report["errors"]) == 7
    assert {e["code"] for e in report["errors"]} == {"TIME_BUDGET_EXCEEDED"}
    assert len(primary_store.projection_pending(database, SOURCE)) == 1


def test_cli_writes_safe_failure_report_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    def fail(): raise RuntimeError("private URL and token")
    monkeypatch.setattr(project, "run", fail)
    path = tmp_path / "reports/projection.json"
    assert project.main(["--report-path", str(path)]) == 1
    report = json.loads(path.read_text())
    assert report["status"] == "degraded" and report["errors"][0]["code"] == "PROJECTION_FAILED"
    assert "private" not in capsys.readouterr().out


def test_primary_mode_is_explicitly_required():
    with pytest.raises(StorageError) as failure:
        project.run(database=object(), environment={})
    assert failure.value.code == "PRIMARY_MODE_REQUIRED"
