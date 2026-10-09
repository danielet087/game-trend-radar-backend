"""Published consumers use the shared catalog owner with runtime compatibility."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from radar_backend.adapters import public_catalog as catalog
from radar_backend.adapters import published_titles
from radar_backend.adapters import steam_preview_metadata as preview
from scripts import build_public_steam_shards as builder
from scripts import public_catalog as legacy


def forbid_legacy(monkeypatch):
    for name in ("player_category_snapshot", "preserve_player_categories",
                 "keep_newer_release", "write_catalog_projection"):
        monkeypatch.setattr(legacy, name, Mock(side_effect=AssertionError("Legacy catalog helper forbidden")))


def test_shard_builder_uses_shared_rules_and_preserves_newer_evidence(tmp_path, monkeypatch):
    forbid_legacy(monkeypatch)
    frontend, source = tmp_path / "frontend", tmp_path / "input.json"
    categories = [{"id": 1, "description": "Multi-player"}]
    original = {"appid": 7, "name": "Original", "followers": 5000,
                "release_start": "2027-02-28", "release_precision": "day",
                "release_display_precision": "date_full",
                "release_date_verified_at": "2026-10-05T00:00:00Z",
                "categories": categories,
                "categories_source": "Steam Store appdetails cc=TW categories",
                "categories_checked_at": "2026-10-05T00:00:00Z"}
    source.write_text(json.dumps({"games": [original]}))
    observed = datetime(2026, 10, 9, tzinfo=timezone.utc)
    builder.build(source, frontend, authoritative_future=True, now=observed)
    incoming = {key: value for key, value in original.items() if not key.startswith("categories")}
    incoming.update(followers=6000, release_start="2027-02-27",
                    release_date_verified_at="2026-10-04T00:00:00Z")
    source.write_text(json.dumps({"games": [incoming]}))
    builder.build(source, frontend, authoritative_future=True, now=observed)
    for name in ("games/7.json", "catalog.json", "calendar/2027-02.json", "steam_upcoming.json"):
        doc = json.loads((frontend / "data" / name).read_text())
        row = doc if name.startswith("games/") else doc["games"][0]
        assert row["followers"] == 6000 and row["release_start"] == "2027-02-28"
        assert row["categories"] == categories
        assert row["categories_checked_at"] == original["categories_checked_at"]


def test_catalog_revision_keeps_full_rows_and_matching_revision_does_not_rewrite(tmp_path, monkeypatch):
    forbid_legacy(monkeypatch)
    row = {"appid": 7, "name": "針影裁夢", "unknown": {"sequence": 1}}
    first = catalog.write_catalog_projection(tmp_path, [row], "first")
    path = tmp_path / "catalog.json"
    original_bytes = path.read_bytes()
    assert catalog.write_catalog_projection(tmp_path, [row], "second") == first
    assert path.read_bytes() == original_bytes
    changed = {**row, "unknown": {"sequence": 2}}
    second = catalog.write_catalog_projection(tmp_path, [changed], "second")
    assert second["catalog_revision"] != first["catalog_revision"]
    assert json.loads(path.read_text())["games"] == [{"appid": 7, "name": "針影裁夢"}]
    canonical = json.dumps([changed], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert second["catalog_revision"] == hashlib.sha256(canonical.encode()).hexdigest()[:20]
    assert path.read_bytes().endswith(b"\n")


def test_default_preview_category_port_uses_shared_catalog_without_legacy_bridge(monkeypatch):
    forbid_legacy(monkeypatch)
    stamp = "2026-10-09T00:00:00+00:00"
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 9, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(catalog, "datetime", FixedDateTime)
    categories = [{"id": 1, "description": "Multi-player"}]
    details = {"name": "English", "type": "game", "steam_appid": 7,
               "categories": categories, "release_date": {"date": "22 Sep, 2026"}}
    row = preview.normalized_metadata(7, details, 6000, "checked", clock=lambda: stamp)
    assert row["categories"] is categories and row["categories_checked_at"] == stamp
    assert row["release_start"] == "2026-09-22"
    assert catalog.player_category_snapshot(row)[1]["categories"] is categories


def test_published_title_refresh_writes_projection_without_old_catalog(tmp_path, monkeypatch):
    forbid_legacy(monkeypatch)
    data = tmp_path / "data"
    (data / "calendar").mkdir(parents=True)
    row = {"appid": 7, "name": "Original", "followers": 6000,
           "release_start": "2026-09-22", "release_precision": "day"}
    (data / "index.json").write_text(json.dumps({"version": 2, "months": ["2026-09"]}))
    (data / "calendar/2026-09.json").write_text(json.dumps({"count": 1, "games": [row]}))
    lookups = []
    def fetch(session, ids, language, interval):
        lookups.append((ids, language))
        return {7: "繁體名稱" if language == "tchinese" else "简体名称"}
    monkeypatch.setattr(published_titles, "localized_batch", fetch)
    stats = published_titles.refresh(data, session=object(), interval=0)
    assert stats["changed_appids"] == [7]
    assert lookups == [([7], "tchinese"), ([7], "schinese")]
    projected = json.loads((data / "catalog.json").read_text())
    assert projected["version"] == 3 and projected["games"][0]["display_name"] == "繁體名稱"
    assert json.loads((data / "index.json").read_text())["catalog_revision"] == projected["revision"]


def test_legacy_snapshot_keeps_late_clock_sources_fields_and_lazy_validation(monkeypatch):
    checked = datetime(2026, 10, 9, tzinfo=timezone.utc)
    utc = object()
    clock = Mock(return_value=checked)
    parser = Mock(return_value=checked)
    monkeypatch.setattr(legacy, "datetime", SimpleNamespace(fromisoformat=parser, now=clock))
    monkeypatch.setattr(legacy, "timezone", SimpleNamespace(utc=utc))
    monkeypatch.setattr(legacy, "timedelta", lambda *args, **kwargs: timedelta(*args, **kwargs))
    monkeypatch.setattr(legacy, "PLAYER_CATEGORY_SOURCES", {"patched source"})
    monkeypatch.setattr(legacy, "PLAYER_CATEGORY_FIELDS", ("custom",))
    assert legacy.player_category_snapshot({"categories_source": "unknown"}) is None
    clock.assert_not_called()
    parser.assert_not_called()
    value = {"categories_source": "patched source", "categories": [],
             "categories_checked_at": "customZ", "custom": {"shared": True}}
    result = legacy.player_category_snapshot(value)
    assert result == (checked, {"custom": value["custom"]})
    assert result[1]["custom"] is value["custom"]
    parser.assert_called_once_with("custom+00:00")
    clock.assert_called_once_with(utc)


def test_legacy_preservation_uses_current_snapshot_and_release_fields(monkeypatch):
    old = {"appid": 7, "custom": "older row"}
    incoming = {"appid": 7, "custom": "incoming", "unknown": {"retained": True}}
    snapshots = Mock(side_effect=[
        (datetime(2026, 10, 8, tzinfo=timezone.utc), {"custom": "incoming"}),
        (datetime(2026, 10, 9, tzinfo=timezone.utc), {"custom": "prior proof"}),
    ])
    monkeypatch.setattr(legacy, "PLAYER_CATEGORY_FIELDS", ("custom",))
    monkeypatch.setattr(legacy, "player_category_snapshot", snapshots)
    result = legacy.preserve_player_categories(old, incoming)
    assert result["custom"] == "prior proof" and incoming["custom"] == "incoming"
    assert result["unknown"] is incoming["unknown"]
    assert [call.args[0] for call in snapshots.call_args_list] == [incoming, old]
    monkeypatch.setattr(legacy, "RELEASE_FIELDS", ("release_start",))
    old = {"release_display_precision": "date_full", "release_start": "2027-02-28",
           "release_date_verified_at": "2026-10-09T00:00:00Z"}
    incoming = {"release_start": "2027-02-27", "release_date_verified_at": "2026-10-08T00:00:00Z"}
    result = legacy.keep_newer_release(old, incoming)
    assert result["release_start"] == old["release_start"]
    assert result["release_date_verified_at"] == incoming["release_date_verified_at"]


def test_legacy_projection_keeps_current_fields_json_and_hash_modules(tmp_path, monkeypatch):
    operations = []
    def dumps(value, **kwargs):
        operations.append(("dumps", kwargs))
        return json.dumps(value, **kwargs)
    def loads(value):
        operations.append(("loads", value))
        return json.loads(value)
    def sha256(raw):
        operations.append(("hash", raw))
        return SimpleNamespace(hexdigest=lambda: "f" * 64)
    monkeypatch.setattr(legacy, "json", SimpleNamespace(dumps=dumps, loads=loads))
    monkeypatch.setattr(legacy, "hashlib", SimpleNamespace(sha256=sha256))
    monkeypatch.setattr(legacy, "FIELDS", ("appid", "hidden"))
    rows = [{"appid": 7, "hidden": "私有欄位", "name": "Official"}]
    receipt = legacy.write_catalog_projection(tmp_path, rows, "first")
    assert receipt["catalog_revision"] == "f" * 20
    assert operations[0] == ("dumps", {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")})
    assert b'"name":"Official"' in operations[1][1]
    assert operations[2] == ("dumps", {"ensure_ascii": False, "separators": (",", ":")})
    path = tmp_path / "catalog.json"
    assert json.loads(path.read_text())["games"] == [{"appid": 7, "hidden": "私有欄位"}]
    before = path.read_bytes()
    legacy.write_catalog_projection(tmp_path, rows, "second")
    assert path.read_bytes() == before and operations[-1][0] == "loads"


def test_preview_explicit_category_callback_remains_supported_even_when_falsy(monkeypatch):
    def forbidden(row):
        raise AssertionError("Explicit port must take priority")
    monkeypatch.setattr(preview, "_preserve_player_categories", forbidden)
    sentinel = object()
    class Callback:
        def __bool__(self):
            return False
        def __call__(self, old, row):
            assert old == {} and row["appid"] == 7
            return sentinel
    result = preview.normalized_metadata(
        7, {"release_date": {"date": "22 Sep, 2026"}}, 6000, None,
        preserve_player_categories=Callback(),
    )
    assert result is sentinel
