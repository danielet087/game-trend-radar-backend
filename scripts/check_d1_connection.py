"""Verify Cloudflare D1 using Actions secrets without touching game data.

The dedicated diagnostic table remains empty after a successful check. Every
run commits, reads and removes only its own uniquely identified test row.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
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
        # Never forward the API token to a response-supplied location.
        return None


def endpoint_from_ids(account_id: str, database_id: str) -> str:
    account_id = account_id.strip()
    database_id = database_id.strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", account_id) is None:
        raise CheckError("INVALID_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID must contain exactly 32 hexadecimal characters.")
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", database_id) is None:
        raise CheckError("INVALID_DATABASE_ID", "CLOUDFLARE_D1_DATABASE_ID must be a canonical UUID.")
    return ("https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id.lower()}/d1/database/{database_id.lower()}/query")


class D1ProbeClient:
    def __init__(self, account_id: str, database_id: str, token: str, *, opener=None,
                 sleep=time.sleep, clock=time.monotonic, budget_seconds: float = 120):
        self.endpoint = endpoint_from_ids(account_id, database_id)
        self.account_id = account_id.strip()
        self.database_id = database_id.strip()
        if "\r" in token or "\n" in token:
            raise CheckError("INVALID_AUTH_TOKEN", "CLOUDFLARE_D1_API_TOKEN must be a nonempty single-line token.")
        token = token.strip()
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise CheckError("INVALID_AUTH_TOKEN", "CLOUDFLARE_D1_API_TOKEN must be a nonempty single-line token.")
        self.token = token
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep
        self.clock = clock
        self.deadline = clock() + budget_seconds

    def for_cleanup(self):
        # A lost write response may consume the main budget after committing.
        # Cleanup must retain its own short opportunity to remove that row.
        return D1ProbeClient(
            self.account_id, self.database_id, self.token,
            opener=self.opener, sleep=self.sleep, clock=self.clock, budget_seconds=30,
        )

    def execute(self, sql: str, args: tuple[str, ...] = ()) -> dict:
        body = json.dumps({"sql": sql, "params": list(args)}).encode("utf-8")
        for attempt in range(3):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise CheckError("TIME_BUDGET_EXCEEDED", "The D1 check exceeded its bounded time budget.")
            request = Request(self.endpoint, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with self.opener.open(request, timeout=min(15, remaining)) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise CheckError("INVALID_RESPONSE", "D1 response exceeded the diagnostic size limit.")
                if self.clock() >= self.deadline:
                    raise CheckError("TIME_BUDGET_EXCEEDED", "The D1 check exceeded its bounded time budget.")
                return self._result(raw)
            except HTTPError as error:
                status = error.code
                error.close()
                if status in {401, 403}:
                    raise CheckError("AUTHORIZATION_FAILED", "Cloudflare rejected the credentials or D1 permissions (HTTP 401/403).") from None
                if status not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                    raise CheckError("HTTP_ERROR", f"Cloudflare D1 request failed with HTTP {status}.") from None
            except (URLError, TimeoutError, OSError, HTTPException):
                if attempt == 2:
                    raise CheckError("NETWORK_ERROR", "Could not reach Cloudflare D1 after three bounded attempts.") from None
            self.sleep(min(2 ** attempt, max(0, self.deadline - self.clock())))
        raise CheckError("NETWORK_ERROR", "Cloudflare D1 request did not complete.")

    @staticmethod
    def _result(raw: bytes) -> dict:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            if data.get("success") is False or data.get("errors"):
                # Provider errors can include SQL values or private identifiers.
                raise CheckError("API_ERROR", "Cloudflare rejected the D1 query; verify the database and token permissions.")
            if data.get("success") is not True:
                raise ValueError
            results = data["result"]
            if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                raise ValueError
            result = results[0]
            if result.get("success") is False or result.get("error") or result.get("errors"):
                raise CheckError("SQL_ERROR", "D1 could not execute the diagnostic SQL; verify read/write permissions.")
            if result.get("success") is not True or not isinstance(result.get("results"), list):
                raise ValueError
            if any(not isinstance(row, dict) for row in result["results"]):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CheckError("INVALID_RESPONSE", "Cloudflare D1 returned an unexpected response format.") from None


def run_probe(client: D1ProbeClient) -> dict:
    probe_id = uuid4().hex
    # Preserve exact Unicode and GroupID64 text across bound SQL parameters.
    payload = json.dumps({"label": "遊戲雷達 '連線測試'", "group_id64": "103582791475572379"}, ensure_ascii=False)
    checked_at = datetime.now(timezone.utc).isoformat()
    client.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (probe_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL)")
    operation_error = None
    cleanup_error = None
    try:
        client.execute(
            f"INSERT INTO {TABLE} (probe_id, checked_at, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(probe_id) DO UPDATE SET checked_at=excluded.checked_at, payload=excluded.payload",
            (probe_id, checked_at, payload),
        )
        result = client.execute(f"SELECT payload FROM {TABLE} WHERE probe_id = ?", (probe_id,))
        if result["results"] != [{"payload": payload}]:
            raise CheckError("READBACK_MISMATCH", "The committed D1 probe value did not match when read back.")
    except CheckError as error:
        operation_error = error
    except Exception:
        operation_error = CheckError("UNEXPECTED_ERROR", "The D1 diagnostic operation encountered an unexpected error.")
    finally:
        try:
            cleanup = client.for_cleanup()
            cleanup.execute(f"DELETE FROM {TABLE} WHERE probe_id = ?", (probe_id,))
            result = cleanup.execute(f"SELECT COUNT(*) AS remaining FROM {TABLE} WHERE probe_id = ?", (probe_id,))
            rows = result["results"]
            if (rows != [{"remaining": 0}] or type(rows[0]["remaining"]) is not int):
                raise CheckError("CLEANUP_FAILED", "The diagnostic D1 row could not be confirmed removed.")
        except CheckError as error:
            cleanup_error = error
        except Exception:
            cleanup_error = CheckError("UNEXPECTED_ERROR", "The D1 cleanup encountered an unexpected error.")
    if cleanup_error:
        raise CheckError(
            "CLEANUP_FAILED",
            "D1 cleanup could not be verified; a test row may remain in _radar_connection_checks.",
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
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "provider": "cloudflare_d1", "status": "failed"}
    try:
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        database_id = os.environ.get("CLOUDFLARE_D1_DATABASE_ID", "")
        token = os.environ.get("CLOUDFLARE_D1_API_TOKEN", "")
        if not all(value.strip() for value in (account_id, database_id, token)):
            raise CheckError("MISSING_SECRETS", "Set CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_D1_DATABASE_ID and CLOUDFLARE_D1_API_TOKEN in this repository's Actions secrets.")
        report.update(run_probe(D1ProbeClient(account_id, database_id, token)))
        report["status"] = "passed"
    except CheckError as error:
        report.update(error_code=error.code, message=str(error), **error.details)
    except Exception:
        report.update(error_code="UNEXPECTED_ERROR", message="The D1 connection check encountered an unexpected error.")
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("## Cloudflare D1 connection check\n\n```json\n" + text + "\n```\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
