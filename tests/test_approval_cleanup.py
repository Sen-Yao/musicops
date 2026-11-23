from __future__ import annotations

import sqlite3

import pytest

from musicops.approval_cleanup import CleanupRefused, inspect_database, purge_database
from musicops.store import MusicOpsStore


def _approval(store: MusicOpsStore, request_id: str):
    return store.create_approval(
        action="library.delete",
        input={"items": [{"file_path": "track.flac"}]},
        preview={"items": [{"file_path": "track.flac"}]},
        request_id=request_id,
        action_schema_digest="a" * 64,
        action_contract_digest="b" * 64,
    )[0]


def test_cleanup_removes_only_unconsumed_targets_and_vacuums(tmp_path):
    path = tmp_path / "ledger.db"
    store = MusicOpsStore(path)
    target = _approval(store, "cleanup-target")
    kept = _approval(store, "cleanup-kept")

    before = inspect_database(path, [target.id], require_all=True, require_no_active=True)
    result = purge_database(path, [target.id], require_all=True, require_no_active=True)

    assert before["target_count"] == 1
    assert result["deleted"] == 1
    assert result["quick_check"] == "ok"
    assert not (tmp_path / "ledger.db-wal").exists()
    assert not (tmp_path / "ledger.db-shm").exists()
    assert store.get_approval(target.id) is None
    assert store.get_approval(kept.id) is not None


def test_cleanup_refuses_active_consumed_or_referenced_approvals(tmp_path):
    active_path = tmp_path / "active.db"
    active_store = MusicOpsStore(active_path)
    active = _approval(active_store, "active-target")
    active_store.create_operation(action="library.search", input={"query": "x"}, request_id="active-operation")
    with pytest.raises(CleanupRefused, match="active_operations_present"):
        inspect_database(active_path, [active.id], require_no_active=True)

    consumed_path = tmp_path / "consumed.db"
    consumed_store = MusicOpsStore(consumed_path)
    consumed = _approval(consumed_store, "consumed-target")
    consumed_store.create_operation_from_approval(
        approval_id=consumed.id,
        request_id="consume-target",
        actor="user",
        reason="approved",
        approved_at=consumed.created_at,
        action_schema_digest="a" * 64,
        action_contract_digest="b" * 64,
    )
    with pytest.raises(CleanupRefused, match="target_approval_consumed"):
        purge_database(consumed_path, [consumed.id])


def test_cleanup_can_compact_legacy_database_without_approvals(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE operations(id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        connection.execute("INSERT INTO operations VALUES ('old-op', 'succeeded')")

    result = purge_database(
        path,
        ["approval_absent"],
        require_no_active=True,
        allow_missing_approvals_table=True,
    )

    assert result["deleted"] == 0
    assert result["quick_check"] == "ok"
