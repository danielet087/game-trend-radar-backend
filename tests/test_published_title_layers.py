from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from radar_backend.application import published_titles as application
from radar_backend.domain import published_titles as rules
from radar_backend.state import published_titles as state


@pytest.mark.parametrize("value,english,expected", [
    (None, "Game", None),
    (True, "Game", None),
    (123, "Game", None),
    ([], "Game", None),
    ("", "Game", None),
    (" \n\t ", "Game", None),
    ("Game", "Game", None),
    (" game ", " GAME ", None),
    ("English fallback", "Another English title", None),
    (" 中文 ", "Game", "中文"),
    (" 游戏 中文 ", "Game", "游戏 中文"),
    ("中文", " 中文 ", None),
    ("中" * 240, "Game", "中" * 240),
    ("中" * 241, "Game", None),
    ("\u3400", "Game", "\u3400"),
    ("\u9fff", "Game", "\u9fff"),
    ("\ua000", "Game", None),
])
def test_candidate_requires_a_distinct_store_chinese_title(value, english, expected):
    assert rules.candidate(value, english) == expected


def test_candidate_uses_current_han_after_english_and_length_checks():
    events = []
    han = SimpleNamespace(search=lambda name: events.append(name) or True)
    assert rules.candidate(" Latin ", "Game", han=han) == "Latin"
    assert rules.candidate(" game ", "GAME", han=han) is None
    assert rules.candidate("x" * 241, "Game", han=han) is None
    assert events == ["Latin"]


def test_candidate_invalid_english_keeps_original_error_order():
    assert rules.candidate(None, None) is None
    assert rules.candidate("  ", None) is None
    with pytest.raises(AttributeError):
        rules.candidate("中" * 241, None)


@pytest.mark.parametrize("extra,tw,cn,expected,source", [
    ({}, " 繁中 ", " 简中 ", "C:繁中", "tchinese"),
    ({}, None, " 简中 ", "C:简中", "schinese_converted"),
    ({}, None, None, "English", "english"),
    ({"name_en": "Original English"}, None, None, "Original English", "english"),
    ({"name_en": 123}, None, None, "123", "english"),
    ({"name_zh_tw": " 原繁中 "}, None, None, "C:原繁中", "tchinese"),
    ({"name_zh_cn": " 原简中 "}, None, None, "C:原简中", "schinese_converted"),
    ({"name_zh_tw": " ", "name_zh_tw_traditional": "舊顯示"}, None, None, "舊顯示", "tchinese"),
    ({"name_zh_tw": None, "name_zh_tw_traditional": 9}, None, None, 9, "tchinese"),
    ({"name_zh_cn_traditional": "舊簡中顯示"}, None, None, "舊簡中顯示", "schinese_converted"),
    ({"name_en_traditional": "不採用此欄位"}, None, None, "English", "english"),
])
def test_published_title_priority_and_retained_display_values(extra, tw, cn, expected, source):
    row = {"appid": 1, "name": "English", **extra}
    result = rules.update_title(row, tw, cn, convert=lambda name: "C:" + name)
    assert result["display_name"] == expected
    assert result["display_name_source"] == source
    assert result["storage_version"] == 2
    assert row == {"appid": 1, "name": "English", **extra}


def test_title_conversion_preserves_raw_names_unknown_fields_and_order():
    nested = {"unexpected": [1, 2]}
    row = {"appid": 2, "name": "English", "unknown": nested,
           "language_support": {"tchinese": False}, "followers": 42}
    events = []
    out = rules.update_title(row, " 简体台湾 ", " 简体大陆 ",
                             convert=lambda name: events.append(name) or name.replace("简体", "繁體"))
    assert events == ["简体台湾", "简体大陆"]
    assert out["name_zh_tw"] == " 简体台湾 "
    assert out["name_zh_cn"] == " 简体大陆 "
    assert out["display_name"] == "繁體台湾"
    assert out["unknown"] is nested
    assert out["language_support"] is row["language_support"]
    assert out["language_support"]["tchinese"] is False
    assert out["followers"] == 42


def test_title_converter_is_not_called_without_nonblank_strings():
    def fail(name):
        raise AssertionError("Unexpected conversion")
    result = rules.update_title({"appid": 8, "name_zh_tw": " ", "name_zh_cn": None},
                                None, None, convert=fail)
    assert result["display_name"] == "Steam App 8"
    with pytest.raises(KeyError, match="appid"):
        rules.update_title({}, None, None, convert=fail)
    assert rules.update_title({"name": "English"}, None, None, convert=fail)["display_name"] == "English"


def test_truthy_nonstring_new_titles_and_false_conversion_keep_existing_policy():
    row = {"appid": 1, "name": "English", "name_zh_tw_traditional": "舊名稱"}
    updated = rules.update_title(row, 7, False, convert=lambda value: None)
    assert updated["name_zh_tw"] == 7
    assert updated["display_name"] == "舊名稱"
    assert "name_zh_cn" not in updated
    converted = rules.update_title(row, "新中文", "新简中", convert=lambda value: "")
    assert converted["display_name"] == "English"
    assert converted["display_name_source"] == "english"


def test_converter_failure_does_not_mutate_original_row():
    row = {"appid": 1, "name": "English", "name_zh_tw": "舊中文"}
    def fail(value):
        raise ValueError("broken conversion")
    with pytest.raises(ValueError, match="broken conversion"):
        rules.update_title(row, "新中文", None, convert=fail)
    assert row == {"appid": 1, "name": "English", "name_zh_tw": "舊中文"}


def test_preserve_accepted_twitch_fields_exactly_and_remove_missing_facts():
    old = {"appid": 1, "followers": 800, "release_start": "2026-09-20",
           "sexual_content_screened": False, "release_date_conflict": None}
    merged = {field: "stale" for field in rules.TWITCH_PUBLISHED_FIELDS}
    merged.update({"unknown": [1], "release_raw": "Unrelated original", "name": "中文"})
    assert rules.preserve_published_twitch_fields(old, merged) is None
    for field in rules.TWITCH_PUBLISHED_FIELDS:
        assert (field in merged) == (field in old)
        if field in old:
            assert merged[field] == old[field]
    assert merged["release_raw"] == "Unrelated original"
    assert merged["unknown"] == [1]
    assert merged["name"] == "中文"


def test_json_state_format_exact_comparison_and_parent_creation(tmp_path):
    path = tmp_path / "new" / "nested" / "game.json"
    value = {"中文": "名称", "unknown": [1, None, False]}
    assert not state.exists(path)
    assert state.save_changed(path, value) is True
    assert state.exists(path)
    assert path.read_bytes() == (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    assert state.read(path) == value
    stamp = path.stat().st_mtime_ns
    assert state.save_changed(path, value) is False
    assert path.stat().st_mtime_ns == stamp
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    assert state.save_changed(path, value) is True


@pytest.mark.parametrize("value", [[], None, "中文", 42, True])
def test_read_rejects_nonobject_json_without_empty_fallback(tmp_path, value):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match=f"Expected JSON object: {path}"):
        state.read(path)


def test_read_missing_and_corrupt_json_propagate(tmp_path):
    path = tmp_path / "bad.json"
    with pytest.raises(FileNotFoundError):
        state.read(path)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        state.read(path)
    assert path.read_text(encoding="utf-8") == "{broken"


def game(appid, *, followers=5000, release="2026-09-20", **extra):
    return {"appid": appid, "name": f"English {appid}", "followers": followers,
            "release_start": release, **extra}


class MemoryRefresh:
    def __init__(self, rows, *, months=None, details=None):
        self.root = Path("published")
        self.events = []
        self.rows = deepcopy(rows)
        self.months = deepcopy(months or {"2026-09": rows})
        self.files = {self.root / "index.json": {"version": 2, "months": list(self.months), "extra": "kept"}}
        for month, entries in self.months.items():
            self.files[self.root / "calendar" / f"{month}.json"] = {
                "version": 2, "month": month, "count": len(entries), "games": deepcopy(entries), "extra": "kept",
            }
        for appid, value in (details or {}).items():
            self.files[self.root / "games" / f"{appid}.json"] = deepcopy(value)
        self.session = object()
        self.clock_calls = 0
        self.replies = {
            "tchinese": {int(row["appid"]): f"中文{row['appid']}" for row in rows},
            "schinese": {int(row["appid"]): f"简中{row['appid']}" for row in rows},
        }

    def read(self, path):
        self.events.append(("read", str(path)))
        return deepcopy(self.files[path])

    def save(self, path, value):
        self.events.append(("save", str(path), deepcopy(value)))
        changed = self.files.get(path) != value
        self.files[path] = deepcopy(value)
        return changed

    def exists(self, path):
        self.events.append(("exists", str(path)))
        return path in self.files

    def lookup(self, session, appids, language, interval):
        assert session is self.session
        self.events.append(("lookup", list(appids), language, interval))
        return {appid: self.replies[language][appid] for appid in appids if appid in self.replies[language]}

    def select(self, value, english):
        self.events.append(("candidate", value, english))
        return rules.candidate(value, english)

    def update(self, row, tw, cn):
        self.events.append(("update", row["appid"], tw, cn))
        return rules.update_title(row, tw, cn, convert=lambda name: name.replace("简", "繁"))

    def qualifies(self, row):
        self.events.append(("qualified", row["appid"]))
        return bool(row.get("verified_twitch"))

    def preserve(self, old, full):
        self.events.append(("preserve", old["appid"]))
        out = dict(full)
        if old.get("verified_twitch"):
            out["twitch_admission"] = old["twitch_admission"]
        return out

    def projection(self, root, rows, at):
        assert root == self.root
        self.events.append(("projection", deepcopy(rows), at))
        return {"catalog_path": "catalog.json", "catalog_revision": "fixed"}

    def sleep(self, seconds):
        self.events.append(("sleep", seconds))

    def clock(self):
        self.clock_calls += 1
        self.events.append(("clock", self.clock_calls))
        return datetime(2026, 10, 9, 1, 0, self.clock_calls, tzinfo=timezone.utc)

    def info(self, message, *args):
        self.events.append(("info", message, *args))

    def run(self, **overrides):
        ports = dict(session=self.session, batch_size=30, interval=2.0,
                     read=self.read, save_changed=self.save, exists=self.exists,
                     localized_batch=self.lookup, candidate=self.select, update_title=self.update,
                     is_twitch_qualified=self.qualifies, preserve_twitch_admission=self.preserve,
                     write_catalog_projection=self.projection, sleep=self.sleep,
                     clock=self.clock, logger=self)
        ports.update(overrides)
        return application.refresh(self.root, **ports)


def test_refresh_fetches_every_language_before_local_writes_and_preserves_order():
    harness = MemoryRefresh([game(9), game(2), game(7, followers=100)])
    result = harness.run(batch_size=1)
    lookups = [event for event in harness.events if event[0] == "lookup"]
    assert lookups == [
        ("lookup", [2], "tchinese", 2.0), ("lookup", [2], "schinese", 2.0),
        ("lookup", [9], "tchinese", 2.0), ("lookup", [9], "schinese", 2.0),
    ]
    assert [event for event in harness.events if event[0] == "sleep"] == [("sleep", 2.0)] * 4
    first_update = next(i for i, event in enumerate(harness.events) if event[0] == "update")
    assert all(event[0] not in {"read", "exists", "save"} for event in harness.events[2:first_update]
               if event[0] not in {"qualified"})
    game_saves = [event for event in harness.events if event[0] == "save" and "/games/" in event[1]]
    assert [event[1] for event in game_saves] == ["published/games/9.json", "published/games/2.json"]
    assert result == {
        "published_qualified": 2, "store_tw_titles": 2, "store_cn_titles": 2,
        "changed_games": 2, "changed_months": ["2026-09"],
        "at": "2026-10-09T01:00:02+00:00", "changed_appids": [2, 9],
    }
    assert harness.files[harness.root / "calendar" / "2026-09.json"]["games"][-1] == game(7, followers=100)
    aggregate = harness.files[harness.root / "steam_upcoming.json"]
    assert [row["appid"] for row in aggregate["games"]] == [2, 9, 7]
    assert aggregate["generated_at"] == "2026-10-09T01:00:01+00:00"
    assert harness.files[harness.root / "index.json"]["extra"] == "kept"
    assert harness.events[-1][0:2] == ("info", "CHINESE_TITLE_REFRESH %s")
    assert json.loads(harness.events[-1][2]) == result


def test_success_logging_precedes_interval_wait_even_after_final_language():
    harness = MemoryRefresh([game(1)])
    harness.run()
    lookup_events = [event for event in harness.events if event[0] in {"lookup", "info", "sleep"}]
    assert lookup_events[:6] == [
        ("lookup", [1], "tchinese", 2.0),
        ("info", "LOCALIZED_LOOKUP language=%s batch=%d/%d returned=%d", "tchinese", 1, 1, 1),
        ("sleep", 2.0),
        ("lookup", [1], "schinese", 2.0),
        ("info", "LOCALIZED_LOOKUP language=%s batch=%d/%d returned=%d", "schinese", 1, 1, 1),
        ("sleep", 2.0),
    ]


def test_refresh_preserves_richer_details_and_exact_accepted_twitch_facts():
    proof = {"schema_version": 1, "appid": 2}
    old = game(2, followers=800, verified_twitch=True, twitch_admission=proof,
               sexual_content_screened=True, release_time_utc="2026-09-19T17:00:00Z")
    full = {**old, "followers": 9000, "release_start": "2026-09-21", "release_date_conflict": True,
            "short_description": "已有介紹", "unknown": [1]}
    full.pop("twitch_admission")
    harness = MemoryRefresh([old, game(3, followers=900)], details={2: full})
    stats = harness.run(interval=0)
    assert stats["changed_appids"] == [2]
    merged = harness.files[harness.root / "games" / "2.json"]
    assert merged["followers"] == 800
    assert merged["release_start"] == "2026-09-20"
    assert merged["release_time_utc"] == old["release_time_utc"]
    assert merged["twitch_admission"] == proof
    assert "release_date_conflict" not in merged
    assert merged["short_description"] == "已有介紹"
    assert merged["unknown"] == [1]
    assert [event for event in harness.events if event[0] == "qualified"] == [
        ("qualified", 2), ("qualified", 3), ("qualified", 2),
    ]
    assert not any(event[0] == "sleep" for event in harness.events)


def test_unchanged_refresh_still_projects_and_saves_aggregates_with_two_clocks():
    old = rules.update_title(game(1), None, None, convert=lambda name: name)
    harness = MemoryRefresh([old], details={1: old})
    harness.replies = {"tchinese": {1: old["name"]}, "schinese": {1: ""}}
    stats = harness.run(interval=0)
    assert stats["changed_games"] == stats["store_tw_titles"] == stats["store_cn_titles"] == 0
    assert stats["changed_months"] == []
    assert [event[1] for event in harness.events if event[0] == "save"] == [
        "published/steam_upcoming.json", "published/index.json",
    ]
    assert sum(event[0] == "projection" for event in harness.events) == 1
    assert harness.clock_calls == 2


def test_changed_detection_uses_old_calendar_even_when_detail_already_current():
    old = game(1)
    current = rules.update_title(old, "中文1", "简中1", convert=lambda name: name.replace("简", "繁"))
    harness = MemoryRefresh([old], details={1: current})
    stats = harness.run(interval=0)
    assert stats["changed_appids"] == [1]
    assert stats["changed_months"] == ["2026-09"]
    assert harness.files[harness.root / "calendar" / "2026-09.json"]["games"] == [current]


def test_only_affected_months_saved_and_richer_rows_reused_in_all_projections():
    a, b = game(1), game(2, followers=100, release="2026-10-01")
    full = {**a, "short_description": "richer"}
    harness = MemoryRefresh([a, b], months={"2026-10": [b], "2026-09": [a]}, details={1: full})
    stats = harness.run(interval=0)
    assert stats["changed_months"] == ["2026-09"]
    calendar_saves = [event[1] for event in harness.events if event[0] == "save" and "/calendar/" in event[1]]
    assert calendar_saves == ["published/calendar/2026-09.json"]
    projected = next(event[1] for event in harness.events if event[0] == "projection")
    assert projected[0]["short_description"] == "richer"
    assert projected[1] == b


@pytest.mark.parametrize("index,month_doc,error,match", [
    ({"version": 1, "months": []}, None, RuntimeError, "Sharded frontend index"),
    ({"version": 2, "months": {}}, None, RuntimeError, "Sharded frontend index"),
    ({"version": 2, "months": ["2026/09"]}, None, RuntimeError, "Bad month in index"),
    ({"version": 2, "months": ["2026-09"]}, {"games": {}}, RuntimeError, "Bad calendar shard"),
    ({"version": 2, "months": ["2026-09"]}, {"games": [game(1), game("1")]}, RuntimeError, "Duplicate AppID"),
    ({"version": 2, "months": ["2026-09"]}, {"games": [game(1, followers=4999)]}, RuntimeError, "No officially qualified"),
    ({"version": 2, "months": ["2026-09"]}, {"games": [game(1, followers="bad")]}, ValueError, "invalid literal"),
])
def test_invalid_inputs_stop_before_requests_or_writes(index, month_doc, error, match):
    harness = MemoryRefresh([game(1)])
    harness.files[harness.root / "index.json"] = index
    if month_doc is not None:
        harness.files[harness.root / "calendar" / "2026-09.json"] = month_doc
    with pytest.raises(error, match=match):
        harness.run()
    assert not any(event[0] in {"lookup", "save", "projection", "clock"} for event in harness.events)


def test_later_calendar_validation_still_precedes_requests():
    harness = MemoryRefresh([game(1)], months={"2026-09": [game(1)], "2026-10": [game(2)]})
    harness.files[harness.root / "calendar" / "2026-10.json"]["games"] = None
    with pytest.raises(RuntimeError, match="Bad calendar shard"):
        harness.run()
    assert not any(event[0] == "lookup" for event in harness.events)


def test_direct_refresh_keeps_original_batch_size_error_and_no_new_limit():
    harness = MemoryRefresh([game(1)])
    with pytest.raises(ZeroDivisionError):
        harness.run(batch_size=0)
    assert not any(event[0] == "lookup" for event in harness.events)
    harness.events.clear()
    assert harness.run(batch_size=100, interval=0)["published_qualified"] == 1


def test_lookup_failure_keeps_all_local_files_untouched():
    harness = MemoryRefresh([game(1), game(2)])
    before = deepcopy(harness.files)
    def fail(session, ids, language, interval):
        harness.events.append(("lookup", ids, language, interval))
        if language == "schinese":
            raise RuntimeError("Failed Steam schinese Store lookup")
        return {1: "中文"}
    with pytest.raises(RuntimeError, match="Failed Steam schinese"):
        harness.run(localized_batch=fail)
    assert harness.files == before
    assert not any(event[0] in {"exists", "save", "projection", "clock"} for event in harness.events)


def test_detail_read_failure_preserves_earlier_local_write_without_aggregate_write():
    harness = MemoryRefresh([game(1), game(2)], details={2: game(2)})
    def read(path):
        if path == harness.root / "games" / "2.json":
            raise ValueError("corrupt detail")
        return harness.read(path)
    with pytest.raises(ValueError, match="corrupt detail"):
        harness.run(read=read, interval=0)
    assert [event[1] for event in harness.events if event[0] == "save"] == ["published/games/1.json"]
    assert not any(event[0] == "projection" for event in harness.events)


def test_month_count_assertion_occurs_after_details_and_before_calendar_save():
    harness = MemoryRefresh([game(1)])
    harness.files[harness.root / "calendar" / "2026-09.json"]["count"] = 2
    with pytest.raises(AssertionError):
        harness.run(interval=0)
    assert [event[1] for event in harness.events if event[0] == "save"] == ["published/games/1.json"]
    assert harness.clock_calls == 0


def test_projection_failure_preserves_detail_and_month_writes_only():
    harness = MemoryRefresh([game(1)])
    def fail(root, rows, at):
        raise OSError("projection failed")
    with pytest.raises(OSError, match="projection failed"):
        harness.run(write_catalog_projection=fail, interval=0)
    assert [event[1] for event in harness.events if event[0] == "save"] == [
        "published/games/1.json", "published/calendar/2026-09.json",
    ]
    assert harness.clock_calls == 1


def test_second_clock_failure_occurs_after_index_save_and_before_final_stats_log():
    harness = MemoryRefresh([game(1)])
    def clock():
        if harness.clock_calls:
            raise RuntimeError("second clock failed")
        return harness.clock()
    with pytest.raises(RuntimeError, match="second clock failed"):
        harness.run(clock=clock, interval=0)
    assert [event[1] for event in harness.events if event[0] == "save"][-1] == "published/index.json"
    assert not any(event[0:2] == ("info", "CHINESE_TITLE_REFRESH %s") for event in harness.events)


def test_save_failure_stops_later_updates_and_is_not_silenced():
    harness = MemoryRefresh([game(1), game(2)])
    def fail(path, value):
        raise PermissionError("read only")
    with pytest.raises(PermissionError, match="read only"):
        harness.run(save_changed=fail, interval=0)
    assert not any(event[0] == "update" and event[1] == 2 for event in harness.events)
    assert not any(event[0] == "projection" for event in harness.events)


def test_missing_sort_field_errors_after_original_detail_and_month_writes():
    row = game(1)
    row.pop("release_start")
    harness = MemoryRefresh([row])
    with pytest.raises(KeyError, match="release_start"):
        harness.run(interval=0)
    assert [event[1] for event in harness.events if event[0] == "save"] == [
        "published/games/1.json", "published/calendar/2026-09.json",
    ]
    assert harness.clock_calls == 0
