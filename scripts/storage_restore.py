"""Restore verified original snapshot bytes from portable local backups.

This command never contacts a provider or modifies the input database. It
publishes a complete output directory only after every selected snapshot has
passed the storage layer's manifest, object and chunk integrity checks.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import tempfile
import time

from radar_backend.storage import documents
from radar_backend.storage.sql import SqliteClient, StorageError


MANIFEST_NAME = "RESTORE_MANIFEST.json"
MAX_SQL_STATEMENT_BYTES = 512 * 1024
MAX_SQL_BYTES = 2 * 1024 * 1024 * 1024


def namespace_for_source(source_id: str) -> str:
    if (not isinstance(source_id, str) or not source_id or len(source_id) > 512
            or "\x00" in source_id):
        raise StorageError("INVALID_SOURCE_ID", "The backup contains an invalid source identifier.")
    try:
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise StorageError("INVALID_SOURCE_ID", "The backup contains an invalid source identifier.") from None
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", source_id).strip("-")[:48] or "source"
    return f"{slug}-{digest}"


def _relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise StorageError("UNSAFE_PATH", "A snapshot contains an unsafe file path.")
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or str(path) != value
            or any(part in {".", ".."} or ":" in part or part.endswith((".", " "))
                   or PureWindowsPath(part).is_reserved() for part in value.split("/"))):
        raise StorageError("UNSAFE_PATH", "A snapshot contains an unsafe file path.")
    return path.parts


def _no_symlinks(path):
    # Reject existing symlink components rather than resolving through them.
    for item in (path, *path.parents):
        if item.is_symlink():
            raise StorageError("UNSAFE_PATH", "Symlink paths are not accepted for local restoration.")


def _schema(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()


def _expected_schema():
    client = SqliteClient(":memory:")
    try:
        documents.initialize(client)
        return [tuple(row) for row in _schema(client.connection)]
    finally:
        client.close()


def _normalized_schema(rows):
    def normalize(sql):
        if sql is None:
            return None
        return re.sub(r"\s+", " ", re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", sql, flags=re.I)).strip().rstrip(";").lower()
    return [(kind, name, table, normalize(sql)) for kind, name, table, sql in rows]


def _import_authorizer(expected):
    tables = {name for kind, name, _, _ in expected if kind == "table"}
    indexes = {(name, table) for kind, name, table, _ in expected if kind == "index"}
    triggers = {(name, table) for kind, name, table, _ in expected if kind == "trigger"}

    def authorize(action, first, second, database, origin):
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if (second or "").lower() == "raise" else sqlite3.SQLITE_DENY
        if database != "main":
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_CREATE_TABLE and first in tables:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_CREATE_INDEX and (first, second) in indexes:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_REINDEX and first in {name for name, _ in indexes}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_CREATE_TRIGGER and (first, second) in triggers:
            return sqlite3.SQLITE_OK
        if action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            if first in tables or first in {"sqlite_master", "sqlite_schema"}:
                return sqlite3.SQLITE_OK
        # Includes ATTACH/DETACH, PRAGMA, virtual/temp tables and extensions.
        return sqlite3.SQLITE_DENY

    return authorize


def _import_sql_gzip(source: Path, destination: Path, expected):
    database = sqlite3.connect(destination)
    deadline = time.monotonic() + 120
    try:
        database.enable_load_extension(False)
        database.set_authorizer(_import_authorizer(expected))
        database.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        total, buffered = 0, bytearray()
        with gzip.open(source, "rb") as compressed:
            while True:
                line = compressed.readline(MAX_SQL_STATEMENT_BYTES + 1)
                if not line:
                    break
                total += len(line)
                buffered.extend(line)
                if (len(buffered) > MAX_SQL_STATEMENT_BYTES or total > MAX_SQL_BYTES
                        or time.monotonic() >= deadline):
                    raise StorageError("SQL_IMPORT_LIMIT", "The SQL backup exceeded a bounded import limit.")
                statement = buffered.decode("utf-8")
                if sqlite3.complete_statement(statement):
                    # Authorizer applies to every statement, including triggers.
                    # execute preserves the dump's explicit BEGIN/COMMIT;
                    # executescript would implicitly commit between records.
                    database.execute(statement)
                    buffered.clear()
            if buffered.strip():
                raise StorageError("INVALID_SQL_BACKUP", "The SQL backup ended with an incomplete statement.")
        if database.in_transaction:
            raise StorageError("INVALID_SQL_BACKUP", "The SQL backup did not finish its transaction.")
    except (sqlite3.Error, OSError, EOFError, UnicodeError):
        raise StorageError("INVALID_SQL_BACKUP", "The SQL backup was invalid or contained a forbidden operation.") from None
    finally:
        database.close()


def _copy_sqlite_readonly(source: Path, destination: Path):
    original, copy = None, None
    try:
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)
        original.execute("PRAGMA query_only=ON")
        copy = sqlite3.connect(destination)
        deadline = time.monotonic() + 120

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise StorageError("SQL_IMPORT_LIMIT", "The SQLite snapshot copy exceeded its time budget.")

        original.backup(copy, pages=1024, progress=progress)
    except (sqlite3.Error, OSError):
        raise StorageError("INVALID_SQLITE_BACKUP", "The input SQLite backup could not be read.") from None
    finally:
        if copy is not None:
            copy.close()
        if original is not None:
            original.close()


def _rename_no_replace(source: Path, destination: Path):
    """Atomically publish without POSIX rename's empty-directory overwrite."""
    if os.name == "nt":
        os.rename(source, destination)  # Windows rename refuses any existing target.
        return
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes, target_bytes = os.fsencode(source), os.fsencode(destination)
    if hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, target_bytes, 1)  # RENAME_NOREPLACE
    elif hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, target_bytes, 4)  # macOS RENAME_EXCL
    else:
        raise StorageError("ATOMIC_RENAME_UNAVAILABLE", "This platform cannot publish without risking an existing output directory.")
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise StorageError("OUTPUT_EXISTS", "Refusing to overwrite an existing restore destination.")
        raise StorageError("OUTPUT_PUBLICATION_FAILED", "The verified restore directory could not be published.")


def restore(*, output, sqlite_path=None, sql_gzip=None, source_id=None) -> dict:
    if (sqlite_path is None) == (sql_gzip is None):
        raise StorageError("INVALID_INPUT", "Specify exactly one SQL gzip or SQLite backup.")
    target = Path(os.path.abspath(output))
    source = Path(os.path.abspath(sqlite_path if sqlite_path is not None else sql_gzip))
    _no_symlinks(target)
    _no_symlinks(source)
    if os.path.lexists(target):
        raise StorageError("OUTPUT_EXISTS", "Refusing to overwrite an existing restore destination.")
    if not target.parent.is_dir():
        raise StorageError("INVALID_OUTPUT", "The restore output's parent directory must already exist.")
    if not source.is_file():
        raise StorageError("INPUT_NOT_FOUND", "The specified backup file does not exist.")
    if source_id is not None:
        namespace_for_source(source_id)
    expected = _expected_schema()
    with tempfile.TemporaryDirectory(prefix=".radar-restore-", dir=target.parent) as temporary:
        temporary = Path(temporary)
        working_db = temporary / "working.sqlite"
        if sql_gzip is not None:
            _import_sql_gzip(source, working_db, expected)
        else:
            _copy_sqlite_readonly(source, working_db)
        client = SqliteClient(working_db)
        try:
            client.connection.enable_load_extension(False)
            if _normalized_schema(_schema(client.connection)) != _normalized_schema(expected):
                raise StorageError("INVALID_STORAGE_SCHEMA", "The backup does not contain the expected radar storage schema.")
            client.connection.execute("PRAGMA query_only=ON")
            check = client.execute("PRAGMA integrity_check")
            if check != [{"integrity_check": "ok"}]:
                raise StorageError("INVALID_SQLITE_BACKUP", "The SQLite backup failed integrity checking.")
            sources = client.execute("SELECT source_id FROM radar_sources ORDER BY source_id")
            identifiers = [row["source_id"] for row in sources]
            if source_id is not None:
                if source_id not in identifiers:
                    raise StorageError("SOURCE_NOT_FOUND", "The requested source is absent from this backup.")
                identifiers = [source_id]
            if not identifiers:
                raise StorageError("EMPTY_BACKUP", "The backup contains no published source snapshots.")
            stage = temporary / "restored"
            stage.mkdir()
            manifest = {"format": "radar-raw-restore-v1",
                        "restored_at": datetime.now(timezone.utc).isoformat(),
                        "sources": [], "total_files": 0, "total_bytes": 0}
            namespaces = set()
            for identifier in identifiers:
                namespace = namespace_for_source(identifier)
                if namespace in namespaces:
                    raise StorageError("NAMESPACE_CONFLICT", "Source namespaces collided; no output was published.")
                namespaces.add(namespace)
                head, files = documents.read_snapshot(client, identifier)
                directory = stage / namespace
                directory.mkdir()
                entry = {key: head[key] for key in (
                    "source_id", "revision", "generation", "manifest_digest", "captured_at")}
                entry.update(namespace=namespace, files=[])
                for name, raw in sorted(files.items()):
                    destination = directory.joinpath(*_relative_path(name))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as stream:
                        stream.write(raw)
                    entry["files"].append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw)})
                    manifest["total_files"] += 1
                    manifest["total_bytes"] += len(raw)
                manifest["sources"].append(entry)
            (stage / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise StorageError("RESTORE_FAILED", "The backup could not be restored completely; no output was published.") from None
        finally:
            client.close()
        _no_symlinks(target)
        if os.path.lexists(target):
            raise StorageError("OUTPUT_EXISTS", "Refusing to overwrite an existing restore destination.")
        _rename_no_replace(stage, target)
        return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--sql-gzip", type=Path)
    inputs.add_argument("--sqlite", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-id")
    args = parser.parse_args(argv)
    try:
        manifest = restore(output=args.output, sqlite_path=args.sqlite, sql_gzip=args.sql_gzip, source_id=args.source_id)
        report = {"status": "passed", "sources": len(manifest["sources"]),
                  "files": manifest["total_files"], "raw_bytes": manifest["total_bytes"]}
    except StorageError as error:
        report = {"status": "failed", "error_code": error.code, "message": str(error)}
    except Exception:
        report = {"status": "failed", "error_code": "RESTORE_FAILED", "message": "Restoration failed; no complete output was published."}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
