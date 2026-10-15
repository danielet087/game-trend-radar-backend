"""Offline protocol contracts and real SQLite persistence for SQL storage clients."""

import io
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from radar_backend.storage.sql import D1Client, SqliteClient, StorageError, TursoClient


ACCOUNT = "1234567890abcdef1234567890abcdef"
DATABASE = "aabbccdd-1234-5678-abcd-1234567890ab"
TOKEN = "secret-storage-token-value"
URL = "libsql://radar-test-private.turso.io"
REQUEST_LIMIT = 512 * 1024


def encode(value):
    return json.dumps(value).encode("utf-8")


def turso_result(rows=None, cols=None):
    return {"cols": [] if cols is None else cols, "rows": [] if rows is None else rows,
            "affected_row_count": 0, "last_insert_rowid": None}


def turso_body(*results):
    if not results:
        results = (turso_result(),)
    return {"baton": None, "base_url": None, "results": [
        *[{"type": "ok", "response": {"type": "execute", "result": result}} for result in results],
        {"type": "ok", "response": {"type": "close"}},
    ]}


def d1_body(*results):
    if not results:
        results = ([],)
    return {"success": True, "errors": [], "messages": [], "result": [
        {"success": True, "results": result, "meta": {"changes": 0}} for result in results
    ]}


def http_error(status, headers=None):
    return HTTPError("https://private-host.invalid/?token=" + TOKEN, status, TOKEN,
                     headers or {}, io.BytesIO(TOKEN.encode()))


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response if isinstance(response, bytes) else encode(response))


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class StorageHTTPTests(unittest.TestCase):
    def client(self, provider, opener, **kwargs):
        kwargs.setdefault("sleep", lambda _: None)
        if provider == "turso":
            return TursoClient(URL, TOKEN, opener=opener, **kwargs)
        return D1Client(ACCOUNT, DATABASE, TOKEN, opener=opener, **kwargs)

    def body(self, provider, count=1):
        if provider == "turso":
            return turso_body(*[turso_result() for _ in range(count)])
        return d1_body(*[[] for _ in range(count)])

    def assert_safe(self, error):
        self.assertIsInstance(error.code, str)
        self.assertTrue(error.code)
        for secret in (TOKEN, ACCOUNT, DATABASE, "radar-test-private", "private-host.invalid"):
            self.assertNotIn(secret, str(error))

    def test_turso_bound_unicode_large_integer_and_null_wire_format(self):
        opener = Opener([turso_body()])
        value = "遊戲雷達 ' ; DROP TABLE games; --"
        self.assertEqual(self.client("turso", opener).execute("SELECT ?, ?, ?", (value, 2**63 - 1, None)), [])
        request, timeout = opener.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://radar-test-private.turso.io/v2/pipeline")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(payload["requests"], [
            {"type": "execute", "stmt": {"sql": "SELECT ?, ?, ?", "args": [
                {"type": "text", "value": value}, {"type": "integer", "value": str(2**63 - 1)},
                {"type": "null"},
            ], "want_rows": True}}, {"type": "close"},
        ])
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 15)

    def test_d1_bound_parameters_and_fixed_endpoint(self):
        opener = Opener([d1_body()])
        params = ["103582791475572379", "中文 ' ?", 2**53 - 1, -(2**53 - 1), None]
        self.assertEqual(self.client("d1", opener).execute("SELECT ?, ?, ?, ?, ?", params), [])
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url,
                         f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database/{DATABASE}/query")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(json.loads(request.data), {"sql": "SELECT ?, ?, ?, ?, ?", "params": params})
        self.assertTrue(0 < timeout <= 15)

    def test_turso_batch_uses_execute_per_statement_and_one_close(self):
        opener = Opener([turso_body(turso_result(), turso_result(
            [[{"type": "text", "value": "ok"}]], [{"name": "value", "decltype": "TEXT"}]))])
        result = self.client("turso", opener).batch([("INSERT INTO t VALUES (?)", ["ok"]), ("SELECT value FROM t", [])])
        self.assertEqual(result, [[], [{"value": "ok"}]])
        self.assertEqual(len(opener.requests), 1)
        requests = json.loads(opener.requests[0][0].data)["requests"]
        self.assertEqual([item["type"] for item in requests], ["execute", "execute", "close"])
        self.assertEqual([item["stmt"]["sql"] for item in requests[:-1]],
                         ["INSERT INTO t VALUES (?)", "SELECT value FROM t"])

    def test_d1_batch_preserves_each_statement_result(self):
        opener = Opener([d1_body([], [{"value": "ok"}])])
        result = self.client("d1", opener).batch([("INSERT INTO t VALUES (?)", ["ok"]), ("SELECT value FROM t", [])])
        self.assertEqual(result, [[], [{"value": "ok"}]])
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(json.loads(opener.requests[0][0].data), {"batch": [
            {"sql": "INSERT INTO t VALUES (?)", "params": ["ok"]},
            {"sql": "SELECT value FROM t", "params": []},
        ]})

    def test_turso_decodes_typed_values_without_large_integer_precision_loss(self):
        names = ["label", "positive", "negative", "missing", "ratio", "whole"]
        cells = [{"type": "text", "value": "遊戲"}, {"type": "integer", "value": str(2**63 - 1)},
                 {"type": "integer", "value": str(-(2**63))}, {"type": "null"},
                 {"type": "float", "value": 1.25}, {"type": "float", "value": 42.0}]
        opener = Opener([turso_body(turso_result([cells], [{"name": name} for name in names]))])
        self.assertEqual(self.client("turso", opener).execute("SELECT ..."), [{
            "label": "遊戲", "positive": 2**63 - 1, "negative": -(2**63),
            "missing": None, "ratio": 1.25, "whole": 42.0,
        }])

    def test_turso_rejects_malformed_or_unsafe_cells(self):
        bad_cells = [
            {"type": "text", "value": 1}, {"type": "integer", "value": "1.2"},
            {"type": "integer", "value": str(2**63)},
            {"type": "integer", "value": str(-(2**63) - 1)},
            {"type": "float", "value": float(2**53)},
            {"type": "float", "value": float(-(2**53))},
            {"type": "float", "value": math.inf}, {"type": "float", "value": math.nan},
            {"type": "float", "value": True}, {"type": "float", "value": "1.25"},
            {"type": "blob", "base64": "aGVsbG8="}, {"type": "unknown", "value": TOKEN}, {}, None,
        ]
        for cell in bad_cells:
            with self.subTest(cell=cell):
                opener = Opener([turso_body(turso_result([[cell]], [{"name": "value"}]))])
                with self.assertRaises(StorageError) as caught:
                    self.client("turso", opener).execute("SELECT value")
                self.assert_safe(caught.exception)
                self.assertEqual(len(opener.requests), 1)

    def test_turso_rejects_row_and_column_shape_mismatches(self):
        bad_results = [
            {}, {"cols": [], "rows": None}, {"cols": None, "rows": []},
            turso_result([[{"type": "text", "value": "x"}]], []),
            turso_result([[]], [{"name": "value"}]),
            turso_result([{"value": "x"}], [{"name": "value"}]),
            turso_result([], [None]), turso_result([], [{"name": None}]),
            turso_result([[{"type": "null"}, {"type": "null"}]], [{"name": "x"}, {"name": "x"}]),
        ]
        for result in bad_results:
            with self.subTest(result=result):
                opener = Opener([turso_body(result)])
                with self.assertRaises(StorageError) as caught:
                    self.client("turso", opener).execute("SELECT value")
                self.assert_safe(caught.exception)

    def test_turso_does_not_accept_partial_pipeline_or_failed_close(self):
        good = turso_body(turso_result(), turso_result())
        errors = [
            {"results": good["results"][:1]},
            {"results": good["results"] + [good["results"][0]]},
            {"results": [good["results"][0], {"type": "error", "error": {"message": TOKEN}}, good["results"][-1]]},
            {"results": good["results"][:2] + [{"type": "error", "error": {"message": TOKEN}}]},
            {"results": [good["results"][-1], good["results"][0], good["results"][-1]]},
        ]
        for body in errors:
            with self.subTest(body=body):
                opener = Opener([body])
                with self.assertRaises(StorageError) as caught:
                    self.client("turso", opener).batch([("SELECT 1", []), ("SELECT 2", [])])
                self.assert_safe(caught.exception)
                self.assertEqual(len(opener.requests), 1)

    def test_d1_checks_outer_and_each_inner_success_and_count(self):
        errors = [
            {"success": False, "errors": [{"message": TOKEN}], "result": []},
            {"success": True, "errors": [{"message": TOKEN}], "result": []},
            {"success": 1, "errors": [], "result": d1_body([], [])["result"]},
            {"success": True, "result": [{"success": True, "results": []}, {"success": False, "error": TOKEN}]},
            d1_body([]), d1_body([], [], []),
            {"success": True, "result": [{"success": True, "results": []}, {"results": []}]},
            {"success": True, "result": [{"success": True, "results": []}, {"success": 1, "results": []}]},
        ]
        for body in errors:
            with self.subTest(body=body):
                opener = Opener([body])
                with self.assertRaises(StorageError) as caught:
                    self.client("d1", opener).batch([("SELECT 1", []), ("SELECT 2", [])])
                self.assert_safe(caught.exception)
                self.assertEqual(len(opener.requests), 1)

    def test_d1_rejects_missing_results_and_non_mapping_rows(self):
        errors = [
            {"success": True, "result": [{"success": True}]}, d1_body(None),
            d1_body({"value": 1}), d1_body([1]), d1_body([None]), d1_body([[1]]),
        ]
        for body in errors:
            with self.subTest(body=body), self.assertRaises(StorageError):
                self.client("d1", Opener([body])).execute("SELECT value")

    def test_invalid_json_or_top_level_response_is_rejected_without_retry(self):
        for provider in ("turso", "d1"):
            for body in (b"not json " + TOKEN.encode(), b"[]", b"{}", b"null"):
                with self.subTest(provider=provider, body=body):
                    opener = Opener([body])
                    with self.assertRaises(StorageError) as caught:
                        self.client(provider, opener).execute("SELECT 1")
                    self.assert_safe(caught.exception)
                    self.assertEqual(len(opener.requests), 1)

    def test_invalid_values_are_rejected_before_any_io(self):
        for provider in ("turso", "d1"):
            for value in (True, False, 1.25, float("nan"), float("inf"), b"bytes", {}, [], 2**63, -(2**63) - 1):
                with self.subTest(provider=provider, type=type(value).__name__):
                    opener = Opener([])
                    with self.assertRaises(StorageError):
                        self.client(provider, opener).execute("SELECT ?", [value])
                    self.assertEqual(opener.requests, [])

    def test_d1_unsafe_numeric_ids_are_rejected_but_string_ids_work(self):
        for value in (2**53, -(2**53), 103582791475572379):
            with self.subTest(value=value):
                opener = Opener([])
                with self.assertRaises(StorageError):
                    self.client("d1", opener).execute("SELECT ?", [value])
                self.assertEqual(opener.requests, [])
                opener = Opener([d1_body([{"group_id64": str(value)}])])
                self.assertEqual(self.client("d1", opener).execute("SELECT ? AS group_id64", [str(value)]),
                                 [{"group_id64": str(value)}])

    def test_invalid_statement_or_parameter_container_is_rejected(self):
        for provider in ("turso", "d1"):
            for sql, params in [("", []), (" \n\t", []), (None, []), (123, []),
                                ("SELECT ?", "text"), ("SELECT ?", {"x": "value"}),
                                ("SELECT ?", None)]:
                with self.subTest(provider=provider, sql=sql, params=params):
                    opener = Opener([])
                    with self.assertRaises(StorageError):
                        self.client(provider, opener).execute(sql, params)
                    self.assertEqual(opener.requests, [])

    def test_batch_is_fully_validated_before_any_request(self):
        for provider in ("turso", "d1"):
            for statements in [
                [("INSERT INTO t VALUES (?)", ["valid"]), ("INSERT INTO t VALUES (?)", [False])],
                [("SELECT 1", []), ("", [])],
                [("SELECT 1", []), ("SELECT 2",)],
            ]:
                with self.subTest(provider=provider, statements=statements):
                    opener = Opener([])
                    with self.assertRaises(StorageError):
                        self.client(provider, opener).batch(statements)
                    self.assertEqual(opener.requests, [])

    def test_empty_batch_has_no_io_and_twenty_five_is_maximum(self):
        for provider in ("turso", "d1"):
            with self.subTest(provider=provider):
                opener = Opener([self.body(provider, 25)])
                client = self.client(provider, opener)
                self.assertEqual(client.batch([]), [])
                self.assertEqual(opener.requests, [])
                self.assertEqual(client.batch([("SELECT 1", [])] * 25), [[]] * 25)
                with self.assertRaises(StorageError):
                    client.batch([("SELECT 1", [])] * 26)
                self.assertEqual(len(opener.requests), 1)

    def test_encoded_size_limit_counts_sql_and_utf8_parameters(self):
        for provider in ("turso", "d1"):
            for sql, params in [("SELECT ?", ["遊" * REQUEST_LIMIT]), (" " * REQUEST_LIMIT + "SELECT 1", [])]:
                with self.subTest(provider=provider, large_sql=len(sql) > 100):
                    opener = Opener([])
                    with self.assertRaises(StorageError):
                        self.client(provider, opener).execute(sql, params)
                    self.assertEqual(opener.requests, [])

    def test_size_limit_is_strictly_less_than_512_kib(self):
        for provider in ("turso", "d1"):
            with self.subTest(provider=provider):
                opener = Opener([self.body(provider), self.body(provider)])
                client = self.client(provider, opener)
                client.execute("SELECT ?", [""])
                overhead = len(opener.requests[0][0].data)
                client.execute("SELECT ?", ["x" * (REQUEST_LIMIT - overhead - 1)])
                self.assertEqual(len(opener.requests[-1][0].data), REQUEST_LIMIT - 1)
                with self.assertRaises(StorageError):
                    client.execute("SELECT ?", ["x" * (REQUEST_LIMIT - overhead)])
                self.assertEqual(len(opener.requests), 2)

    def test_transient_errors_retry_at_most_three_attempts(self):
        for provider in ("turso", "d1"):
            for errors in ([http_error(429), http_error(503)], [URLError(TOKEN), TimeoutError(TOKEN)]):
                with self.subTest(provider=provider, errors=errors):
                    opener = Opener([*errors, self.body(provider)])
                    self.assertEqual(self.client(provider, opener).execute("SELECT 1"), [])
                    self.assertEqual(len(opener.requests), 3)
                    payloads = [request.data for request, _ in opener.requests]
                    self.assertEqual(payloads, [payloads[0]] * 3)
            for error in (http_error(429), http_error(503), URLError(TOKEN), TimeoutError(TOKEN)):
                with self.subTest(provider=provider, error=type(error).__name__):
                    opener = Opener([error, error, error])
                    with self.assertRaises(StorageError) as caught:
                        self.client(provider, opener).execute("SELECT 1")
                    self.assert_safe(caught.exception)
                    self.assertEqual(len(opener.requests), 3)

    def test_permanent_http_failures_and_redirects_are_not_retried(self):
        for provider in ("turso", "d1"):
            for status in (301, 302, 307, 308, 400, 401, 403, 404, 408, 409, 422):
                with self.subTest(provider=provider, status=status):
                    opener = Opener([http_error(status)])
                    with self.assertRaises(StorageError) as caught:
                        self.client(provider, opener).execute("SELECT 1")
                    self.assert_safe(caught.exception)
                    self.assertEqual(len(opener.requests), 1)

    def test_default_opener_has_redirect_rejection_handler(self):
        for provider in ("turso", "d1"):
            with self.subTest(provider=provider):
                with patch("radar_backend.storage.sql.build_opener", return_value=Opener([])) as factory:
                    self.client(provider, None)
                self.assertTrue(factory.called)
                handlers = [handler for handler in factory.call_args.args if hasattr(handler, "redirect_request")]
                self.assertTrue(handlers, "Default opener must install an explicit redirect-rejection handler")
                for handler in handlers:
                    self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.invalid"))

    def test_budget_is_shared_across_calls_and_clamps_request_timeout(self):
        for provider in ("turso", "d1"):
            with self.subTest(provider=provider):
                clock = Clock()
                opener = Opener([self.body(provider)])
                client = self.client(provider, opener, clock=clock, sleep=clock.sleep, budget_seconds=3)
                clock.now = 1.0
                self.assertEqual(client.execute("SELECT 1"), [])
                self.assertTrue(0 < opener.requests[0][1] <= 2)
                clock.now = 3.1
                with self.assertRaises(StorageError):
                    client.execute("SELECT 2")
                self.assertEqual(len(opener.requests), 1)

    def test_retry_after_cannot_exceed_total_budget(self):
        for provider in ("turso", "d1"):
            with self.subTest(provider=provider):
                clock = Clock()
                opener = Opener([http_error(429, {"Retry-After": "600"})])
                client = self.client(provider, opener, clock=clock, sleep=clock.sleep, budget_seconds=2)
                with self.assertRaises(StorageError) as caught:
                    client.execute("SELECT 1")
                self.assert_safe(caught.exception)
                self.assertEqual(len(opener.requests), 1)
                self.assertLessEqual(sum(clock.sleeps), 2)

    def test_invalid_urls_identifiers_and_tokens_are_rejected_locally(self):
        bad_urls = ["", "http://radar-test-private.turso.io", "https://evil.invalid", "https://turso.io.evil.invalid",
                    "https://user:pass@radar-test-private.turso.io", "https://radar-test-private.turso.io?token=x",
                    "https://radar-test-private.turso.io/#fragment", "https://radar-test-private.turso.io/v2/pipeline"]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(StorageError):
                TursoClient(url, TOKEN, opener=Opener([]))
        for account, database in [("", DATABASE), ("g" * 32, DATABASE), (ACCOUNT + "/evil", DATABASE),
                                  (ACCOUNT, "not-uuid"), (ACCOUNT, DATABASE + "?token=x")]:
            with self.subTest(account=account, database=database), self.assertRaises(StorageError):
                D1Client(account, database, TOKEN, opener=Opener([]))
        for provider in ("turso", "d1"):
            for token in ("", "abc\ndef", "abc\r\n", "abc def", "abc\tdef", "中文"):
                with self.subTest(provider=provider, token=token), self.assertRaises(StorageError):
                    if provider == "turso":
                        TursoClient(URL, token, opener=Opener([]))
                    else:
                        D1Client(ACCOUNT, DATABASE, token, opener=Opener([]))

    def test_from_env_uses_existing_secret_names_and_missing_secrets_fail_safely(self):
        env = {"TURSO_DATABASE_URL": URL, "TURSO_AUTH_TOKEN": TOKEN, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
               "CLOUDFLARE_D1_DATABASE_ID": DATABASE, "CLOUDFLARE_D1_API_TOKEN": TOKEN}
        for provider, cls in (("turso", TursoClient), ("d1", D1Client)):
            with self.subTest(provider=provider):
                opener = Opener([self.body(provider)])
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(cls.from_env(opener=opener).execute("SELECT 1"), [])
                with patch.dict(os.environ, {}, clear=True), self.assertRaises(StorageError) as caught:
                    cls.from_env(opener=Opener([]))
                self.assert_safe(caught.exception)


class SQLiteStorageTests(unittest.TestCase):
    def client(self, path=":memory:"):
        client = SqliteClient(path)
        self.addCleanup(client.close)
        return client

    def test_round_trip_unicode_null_and_signed_64_bit_integer(self):
        client = self.client()
        client.execute("CREATE TABLE t (label TEXT, group_id INTEGER, missing TEXT)")
        label = "遊戲雷達 ' ; DROP TABLE t; --"
        self.assertEqual(client.execute("INSERT INTO t VALUES (?, ?, ?)", [label, 2**63 - 1, None]), [])
        self.assertEqual(client.execute("SELECT * FROM t"), [{"label": label, "group_id": 2**63 - 1, "missing": None}])

    def test_trigger_with_internal_semicolons_is_one_statement_and_autocommits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "storage.sqlite"
            client = self.client(path)
            client.execute("CREATE TABLE source (id TEXT PRIMARY KEY)")
            client.execute("CREATE TABLE audit (id TEXT, operation TEXT)")
            client.execute("""CREATE TRIGGER record_insert AFTER INSERT ON source BEGIN
                INSERT INTO audit VALUES (NEW.id, 'first');
                INSERT INTO audit VALUES (NEW.id, 'second');
            END""")
            client.execute("INSERT INTO source VALUES (?)", ["own-row"])
            with sqlite3.connect(path) as reader:
                self.assertEqual(reader.execute("SELECT id, operation FROM audit ORDER BY operation").fetchall(),
                                 [("own-row", "first"), ("own-row", "second")])
                self.assertEqual(reader.execute("SELECT id FROM source").fetchall(), [("own-row",)])
            client.close()

    def test_batch_failure_keeps_previously_committed_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "storage.sqlite"
            client = self.client(path)
            client.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
            with self.assertRaises(StorageError) as caught:
                client.batch([("INSERT INTO t VALUES (?)", ["first"]),
                              ("INSERT INTO t VALUES (?)", ["first"]),
                              ("INSERT INTO t VALUES (?)", ["never-run"])])
            self.assertTrue(caught.exception.code)
            with sqlite3.connect(path) as reader:
                self.assertEqual(reader.execute("SELECT id FROM t").fetchall(), [("first",)])
            self.assertEqual(client.execute("SELECT id FROM t"), [{"id": "first"}])
            client.close()

    def test_invalid_later_batch_parameter_prevents_all_writes(self):
        client = self.client()
        client.execute("CREATE TABLE t (id TEXT)")
        for bad_statement in [("INSERT INTO t VALUES (?)", [False]), ("", []), ("SELECT 1",)]:
            with self.subTest(bad_statement=bad_statement), self.assertRaises(StorageError):
                client.batch([("INSERT INTO t VALUES (?)", ["should-not-exist"]), bad_statement])
            self.assertEqual(client.execute("SELECT * FROM t"), [])

    def test_batch_limits_and_empty_batch(self):
        client = self.client()
        self.assertEqual(client.batch([]), [])
        self.assertEqual(client.batch([("SELECT 1 AS value", [])] * 25), [[{"value": 1}]] * 25)
        with self.assertRaises(StorageError):
            client.batch([("SELECT 1", [])] * 26)

    def test_sqlite_uses_same_strict_parameter_types(self):
        client = self.client()
        for value in (True, False, 1.25, b"bytes", {}, [], 2**63, -(2**63) - 1):
            with self.subTest(value=value), self.assertRaises(StorageError):
                client.execute("SELECT ?", [value])
        self.assertEqual(client.execute("SELECT ? AS value", [-(2**63)]), [{"value": -(2**63)}])

    def test_sqlite_error_does_not_expose_sql_values(self):
        client = self.client()
        with self.assertRaises(StorageError) as caught:
            client.execute(f"SELECT * FROM {TOKEN.replace('-', '_')}")
        self.assertNotIn(TOKEN.replace("-", "_"), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
