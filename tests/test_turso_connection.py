"""Offline checks for credential handling and diagnostic failure recovery."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts.check_turso_connection import (
    CheckError, NoRedirect, TursoProbeClient, endpoint_from_url, main, run_probe,
)


def wire_result(rows=None):
    return json.dumps({"results": [
        {"type": "ok", "response": {"type": "execute", "result": {"rows": rows or []}}},
        {"type": "ok", "response": {"type": "close"}},
    ]}).encode()


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, results):
        self.results = iter(results)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        item = next(self.results)
        if isinstance(item, Exception):
            raise item
        return Response(item)


class MemoryProbe:
    """Emulate the database semantics needed to check ownership and cleanup."""
    def __init__(self, *, corrupt=False, fail_insert=False, fail_cleanup=False):
        self.rows = {"another-run": "leave this row alone"}
        self.corrupt = corrupt
        self.fail_insert = fail_insert
        self.fail_cleanup = fail_cleanup

    def for_cleanup(self):
        return self

    def execute(self, sql, args=()):
        if sql.startswith("CREATE"):
            return {}
        if sql.startswith("INSERT"):
            self.rows[args[0]] = args[2]
            if self.fail_insert:
                # The database committed, but the client lost the response.
                raise CheckError("NETWORK_ERROR", "Write response was lost.")
            return {}
        if sql.startswith("SELECT payload"):
            value = "corrupt" if self.corrupt else self.rows[args[0]]
            return {"rows": [[{"type": "text", "value": value}]]}
        if sql.startswith("DELETE"):
            if self.fail_cleanup:
                raise CheckError("NETWORK_ERROR", "Cleanup response was lost.")
            self.rows.pop(args[0], None)
            return {}
        if sql.startswith("SELECT COUNT"):
            return {"rows": [[{"type": "integer", "value": str(int(args[0] in self.rows))}]]}
        raise AssertionError("Unexpected statement")


class TursoConnectionTests(unittest.TestCase):
    def test_only_credential_free_https_turso_endpoints_are_allowed(self):
        self.assertEqual(endpoint_from_url("libsql://radar-team.aws-us-east-1.turso.io/"),
                         "https://radar-team.aws-us-east-1.turso.io/v2/pipeline")
        for url in ["http://radar.turso.io", "https://example.com", "https://radar.turso.io.evil.test",
                    "https://user:password@radar.turso.io", "https://radar.turso.io?authToken=secret",
                    "https://radar.turso.io/v2/pipeline", "https://radar.turso.io:444", "invalid"]:
            with self.subTest(url=url), self.assertRaises(CheckError):
                endpoint_from_url(url)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test"))

    def test_network_retries_are_bounded_and_do_not_echo_credentials(self):
        opener = Opener([URLError("secret-token https://private-host") for _ in range(3)])
        client = TursoProbeClient("libsql://radar.turso.io", "secret-token", opener=opener, sleep=lambda _: None)
        with self.assertRaises(CheckError) as failure:
            client.execute("SELECT 1")
        self.assertEqual(len(opener.requests), 3)
        self.assertNotIn("secret-token", str(failure.exception))
        self.assertNotIn("private-host", str(failure.exception))
        self.assertTrue(all(0 < timeout <= 15 for _, timeout in opener.requests))

    def test_authentication_failure_is_not_retried_or_echoed(self):
        error = HTTPError("https://radar.turso.io?token=secret-token", 401, "secret-token", {}, io.BytesIO(b"secret-token"))
        opener = Opener([error])
        client = TursoProbeClient("libsql://radar.turso.io", "secret-token", opener=opener)
        with self.assertRaises(CheckError) as failure:
            client.execute("SELECT 1")
        self.assertEqual(failure.exception.code, "AUTHORIZATION_FAILED")
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn("secret-token", str(failure.exception))

    def test_sql_error_in_successful_http_response_still_fails(self):
        raw = json.dumps({"results": [
            {"type": "error", "error": {"code": "SQLITE_AUTH", "message": "secret-token"}},
            {"type": "ok", "response": {"type": "close"}},
        ]}).encode()
        with self.assertRaises(CheckError) as failure:
            TursoProbeClient._result(raw)
        self.assertEqual(failure.exception.code, "WRITE_PERMISSION_REQUIRED")
        self.assertNotIn("secret-token", str(failure.exception))

    def test_parameters_are_bound_and_connection_closed(self):
        opener = Opener([wire_result()])
        client = TursoProbeClient("libsql://radar.turso.io", "secret-token", opener=opener)
        value = "遊戲雷達 ' ; DROP TABLE games; --"
        client.execute("SELECT ?", (value,))
        request = opener.requests[0][0]
        payload = json.loads(request.data)
        self.assertEqual(payload["requests"][0]["stmt"]["sql"], "SELECT ?")
        self.assertEqual(payload["requests"][0]["stmt"]["args"], [{"type": "text", "value": value}])
        self.assertEqual(payload["requests"][-1], {"type": "close"})

    def test_probe_cleans_only_its_own_committed_row(self):
        client = MemoryProbe()
        report = run_probe(client)
        self.assertEqual(report["write"], "passed")
        self.assertEqual(report["cleanup"], "passed")
        self.assertEqual(client.rows, {"another-run": "leave this row alone"})

    def test_probe_cleans_up_after_lost_write_response_or_bad_readback(self):
        for kwargs, code in [({"fail_insert": True}, "NETWORK_ERROR"), ({"corrupt": True}, "READBACK_MISMATCH")]:
            with self.subTest(**kwargs):
                client = MemoryProbe(**kwargs)
                with self.assertRaises(CheckError) as failure:
                    run_probe(client)
                self.assertEqual(failure.exception.code, code)
                self.assertEqual(client.rows, {"another-run": "leave this row alone"})

    def test_failed_cleanup_cannot_report_success(self):
        client = MemoryProbe(fail_cleanup=True)
        with self.assertRaises(CheckError) as failure:
            run_probe(client)
        self.assertEqual(failure.exception.code, "CLEANUP_FAILED")

    def test_expired_time_budget_makes_no_request(self):
        opener = Opener([])
        client = TursoProbeClient("libsql://radar.turso.io", "secret-token", opener=opener, budget_seconds=0)
        with self.assertRaises(CheckError) as failure:
            client.execute("SELECT 1")
        self.assertEqual(failure.exception.code, "TIME_BUDGET_EXCEEDED")
        self.assertEqual(opener.requests, [])

    def test_cleanup_has_an_independent_bounded_budget(self):
        now = [0.0]
        opener = Opener([wire_result()])
        client = TursoProbeClient("libsql://radar.turso.io", "secret-token", opener=opener, clock=lambda: now[0])
        now[0] = 121.0
        with self.assertRaises(CheckError):
            client.execute("SELECT 1")
        cleanup = client.for_cleanup()
        cleanup.execute("DELETE FROM _radar_connection_checks WHERE probe_id = ?", ("own-row",))
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(cleanup.deadline, 151.0)

    def test_cleanup_failure_preserves_original_failure_code(self):
        client = MemoryProbe(fail_insert=True, fail_cleanup=True)
        with self.assertRaises(CheckError) as failure:
            run_probe(client)
        self.assertEqual(failure.exception.code, "CLEANUP_FAILED")
        self.assertEqual(failure.exception.details["operation_error_code"], "NETWORK_ERROR")
        self.assertEqual(failure.exception.details["cleanup_error_code"], "NETWORK_ERROR")

    def test_missing_secrets_and_unexpected_errors_produce_safe_failure_reports(self):
        for env in [{}, {"TURSO_DATABASE_URL": "libsql://radar.turso.io", "TURSO_AUTH_TOKEN": "secret-token"}]:
            output = io.StringIO()
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output), \
                    patch("scripts.check_turso_connection.run_probe", side_effect=RuntimeError("secret-token")):
                code = main([])
            self.assertEqual(code, 1)
            self.assertNotIn("secret-token", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
