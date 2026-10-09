"""Publication use case contracts with an observable in-memory filesystem."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone, tzinfo
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from radar_backend.application.public_shards import build
from radar_backend.domain.public_shards import valid_record


NOW = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
SOURCE = Path("/publication-source.json")
FRONTEND = Path("/publication-frontend")
DATA = FRONTEND / "data"


def game(appid=1, day="2026-10-10", followers=5000, **extra):
    return {
        "appid": appid,
        "release_start": day,
        "release_precision": "day",
        "release_display_precision": "date_full",
        "followers": followers,
        "name": f"Game {appid}",
        "storage_version": 2,
        **extra,
    }


class MemoryPublication:
    def __init__(self, payload=None, *, files=None, blocked=(), ordered_games=None):
        self.files = deepcopy(files or {})
        self.files[SOURCE] = deepcopy({"games": []} if payload is None else payload)
        self.events = []
        self.blocked = set(blocked)
        self.ordered_games = ordered_games
        self.failure = None
        self.projection = {"catalog_revision": "test-revision", "catalog_path": "catalog.json"}
        self.changed = None

    def record(self, *event):
        self.events.append(event)
        if self.failure and self.failure[:2] == event[:2]:
            raise self.failure[2]

    def load(self, path, default):
        self.record("load", path)
        return deepcopy(self.files.get(path, default))

    def write(self, path, payload):
        self.record("write", path)
        changed = self.files.get(path) != payload if self.changed is None else self.changed(path, payload)
        self.files[path] = deepcopy(payload)
        return changed

    def exists(self, path):
        self.record("exists", path)
        return any(file.parent == path for file in self.files)

    def glob(self, path, pattern):
        self.record("glob", path, pattern)
        if path == DATA / "games" and self.ordered_games is not None:
            return list(self.ordered_games)
        return [file for file in self.files if file.parent == path and fnmatchcase(file.name, pattern)]

    def unlink(self, path):
        self.record("unlink", path)
        del self.files[path]

    def exclusions(self):
        self.record("exclusions", None)
        return self.blocked

    def disallowed(self, row, blocked):
        return int(row["appid"]) in blocked or row.get("adult") is True

    @staticmethod
    def twitch(row):
        return row.get("twitch") is True

    def valid(self, row):
        return valid_record(row, is_twitch_qualified=self.twitch)

    def merge(self, prior, incoming):
        self.record("merge", int(incoming["appid"]))
        return {**prior, **incoming, "appid": int(incoming["appid"])}

    def display(self, row):
        self.record("display", int(row["appid"]))
        if row.get("name_zh_tw"):
            row["name_zh_tw_traditional"] = row["name_zh_tw"].replace("国", "國")

    def project(self, directory, rows, generated_at):
        self.record("projection", directory)
        self.projected = (directory, deepcopy(rows), generated_at)
        return self.projection

    def clock(self):
        self.record("clock", None)
        return NOW

    def publish(self, **kwargs):
        ports = {
            "clock": self.clock,
            "load_json": self.load,
            "write_if_changed": self.write,
            "exists": self.exists,
            "glob": self.glob,
            "unlink": self.unlink,
            "excluded_appids": self.exclusions,
            "is_disallowed": self.disallowed,
            "is_twitch_qualified": self.twitch,
            "valid_record": self.valid,
            "merge_game": self.merge,
            "add_traditional_display_names": self.display,
            "write_catalog_projection": self.project,
            **kwargs,
        }
        return build(SOURCE, FRONTEND, **ports)


def test_full_publication_order_paths_payloads_and_stats():
    state = MemoryPublication({"generated_at": "source-time", "games": [game(7)]})
    stats = state.publish()
    assert state.events == [
        ("clock", None),
        ("load", SOURCE),
        ("exclusions", None),
        ("load", DATA / "index.json"),
        ("load", DATA / "excluded_date_appids.json"),
        ("exists", DATA / "games"),
        ("glob", DATA / "games", "*.json"),
        ("merge", 7),
        ("write", DATA / "games/7.json"),
        ("display", 7),
        ("write", DATA / "calendar/2026-10.json"),
        ("exists", DATA / "calendar"),
        ("glob", DATA / "calendar", "????-??.json"),
        ("write", DATA / "lists/upcoming.json"),
        ("write", DATA / "lists/released.json"),
        ("write", DATA / "steam_upcoming.json"),
        ("projection", DATA),
        ("write", DATA / "index.json"),
    ]
    assert stats == {
        "games": 1, "incoming": 1, "changed_games": 1, "months": 1,
        "changed_months": 1, "upcoming": 1, "released": 0, "removed_stale_future": 0,
    }
    assert state.files[DATA / "calendar/2026-10.json"] == {
        "version": 2, "generated_at": "source-time", "month": "2026-10", "count": 1,
        "games": [game(7)],
    }
    assert state.files[DATA / "lists/upcoming.json"]["appids"] == [7]
    assert state.files[DATA / "lists/released.json"]["appids"] == []
    assert state.files[DATA / "steam_upcoming.json"]["games"] == state.projected[1]
    index = state.files[DATA / "index.json"]
    assert index == {
        "version": 2, "generated_at": "source-time",
        "source": "Steam AppID-sharded public catalog", "game_count": 1,
        "months": ["2026-10"], "calendar_path": "calendar/{YYYY-MM}.json",
        "game_path": "games/{appid}.json",
        "lists": {"upcoming": "lists/upcoming.json", "released": "lists/released.json"},
        "legacy_fallback": "steam_upcoming.json", "release_date_audited": False,
        "catalog_revision": "test-revision", "catalog_path": "catalog.json",
    }


@pytest.mark.parametrize("payload", [[], "broken", 7, {}, {"games": None}, {"games": {}}])
def test_invalid_source_stops_before_exclusions_or_any_state_change(payload):
    state = MemoryPublication(payload, files={DATA / "games/99.json": game(99)})
    with pytest.raises(ValueError, match="Invalid source catalog"):
        state.publish()
    assert state.events == [("clock", None), ("load", SOURCE)]
    assert DATA / "games/99.json" in state.files


def test_empty_authoritative_source_stops_before_deletion():
    state = MemoryPublication(files={DATA / "games/99.json": game(99)})
    with pytest.raises(ValueError, match="Empty authoritative catalog"):
        state.publish(authoritative_future=True)
    assert state.events == [("clock", None), ("load", SOURCE)]
    assert DATA / "games/99.json" in state.files


class UnknownOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize("now", [datetime(2026, 10, 9), datetime(2026, 10, 9, tzinfo=UnknownOffset())])
def test_invalid_observation_fails_before_reading_source(now):
    state = MemoryPublication({"games": [game()]})
    with pytest.raises(ValueError, match="Timezone-aware"):
        state.publish(now=now)
    assert state.events == []


@pytest.mark.parametrize("generated_at", [None, "", 0, False])
def test_falsey_generated_at_uses_one_observation(generated_at):
    state = MemoryPublication({"generated_at": generated_at, "games": [game()]})
    state.publish()
    assert state.files[DATA / "index.json"]["generated_at"] == NOW.isoformat()
    assert state.events.count(("clock", None)) == 1
    for path, payload in state.files.items():
        if path != SOURCE and isinstance(payload, dict) and "generated_at" in payload:
            assert payload["generated_at"] == NOW.isoformat()


def test_explicit_observation_skips_clock_and_uses_taiwan_midnight():
    state = MemoryPublication({"games": [game(1, "2026-10-09"), game(2, "2026-10-10")]})
    state.publish(now=NOW)
    assert ("clock", None) not in state.events
    assert state.files[DATA / "lists/released.json"]["appids"] == [1]
    assert state.files[DATA / "lists/upcoming.json"]["appids"] == [2]


@pytest.mark.parametrize("bad_id,error", [("bad", ValueError), ([], TypeError), (float("inf"), OverflowError)])
def test_incoming_id_coercion_fails_before_loading_public_state(bad_id, error):
    state = MemoryPublication({"games": [{"appid": bad_id}]})
    with pytest.raises(error):
        state.publish()
    assert state.events == [("clock", None), ("load", SOURCE)]


@pytest.mark.parametrize("index", [[], 3, None])
def test_invalid_index_keeps_existing_attribute_error_contract(index):
    state = MemoryPublication(files={DATA / "index.json": index})
    with pytest.raises(AttributeError):
        state.publish()
    assert state.events[-1] == ("load", DATA / "index.json")


@pytest.mark.parametrize("exclusions,error", [([], AttributeError), ({"appids": ["bad"]}, ValueError), ({"appids": None}, TypeError)])
def test_invalid_precision_exclusions_fails_before_glob(exclusions, error):
    state = MemoryPublication(files={DATA / "excluded_date_appids.json": exclusions})
    with pytest.raises(error):
        state.publish()
    assert not any(event[0] == "glob" for event in state.events)


@pytest.mark.parametrize(
    "audit,authoritative,precision,published",
    [(False, False, None, True), (True, False, None, False),
     (False, True, None, False), (True, False, "date_full", True),
     (1, False, None, True), ("yes", False, None, True)],
)
def test_audit_literal_true_and_authoritative_date_gate(audit, authoritative, precision, published):
    row = game(release_display_precision=precision)
    state = MemoryPublication({"games": [row]}, files={DATA / "index.json": {"release_date_audited": audit}})
    result = state.publish(authoritative_future=authoritative)
    assert result["games"] == int(published)
    assert state.files[DATA / "index.json"]["release_date_audited"] is (audit is True or authoritative)


@pytest.mark.parametrize(
    "day,followers,twitch,recent_source,visible,list_name",
    [("2026-10-10", 4999, False, None, False, None),
     ("2026-10-10", 5000, False, None, True, "upcoming"),
     ("2026-10-10", 100, True, None, True, "upcoming"),
     ("2026-10-09", 3000, False, "tracked_release", True, None),
     ("2026-10-09", 3001, False, "tracked_release", True, "released"),
     ("2026-10-09", 3001, False, "direct_release", True, "released"),
     ("2026-10-09", 3001, False, "other", True, None),
     ("2026-10-09", 100, True, None, True, "released"),
     ("2026-09-10", 5000, False, None, True, "released"),
     ("2026-09-09", 5000, False, None, True, None)],
)
def test_threshold_and_released_window_interactions(day, followers, twitch, recent_source, visible, list_name):
    state = MemoryPublication({"games": [game(day=day, followers=followers, twitch=twitch, recent_source=recent_source)]})
    stats = state.publish()
    assert stats["games"] == int(visible)
    assert state.files[DATA / "lists/upcoming.json"]["appids"] == ([1] if list_name == "upcoming" else [])
    assert state.files[DATA / "lists/released.json"]["appids"] == ([1] if list_name == "released" else [])


def test_blocked_and_precision_excluded_files_removed_but_full_date_retained():
    files = {
        DATA / "games/1.json": game(1),
        DATA / "games/2.json": game(2, release_display_precision="month"),
        DATA / "games/3.json": game(3),
        DATA / "excluded_date_appids.json": {"appids": [2, "3"]},
    }
    state = MemoryPublication({"games": [game(1), game(2), game(3)]}, files=files, blocked={1})
    result = state.publish()
    assert result["games"] == 2  # Incoming full-date proof can restore AppID 2 after stale deletion.
    assert result["removed_stale_future"] == 0
    assert [(event[0], event[1]) for event in state.events if event[0] == "unlink"] == [
        ("unlink", DATA / "games/1.json"), ("unlink", DATA / "games/2.json"),
    ]
    assert DATA / "games/1.json" not in state.files
    assert state.files[DATA / "lists/upcoming.json"]["appids"] == [2, 3]


@pytest.mark.parametrize("authoritative", [False, True])
def test_authoritative_removal_preserves_history_twitch_and_current_ids(authoritative):
    files = {
        DATA / "games/1.json": game(1, "2026-10-09"),
        DATA / "games/2.json": game(2),
        DATA / "games/3.json": game(3, twitch=True, followers=10),
        DATA / "games/4.json": game(4),
    }
    state = MemoryPublication({"games": [game(4)]}, files=files)
    stats = state.publish(authoritative_future=authoritative)
    assert stats["removed_stale_future"] == int(authoritative)
    assert (DATA / "games/2.json" in state.files) is (not authoritative)
    assert DATA / "games/1.json" in state.files
    assert DATA / "games/3.json" in state.files
    assert DATA / "games/4.json" in state.files


def test_invalid_incoming_record_still_prevents_absent_id_removal():
    state = MemoryPublication({"games": [{"appid": 5, "followers": 0}]}, files={DATA / "games/5.json": game(5)})
    result = state.publish(authoritative_future=True)
    assert result["games"] == 1
    assert result["incoming"] == 1
    assert result["removed_stale_future"] == 0


def test_nonnumeric_game_filename_is_not_loaded_in_removal_pass():
    extra = DATA / "games/note.json"
    state = MemoryPublication(files={extra: "note"})
    state.publish()
    assert state.events.count(("load", extra)) == 1  # Existing scan only.
    assert extra in state.files


def test_duplicate_existing_appid_uses_glob_order_not_filename_sorting():
    first = DATA / "games/20.json"
    second = DATA / "games/10.json"
    state = MemoryPublication(files={first: game(9, name="first"), second: game(9, name="second")}, ordered_games=[first, second])
    result = state.publish()
    assert result["games"] == 1
    assert state.files[DATA / "steam_upcoming.json"]["games"][0]["name"] == "second"
    assert [event[1] for event in state.events if event[0] == "load" and event[1].parent == DATA / "games"] == [first, second, first, second]


def test_preserved_verified_date_keeps_original_month_and_audit_evidence():
    prior = game(8, "2026-12-20", release_date_verified_at="verified", release_raw="Dec 20")
    incoming = game(8, "2026-11-15", release_display_precision="month", followers=7000)
    state = MemoryPublication({"games": [incoming]}, files={DATA / "games/8.json": prior})
    state.publish()
    published = state.files[DATA / "games/8.json"]
    assert published["followers"] == 7000
    assert published["release_start"] == published["release_end"] == "2026-12-20"
    assert published["release_raw"] == "Dec 20"
    assert published["release_display_precision"] == "date_full"
    assert published["release_date_verified_at"] == "verified"
    assert state.files[DATA / "index.json"]["months"] == ["2026-12"]


def test_backfill_updates_omitted_records_before_calendar_and_keeps_raw_name():
    prior = game(6, "2026-10-01", name_zh_tw="中国")
    del prior["storage_version"]
    state = MemoryPublication(files={DATA / "games/6.json": prior})
    result = state.publish()
    assert result["changed_games"] == 1
    stored = state.files[DATA / "games/6.json"]
    assert stored["name_zh_tw"] == "中国"
    assert stored["name_zh_tw_traditional"] == "中國"
    assert stored["storage_version"] == 2
    assert state.events.index(("write", DATA / "games/6.json")) < state.events.index(("write", DATA / "calendar/2026-10.json"))


def test_backfill_can_add_second_changed_game_after_incoming_write():
    state = MemoryPublication({"games": [game(6, name_zh_tw="中国")]})
    result = state.publish()
    assert result["changed_games"] == 2
    assert state.events.count(("write", DATA / "games/6.json")) == 2


def test_sorting_month_publication_and_obsolete_month_deletion_order():
    state = MemoryPublication(
        {"games": [game(4, "2026-12-10"), game(3, "2026-11-01", 5000),
                   game(2, "2026-11-01", 9000), game(1, "2026-11-01", 5000)]},
        files={DATA / "calendar/2026-09.json": {}, DATA / "calendar/notes.json": {}},
    )
    result = state.publish()
    assert [row["appid"] for row in state.projected[1]] == [2, 1, 3, 4]
    assert result["months"] == 2
    calendar_events = [event for event in state.events if event[0] in {"write", "unlink"} and event[1].parent == DATA / "calendar"]
    assert calendar_events == [
        ("write", DATA / "calendar/2026-11.json"),
        ("write", DATA / "calendar/2026-12.json"),
        ("unlink", DATA / "calendar/2026-09.json"),
    ]
    assert DATA / "calendar/notes.json" in state.files
    assert state.files[DATA / "index.json"]["months"] == ["2026-11", "2026-12"]


def test_change_counters_use_writer_result_only_for_games_and_months():
    state = MemoryPublication({"games": [game(1), game(2, "2026-11-01")]})
    state.changed = lambda path, payload: path.name in {"2.json", "2026-10.json"}
    result = state.publish()
    assert result["changed_games"] == 1
    assert result["changed_months"] == 1
    assert result["upcoming"] == 2


def test_empty_nonauthoritative_source_replaces_fallback_and_removes_obsolete_months():
    state = MemoryPublication(files={DATA / "calendar/2026-10.json": {"games": [game()]}, DATA / "steam_upcoming.json": {"games": [game()]}})
    result = state.publish()
    assert result == {"games": 0, "incoming": 0, "changed_games": 0, "months": 0, "changed_months": 0, "upcoming": 0, "released": 0, "removed_stale_future": 0}
    assert DATA / "calendar/2026-10.json" not in state.files
    assert state.files[DATA / "steam_upcoming.json"]["games"] == []
    assert state.files[DATA / "index.json"]["game_count"] == 0


def test_projection_fields_override_index_defaults_and_follow_fallback_write():
    state = MemoryPublication({"games": [game()]})
    state.projection = {"game_count": 42, "generated_at": "projection-time", "catalog_revision": "revision"}
    state.publish()
    assert state.files[DATA / "index.json"]["game_count"] == 42
    assert state.files[DATA / "index.json"]["generated_at"] == "projection-time"
    assert state.events.index(("write", DATA / "steam_upcoming.json")) < state.events.index(("projection", DATA)) < state.events.index(("write", DATA / "index.json"))


@pytest.mark.parametrize("error", [ValueError("skip unlink"), OSError("fatal unlink"), TypeError("fatal unlink")])
def test_removal_pass_catches_only_value_error_and_keeps_mutation_order(error):
    stale = DATA / "games/99.json"
    state = MemoryPublication({"games": [game()]}, files={stale: game(99)})
    state.failure = ("unlink", stale, error)
    if isinstance(error, ValueError):
        result = state.publish(authoritative_future=True)
        assert result["games"] == 1  # Existing map removal precedes failing unlink.
        assert result["removed_stale_future"] == 0
        assert stale in state.files
    else:
        with pytest.raises(type(error), match="fatal unlink"):
            state.publish(authoritative_future=True)
        assert not any(event[0] == "write" for event in state.events)


@pytest.mark.parametrize("failure_path", [DATA / "games/1.json", DATA / "calendar/2026-10.json", DATA / "lists/upcoming.json", DATA / "lists/released.json", DATA / "steam_upcoming.json", DATA / "index.json"])
def test_write_failure_stops_at_original_partial_publication_boundary(failure_path):
    state = MemoryPublication({"games": [game()]})
    state.failure = ("write", failure_path, OSError("write failed"))
    with pytest.raises(OSError, match="write failed"):
        state.publish()
    assert state.events[-1] == ("write", failure_path)
    assert failure_path not in state.files
    if failure_path != DATA / "games/1.json":
        assert DATA / "games/1.json" in state.files


def test_obsolete_month_failure_occurs_after_new_month_write_before_lists():
    stale = DATA / "calendar/2026-09.json"
    state = MemoryPublication({"games": [game()]}, files={stale: {}})
    state.failure = ("unlink", stale, ValueError("month deletion failed"))
    with pytest.raises(ValueError, match="month deletion failed"):
        state.publish()
    assert DATA / "calendar/2026-10.json" in state.files
    assert DATA / "lists/upcoming.json" not in state.files


def test_projection_failure_preserves_fallback_and_leaves_old_index():
    prior = {"old": "index"}
    state = MemoryPublication({"games": [game()]}, files={DATA / "index.json": prior})
    state.failure = ("projection", DATA, RuntimeError("projection failed"))
    with pytest.raises(RuntimeError, match="projection failed"):
        state.publish()
    assert state.files[DATA / "steam_upcoming.json"]["games"] == [game()]
    assert state.files[DATA / "index.json"] == prior
    assert state.events[-1] == ("projection", DATA)


def test_injected_timezone_and_timedelta_are_observed_at_original_two_conversions():
    state = MemoryPublication({"games": [game()]})
    calls = []

    def duration(**kwargs):
        calls.append(("duration", kwargs))
        return timedelta(**kwargs)

    def zone(delta):
        calls.append(("zone", delta))
        return timezone(delta)

    state.publish(timezone_type=zone, timedelta_type=duration)
    assert calls == [
        ("duration", {"hours": 8}), ("zone", timedelta(hours=8)),
        ("duration", {"hours": 8}), ("zone", timedelta(hours=8)),
        ("duration", {"days": 30}),
    ]


def test_filesystem_methods_are_never_used_by_application(monkeypatch):
    state = MemoryPublication({"games": [game()]})

    def forbidden(*args, **kwargs):
        raise AssertionError("Application touched filesystem directly")

    for method in ("exists", "glob", "unlink", "read_text", "write_text", "mkdir"):
        monkeypatch.setattr(Path, method, forbidden)
    assert state.publish()["games"] == 1


def test_filtered_existing_game_is_omitted_from_catalog_without_unrequested_file_deletion():
    low_followers = DATA / "games/19.json"
    state = MemoryPublication(files={low_followers: game(19, followers=4000)})
    result = state.publish()
    assert result["games"] == 0
    assert low_followers in state.files
    assert not any(event[0] == "unlink" for event in state.events)
    assert state.files[DATA / "steam_upcoming.json"]["games"] == []


def test_nonempty_authoritative_input_with_invalid_rows_can_remove_absent_future():
    stale = DATA / "games/19.json"
    state = MemoryPublication({"games": [None]}, files={stale: game(19)})
    result = state.publish(authoritative_future=True)
    assert result["games"] == 0
    assert result["incoming"] == 1
    assert result["removed_stale_future"] == 1
    assert stale not in state.files


def test_only_game_stem_parse_value_errors_are_skipped_without_hiding_row_attribute_errors():
    malformed = DATA / "games/19.json"
    state = MemoryPublication({"games": [game()]}, files={malformed: ["invalid"]})
    with pytest.raises(AttributeError):
        state.publish(authoritative_future=True)
    assert state.events[-1] == ("load", malformed)
    assert not any(event[0] == "write" for event in state.events)


def test_stale_removal_counter_includes_game_also_blocked_for_adult_content():
    stale = DATA / "games/19.json"
    state = MemoryPublication({"games": [game()]}, files={stale: game(19)}, blocked={19})
    result = state.publish(authoritative_future=True)
    assert result["removed_stale_future"] == 1
    assert stale not in state.files


def test_existing_scan_reads_only_present_directory_but_removal_always_globs():
    state = MemoryPublication()
    state.publish(exists=lambda path: False)
    assert state.events.count(("glob", DATA / "games", "*.json")) == 1
    assert not any(event == ("glob", DATA / "calendar", "????-??.json") for event in state.events)


def test_merge_and_backfill_errors_propagate_after_their_original_prior_effects():
    state = MemoryPublication({"games": [game()]})
    state.failure = ("display", 1, ValueError("display failed"))
    with pytest.raises(ValueError, match="display failed"):
        state.publish()
    assert state.files[DATA / "games/1.json"] == game()
    assert not any(event[0] == "projection" for event in state.events)
    assert DATA / "calendar/2026-10.json" not in state.files


def test_lazy_glob_failures_stop_scan_before_first_incoming_write():
    one = DATA / "games/19.json"
    state = MemoryPublication({"games": [game()]}, files={one: game(19)})

    def lazy_glob(path, pattern):
        yield one
        raise OSError("enumeration failed")

    with pytest.raises(OSError, match="enumeration failed"):
        state.publish(glob=lazy_glob)
    assert state.events[-1] == ("load", one)
    assert not any(event[0] == "write" for event in state.events)


def test_duplicate_incoming_records_are_merged_and_written_in_input_order():
    state = MemoryPublication({"games": [game(1, name="first"), game(1, name="second")]})
    result = state.publish()
    assert result["incoming"] == 2
    assert result["games"] == 1
    assert result["changed_games"] == 2
    assert state.events.count(("merge", 1)) == 2
    assert state.events.count(("write", DATA / "games/1.json")) == 2
    assert state.files[DATA / "games/1.json"]["name"] == "second"
