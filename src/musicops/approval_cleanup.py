from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable


class CleanupRefused(RuntimeError):
    pass


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _placeholders(values: Iterable[str]) -> str:
    return ",".join("?" for _ in values)


def _raw_matches(path: Path, identifiers: list[str]) -> int:
    matches = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if not candidate.is_file():
            continue
        payload = candidate.read_bytes()
        matches += sum(payload.count(identifier.encode("utf-8")) for identifier in identifiers)
    return matches


def inspect_database(
    database: str | Path,
    identifiers: Iterable[str],
    *,
    require_all: bool = False,
    require_no_active: bool = False,
    allow_missing_approvals_table: bool = False,
) -> dict[str, Any]:
    path = Path(database)
    ids = sorted(set(identifiers))
    if not ids or any(not value.startswith("approval_") for value in ids):
        raise CleanupRefused("approval_identifiers_invalid")
    if not path.is_file():
        raise CleanupRefused("database_missing")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        active = 0
        if "operations" in tables and {"status"} <= _table_columns(connection, "operations"):
            active = int(
                connection.execute(
                    "SELECT count(*) FROM operations WHERE status IN ('queued','running')"
                ).fetchone()[0]
            )
        if require_no_active and active:
            raise CleanupRefused("active_operations_present")
        if "approvals" not in tables:
            if not allow_missing_approvals_table:
                raise CleanupRefused("approvals_table_missing")
            return {
                "database": str(path),
                "approval_table": False,
                "target_count": 0,
                "active_operations": active,
                "operation_references": 0,
                "raw_matches": _raw_matches(path, ids),
            }
        approval_columns = _table_columns(connection, "approvals")
        if not {"id", "consumed_by_operation_id"} <= approval_columns:
            raise CleanupRefused("approval_schema_unsupported")
        placeholders = _placeholders(ids)
        rows = connection.execute(
            f"SELECT id, consumed_by_operation_id FROM approvals WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        ).fetchall()
        if require_all and len(rows) != len(ids):
            raise CleanupRefused("target_approval_set_incomplete")
        if any(row[1] is not None for row in rows):
            raise CleanupRefused("target_approval_consumed")
        operation_references = 0
        if "operations" in tables and "approval_id" in _table_columns(connection, "operations"):
            operation_references = int(
                connection.execute(
                    f"SELECT count(*) FROM operations WHERE approval_id IN ({placeholders})",  # noqa: S608
                    ids,
                ).fetchone()[0]
            )
        if operation_references:
            raise CleanupRefused("target_approval_has_operation_reference")
        return {
            "database": str(path),
            "approval_table": True,
            "target_count": len(rows),
            "active_operations": active,
            "operation_references": operation_references,
            "raw_matches": _raw_matches(path, ids),
        }
    finally:
        connection.close()


def purge_database(
    database: str | Path,
    identifiers: Iterable[str],
    *,
    require_all: bool = False,
    require_no_active: bool = False,
    allow_missing_approvals_table: bool = False,
) -> dict[str, Any]:
    path = Path(database)
    ids = sorted(set(identifiers))
    before = inspect_database(
        path,
        ids,
        require_all=require_all,
        require_no_active=require_no_active,
        allow_missing_approvals_table=allow_missing_approvals_table,
    )
    original_mode = path.stat().st_mode & 0o777
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("BEGIN IMMEDIATE")
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if require_no_active and "operations" in tables:
            active = int(
                connection.execute(
                    "SELECT count(*) FROM operations WHERE status IN ('queued','running')"
                ).fetchone()[0]
            )
            if active:
                connection.execute("ROLLBACK")
                raise CleanupRefused("active_operations_present")
        deleted = 0
        if "approvals" in tables:
            placeholders = _placeholders(ids)
            rows = connection.execute(
                f"SELECT id, consumed_by_operation_id FROM approvals WHERE id IN ({placeholders})",  # noqa: S608
                ids,
            ).fetchall()
            if require_all and len(rows) != len(ids):
                connection.execute("ROLLBACK")
                raise CleanupRefused("target_approval_set_incomplete")
            if any(row[1] is not None for row in rows):
                connection.execute("ROLLBACK")
                raise CleanupRefused("target_approval_consumed")
            if "operations" in tables and "approval_id" in _table_columns(connection, "operations"):
                references = int(
                    connection.execute(
                        f"SELECT count(*) FROM operations WHERE approval_id IN ({placeholders})",  # noqa: S608
                        ids,
                    ).fetchone()[0]
                )
                if references:
                    connection.execute("ROLLBACK")
                    raise CleanupRefused("target_approval_has_operation_reference")
            cursor = connection.execute(
                f"DELETE FROM approvals WHERE id IN ({placeholders})",  # noqa: S608
                ids,
            )
            deleted = int(cursor.rowcount)
        elif not allow_missing_approvals_table:
            connection.execute("ROLLBACK")
            raise CleanupRefused("approvals_table_missing")
        connection.execute("COMMIT")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
        os.chmod(path, original_mode)
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    after = inspect_database(
        path,
        ids,
        require_all=False,
        require_no_active=require_no_active,
        allow_missing_approvals_table=allow_missing_approvals_table,
    )
    if after["target_count"] or after["operation_references"] or after["raw_matches"]:
        raise CleanupRefused("approval_identifier_residual_detected")
    with sqlite3.connect(path) as verification:
        quick_check = verification.execute("PRAGMA quick_check").fetchone()[0]
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    return {
        "database": str(path),
        "deleted": deleted,
        "active_operations": after["active_operations"],
        "remaining_targets": after["target_count"],
        "raw_matches": after["raw_matches"],
        "quick_check": quick_check,
        "before_raw_matches": before["raw_matches"],
    }


def _read_ids(path: str) -> list[str]:
    if path == "-":
        import sys

        lines = sys.stdin.read().splitlines()
    else:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "apply"))
    parser.add_argument("--database", action="append", required=True)
    parser.add_argument("--ids-file", required=True)
    parser.add_argument("--require-all", action="store_true")
    parser.add_argument("--require-no-active", action="store_true")
    parser.add_argument("--allow-missing-approvals-table", action="store_true")
    args = parser.parse_args()
    ids = _read_ids(args.ids_file)
    function = inspect_database if args.mode == "preflight" else purge_database
    results = [
        function(
            database,
            ids,
            require_all=args.require_all,
            require_no_active=args.require_no_active,
            allow_missing_approvals_table=args.allow_missing_approvals_table,
        )
        for database in args.database
    ]
    print(json.dumps({"ok": True, "database_count": len(results), "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except CleanupRefused as exc:
        print(json.dumps({"ok": False, "detail": str(exc)}, sort_keys=True))
        raise SystemExit(2) from exc
