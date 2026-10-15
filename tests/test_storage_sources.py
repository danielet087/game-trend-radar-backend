"""Lossless, immutable source reads and complete production source coverage."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from radar_backend.storage import sources
from radar_backend.storage.sql import StorageError


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Storage test", "-c", "user.email=storage@example.test",
         "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def repository(root):
    root.mkdir()
    git(root, "init", "--initial-branch=main", "--quiet")
    return root


def commit_files(root, files, *, message="source snapshot"):
    for relative, value in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value)
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def backend_spec():
    return next(spec for spec in sources.SOURCES
                if spec["repository"] == "danielet087/game-trend-radar-backend"
                and spec["branch"] == "main")


def test_snapshot_reads_committed_bytes_ignoring_dirty_untracked_and_worktree_symlinks(tmp_path):
    root = repository(tmp_path / "source")
    raw = '{\r\n  "名稱": "遊戲雷達", "group": "103582791475572379", "unknown": {"x": 1}\r\n}\n'.encode()
    revision = commit_files(root, {"data/state.json": raw})
    outside = tmp_path / "private.json"
    outside.write_bytes(b'{"private":"must-not-read"}')
    (root / "data/state.json").unlink()
    (root / "data/state.json").symlink_to(outside)
    (root / "data/untracked.json").write_bytes(b"invalid untracked data")

    snapshot = sources.snapshot_from_checkout(backend_spec(), root)

    assert snapshot["revision"] == revision
    assert snapshot["files"] == {"data/state.json": raw}
    assert snapshot["raw_bytes"] == len(raw)
    assert sources.file_manifest(snapshot["files"]) == {
        "data/state.json": {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
    }
    assert (root / "data/state.json").is_symlink()
    assert outside.read_bytes() == b'{"private":"must-not-read"}'


def test_head_moving_after_capture_cannot_mix_two_commits(tmp_path, monkeypatch):
    root = repository(tmp_path / "source")
    old_files = {"data/a.json": b'{"version":1}', "data/b.json": b'{"version":1}'}
    revision = commit_files(root, old_files)
    original_git = sources._git
    advanced = []

    def moving_head(args, **kwargs):
        if args[0] == "ls-tree" and not advanced:
            advanced.append(commit_files(root, {"data/a.json": b'{"version":2}',
                                                "data/b.json": b'{"version":2}'}, message="new source"))
        return original_git(args, **kwargs)

    monkeypatch.setattr(sources, "_git", moving_head)
    snapshot = sources.snapshot_from_checkout(backend_spec(), root)

    assert advanced and git(root, "rev-parse", "HEAD") != revision
    assert snapshot["revision"] == revision
    assert snapshot["files"] == old_files


@pytest.mark.parametrize("path,raw", [
    ("data/bad.json", b'{"secret":"provider-private",'),
    ("data/bad.json", b'{"key":1,"key":2}'),
    ("data/bad.json", b'{"value":NaN}'),
    ("data/bad.json", b'{"value":Infinity}'),
    ("data/bad.json", b'{"value":-Infinity}'),
    ("data/bad.json", b'{"value":1e999}'),
    ("data/bad.json", b'{"value":-1e999}'),
    ("data/bad.json", b'{"value":"\xff"}'),
    ("data/bad.jsonl", b'{"ok":true}\n{"key":1,"key":2}\n'),
    ("data/bad.ndjson", b'{"ok":true}\n{"value":1e999}\n'),
])
def test_invalid_document_rejects_entire_source_without_exposing_contents(tmp_path, path, raw):
    root = repository(tmp_path / "source")
    commit_files(root, {"data/good.json": b'{"good":true}', path: raw})
    with pytest.raises(StorageError) as failure:
        sources.snapshot_from_checkout(backend_spec(), root)
    assert failure.value.code == "INVALID_SOURCE_JSON"
    assert "provider-private" not in str(failure.value)


def test_all_production_repositories_and_backend_state_branch_are_included(tmp_path):
    expected = {
        "danielet087/game-trend-radar@main": {
            "data/catalog.json", "data/insights-state.json", "data/twitch_history/2026-10-15.json",
            "data/twitch_tracking.json", "data/twitch_steam_mapping.json", "data/twitch_steam_discovery.json",
            "data/nintendo_master.json", "data/nintendo_upcoming.json", "data/publication/steam_content.json",
        },
        "danielet087/game-trend-radar-backend@main": {
            "data/steam_candidate_state.json", "data/steam_candidates.json", "data/steam_prefilter_state.json",
            "data/steam_candidates_eligible.json", "data/steam_upcoming_master.json",
            "data/twitch_steam_import_state.json", "experiments/steam_official_daily_catchup/checkpoint.json",
            "reports/retained-state.json",
        },
        "danielet087/game-trend-radar-backend@steam-state": {"data/steam_followers_checkpoint.json"},
        "danielet087/game-trend-radar-content-backend@main": {"content_backend/descriptions_zh_tw.json"},
        "danielet087/game-trend-radar-igdb-backend@main": {"data/platforms.json"},
        "danielet087/game-trend-radar-twitch-backend@main": {"data/twitch_tracking.json"},
        "danielet087/game-trend-radar-youtube-backend@main": set(),
    }
    assert {sources.source_id(spec) for spec in sources.SOURCES} == set(expected)
    assert len({sources.checkout_name(spec) for spec in sources.SOURCES}) == len(expected)
    for index, spec in enumerate(sources.SOURCES):
        identity = sources.source_id(spec)
        root = repository(tmp_path / str(index))
        files = {path: b'{"unknown": ["preserve", "all", "fields"]}\n' for path in expected[identity]}
        # Test/fixture/output/package JSON is deliberately outside production source roots.
        ignored = {"tests/fixture.json": b"invalid", "output/transient.json": b"invalid",
                   "package.json": b"invalid", "node_modules/dependency/data.json": b"invalid"}
        commit_files(root, {**files, **ignored})
        snapshot = sources.snapshot_from_checkout(spec, root)
        assert snapshot["source_id"] == identity
        assert snapshot["files"] == files


def test_jsonl_ndjson_and_explicit_content_file_preserve_exact_bytes(tmp_path):
    root = repository(tmp_path / "source")
    spec = {"repository": "test/source", "branch": "main", "prefixes": ("data/",),
            "files": ("content/localized.json",)}
    files = {"data/events.jsonl": b' {"a":1}\r\n\r\n{"b":2}\n',
             "data/observations.ndjson": b'{"x":null}\n{"x":true}',
             "content/localized.json": ' {"繁中":"內容"}\n'.encode()}
    commit_files(root, {**files, "data/readme.txt": b"not a JSON source", "unselected.json": b"invalid"})
    assert sources.snapshot_from_checkout(spec, root)["files"] == files


def test_tracked_json_symlink_is_rejected_before_its_target_can_be_read(tmp_path, monkeypatch):
    root = repository(tmp_path / "source")
    (root / "data").mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"private":"not source data"}')
    (root / "data/linked.json").symlink_to(outside)
    commit_files(root, {})
    original_git = sources._git
    blob_reads = []

    def guarded_git(args, **kwargs):
        if args[0] == "cat-file":
            blob_reads.append(args)
        return original_git(args, **kwargs)

    monkeypatch.setattr(sources, "_git", guarded_git)
    with pytest.raises(StorageError) as failure:
        sources.snapshot_from_checkout(backend_spec(), root)
    assert failure.value.code == "INVALID_SOURCE"
    assert blob_reads == []


def test_ambiguous_backslash_filename_cannot_be_exported_as_another_path(tmp_path):
    root = repository(tmp_path / "source")
    commit_files(root, {"data/folder\\file.json": b"{}"})
    with pytest.raises(StorageError) as failure:
        sources.snapshot_from_checkout(backend_spec(), root)
    assert failure.value.code == "INVALID_SOURCE"


def test_required_source_cannot_be_replaced_by_an_empty_snapshot(tmp_path):
    root = repository(tmp_path / "source")
    commit_files(root, {"README.md": b"No dataset available"})
    with pytest.raises(StorageError) as failure:
        sources.snapshot_from_checkout(backend_spec(), root)
    assert failure.value.code == "EMPTY_SOURCE"


@pytest.mark.parametrize("limit_name,limit", [("MAX_FILE_BYTES", 1), ("MAX_SOURCE_BYTES", 3)])
def test_size_limit_rejects_whole_source(tmp_path, monkeypatch, limit_name, limit):
    root = repository(tmp_path / "source")
    commit_files(root, {"data/a.json": b"{}", "data/b.json": b"{}"})
    monkeypatch.setattr(sources, limit_name, limit)
    with pytest.raises(StorageError) as failure:
        sources.snapshot_from_checkout(backend_spec(), root)
    assert failure.value.code == "SOURCE_SIZE_LIMIT"


def test_clone_refuses_existing_directory_without_resetting_it(tmp_path, monkeypatch):
    spec = backend_spec()
    destination = tmp_path / sources.checkout_name(spec)
    destination.mkdir()
    sentinel = destination / "user-work.txt"
    sentinel.write_text("keep this", encoding="utf-8")
    monkeypatch.setattr(sources, "_git", lambda *args, **kwargs: pytest.fail("Existing work must not be reset"))
    with pytest.raises(StorageError) as failure:
        sources.clone_source(spec, tmp_path)
    assert failure.value.code == "SOURCE_DIRECTORY_EXISTS"
    assert sentinel.read_text() == "keep this"
