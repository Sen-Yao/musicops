from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from musicops.operation_types import OperationStatus


DEFAULT_PAYLOAD_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_TEMPORARY_TTL_SECONDS = 7 * 24 * 60 * 60
NEVER_EXPIRES_AT = 253402300799.0
LARGE_VALUE_KEYS = {
    "audio_base64",
    "canonical_lrc",
    "cover_base64",
    "cover_data",
    "data_uri",
    "lyrics",
    "lyrics_text",
    "raw_response",
    "transcript",
    "translated_lrc",
    "resolved_canonical_lrc",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value is not None else default


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _summarize_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= 500:
            return value
        encoded = value.encode("utf-8")
        return {
            "omitted": True,
            "length": len(value),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    if isinstance(value, dict):
        return {
            key: (
                {
                    "omitted": True,
                    "length": len(item) if isinstance(item, str) else None,
                    "sha256": hashlib.sha256(str(item).encode("utf-8")).hexdigest(),
                }
                if key in LARGE_VALUE_KEYS and isinstance(item, str) and item not in (None, "")
                else _summarize_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_summarize_value(item) for item in value]
    return value


@dataclass(frozen=True)
class OperationRecord:
    id: str
    action: str
    status: str
    input: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    request_id: str
    approval_id: str | None
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: float | None
    payload_expires_at: float
    created_at: float
    updated_at: float
    started_at: float | None
    completed_at: float | None
    mutation_plan: dict[str, Any] | None = None

    def to_dict(self, *, full: bool = False) -> dict[str, Any]:
        payload = {
            "operation_id": self.id,
            "action": self.action,
            "status": self.status,
            "request_id": self.request_id,
            "approval_id": self.approval_id,
            "mutation_plan": self.mutation_plan if full else _summarize_value(self.mutation_plan),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input": self.input if full else _summarize_value(self.input),
            "result": self.result if full else _summarize_value(self.result),
            "error": self.error,
        }
        if full:
            payload["lease_owner"] = self.lease_owner
            payload["lease_expires_at"] = self.lease_expires_at
            payload["payload_expires_at"] = self.payload_expires_at
        return payload


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    action: str
    request_id: str
    intent_sha256: str
    action_schema_digest: str
    action_contract_digest: str
    input: dict[str, Any]
    preview: dict[str, Any]
    expires_at: float | None
    created_at: float
    consumed_by_operation_id: str | None
    actor: str | None
    reason: str | None
    approved_at: float | None
    payload_expires_at: float

    def to_dict(self, *, include_input: bool = False, full: bool = False) -> dict[str, Any]:
        payload = {
            "approval_id": self.id,
            "action": self.action,
            "request_id": self.request_id,
            "intent_sha256": self.intent_sha256,
            "action_schema_digest": self.action_schema_digest,
            "action_contract_digest": self.action_contract_digest,
            "preview": self.preview if full else _summarize_value(self.preview),
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "consumed": self.consumed_by_operation_id is not None,
            "operation_id": self.consumed_by_operation_id,
            "actor": self.actor,
            "reason": self.reason,
            "approved_at": self.approved_at,
        }
        if include_input:
            payload["input"] = self.input if full else _summarize_value(self.input)
        return payload


@dataclass(frozen=True)
class TemporaryFileRecord:
    id: str
    kind: str
    path: str
    sha256: str
    size: int
    media_type: str
    source_operation_id: str
    metadata: dict[str, Any]
    expires_at: float
    created_at: float
    deleted_at: float | None

    def to_dict(self, *, include_path: bool = False) -> dict[str, Any]:
        payload = {
            "temporary_file_id": self.id,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
            "source_operation_id": self.source_operation_id,
            "metadata": self.metadata,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "available": self.deleted_at is None,
            "deleted_at": self.deleted_at,
        }
        if include_path:
            payload["path"] = self.path
        return payload


class RequestConflict(ValueError):
    pass


class ApprovalError(ValueError):
    pass


class MusicOpsStore:
    """Durable execution records for independent MusicOps actions."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        payload_ttl_seconds: int = DEFAULT_PAYLOAD_TTL_SECONDS,
        temporary_ttl_seconds: int = DEFAULT_TEMPORARY_TTL_SECONDS,
    ):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.payload_ttl_seconds = max(60, int(payload_ttl_seconds))
        self.temporary_ttl_seconds = max(60, int(temporary_ttl_seconds))
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        return con

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', '2');

                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','uncertain','cancelled')),
                    input_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    request_id TEXT NOT NULL UNIQUE,
                    approval_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    payload_expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS operations_status_created_idx ON operations(status, created_at);
                CREATE INDEX IF NOT EXISTS operations_action_created_idx ON operations(action, created_at);

                CREATE TABLE IF NOT EXISTS operation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operations(id)
                );
                CREATE INDEX IF NOT EXISTS operation_events_operation_type_idx
                    ON operation_events(operation_id, event_type, id);

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    intent_sha256 TEXT NOT NULL,
                    action_schema_digest TEXT NOT NULL DEFAULT '',
                    action_contract_digest TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    consumed_by_operation_id TEXT,
                    actor TEXT,
                    reason TEXT,
                    approved_at REAL,
                    payload_expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS temporary_files (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    source_operation_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    deleted_at REAL,
                    FOREIGN KEY(source_operation_id) REFERENCES operations(id)
                );
                CREATE INDEX IF NOT EXISTS temporary_files_expiry_idx ON temporary_files(expires_at, deleted_at);

                CREATE TABLE IF NOT EXISTS resource_locks (
                    resource_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('read','write')),
                    lease_expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(resource_key, operation_id),
                    FOREIGN KEY(operation_id) REFERENCES operations(id)
                );

                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    heartbeat_at REAL NOT NULL,
                    started_at REAL NOT NULL
                );
                """
            )
            operation_columns = {row["name"] for row in con.execute("PRAGMA table_info(operations)")}
            if "mutation_plan_json" not in operation_columns:
                con.execute("ALTER TABLE operations ADD COLUMN mutation_plan_json TEXT")
            approval_columns = {row["name"] for row in con.execute("PRAGMA table_info(approvals)")}
            if "request_id" not in approval_columns:
                con.execute("ALTER TABLE approvals ADD COLUMN request_id TEXT")
            if "payload_expires_at" not in approval_columns:
                con.execute("ALTER TABLE approvals ADD COLUMN payload_expires_at REAL")
            if "action_schema_digest" not in approval_columns:
                con.execute("ALTER TABLE approvals ADD COLUMN action_schema_digest TEXT NOT NULL DEFAULT ''")
            if "action_contract_digest" not in approval_columns:
                con.execute("ALTER TABLE approvals ADD COLUMN action_contract_digest TEXT NOT NULL DEFAULT ''")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS approvals_request_id_idx ON approvals(request_id)")

    @staticmethod
    def canonical_sha256(value: Any) -> str:
        return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

    def ready(self) -> bool:
        try:
            with self._connect() as con:
                row = con.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
                return bool(row and row["value"] == "2")
        except sqlite3.Error:
            return False

    def create_approval(
        self,
        *,
        action: str,
        input: dict[str, Any],
        preview: dict[str, Any],
        request_id: str,
        action_schema_digest: str,
        action_contract_digest: str,
        ttl_seconds: int = 900,
    ) -> tuple[ApprovalRecord, bool]:
        now = time.time()
        approval_id = _id("approval")
        provenance = {
            "action_schema_digest": action_schema_digest.lower(),
            "action_contract_digest": action_contract_digest.lower(),
        }
        if any(len(value) != 64 for value in provenance.values()):
            raise ValueError("approval_provenance_invalid")
        intent_sha256 = self.canonical_sha256({"action": action, "input": input, "preview": preview, **provenance})
        # Approval authorization is single-use but has no wall-clock expiry.
        # Keep the legacy column populated with a sentinel for schema compatibility.
        expires_at = 0.0
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()
            if existing is not None:
                if (
                    existing["action"] != action
                    or existing["input_json"] != _json(input)
                    or existing["action_schema_digest"] != provenance["action_schema_digest"]
                    or existing["action_contract_digest"] != provenance["action_contract_digest"]
                ):
                    con.execute("ROLLBACK")
                    raise RequestConflict("request_id_reused_with_different_intent")
                con.execute("COMMIT")
                return self._approval_from_row(existing), True
            con.execute(
                """
                INSERT INTO approvals(
                    id, action, request_id, intent_sha256, action_schema_digest,
                    action_contract_digest, input_json, preview_json,
                    expires_at, created_at, payload_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    action,
                    request_id,
                    intent_sha256,
                    provenance["action_schema_digest"],
                    provenance["action_contract_digest"],
                    _json(input),
                    _json(preview),
                    expires_at,
                    now,
                    now + self.payload_ttl_seconds,
                ),
            )
            con.execute("COMMIT")
        record = self.get_approval(approval_id)
        assert record is not None
        return record, False

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._approval_from_row(row) if row else None

    def create_operation(
        self,
        *,
        action: str,
        input: dict[str, Any],
        request_id: str,
        max_attempts: int = 1,
        mutation_plan: dict[str, Any] | None = None,
    ) -> tuple[OperationRecord, bool]:
        return self._create_operation(
            action=action,
            input=input,
            request_id=request_id,
            max_attempts=max_attempts,
            approval=None,
            mutation_plan=mutation_plan,
        )

    def create_operation_from_approval(
        self,
        *,
        approval_id: str,
        request_id: str,
        actor: str,
        reason: str,
        approved_at: float,
        action_schema_digest: str,
        action_contract_digest: str,
        max_attempts: int = 1,
    ) -> tuple[OperationRecord, bool]:
        approval = self.get_approval(approval_id)
        if approval is None:
            raise ApprovalError("approval_not_found")
        return self._create_operation(
            action=approval.action,
            input=approval.input,
            request_id=request_id,
            max_attempts=max_attempts,
            approval={
                "approval_id": approval_id,
                "actor": actor,
                "reason": reason,
                "approved_at": approved_at,
                "action_schema_digest": action_schema_digest.lower(),
                "action_contract_digest": action_contract_digest.lower(),
            },
        )

    def _create_operation(
        self,
        *,
        action: str,
        input: dict[str, Any],
        request_id: str,
        max_attempts: int,
        approval: dict[str, Any] | None,
        mutation_plan: dict[str, Any] | None = None,
    ) -> tuple[OperationRecord, bool]:
        now = time.time()
        operation_id = _id("op")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM operations WHERE request_id = ?", (request_id,)).fetchone()
            if existing is not None:
                if existing["action"] != action or existing["input_json"] != _json(input):
                    con.execute("ROLLBACK")
                    raise RequestConflict("request_id_reused_with_different_intent")
                con.execute("COMMIT")
                return self._operation_from_row(existing), True

            approval_id = None
            if approval is not None:
                approval_id = str(approval["approval_id"])
                row = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
                if row is None:
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_not_found")
                if row["consumed_by_operation_id"] is not None:
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_already_consumed")
                if row["action"] != action or row["input_json"] != _json(input):
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_intent_mismatch")
                if not row["action_schema_digest"] or not row["action_contract_digest"]:
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_provenance_missing")
                if row["action_schema_digest"] != approval["action_schema_digest"]:
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_action_schema_changed")
                if row["action_contract_digest"] != approval["action_contract_digest"]:
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_action_contract_changed")
                approved_at = float(approval["approved_at"])
                if approved_at < float(row["created_at"]) or approved_at > now + 60:
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_time_invalid")
                if not str(approval["actor"]).strip() or not str(approval["reason"]).strip():
                    con.execute("ROLLBACK")
                    raise ApprovalError("approval_actor_and_reason_required")

            con.execute(
                """
                INSERT INTO operations(
                    id, action, status, input_json, request_id, approval_id, attempts, max_attempts,
                    payload_expires_at, created_at, updated_at, mutation_plan_json
                ) VALUES (?, ?, 'queued', ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    action,
                    _json(input),
                    request_id,
                    approval_id,
                    max(1, int(max_attempts)),
                    now + self.payload_ttl_seconds,
                    now,
                    now,
                    _json(mutation_plan) if mutation_plan is not None else None,
                ),
            )
            if approval is not None:
                con.execute(
                    """
                    UPDATE approvals SET consumed_by_operation_id = ?, actor = ?, reason = ?, approved_at = ?
                    WHERE id = ?
                    """,
                    (
                        operation_id,
                        str(approval["actor"]).strip(),
                        str(approval["reason"]).strip(),
                        float(approval["approved_at"]),
                        approval_id,
                    ),
                )
            con.execute(
                "INSERT INTO operation_events(operation_id, event_type, payload_json, created_at) VALUES (?, 'queued', ?, ?)",
                (operation_id, _json({"action": action}), now),
            )
            row = con.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            con.execute("COMMIT")
        assert row is not None
        return self._operation_from_row(row), False

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        return self._operation_from_row(row) if row else None

    def get_operation_by_request_id(self, request_id: str) -> OperationRecord | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM operations WHERE request_id = ?", (request_id,)).fetchone()
        return self._operation_from_row(row) if row else None

    def list_operations(self, *, action: str = "", status: str = "", limit: int = 100) -> list[OperationRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        if action:
            clauses.append("action = ?")
            values.append(action)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit), 500)))
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM operations{where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                values,
            ).fetchall()
        return [self._operation_from_row(row) for row in rows]

    def append_operation_event(self, operation_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._connect() as con:
            exists = con.execute("SELECT 1 FROM operations WHERE id = ?", (operation_id,)).fetchone()
            if exists is None:
                raise ValueError("operation_not_found")
            cursor = con.execute(
                "INSERT INTO operation_events(operation_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (operation_id, str(event_type), _json(payload), now),
            )
            event_id = int(cursor.lastrowid)
        return {
            "event_id": event_id,
            "operation_id": operation_id,
            "event_type": str(event_type),
            "payload": payload,
            "created_at": now,
        }

    def list_operation_events(self, operation_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id, operation_id, event_type, payload_json, created_at
                FROM operation_events WHERE operation_id = ? ORDER BY id DESC LIMIT ?
                """,
                (operation_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [
            {
                "event_id": int(row["id"]),
                "operation_id": row["operation_id"],
                "event_type": row["event_type"],
                "payload": _load(row["payload_json"], {}),
                "created_at": float(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def latest_operation_event(self, operation_id: str, *, event_type: str = "") -> dict[str, Any] | None:
        clause = " AND event_type = ?" if event_type else ""
        values: list[Any] = [operation_id]
        if event_type:
            values.append(event_type)
        with self._connect() as con:
            row = con.execute(
                f"""
                SELECT id, operation_id, event_type, payload_json, created_at
                FROM operation_events WHERE operation_id = ?{clause} ORDER BY id DESC LIMIT 1
                """,  # noqa: S608
                values,
            ).fetchone()
        if row is None:
            return None
        return {
            "event_id": int(row["id"]),
            "operation_id": row["operation_id"],
            "event_type": row["event_type"],
            "payload": _load(row["payload_json"], {}),
            "created_at": float(row["created_at"]),
        }

    def queue_position(self, operation_id: str) -> int | None:
        operation = self.get_operation(operation_id)
        if operation is None or operation.status != OperationStatus.QUEUED.value:
            return None
        with self._connect() as con:
            row = con.execute(
                """
                SELECT COUNT(*) AS position FROM operations
                WHERE status = 'queued' AND (created_at < ? OR (created_at = ? AND id <= ?))
                """,
                (operation.created_at, operation.created_at, operation.id),
            ).fetchone()
        return int(row["position"]) if row else None

    def queued_operations_ahead(self, operation_id: str) -> list[OperationRecord]:
        operation = self.get_operation(operation_id)
        if operation is None or operation.status != OperationStatus.QUEUED.value:
            return []
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM operations
                WHERE status = 'queued' AND (created_at < ? OR (created_at = ? AND id < ?))
                ORDER BY created_at, id
                """,
                (operation.created_at, operation.created_at, operation.id),
            ).fetchall()
        return [self._operation_from_row(row) for row in rows]

    def current_running_operation(self) -> OperationRecord | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM operations WHERE status = 'running' ORDER BY started_at, created_at LIMIT 1"
            ).fetchone()
        return self._operation_from_row(row) if row else None

    def is_cancel_requested(self, operation_id: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM operation_events WHERE operation_id = ? AND event_type = 'cancel_requested' LIMIT 1",
                (operation_id,),
            ).fetchone()
        return row is not None

    def claim_next_operation(self, worker_id: str, *, lease_seconds: float) -> OperationRecord | None:
        now = time.time()
        expires_at = now + lease_seconds
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._heartbeat_worker(con, worker_id, now)
            row = con.execute("SELECT * FROM operations WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            con.execute(
                """
                UPDATE operations SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                    attempts = attempts + 1, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, expires_at, now, now, row["id"]),
            )
            con.execute(
                "INSERT INTO operation_events(operation_id, event_type, payload_json, created_at) VALUES (?, 'claimed', ?, ?)",
                (row["id"], _json({"worker_id": worker_id}), now),
            )
            claimed = con.execute("SELECT * FROM operations WHERE id = ?", (row["id"],)).fetchone()
            con.execute("COMMIT")
        return self._operation_from_row(claimed) if claimed else None

    def heartbeat(self, operation_id: str, worker_id: str, *, lease_seconds: float) -> bool:
        now = time.time()
        with self._connect() as con:
            changed = con.execute(
                """
                UPDATE operations SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (now + lease_seconds, now, operation_id, worker_id),
            ).rowcount
            if changed:
                self._heartbeat_worker(con, worker_id, now)
                con.execute(
                    "UPDATE resource_locks SET lease_expires_at = ? WHERE operation_id = ?",
                    (now + lease_seconds, operation_id),
                )
        return bool(changed)

    def acquire_resources(
        self,
        operation_id: str,
        resources: Iterable[tuple[str, str]],
        *,
        lease_expires_at: float,
    ) -> bool:
        normalized = sorted({(mode, key) for mode, key in resources if mode in {"read", "write"} and key})
        if not normalized:
            return True
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM resource_locks WHERE lease_expires_at < ?", (now,))
            for mode, key in normalized:
                conflicts = con.execute(
                    """
                    SELECT 1 FROM resource_locks
                    WHERE resource_key = ? AND operation_id != ? AND (? = 'write' OR mode = 'write')
                    LIMIT 1
                    """,
                    (key, operation_id, mode),
                ).fetchone()
                if conflicts:
                    con.execute("ROLLBACK")
                    return False
            for mode, key in normalized:
                con.execute(
                    """
                    INSERT OR REPLACE INTO resource_locks(resource_key, operation_id, mode, lease_expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, operation_id, mode, lease_expires_at, now),
                )
            con.execute("COMMIT")
        return True

    def requeue(self, operation_id: str, worker_id: str, *, reason: str) -> OperationRecord:
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                UPDATE operations SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?, error_json = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (now, _json({"code": reason}), operation_id, worker_id),
            )
            con.execute("DELETE FROM resource_locks WHERE operation_id = ?", (operation_id,))
            con.execute(
                "INSERT INTO operation_events(operation_id, event_type, payload_json, created_at) VALUES (?, 'requeued', ?, ?)",
                (operation_id, _json({"reason": reason}), now),
            )
            row = con.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            con.execute("COMMIT")
        if row is None:
            raise ValueError("operation_not_found")
        return self._operation_from_row(row)

    def finish(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> OperationRecord:
        if status not in {item.value for item in OperationStatus}:
            raise ValueError("invalid_operation_status")
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            params: list[Any] = [status, _json(result) if result is not None else None, _json(error) if error is not None else None, now, now, operation_id]
            owner_clause = ""
            if worker_id:
                owner_clause = " AND lease_owner = ?"
                params.append(worker_id)
            con.execute(
                f"""
                UPDATE operations SET status = ?, result_json = ?, error_json = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?, completed_at = ?
                WHERE id = ?{owner_clause}
                """,  # noqa: S608
                params,
            )
            con.execute("DELETE FROM resource_locks WHERE operation_id = ?", (operation_id,))
            con.execute(
                "INSERT INTO operation_events(operation_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (operation_id, status, _json({"error": error} if error else {}), now),
            )
            row = con.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            con.execute("COMMIT")
        if row is None:
            raise ValueError("operation_not_found")
        return self._operation_from_row(row)

    def cancel(self, operation_id: str) -> OperationRecord:
        current = self.get_operation(operation_id)
        if current is None:
            raise ValueError("operation_not_found")
        if current.status == OperationStatus.QUEUED.value:
            return self.finish(operation_id, status=OperationStatus.CANCELLED.value)
        if current.status == OperationStatus.RUNNING.value:
            if not self.is_cancel_requested(operation_id):
                self.append_operation_event(operation_id, "cancel_requested", {"requested_at": time.time()})
            refreshed = self.get_operation(operation_id)
            assert refreshed is not None
            return refreshed
        raise ValueError("operation_not_cancellable")

    def expired_running_operations(self, *, now: float | None = None) -> list[OperationRecord]:
        at = time.time() if now is None else now
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM operations WHERE status = 'running' AND lease_expires_at < ? ORDER BY created_at",
                (at,),
            ).fetchall()
        return [self._operation_from_row(row) for row in rows]

    def recover_expired(self, operation_id: str, *, safe_to_retry: bool) -> OperationRecord:
        current = self.get_operation(operation_id)
        if current is None:
            raise ValueError("operation_not_found")
        target = "queued" if safe_to_retry and current.attempts < current.max_attempts else "uncertain"
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                UPDATE operations SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    error_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    target,
                    _json({"code": "worker_lease_expired", "safe_to_retry": safe_to_retry}),
                    now,
                    None if target == "queued" else now,
                    operation_id,
                ),
            )
            con.execute("DELETE FROM resource_locks WHERE operation_id = ?", (operation_id,))
            row = con.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            con.execute("COMMIT")
        assert row is not None
        return self._operation_from_row(row)

    def create_temporary_file(
        self,
        *,
        kind: str,
        path: str,
        sha256: str,
        size: int,
        media_type: str,
        source_operation_id: str,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> TemporaryFileRecord:
        now = time.time()
        record_id = _id("temp")
        expires_at = now + (self.temporary_ttl_seconds if ttl_seconds is None else max(60, int(ttl_seconds)))
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO temporary_files(
                    id, kind, path, sha256, size, media_type, source_operation_id,
                    metadata_json, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record_id, kind, path, sha256, int(size), media_type, source_operation_id, _json(metadata or {}), expires_at, now),
            )
        record = self.get_temporary_file(record_id)
        assert record is not None
        return record

    def get_temporary_file(self, temporary_file_id: str) -> TemporaryFileRecord | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM temporary_files WHERE id = ?", (temporary_file_id,)).fetchone()
        return self._temporary_from_row(row) if row else None

    def mark_temporary_deleted(self, temporary_file_id: str, *, at: float | None = None) -> TemporaryFileRecord:
        deleted_at = time.time() if at is None else at
        with self._connect() as con:
            changed = con.execute(
                "UPDATE temporary_files SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (deleted_at, temporary_file_id),
            ).rowcount
        if not changed:
            current = self.get_temporary_file(temporary_file_id)
            if current is None:
                raise ValueError("temporary_file_not_found")
            return current
        record = self.get_temporary_file(temporary_file_id)
        assert record is not None
        return record

    def renew_temporary_file(self, temporary_file_id: str, *, ttl_seconds: int) -> TemporaryFileRecord:
        expires_at = time.time() + max(60, int(ttl_seconds))
        with self._connect() as con:
            changed = con.execute(
                "UPDATE temporary_files SET expires_at = ? WHERE id = ? AND deleted_at IS NULL",
                (expires_at, temporary_file_id),
            ).rowcount
        if not changed:
            current = self.get_temporary_file(temporary_file_id)
            if current is None:
                raise ValueError("temporary_file_not_found")
            return current
        record = self.get_temporary_file(temporary_file_id)
        assert record is not None
        return record

    def cleanup_expired(self, *, temporary_root: Path, now: float | None = None) -> dict[str, int]:
        at = time.time() if now is None else now
        deleted_files = 0
        scrubbed_operations = 0
        root = temporary_root.resolve()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM temporary_files WHERE deleted_at IS NULL AND expires_at <= ?",
                (at,),
            ).fetchall()
            for row in rows:
                path = Path(str(row["path"])).resolve()
                if root in path.parents:
                    path.unlink(missing_ok=True)
                con.execute("UPDATE temporary_files SET deleted_at = ? WHERE id = ?", (at, row["id"]))
                deleted_files += 1

            operations = con.execute(
                "SELECT * FROM operations WHERE payload_expires_at <= ?",
                (at,),
            ).fetchall()
            for row in operations:
                input_value = _load(row["input_json"], {})
                result_value = _load(row["result_json"], None)
                summarized_input = _summarize_value(input_value)
                summarized_result = _summarize_value(result_value)
                mutation_plan = _load(row["mutation_plan_json"], None)
                summarized_plan = _summarize_value(mutation_plan)
                if summarized_input != input_value or summarized_result != result_value or mutation_plan != summarized_plan:
                    con.execute(
                        "UPDATE operations SET input_json = ?, result_json = ?, payload_expires_at = ?, mutation_plan_json = ? WHERE id = ?",
                        (_json(summarized_input), _json(summarized_result) if result_value is not None else None, NEVER_EXPIRES_AT, _json(summarized_plan) if mutation_plan is not None else None, row["id"]),
                    )
                    scrubbed_operations += 1
            approvals = con.execute(
                "SELECT * FROM approvals WHERE payload_expires_at IS NOT NULL AND payload_expires_at <= ?",
                (at,),
            ).fetchall()
            for row in approvals:
                input_value = _load(row["input_json"], {})
                preview_value = _load(row["preview_json"], {})
                summarized_input = _summarize_value(input_value)
                summarized_preview = _summarize_value(preview_value)
                if summarized_input != input_value or summarized_preview != preview_value:
                    con.execute(
                        "UPDATE approvals SET input_json = ?, preview_json = ?, payload_expires_at = ? WHERE id = ?",
                        (_json(summarized_input), _json(summarized_preview), NEVER_EXPIRES_AT, row["id"]),
                    )
        return {"temporary_files_deleted": deleted_files, "operations_scrubbed": scrubbed_operations}

    def heartbeat_worker(self, worker_id: str, *, at: float | None = None) -> None:
        now = time.time() if at is None else at
        with self._connect() as con:
            self._heartbeat_worker(con, worker_id, now)

    @staticmethod
    def _heartbeat_worker(con: sqlite3.Connection, worker_id: str, now: float) -> None:
        con.execute(
            """
            INSERT INTO workers(worker_id, heartbeat_at, started_at) VALUES (?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at
            """,
            (worker_id, now, now),
        )

    def worker_healthy(self, *, max_age_seconds: float) -> bool:
        cutoff = time.time() - max_age_seconds
        with self._connect() as con:
            row = con.execute("SELECT 1 FROM workers WHERE heartbeat_at >= ? LIMIT 1", (cutoff,)).fetchone()
        return row is not None

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            id=row["id"],
            action=row["action"],
            status=row["status"],
            input=_load(row["input_json"], {}),
            result=_load(row["result_json"], None),
            error=_load(row["error_json"], None),
            request_id=row["request_id"],
            approval_id=row["approval_id"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            payload_expires_at=float(row["payload_expires_at"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            mutation_plan=_load(row["mutation_plan_json"], None),
        )

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"],
            action=row["action"],
            request_id=row["request_id"] or "",
            intent_sha256=row["intent_sha256"],
            action_schema_digest=row["action_schema_digest"],
            action_contract_digest=row["action_contract_digest"],
            input=_load(row["input_json"], {}),
            preview=_load(row["preview_json"], {}),
            expires_at=None,
            created_at=float(row["created_at"]),
            consumed_by_operation_id=row["consumed_by_operation_id"],
            actor=row["actor"],
            reason=row["reason"],
            approved_at=row["approved_at"],
            payload_expires_at=float(row["payload_expires_at"] or 0),
        )

    @staticmethod
    def _temporary_from_row(row: sqlite3.Row) -> TemporaryFileRecord:
        return TemporaryFileRecord(
            id=row["id"],
            kind=row["kind"],
            path=row["path"],
            sha256=row["sha256"],
            size=int(row["size"]),
            media_type=row["media_type"],
            source_operation_id=row["source_operation_id"],
            metadata=_load(row["metadata_json"], {}),
            expires_at=float(row["expires_at"]),
            created_at=float(row["created_at"]),
            deleted_at=row["deleted_at"],
        )
