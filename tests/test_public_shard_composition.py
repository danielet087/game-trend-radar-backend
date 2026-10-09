"""Direct shard composition and historical call-time dependency contracts."""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from radar_backend.adapters import public_shards as shards
from radar_backend.publication import steam as publication
from scripts import build_public_steam_shards as legacy
from scripts import steam_adult_exclusions as legacy_adults
from scripts import twitch_steam_admission as legacy_twitch


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def row(appid, day, followers=5000, **values):
    return {"appid": appid, "name": f"Game {appid}", "followers": followers,
            "release_start": day, "release_end": day, "release_precision": "day",
            "release_display_precision": "date_full", **values}


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def forbid_legacy(monkeypatch):
    for name in ("load_json", "write_if_changed", "valid_record", "merge_game", "build"):
        monkeypatch.setattr(legacy, name, Mock(side_effect=AssertionError("Legacy builder forbidden")))
    for module, names in ((legacy_adults, ("excluded_appids", "is_disallowed")),
                          (legacy_twitch, ("is_twitch_qualified", "preserve_twitch_admission"))):
        for name in names:
            monkeypatch.setattr(module, name, Mock(side_effect=AssertionError("Legacy source shim forbidden")))


def test_direct_shards_preserve_history_twitch_and_newer_metadata_with_legacy_forbidden(tmp_path, monkeypatch):
    forbid_legacy(monkeypatch)
    monkeypatch.setattr(shards, "excluded_appids", lambda: {90})
    frontend, source = tmp_path / "frontend", tmp_path / "source.json"
    data = frontend / "data"
    proof = {
        "schema_version": 1, "method": "twitch_igdb_external_steam_v1", "appid": 3,
        "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-02T08:00:00Z",
        "source_frontend_commit": "a" * 40,
        "source_enrollment": {"source": "igdb_first_release_date", "observed_at": "2026-10-01T08:00:00Z",
                              "viewer_count": 8000, "min_viewers": 7000},
    }
    twitch = row(3, "2026-12-01", 120, twitch_admission=proof,
                 steam_type="game", sexual_content_screened=True,
                 release_time_utc="2026-12-01T07:00:00Z", release_date_timezone="Asia/Taipei",
                 follower_checked_at="2026-10-08T00:00:00Z")
    assert shards.is_twitch_qualified(twitch)
    categories = [{"id": 1, "description": "Multi-player"}]
    rich = row(4, "2027-01-20", name_zh_tw="针影裁梦", short_description="已有介紹",
               unknown={"retained": True}, release_date_verified_at="2026-10-08T00:00:00Z",
               categories=categories, categories_source="Steam Store appdetails cc=TW categories",
               categories_checked_at="2026-10-08T00:00:00Z")
    existing = [row(1, "2026-11-01"), row(2, "2026-09-20"), twitch, rich,
                row(5, "2026-10-08", 3001, recent_source="direct_release"),
                row(6, "2026-10-08", 3000, recent_source="direct_release"),
                row(90, "2026-11-01")]
    for game in existing:
        save(data / "games" / f"{game['appid']}.json", game)
    save(data / "calendar/2026-11.json", {"old": True})
    incoming = row(4, "2027-01-15", 6000, name="Updated English")
    incoming.pop("release_display_precision")
    save(source, {"generated_at": NOW.isoformat(), "games": [incoming, row(7, "2026-10-09")]})
    stats = shards.build(source, frontend, authoritative_future=True, now=NOW)
    assert publication.build is shards.build
    assert stats["games"] == 6 and stats["removed_stale_future"] == 2
    assert not (data / "games/1.json").exists() and not (data / "games/90.json").exists()
    assert not (data / "calendar/2026-11.json").exists()
    detail = json.loads((data / "games/4.json").read_text())
    assert detail["release_start"] == "2027-01-20" and detail["followers"] == 6000
    assert detail["short_description"] == rich["short_description"]
    assert detail["unknown"] == rich["unknown"] and detail["categories"] == categories
    assert detail["name_zh_tw"] == "针影裁梦" and detail["display_name"] == "針影裁夢"
    assert json.loads((data / "games/3.json").read_text())["twitch_admission"] == proof
    assert json.loads((data / "lists/upcoming.json").read_text())["appids"] == [7, 3, 4]
    assert json.loads((data / "lists/released.json").read_text())["appids"] == [2, 5]
    index = json.loads((data / "index.json").read_text())
    catalog = json.loads((data / "catalog.json").read_text())
    fallback = json.loads((data / "steam_upcoming.json").read_text())
    assert index["game_count"] == catalog["count"] == fallback["count"] == 6
    assert index["catalog_revision"] == catalog["revision"]
    assert index["release_date_audited"] is True


@pytest.mark.parametrize("payload", ["broken-json", "{}", '{"games":[]}', "[]"])
def test_direct_authoritative_validation_finishes_before_any_existing_file_change(tmp_path, monkeypatch, payload):
    forbid_legacy(monkeypatch)
    frontend, source = tmp_path / "frontend", tmp_path / "source.json"
    retained = frontend / "data/games/7.json"
    save(retained, row(7, "2027-01-01"))
    before = retained.read_bytes()
    source.write_text(payload)
    with pytest.raises(ValueError):
        shards.build(source, frontend, authoritative_future=True, now=NOW)
    assert retained.read_bytes() == before
    assert not (frontend / "data/index.json").exists()


def test_legacy_timestamp_skip_uses_current_load_callback_before_json_encoding(tmp_path, monkeypatch):
    path = tmp_path / "shard.json"
    path.write_text('{"generated_at":"old","count":1}\n')
    before = path.read_bytes()
    calls = []
    class Loader:
        def __bool__(self):
            return False
        def __call__(self, target, default):
            calls.append((target, default))
            return {"generated_at": "old", "count": 1}
    encoder = Mock(side_effect=AssertionError("Unchanged content must not encode"))
    monkeypatch.setattr(legacy, "load_json", Loader())
    monkeypatch.setattr(legacy, "json", SimpleNamespace(dumps=encoder))
    assert legacy.write_if_changed(path, {"generated_at": "new", "count": 1}) is False
    assert calls == [(path, {})] and path.read_bytes() == before
    encoder.assert_not_called()


def test_legacy_validity_and_merge_use_current_rules_callbacks_and_field_sets(monkeypatch):
    parse = Mock(return_value=SimpleNamespace(isoformat=lambda: "2026-11-11"))
    qualified = Mock(return_value=True)
    monkeypatch.setattr(legacy, "date", SimpleNamespace(fromisoformat=parse))
    monkeypatch.setattr(legacy, "is_twitch_qualified", qualified)
    record = row(7, "2026-11-11", 1)
    assert legacy.valid_record(record)
    parse.assert_called_once_with("2026-11-11")
    qualified.assert_called_once_with(record)
    events = []
    for name, label in (("keep_newer_release", "date"), ("preserve_twitch_admission", "twitch"),
                        ("preserve_player_categories", "categories")):
        monkeypatch.setattr(legacy, name, lambda old, new, label=label:
                            (events.append(label), new)[1])
    def display(game):
        events.append("display")
        game["display_name"] = "patched"
    monkeypatch.setattr(legacy, "add_traditional_display_names", display)
    monkeypatch.setattr(legacy, "CORE_FIELDS", {"followers", "custom"})
    monkeypatch.setattr(legacy, "PLAYER_CATEGORY_FIELDS", ("mode",))
    old = {"appid": 7, "followers": 5000, "custom": "old", "mode": [1], "unknown": {"kept": True}}
    new = {"appid": "7", "followers": 6000, "custom": "new", "unknown": "ignored"}
    merged = legacy.merge_game(old, new)
    assert events == ["date", "twitch", "categories", "display"]
    assert merged["custom"] == "new" and merged["unknown"] is old["unknown"]
    assert merged["appid"] == 7 and merged["storage_version"] == 2 and "mode" not in merged
    assert old["mode"] == [1] and new["appid"] == "7"


def test_legacy_build_resolves_runtime_state_qualification_projection_and_clock(tmp_path, monkeypatch):
    frontend, source = tmp_path / "frontend", tmp_path / "source.json"
    save(source, {"games": [row(7, "2026-11-11", 1)]})
    events = []
    original_read, original_write = legacy.load_json, legacy.write_if_changed
    monkeypatch.setattr(legacy, "load_json", lambda path, default:
                        (events.append(("read", path.name)), original_read(path, default))[1])
    monkeypatch.setattr(legacy, "write_if_changed", lambda path, value:
                        (events.append(("write", path.name)), original_write(path, value))[1])
    monkeypatch.setattr(legacy, "excluded_appids", lambda: set())
    monkeypatch.setattr(legacy, "is_disallowed", lambda row, blocked: False)
    monkeypatch.setattr(legacy, "is_twitch_qualified", lambda row: True)
    monkeypatch.setattr(legacy, "valid_record", lambda row: True)
    monkeypatch.setattr(legacy, "merge_game", lambda old, new: {**new, "unknown": "patched merge"})
    monkeypatch.setattr(legacy, "add_traditional_display_names", lambda game: game.update(display_name="patched name"))
    monkeypatch.setattr(legacy, "write_catalog_projection", lambda path, rows, at:
                        (events.append(("projection", at)), {"catalog_revision": "patched revision"})[1])
    clock = Mock(return_value=NOW)
    monkeypatch.setattr(legacy, "datetime", SimpleNamespace(now=clock))
    result = legacy.build(source, frontend, authoritative_future=True)
    assert result["games"] == result["upcoming"] == 1
    clock.assert_called_once_with(legacy.timezone.utc)
    detail = json.loads((frontend / "data/games/7.json").read_text())
    assert detail["unknown"] == "patched merge" and detail["display_name"] == "patched name"
    assert ("projection", NOW.isoformat()) in events
    assert [event for event in events if event[0] == "write"][-1] == ("write", "index.json")
    assert json.loads((frontend / "data/index.json").read_text())["catalog_revision"] == "patched revision"


def test_direct_build_replays_frozen_taiwan_day_without_reading_a_new_clock(tmp_path, monkeypatch):
    forbid_legacy(monkeypatch)
    monkeypatch.setattr(shards, "excluded_appids", lambda: set())
    clock = Mock(side_effect=AssertionError("Frozen publication must reuse its clock"))
    monkeypatch.setattr(shards, "datetime", SimpleNamespace(now=clock))
    observed = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)  # Oct 9 in Taiwan.
    frontend, source = tmp_path / "frontend", tmp_path / "source.json"
    save(source, {"games": [row(7, "2026-10-09")]})
    first = shards.build(source, frontend, authoritative_future=True, now=observed)
    before = {path.relative_to(frontend): path.read_bytes() for path in frontend.rglob("*.json")}
    second = shards.build(source, frontend, authoritative_future=True, now=observed)
    after = {path.relative_to(frontend): path.read_bytes() for path in frontend.rglob("*.json")}
    assert first["upcoming"] == second["upcoming"] == 1 and first["released"] == second["released"] == 0
    assert second["changed_games"] == second["changed_months"] == 0 and before == after
    clock.assert_not_called()
