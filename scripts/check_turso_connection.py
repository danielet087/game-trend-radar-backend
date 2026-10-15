"""Verify a Turso database using Actions secrets, without touching game data.

The only persistent object is an empty, dedicated diagnostic table. Each run
uses its own probe ID and removes only its own row, including on failure.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


TABLE = "_radar_connection_checks"
MAX_RESPONSE_BYTES = 1024 * 1024


class CheckError(RuntimeError):
    """Only locally defined, credential-free messages may be exposed."""

    def __init__(self, code: str, message: str, **details: str):
        super().__init__(message)
        self.code = code
        self.details = details


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the database's bearer token to another host.
        return None


def endpoint_from_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname or ""
        valid = (
            parsed.scheme in {"libsql", "https"}
            and re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.turso\.io", host)
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise CheckError(
            "INVALID_DATABASE_URL",
            "TURSO_DATABASE_URL must be a libsql:// or https:// Turso database URL without credentials or query parameters.",
        )
    return f"https://{host}/v2/pipeline"


class TursoProbeClient:
    def __init__(self, url: str, token: str, *, opener=None, sleep=time.sleep,
                 clock=time.monotonic, budget_seconds: float = 120):
        self.endpoint = endpoint_from_url(url)
        token = token.strip()
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise CheckError("INVALID_AUTH_TOKEN", "TURSO_AUTH_TOKEN is missing or is not a valid single-line token.")
        self.token = token
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep
        self.clock = clock
        self.deadline = clock() + budget_seconds

    def for_cleanup(self):
        # Reserve a separate, bounded window even if the main probe timed out
        # after the server committed its row but before returning a response.
        return TursoProbeClient(
            self.endpoint.removesuffix("/v2/pipeline"), self.token,
            opener=self.opener, sleep=self.sleep, clock=self.clock, budget_seconds=30,
        )

    def execute(self, sql: str, args: tuple[str, ...] = ()) -> dict:
        payload = {
            "requests": [
                {"type": "execute", "stmt": {
                    "sql": sql,
                    "args": [{"type": "text", "value": value} for value in args],
                    "want_rows": True,
                }},
                {"type": "close"},
            ]
        }
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(3):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise CheckError("TIME_BUDGET_EXCEEDED", "The connection check exceeded its bounded time budget.")
            request = Request(self.endpoint, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with self.opener.open(request, timeout=min(15, remaining)) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise CheckError("INVALID_RESPONSE", "Database response exceeded the diagnostic size limit.")
                return self._result(raw)
            except HTTPError as error:
                status = error.code
                error.close()
                if status in {401, 403}:
                    raise CheckError("AUTHORIZATION_FAILED", "Turso rejected the credentials or database permissions (HTTP 401/403).") from None
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise CheckError("HTTP_ERROR", f"Turso request failed with HTTP {status}.") from None
            except (URLError, TimeoutError, socket.timeout, OSError):
                if attempt == 2:
                    raise CheckError("NETWORK_ERROR", "Could not reach Turso after three bounded attempts.") from None
            delay = min(2 ** attempt, max(0, self.deadline - self.clock()))
            self.sleep(delay)
        raise CheckError("NETWORK_ERROR", "Turso request did not complete.")

    @staticmethod
    def _result(raw: bytes) -> dict:
        try:
            data = json.loads(raw)
            results = data["results"]
            if len(results) != 2:
                raise ValueError
            for item in results:
                if item.get("type") == "error":
                    # Provider error messages may contain SQL values. Never echo them.
                    code = item.get("error", {}).get("code", "")
                    if code in {"SQLITE_AUTH", "SQLITE_READONLY", "AUTH_ERROR", "FORBIDDEN"}:
                        raise CheckError("WRITE_PERMISSION_REQUIRED", "The database token must allow reads and writes.")
                    raise CheckError("SQL_ERROR", "Turso returned a SQL error; confirm this database uses libSQL and has a read/write token.")
                if item["type"] != "ok":
                    raise ValueError
            if results[0]["response"]["type"] != "execute" or results[1]["response"]["type"] != "close":
                raise ValueError
            result = results[0]["response"]["result"]
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CheckError("INVALID_RESPONSE", "Turso returned an unexpected response format.") from None


def run_probe(client: TursoProbeClient) -> dict:
    probe_id = uuid4().hex
    # Verify Unicode and exact large-ID storage without JavaScript number coercion.
    payload = json.dumps({"label": "遊戲雷達 '連線測試'", "group_id64": "103582791475572379"}, ensure_ascii=False)
    checked_at = datetime.now(timezone.utc).isoformat()
    client.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (probe_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL)")
    operation_error = None
    cleanup_error = None
    try:
        # Retrying an ambiguous network failure must not create duplicate rows.
        client.execute(
            f"INSERT INTO {TABLE} (probe_id, checked_at, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(probe_id) DO UPDATE SET checked_at=excluded.checked_at, payload=excluded.payload",
            (probe_id, checked_at, payload),
        )
        result = client.execute(f"SELECT payload FROM {TABLE} WHERE probe_id = ?", (probe_id,))
        if result.get("rows") != [[{"type": "text", "value": payload}]]:
            raise CheckError("READBACK_MISMATCH", "The committed probe value did not match when read back.")
    except CheckError as error:
        operation_error = error
    finally:
        try:
            cleanup_client = client.for_cleanup()
            cleanup_client.execute(f"DELETE FROM {TABLE} WHERE probe_id = ?", (probe_id,))
            result = cleanup_client.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE probe_id = ?", (probe_id,))
            if result.get("rows") != [[{"type": "integer", "value": "0"}]]:
                raise CheckError("CLEANUP_FAILED", "The diagnostic row could not be confirmed removed.")
        except CheckError as error:
            cleanup_error = error
    if cleanup_error:
        raise CheckError(
            "CLEANUP_FAILED",
            "Diagnostic cleanup could not be verified; a test row may remain in _radar_connection_checks.",
            operation_error_code=operation_error.code if operation_error else "NONE",
            cleanup_error_code=cleanup_error.code,
        )
    if operation_error:
        raise operation_error
    return {"read": "passed", "write": "passed", "unicode_and_large_id": "passed", "cleanup": "passed"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-path", type=Path)
    args = parser.parse_args(argv)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "provider": "turso", "status": "failed"}
    try:
        url = os.environ.get("TURSO_DATABASE_URL", "")
        token = os.environ.get("TURSO_AUTH_TOKEN", "")
        if not url.strip() or not token.strip():
            raise CheckError("MISSING_SECRETS", "Set both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in this repository's Actions secrets.")
        report.update(run_probe(TursoProbeClient(url, token)))
        report["status"] = "passed"
    except CheckError as error:
        report.update(error_code=error.code, message=str(error), **error.details)
    except Exception:
        # Unexpected exceptions can contain headers/URLs; never print their text.
        report.update(error_code="UNEXPECTED_ERROR", message="The connection check encountered an unexpected error.")
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("## Turso connection check\n\n```json\n" + text + "\n```\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
