"""Filesystem failure and write-order contracts for public shard JSON."""
import copy
import json
from types import SimpleNamespace

import pytest

from radar_backend.state import public_shards as state


class RecordingPath:
    def __init__(self, reads=(), *, mkdir_error=None, write_error=None):
        self.reads = list(reads)
        self.events = []
        self.mkdir_error = mkdir_error
        self.write_error = write_error
        self.parent = SimpleNamespace(mkdir=self.mkdir)

    def read_text(self, **kwargs):
        self.events.append(("read", kwargs))
        result = self.reads.pop(0) if self.reads else FileNotFoundError("missing")
        if isinstance(result, BaseException):
            raise result
        return result

    def mkdir(self, **kwargs):
        self.events.append(("mkdir", kwargs))
        if self.mkdir_error is not None:
            raise self.mkdir_error

    def write_text(self, text, **kwargs):
        self.events.append(("write", text, kwargs))
        if self.write_error is not None:
            raise self.write_error
        return len(text)


@pytest.mark.parametrize("payload", [None, False, 0, 7.5, "中文", [], {"games": []}])
def test_load_json_preserves_json_value_types(payload):
    path = RecordingPath([json.dumps(payload, ensure_ascii=False)])
    assert state.load_json(path, object()) == payload
    assert path.events == [("read", {"encoding": "utf-8"})]


@pytest.mark.parametrize("error", [
    FileNotFoundError("missing"), PermissionError("denied"),
    IsADirectoryError("directory"), OSError("io"),
    ValueError("bad text"), TypeError("bad text type"),
])
def test_load_read_failure_returns_same_default_object(error):
    default = {"keep": []}
    assert state.load_json(RecordingPath([error]), default) is default


@pytest.mark.parametrize("error", [ValueError("json"), TypeError("json"), OSError("json")])
def test_load_json_module_failure_returns_same_default_object(error):
    default = []

    def loads(text):
        assert text == "raw"
        raise error

    assert state.load_json(RecordingPath(["raw"]), default,
                           json_module=SimpleNamespace(loads=loads)) is default


@pytest.mark.parametrize("error", [AttributeError("broken"), RuntimeError("broken")])
def test_load_unhandled_read_error_propagates(error):
    with pytest.raises(type(error), match="broken"):
        state.load_json(RecordingPath([error]), {})


def test_load_missing_json_method_is_not_tolerated():
    with pytest.raises(AttributeError):
        state.load_json(RecordingPath(["{}"]), {}, json_module=object())


def test_load_injected_json_result_preserves_identity():
    returned = {"unusual": object()}
    observed = []
    module = SimpleNamespace(loads=lambda raw: observed.append(raw) or returned)
    assert state.load_json(RecordingPath(["source"]), None, json_module=module) is returned
    assert observed == ["source"]


def test_timestamp_only_change_skips_serialization_and_second_read(tmp_path):
    path = tmp_path / "month.json"
    source = '{"generated_at": "old", "games": [{"name": "中文"}]}\n'
    path.write_text(source, encoding="utf-8")
    payload = {"generated_at": "new", "games": [{"name": "中文"}]}
    original = copy.deepcopy(payload)

    def forbidden_dump(*args, **kwargs):
        raise AssertionError("unchanged timestamp must not serialize")

    assert state.write_if_changed(path, payload,
                                 json_module=SimpleNamespace(loads=json.loads, dumps=forbidden_dump)) is False
    assert path.read_text(encoding="utf-8") == source
    assert payload == original


@pytest.mark.parametrize("timestamp", [None, False, 0, [], {}, "new"])
def test_generated_at_presence_triggers_comparison_regardless_of_value(timestamp):
    path = RecordingPath(['{"count": 1}'])
    assert state.write_if_changed(path, {"generated_at": timestamp, "count": 1}) is False
    assert path.events == [("read", {"encoding": "utf-8"})]


def test_timestamp_comparison_retains_python_equality_for_bool_and_number():
    path = RecordingPath(['{"generated_at": "old", "count": true}'])
    assert state.write_if_changed(path, {"generated_at": "new", "count": 1}) is False
    assert len(path.events) == 1


@pytest.mark.parametrize("prior", [None, False, 0, "", [], [1]])
def test_non_dictionary_prior_cannot_trigger_timestamp_comparison_skip(prior):
    payload = {"generated_at": "new"}
    path = RecordingPath([json.dumps(prior)])
    assert state.write_if_changed(path, payload) is True
    assert [event[0] for event in path.events] == ["read", "read", "mkdir", "write"]
    assert path.events[-1] == ("write", '{\n  "generated_at": "new"\n}\n', {"encoding": "utf-8"})


def test_missing_file_and_only_timestamp_keep_existing_empty_dictionary_behavior():
    path = RecordingPath([FileNotFoundError("missing")])
    assert state.write_if_changed(path, {"generated_at": "new"}) is False
    assert len(path.events) == 1


def test_invalid_json_and_only_timestamp_keep_default_comparison_behavior():
    path = RecordingPath(["malformed json"])
    assert state.write_if_changed(path, {"generated_at": "new"}) is False
    assert len(path.events) == 1


def test_changed_timestamp_payload_orders_load_serialization_read_mkdir_write():
    path = RecordingPath(["prior text", "different bytes"])

    def loads(raw):
        path.events.append(("loads", raw))
        return {"generated_at": "old", "count": 1}

    payload = {"generated_at": "new", "count": 2}

    def dumps(value, **kwargs):
        assert value is payload
        path.events.append(("dumps", kwargs))
        return "serialized"

    assert state.write_if_changed(path, payload,
                                 json_module=SimpleNamespace(loads=loads, dumps=dumps)) is True
    assert path.events == [
        ("read", {"encoding": "utf-8"}), ("loads", "prior text"),
        ("dumps", {"ensure_ascii": False, "indent": 2}),
        ("read", {"encoding": "utf-8"}),
        ("mkdir", {"parents": True, "exist_ok": True}),
        ("write", "serialized\n", {"encoding": "utf-8"}),
    ]


def test_supplied_load_callback_keeps_two_argument_signature_and_default_identity():
    path = RecordingPath()
    payload = {"generated_at": "new", "count": 1}
    calls = []

    def load_json(*args):
        calls.append(args)
        assert args[0] is path
        assert args[1] == {}
        return {"generated_at": "old", "count": 1}

    assert state.write_if_changed(path, payload, load_json=load_json,
                                 json_module=object()) is False
    assert len(calls) == 1
    assert path.events == []


def test_falsey_supplied_callback_is_used_instead_of_default():
    class FalseyLoader:
        def __bool__(self):
            return False

        def __call__(self, path, default):
            return {"count": 1}

    path = RecordingPath()
    assert state.write_if_changed(path, {"generated_at": "new", "count": 1},
                                 load_json=FalseyLoader(), json_module=object()) is False
    assert path.events == []


def test_default_load_callback_is_resolved_at_call_time(monkeypatch):
    path = RecordingPath()
    module = object()
    calls = []

    def replacement(path_arg, default, *, json_module):
        calls.append((path_arg, default, json_module))
        return {"count": 1}

    monkeypatch.setattr(state, "load_json", replacement)
    assert state.write_if_changed(path, {"generated_at": "new", "count": 1},
                                 json_module=module) is False
    assert calls == [(path, {}, module)]
    assert path.events == []


@pytest.mark.parametrize("payload", [[], "text", None, False, {"count": 1}])
def test_payload_without_generated_at_never_calls_load_callback(payload):
    def forbidden(*args):
        raise AssertionError("prior JSON load must not occur")

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path = RecordingPath([text])
    assert state.write_if_changed(path, payload, load_json=forbidden) is False
    assert path.events == [("read", {"encoding": "utf-8"})]


def test_identical_rendered_bytes_skip_mkdir_and_write_after_timestamp_load():
    payload = {"generated_at": "new", "count": 2}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path = RecordingPath([text])
    assert state.write_if_changed(path, payload,
                                 load_json=lambda path, default: {"count": 1}) is False
    assert path.events == [("read", {"encoding": "utf-8"})]


@pytest.mark.parametrize("error", [
    FileNotFoundError("missing"), PermissionError("permission"),
    IsADirectoryError("directory"), OSError("io"),
])
def test_second_read_oserror_falls_through_to_write(error):
    path = RecordingPath([error])
    assert state.write_if_changed(path, {"count": 2}) is True
    assert [event[0] for event in path.events] == ["read", "mkdir", "write"]


@pytest.mark.parametrize("error", [
    ValueError("decode"), TypeError("read type"), AttributeError("read method"),
    RuntimeError("read failure"),
])
def test_second_read_other_errors_propagate_without_mkdir_or_write(error):
    path = RecordingPath([error])
    with pytest.raises(type(error), match=str(error)):
        state.write_if_changed(path, {"count": 2})
    assert path.events == [("read", {"encoding": "utf-8"})]


@pytest.mark.parametrize("error", [OSError("callback"), ValueError("callback"), TypeError("callback")])
def test_supplied_callback_failure_is_not_caught(error):
    path = RecordingPath()

    def fail(path, default):
        raise error

    with pytest.raises(type(error), match="callback"):
        state.write_if_changed(path, {"generated_at": "new"}, load_json=fail)
    assert path.events == []


@pytest.mark.parametrize("payload,error_type", [
    ({"x": object()}, TypeError), ({"x": {1, 2}}, TypeError),
])
def test_serialization_failure_precedes_file_read(payload, error_type):
    path = RecordingPath()
    with pytest.raises(error_type):
        state.write_if_changed(path, payload)
    assert path.events == []


def test_circular_serialization_failure_propagates_without_writing():
    payload = {"generated_at": "new"}
    payload["cycle"] = payload
    path = RecordingPath()
    with pytest.raises(ValueError, match="Circular"):
        state.write_if_changed(path, payload, load_json=lambda path, default: {})
    assert path.events == []


@pytest.mark.parametrize("stage,error", [
    ("mkdir", PermissionError("mkdir")), ("mkdir", ValueError("mkdir")),
    ("write", PermissionError("write")), ("write", ValueError("write")),
])
def test_mkdir_and_write_failures_propagate_at_original_step(stage, error):
    kwargs = {f"{stage}_error": error}
    path = RecordingPath([FileNotFoundError("missing")], **kwargs)
    with pytest.raises(type(error), match=stage):
        state.write_if_changed(path, {"count": 1})
    expected = ["read", "mkdir"] + (["write"] if stage == "write" else [])
    assert [event[0] for event in path.events] == expected


def test_utf8_pretty_bytes_newline_key_order_and_payload_references(tmp_path):
    nested = [{"title": "臺灣 遊戲", "enabled": False}]
    payload = {"z": nested, "a": None}
    original = copy.deepcopy(payload)
    path = tmp_path / "new" / "deeper" / "7.json"
    assert state.write_if_changed(path, payload) is True
    expected = ('{\n  "z": [\n    {\n      "title": "臺灣 遊戲",\n'
                '      "enabled": false\n    }\n  ],\n  "a": null\n}\n')
    assert path.read_bytes() == expected.encode("utf-8")
    assert payload == original
    assert payload["z"] is nested
    assert state.write_if_changed(path, payload) is False


def test_same_data_with_other_whitespace_is_rewritten_without_timestamp(tmp_path):
    path = tmp_path / "7.json"
    path.write_text('{"count":1}', encoding="utf-8")
    assert state.write_if_changed(path, {"count": 1}) is True
    assert path.read_bytes() == b'{\n  "count": 1\n}\n'


def test_write_follows_existing_symlink_and_preserves_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert state.write_if_changed(link, {"count": 1}) is True
    assert link.is_symlink()
    assert target.read_bytes() == b'{\n  "count": 1\n}\n'


def test_exists_passes_through_path_method_result():
    result = object()
    calls = []
    path = SimpleNamespace(exists=lambda: calls.append("exists") or result)
    assert state.exists(path) is result
    assert calls == ["exists"]


def test_glob_returns_original_iterator_without_consuming_or_sorting():
    events = []

    def iterator():
        events.append("iterated")
        yield "z"
        yield "a"

    result = iterator()
    path = SimpleNamespace(glob=lambda pattern: events.append(("glob", pattern)) or result)
    assert state.glob(path, "????-??.json") is result
    assert events == [("glob", "????-??.json")]
    assert list(result) == ["z", "a"]
    assert events[-1] == "iterated"


def test_unlink_calls_original_path_method_without_new_kwargs():
    calls = []
    path = SimpleNamespace(unlink=lambda: calls.append("unlink"))
    assert state.unlink(path) is None
    assert calls == ["unlink"]


@pytest.mark.parametrize("method,args", [("exists", ()), ("glob", ("*.json",)), ("unlink", ())])
@pytest.mark.parametrize("error", [OSError("path operation"), ValueError("path operation")])
def test_path_operation_errors_propagate(method, args, error):
    def fail(*call_args):
        assert call_args == args
        raise error

    path = SimpleNamespace(**{method: fail})
    with pytest.raises(type(error), match="path operation"):
        getattr(state, method)(path, *args)
