import pytest

from radar_core.storage import documents, primary_store
from radar_core.storage.sources import SOURCES, git_backed, source_id
from radar_core.storage.sql import SqliteClient, StorageError
from scripts import bootstrap_twitch_views as bootstrap
from scripts import storage_project as project
from scripts import storage_sync as sync

FILES = {
    "data/twitch_live.json": b'{"live": 1}',
    "data/twitch_history/2026-11-17.json": b'{"hours": [1]}',
    "data/twitch_history_compact/2026-11-17.json": b'{"games": {}}',
    "data/twitch_history_close/2026-11-16.json": b'{"close": 1}',
    "data/twitch_series/index.json": b'{"games": {}}',
    "data/twitch_series/123/2026-11.json": b'{"points": []}',
    "data/twitch_history_extra.json": b'{"not": "a view"}',
}


@pytest.fixture()
def database(tmp_path):
    client = SqliteClient(tmp_path / "primary.sqlite")
    documents.initialize(client)
    documents.publish_snapshot(client, bootstrap.FRONTEND, "frontend-1", FILES, "2026-11-17T00:00:00Z")
    primary_store.activate_primary(client, bootstrap.FRONTEND)
    yield client
    client.close()


def test_the_views_source_is_database_only_and_covers_exactly_the_four_directories():
    spec = next(item for item in SOURCES if source_id(item) == "danielet087/game-trend-radar@twitch-views")
    assert not git_backed(spec)
    assert spec["prefixes"] == ("data/twitch_history/", "data/twitch_history_compact/",
                                "data/twitch_history_close/", "data/twitch_series/")
    assert [source_id(item) for item in SOURCES if not git_backed(item)][0] == bootstrap.VIEWS


def test_dry_run_reports_without_writing(database):
    report = bootstrap.run(database)
    assert report["status"] == "dry_run"
    assert report["files"] == 5
    assert report["by_prefix"]["data/twitch_series/"] == 2
    assert documents._head(database, bootstrap.VIEWS) is None


def test_apply_copies_only_view_bytes_activates_and_is_never_repeated(database):
    report = bootstrap.run(database, apply=True)
    assert report["status"] == "bootstrapped" and report["active"] is True and report["generation"] == 1
    _, copied = primary_store.read_files(database, bootstrap.VIEWS)
    assert copied == {path: raw for path, raw in FILES.items() if path.startswith(bootstrap.PREFIXES)}
    # The frontend source keeps its copy until the Twitch publisher stops writing it.
    _, frontend = primary_store.read_files(database, bootstrap.FRONTEND)
    assert frontend == FILES
    again = bootstrap.run(database, apply=True)
    assert again["status"] == "exists" and again["generation"] == 1


def test_a_busy_lease_is_retried_and_other_errors_are_not(database, monkeypatch):
    calls = []

    def busy_then_ok(client, *, apply=False):
        calls.append(apply)
        if len(calls) < 3:
            raise StorageError("LEASE_BUSY", "busy")
        return {"status": "dry_run"}

    monkeypatch.setattr(bootstrap, "bootstrap", busy_then_ok)
    assert bootstrap.run(database, sleep=lambda seconds: None)["attempts"] == 3
    monkeypatch.setattr(bootstrap, "bootstrap", lambda client, apply=False: (_ for _ in ()).throw(StorageError("NO_TWITCH_VIEWS", "x")))
    with pytest.raises(StorageError):
        bootstrap.run(database, sleep=lambda seconds: None)


def test_git_tools_never_touch_the_database_only_source(database, tmp_path):
    bootstrap.run(database, apply=True)
    results, errors = sync.ingest(database, local_root=tmp_path / "unused")
    views = next(item for item in results if item["source_id"] == bootstrap.VIEWS)
    assert views["import_status"] == "database_only"
    assert not any(error["stage"] == bootstrap.VIEWS for error in errors)
    spec = next(item for item in SOURCES if not git_backed(item))
    report = project.run(environment={"RADAR_DATABASE_PRIMARY": "1"}, database=database, sources=(spec,))
    assert report == {**report, "status": "passed", "errors": [],
                      "sources": [{"source_id": bootstrap.VIEWS, "status": "database_only"}]}
