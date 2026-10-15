"""SQL transports with strict wire validation and credential-free failures.

Callers must make writes retry-safe, including writes passed to execute(). A
lost response may follow a committed write. A batch is a transport convenience,
not a transaction: an error can leave earlier statements committed. Use one SQL
statement and database triggers for atomic compare-and-swap/outbox operations.
Each SQL string is one statement; CREATE TRIGGER bodies may contain semicolons.
"""
from __future__ import annotations

from collections.abc import Sequence
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


Param = str | int | None
Statement = tuple[str, Sequence[Param]]
MAX_BATCH_STATEMENTS = 25
MAX_REQUEST_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_SAFE_JSON_INT = 2 ** 53 - 1
MIN_SQLITE_INT = -(2 ** 63)
MAX_SQLITE_INT = 2 ** 63 - 1


class StorageError(RuntimeError):
    """Safe local code/message; never wrap raw provider or transport errors."""

    def __init__(self, code: str, message: str = "Storage operation failed."):
        super().__init__(message)
        self.code = code


class SqlClient(Protocol):
    def execute(self, sql: str, params: Sequence[Param] = ()) -> list[dict]: ...

    def batch(self, statements: list[Statement]) -> list[list[dict]]: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _statement(sql, params, *, d1=False) -> Statement:
    if not isinstance(sql, str) or not sql.strip():
        raise StorageError("INVALID_STATEMENT", "A nonempty SQL statement is required.")
    if not isinstance(params, Sequence) or isinstance(params, (str, bytes, bytearray)):
        raise StorageError("INVALID_PARAMS", "SQL parameters must be a sequence of strings, integers or nulls.")
    normalized = []
    for value in params:
        if value is None or type(value) is str:
            normalized.append(value)
        elif type(value) is int and MIN_SQLITE_INT <= value <= MAX_SQLITE_INT:
            if d1 and abs(value) > MAX_SAFE_JSON_INT:
                raise StorageError("INVALID_PARAMS", "Large D1 identifiers must be passed as strings to preserve precision.")
            normalized.append(value)
        else:
            raise StorageError("INVALID_PARAMS", "SQL parameters require strings, signed 64-bit integers or nulls; floats and booleans are not coerced.")
    return sql, normalized


def _statements(statements, *, d1=False) -> list[Statement]:
    if not isinstance(statements, (list, tuple)) or len(statements) > MAX_BATCH_STATEMENTS:
        raise StorageError("BATCH_LIMIT", "Split SQL batches into at most 25 statements.")
    normalized = []
    for statement in statements:
        if not isinstance(statement, (tuple, list)) or len(statement) != 2:
            raise StorageError("INVALID_STATEMENT", "Each batch entry requires SQL and its parameters.")
        normalized.append(_statement(*statement, d1=d1))
    return normalized


def _encode(payload) -> bytes:
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise StorageError("INVALID_PARAMS", "SQL request contains an unsupported value.") from None
    if len(body) >= MAX_REQUEST_BYTES:
        raise StorageError("REQUEST_TOO_LARGE", "Split SQL requests below 512 KiB of encoded JSON.")
    return body


def _token(value):
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise StorageError("INVALID_CONFIG", "A single-line database token is required.")
    value = value.strip()
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise StorageError("INVALID_CONFIG", "A nonempty ASCII database token is required.")
    return value


def _turso_endpoint(value):
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname or ""
        valid = (parsed.scheme in {"https", "libsql"}
                 and re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.turso\.io", host)
                 and parsed.port in {None, 443}
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise StorageError("INVALID_CONFIG", "A credential-free HTTPS or libSQL Turso database URL is required.")
    return f"https://{host}/v2/pipeline"


def _d1_endpoint(account_id, database_id):
    if (not isinstance(account_id, str) or not isinstance(database_id, str)
            or re.fullmatch(r"[0-9a-fA-F]{32}", account_id.strip()) is None
            or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", database_id.strip()) is None):
        raise StorageError("INVALID_CONFIG", "D1 requires a 32-character hexadecimal account ID and a database UUID.")
    return ("https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id.strip().lower()}/d1/database/{database_id.strip().lower()}/query")


def _retry_after(value):
    """Only a delay leaves this function; never echo server header text."""
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value) if re.fullmatch(r"\d+(?:\.\d+)?", value.strip()) else (
            parsedate_to_datetime(value).timestamp() - time.time())
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (ValueError, TypeError, OverflowError):
        return None


class _HttpClient:
    def __init__(self, endpoint, token, *, opener=None, sleep=time.sleep,
                 clock=time.monotonic, budget_seconds=600):
        if (type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds)
                or budget_seconds < 0):
            raise StorageError("INVALID_CONFIG", "A finite, nonnegative storage time budget is required.")
        self.endpoint = endpoint
        self._token = _token(token)
        self.opener = opener if opener is not None else build_opener(NoRedirect())
        self.sleep, self.clock = sleep, clock
        self.deadline = clock() + budget_seconds

    def _send(self, payload):
        body = _encode(payload)
        for attempt in range(3):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise StorageError("TIME_BUDGET_EXCEEDED", "The storage time budget was exhausted.")
            delay = 2 ** attempt
            request = Request(self.endpoint, data=body, method="POST", headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json", "Accept": "application/json",
            })
            try:
                with self.opener.open(request, timeout=min(15, remaining)) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise StorageError("INVALID_RESPONSE", "Storage response exceeded the bounded response size.")
                if self.clock() >= self.deadline:
                    raise StorageError("TIME_BUDGET_EXCEEDED", "The storage time budget was exhausted.")
                try:
                    return json.loads(raw)
                except (ValueError, TypeError, UnicodeError):
                    raise StorageError("INVALID_RESPONSE", "Storage returned an invalid JSON response.") from None
            except HTTPError as error:
                status = error.code
                retry_after = _retry_after(error.headers.get("Retry-After")) if error.headers else None
                error.close()
                if status in {401, 403}:
                    raise StorageError("AUTHORIZATION_FAILED", "Storage authentication or permissions were rejected.") from None
                if (status != 429 and not 500 <= status <= 599) or attempt == 2:
                    raise StorageError("HTTP_ERROR", f"Storage request failed with HTTP {status}.") from None
                if retry_after is not None:
                    delay = max(delay, retry_after)
            except (URLError, TimeoutError, OSError, HTTPException):
                if attempt == 2:
                    raise StorageError("NETWORK_ERROR", "Storage request failed after three bounded attempts.") from None
            remaining = self.deadline - self.clock()
            if delay >= remaining:
                raise StorageError("TIME_BUDGET_EXCEEDED", "Storage retry delay exceeds the remaining time budget.")
            self.sleep(delay)
        raise StorageError("NETWORK_ERROR", "Storage request did not complete.")


def _turso_value(value):
    if not isinstance(value, dict):
        raise ValueError
    kind = value.get("type")
    if kind == "null":
        if value.get("value") is not None:
            raise ValueError
        return None
    data = value.get("value")
    if kind == "text" and type(data) is str:
        return data
    if kind == "integer" and type(data) is str and re.fullmatch(r"-?\d+", data, flags=re.ASCII):
        number = int(data)
        if MIN_SQLITE_INT <= number <= MAX_SQLITE_INT:
            return number
    if kind == "float" and type(data) in (int, float) and math.isfinite(data) and abs(data) <= MAX_SAFE_JSON_INT:
        return float(data)
    raise ValueError


class TursoClient(_HttpClient):
    def __init__(self, url, token, **kwargs):
        super().__init__(_turso_endpoint(url), token, **kwargs)

    @classmethod
    def from_env(cls, **kwargs):
        return cls(os.environ.get("TURSO_DATABASE_URL", ""), os.environ.get("TURSO_AUTH_TOKEN", ""), **kwargs)

    def execute(self, sql: str, params: Sequence[Param] = ()) -> list[dict]:
        return self.batch([(sql, params)])[0]

    def batch(self, statements: list[Statement]) -> list[list[dict]]:
        statements = _statements(statements)
        if not statements:
            return []
        requests = []
        for sql, params in statements:
            args = [{"type": "null"} if value is None else
                    {"type": "integer", "value": str(value)} if type(value) is int else
                    {"type": "text", "value": value} for value in params]
            requests.append({"type": "execute", "stmt": {"sql": sql, "args": args, "want_rows": True}})
        data = self._send({"requests": [*requests, {"type": "close"}]})
        try:
            results = data["results"]
            if not isinstance(results, list) or len(results) != len(statements) + 1:
                raise ValueError
            decoded = []
            for index, item in enumerate(results):
                if item.get("type") == "error":
                    raise StorageError("SQL_ERROR", "Turso rejected a SQL operation; a batch may be partially committed.")
                expected = "close" if index == len(statements) else "execute"
                if item["type"] != "ok" or item["response"]["type"] != expected:
                    raise ValueError
                if expected == "close":
                    continue
                result = item["response"]["result"]
                columns, rows = result["cols"], result["rows"]
                if not isinstance(columns, list) or not isinstance(rows, list):
                    raise ValueError
                names = [column["name"] for column in columns]
                if any(type(name) is not str for name in names) or len(set(names)) != len(names):
                    raise ValueError
                output = []
                for row in rows:
                    if not isinstance(row, list) or len(row) != len(names):
                        raise ValueError
                    output.append(dict(zip(names, (_turso_value(value) for value in row))))
                decoded.append(output)
            return decoded
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise StorageError("INVALID_RESPONSE", "Turso returned an incomplete or unsupported SQL response.") from None


def _d1_value(value):
    if value is None or type(value) is str:
        return True
    if type(value) is int:
        return abs(value) <= MAX_SAFE_JSON_INT
    if type(value) is float:
        return math.isfinite(value) and abs(value) <= MAX_SAFE_JSON_INT
    return False


class D1Client(_HttpClient):
    def __init__(self, account_id, database_id, token, **kwargs):
        super().__init__(_d1_endpoint(account_id, database_id), token, **kwargs)

    @classmethod
    def from_env(cls, **kwargs):
        return cls(os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
                   os.environ.get("CLOUDFLARE_D1_DATABASE_ID", ""),
                   os.environ.get("CLOUDFLARE_D1_API_TOKEN", ""), **kwargs)

    def execute(self, sql: str, params: Sequence[Param] = ()) -> list[dict]:
        sql, params = _statement(sql, params, d1=True)
        return self._results(self._send({"sql": sql, "params": params}), 1)[0]

    def batch(self, statements: list[Statement]) -> list[list[dict]]:
        statements = _statements(statements, d1=True)
        if not statements:
            return []
        data = self._send({"batch": [{"sql": sql, "params": params} for sql, params in statements]})
        return self._results(data, len(statements))

    @staticmethod
    def _results(data, count):
        try:
            if not isinstance(data, dict):
                raise ValueError
            if data.get("success") is False or data.get("errors"):
                raise StorageError("API_ERROR", "Cloudflare rejected the D1 SQL request; no complete result was acknowledged.")
            results = data["result"]
            if data.get("success") is not True or not isinstance(results, list) or len(results) != count:
                raise ValueError
            decoded = []
            for result in results:
                if result.get("success") is False or result.get("error") or result.get("errors"):
                    raise StorageError("SQL_ERROR", "D1 rejected a SQL operation; a batch may be partially committed.")
                rows = result["results"]
                if result.get("success") is not True or not isinstance(rows, list):
                    raise ValueError
                for row in rows:
                    if (not isinstance(row, dict) or any(type(name) is not str for name in row)
                            or any(not _d1_value(value) for value in row.values())):
                        raise ValueError
                decoded.append(rows)
            return decoded
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise StorageError("INVALID_RESPONSE", "D1 returned an incomplete or unsupported SQL response.") from None


class SqliteClient:
    """Local parity adapter. Each statement autocommits, including triggers."""

    def __init__(self, path: str | Path = ":memory:"):
        try:
            self.connection = sqlite3.connect(path, isolation_level=None, timeout=15)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
        except (sqlite3.Error, TypeError, OSError):
            raise StorageError("INVALID_CONFIG", "The SQLite database could not be opened.") from None

    def execute(self, sql: str, params: Sequence[Param] = ()) -> list[dict]:
        sql, params = _statement(sql, params)
        _encode({"sql": sql, "params": params})
        return self._execute(sql, params)

    def _execute(self, sql, params):
        cursor = None
        try:
            cursor = self.connection.execute(sql, params)
            names = [column[0] for column in cursor.description or ()]
            if len(set(names)) != len(names):
                raise StorageError("INVALID_RESPONSE", "SQLite result column names must be unique.")
            rows = [dict(row) for row in cursor.fetchall()]
            return rows
        except (sqlite3.Error, OverflowError, UnicodeError):
            raise StorageError("SQL_ERROR", "SQLite rejected a SQL operation; a batch may be partially committed.") from None
        finally:
            if cursor is not None:
                cursor.close()

    def batch(self, statements: list[Statement]) -> list[list[dict]]:
        statements = _statements(statements)
        _encode({"batch": [{"sql": sql, "params": params} for sql, params in statements]})
        return [self._execute(sql, params) for sql, params in statements]

    def close(self):
        self.connection.close()
