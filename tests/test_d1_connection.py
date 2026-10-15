"""Offline D1 protocol, credential safety, committed reads and cleanup checks."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts.check_d1_connection import (
    CheckError, D1ProbeClient, MAX_RESPONSE_BYTES, NoRedirect, TABLE,
    endpoint_from_ids, main, run_probe,
)


ACCOUNT = "1234567890abcdef1234567890abcdef"
DATABASE = "aabbccdd-1234-5678-abcd-1234567890ab"
TOKEN = "secret-token-test-value"


def wire_result(rows=None):
    return json.dumps({"success": True, "errors": [], "messages": [], "result": [
        {"success": True, "results": rows or [], "meta": {"changes": 0}},
    ]}).encode()


def http_error(status):
    return HTTPError("https://private-host?token=" + TOKEN, status, TOKEN, {}, io.BytesIO(TOKEN.encode()))


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)


class SQLiteOpener:
    """Execute actual bound SQL and commit each independent HTTP request."""
    def __init__(self, *, lost_inserts=0, corrupt_read=False, fail_cleanup=False):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(f"CREATE TABLE {TABLE} (probe_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.execute(f"INSERT INTO {TABLE} VALUES (?, ?, ?)", ("another-run", "before", "keep this row"))
        self.db.commit()
        self.requests = []
        self.lost_inserts = lost_inserts
        self.corrupt_read = corrupt_read
        self.fail_cleanup = fail_cleanup

    def open(self, request, *, timeout):
        payload = json.loads(request.data)
        self.requests.append(payload)
        sql, params = payload["sql"], payload["params"]
        if sql.startswith("DELETE") and self.fail_cleanup:
            raise http_error(403)
        cursor = self.db.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]
        self.db.commit()
        if sql.startswith("INSERT") and self.lost_inserts:
            self.lost_inserts -= 1
            raise URLError("lost write response " + TOKEN)
        if sql.startswith("SELECT payload") and self.corrupt_read:
            rows = [{"payload": "corrupted value"}]
        return io.BytesIO(wire_result(rows))


class D1ConnectionTests(unittest.TestCase):
    def client(self, opener, **kwargs):
        return D1ProbeClient(ACCOUNT, DATABASE, TOKEN, opener=opener, sleep=lambda _: None, **kwargs)

    def assert_safe(self, value):
        for forbidden in (TOKEN, ACCOUNT, DATABASE, "private-host", "Authorization: Bearer"):
            self.assertNotIn(forbidden, value)

    def test_fixed_api_endpoint_validates_identifiers_before_io(self):
        self.assertEqual(endpoint_from_ids(ACCOUNT.upper(), DATABASE.upper()),
                         f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database/{DATABASE}/query")
        for account, database in [("", DATABASE), (ACCOUNT + "/evil", DATABASE), ("g" * 32, DATABASE),
                                  (ACCOUNT, "not-uuid"), (ACCOUNT, DATABASE + "?token=x"),
                                  (ACCOUNT, "https://elsewhere.test")]:
            with self.subTest(account=account, database=database), self.assertRaises(CheckError):
                endpoint_from_ids(account, database)

    def test_token_rejects_empty_multiline_and_header_injection(self):
        for token in ("", "abc\ndef", "abc\r\n", "abc def", "abc\tdef", "中文"):
            with self.subTest(token=token), self.assertRaises(CheckError) as failure:
                D1ProbeClient(ACCOUNT, DATABASE, token)
            self.assertEqual(failure.exception.code, "INVALID_AUTH_TOKEN")

    def test_redirects_are_never_followed_or_retried(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test"))
        opener = Opener([http_error(302)])
        with self.assertRaises(CheckError) as failure:
            self.client(opener).execute("SELECT 1")
        self.assertEqual(failure.exception.code, "HTTP_ERROR")
        self.assertEqual(len(opener.requests), 1)
        self.assert_safe(str(failure.exception))

    def test_bound_params_and_safe_request_shape(self):
        opener = Opener([wire_result()])
        value = "遊戲雷達 ' ; DROP TABLE games; --"
        self.client(opener).execute("SELECT ?", (value,))
        request, timeout = opener.requests[0]
        self.assertEqual(json.loads(request.data), {"sql": "SELECT ?", "params": [value]})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(0 < timeout <= 15)

    def test_network_failures_are_bounded_and_sanitized(self):
        opener = Opener([URLError(TOKEN + " private-host") for _ in range(3)])
        with self.assertRaises(CheckError) as failure:
            self.client(opener).execute("SELECT 1")
        self.assertEqual(failure.exception.code, "NETWORK_ERROR")
        self.assertEqual(len(opener.requests), 3)
        self.assert_safe(str(failure.exception))

    def test_temporary_http_failure_can_recover_but_never_exceeds_three_attempts(self):
        for responses, success in [([http_error(429), http_error(503), wire_result()], True),
                                   ([http_error(503) for _ in range(3)], False)]:
            with self.subTest(success=success):
                opener = Opener(responses)
                if success:
                    self.assertEqual(self.client(opener).execute("SELECT 1")["results"], [])
                else:
                    with self.assertRaises(CheckError) as failure:
                        self.client(opener).execute("SELECT 1")
                    self.assertEqual(failure.exception.code, "HTTP_ERROR")
                self.assertEqual(len(opener.requests), 3)

    def test_authentication_and_permanent_http_failures_are_not_retried(self):
        for status, code in [(401, "AUTHORIZATION_FAILED"), (403, "AUTHORIZATION_FAILED"), (404, "HTTP_ERROR")]:
            with self.subTest(status=status):
                opener = Opener([http_error(status)])
                with self.assertRaises(CheckError) as failure:
                    self.client(opener).execute("SELECT 1")
                self.assertEqual(failure.exception.code, code)
                self.assertEqual(len(opener.requests), 1)
                self.assert_safe(str(failure.exception))

    def test_http_200_provider_and_sql_failures_cannot_pass_or_echo_body(self):
        for data, code in [
            ({"success": False, "errors": [{"message": TOKEN}], "result": []}, "API_ERROR"),
            ({"success": True, "errors": [{"message": TOKEN}], "result": []}, "API_ERROR"),
            ({"success": True, "result": [{"success": False, "error": TOKEN}]}, "SQL_ERROR"),
        ]:
            with self.subTest(code=code):
                opener = Opener([json.dumps(data).encode()])
                with self.assertRaises(CheckError) as failure:
                    self.client(opener).execute("SELECT 1")
                self.assertEqual(failure.exception.code, code)
                self.assertEqual(len(opener.requests), 1)
                self.assert_safe(str(failure.exception))

    def test_invalid_json_missing_success_and_malformed_results_fail(self):
        for data in [b"not-json", b"[]", b"{}", b'{"success": 1, "result": []}',
                     b'{"success": true, "result": []}',
                     b'{"success": true, "result": [{"results": []}]}',
                     b'{"success": true, "result": [{"success": true, "results": [1]}]}']:
            with self.subTest(data=data), self.assertRaises(CheckError) as failure:
                D1ProbeClient._result(data)
            self.assertEqual(failure.exception.code, "INVALID_RESPONSE")

    def test_oversized_response_fails_without_echoing_contents(self):
        opener = Opener([b"x" * (MAX_RESPONSE_BYTES + 1)])
        with self.assertRaises(CheckError) as failure:
            self.client(opener).execute("SELECT 1")
        self.assertEqual(failure.exception.code, "INVALID_RESPONSE")

    def test_real_sql_committed_read_and_cleanup_preserve_other_runs(self):
        opener = SQLiteOpener(lost_inserts=1)
        self.addCleanup(opener.db.close)
        report = run_probe(self.client(opener))
        self.assertEqual(report, {"read": "passed", "write": "passed", "unicode_and_large_id": "passed", "cleanup": "passed"})
        rows = [tuple(row) for row in opener.db.execute(f"SELECT * FROM {TABLE}")]
        self.assertEqual(rows, [("another-run", "before", "keep this row")])
        inserts = [request for request in opener.requests if request["sql"].startswith("INSERT")]
        self.assertEqual(len(inserts), 2)
        self.assertEqual(inserts[0], inserts[1])
        stored = json.loads(inserts[0]["params"][2])
        self.assertEqual(stored, {"label": "遊戲雷達 '連線測試'", "group_id64": "103582791475572379"})
        self.assertEqual(len(inserts[0]["params"][0]), 32)
        self.assertEqual(sum(r["sql"].startswith("SELECT payload") for r in opener.requests), 1)

    def test_failed_insert_response_and_bad_readback_still_clean_owned_row(self):
        for options, code in [({"lost_inserts": 3}, "NETWORK_ERROR"), ({"corrupt_read": True}, "READBACK_MISMATCH")]:
            with self.subTest(options=options):
                opener = SQLiteOpener(**options)
                self.addCleanup(opener.db.close)
                with self.assertRaises(CheckError) as failure:
                    run_probe(self.client(opener))
                self.assertEqual(failure.exception.code, code)
                self.assertEqual([row[0] for row in opener.db.execute(f"SELECT probe_id FROM {TABLE}")], ["another-run"])

    def test_cleanup_failure_preserves_both_safe_error_codes(self):
        opener = SQLiteOpener(lost_inserts=3, fail_cleanup=True)
        self.addCleanup(opener.db.close)
        with self.assertRaises(CheckError) as failure:
            run_probe(self.client(opener))
        self.assertEqual(failure.exception.code, "CLEANUP_FAILED")
        self.assertEqual(failure.exception.details, {
            "operation_error_code": "NETWORK_ERROR", "cleanup_error_code": "AUTHORIZATION_FAILED",
        })
        self.assert_safe(str(failure.exception))

    def test_cleanup_has_independent_thirty_second_budget_after_main_expiry(self):
        now = [0.0]
        opener = Opener([wire_result()])
        client = self.client(opener, clock=lambda: now[0])
        now[0] = 121.0
        with self.assertRaises(CheckError) as failure:
            client.execute("SELECT 1")
        self.assertEqual(failure.exception.code, "TIME_BUDGET_EXCEEDED")
        self.assertEqual(opener.requests, [])
        cleanup = client.for_cleanup()
        cleanup.execute(f"DELETE FROM {TABLE} WHERE probe_id = ?", ("own-row",))
        self.assertEqual(cleanup.deadline, 151.0)
        self.assertEqual(len(opener.requests), 1)

    def test_main_reports_success_and_saves_only_safe_status(self):
        env = {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_D1_DATABASE_ID": DATABASE,
               "CLOUDFLARE_D1_API_TOKEN": TOKEN}
        opener = SQLiteOpener()
        self.addCleanup(opener.db.close)
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            summary_path = Path(directory) / "summary.md"
            output = io.StringIO()
            with patch.dict(os.environ, {**env, "GITHUB_STEP_SUMMARY": str(summary_path)}, clear=True), \
                    patch("scripts.check_d1_connection.build_opener", return_value=opener), contextlib.redirect_stdout(output):
                status = main(["--report-path", str(report_path)])
            self.assertEqual(status, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["provider"], "cloudflare_d1")
            self.assertEqual(report["status"], "passed")
            self.assertEqual(json.loads(report_path.read_text()), report)
            self.assert_safe(output.getvalue() + report_path.read_text() + summary_path.read_text())

    def test_missing_secrets_and_unexpected_errors_return_sanitized_failures(self):
        valid_env = {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_D1_DATABASE_ID": DATABASE,
                     "CLOUDFLARE_D1_API_TOKEN": TOKEN}
        for env, code in [({}, "MISSING_SECRETS"), (valid_env, "UNEXPECTED_ERROR")]:
            output = io.StringIO()
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output), \
                    patch("scripts.check_d1_connection.run_probe", side_effect=RuntimeError(TOKEN)):
                status = main([])
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output.getvalue())["error_code"], code)
            self.assert_safe(output.getvalue())


if __name__ == "__main__":
    unittest.main()
