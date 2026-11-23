from __future__ import annotations

from pathlib import Path
import time

from musicops.store import ApprovalError, MusicOpsStore


SCHEMA_DIGEST = "a" * 64
CONTRACT_DIGEST = "b" * 64


def test_approvals_are_single_use_without_wall_clock_expiry(tmp_path):
    store = MusicOpsStore(tmp_path / "ledger.db")
    approval, _ = store.create_approval(
        action="library.import",
        input={"staged_file_id": "staged"},
        preview={"items": [{"target_file_path": "track.flac"}]},
        request_id="no-expiry-approval",
        action_schema_digest=SCHEMA_DIGEST,
        action_contract_digest=CONTRACT_DIGEST,
    )

    assert approval.expires_at is None
    with store._connect() as con:
        con.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", (time.time() - 1, approval.id))

    persisted = store.get_approval(approval.id)
    assert persisted is not None and persisted.expires_at is None
    operation, reused = store.create_operation_from_approval(
        approval_id=approval.id,
        request_id="execute-no-expiry-approval",
        actor="user",
        reason="Approved exact preview",
        approved_at=time.time(),
        action_schema_digest=SCHEMA_DIGEST,
        action_contract_digest=CONTRACT_DIGEST,
    )
    assert reused is False and operation.approval_id == approval.id

    try:
        store.create_operation_from_approval(
            approval_id=approval.id,
            request_id="execute-no-expiry-approval-again",
            actor="user",
            reason="Approved exact preview",
            approved_at=time.time(),
            action_schema_digest=SCHEMA_DIGEST,
            action_contract_digest=CONTRACT_DIGEST,
        )
    except ApprovalError as exc:
        assert str(exc) == "approval_already_consumed"
    else:
        raise AssertionError("approval was not single-use")


def test_store_has_no_manager_tables_and_recovers_leases(tmp_path):
    store = MusicOpsStore(tmp_path / "ledger.db")
    safe, _ = store.create_operation(action="library.search", input={"query": "x"}, request_id="safe-request", max_attempts=2)
    unsafe, _ = store.create_operation(action="library.delete", input={"items": []}, request_id="unsafe-request", max_attempts=1)
    claimed = store.claim_next_operation("worker-a", lease_seconds=1)
    assert claimed and claimed.id == safe.id
    with store._connect() as con:
        con.execute("UPDATE operations SET lease_expires_at = ? WHERE id = ?", (time.time() - 1, safe.id))
    assert store.recover_expired(safe.id, safe_to_retry=True).status == "queued"
    claimed = store.claim_next_operation("worker-a", lease_seconds=60)
    assert claimed and claimed.id == safe.id
    store.finish(safe.id, status="succeeded", result={"ok": True}, worker_id="worker-a")
    claimed = store.claim_next_operation("worker-b", lease_seconds=1)
    assert claimed and claimed.id == unsafe.id
    with store._connect() as con:
        con.execute("UPDATE operations SET lease_expires_at = ? WHERE id = ?", (time.time() - 1, unsafe.id))
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert store.recover_expired(unsafe.id, safe_to_retry=False).status == "uncertain"
    assert not ({"runs", "todos", "revisions", "evidence", "artifacts"} & names)


def test_operation_event_queries_have_cancel_and_progress_index(tmp_path):
    store = MusicOpsStore(tmp_path / "ledger.db")

    with store._connect() as con:
        indexes = {row[1] for row in con.execute("PRAGMA index_list(operation_events)")}

    assert "operation_events_operation_type_idx" in indexes


def test_large_payloads_scrub_after_30_days_and_small_history_remains(tmp_path):
    store = MusicOpsStore(tmp_path / "ledger.db", payload_ttl_seconds=60, temporary_ttl_seconds=60)
    operation, _ = store.create_operation(
        action="lyrics.translate",
        input={"canonical_lrc": "x" * 2000, "small": "kept"},
        request_id="retention-request",
    )
    store.finish(operation.id, status="succeeded", result={"translated_lrc": "y" * 2000})
    approval, _ = store.create_approval(
        action="library.update_lyrics",
        input={"lyrics": "z" * 2000},
        preview={"items": [{"lyrics_text": "z" * 2000}]},
        request_id="retention-approval",
        action_schema_digest=SCHEMA_DIGEST,
        action_contract_digest=CONTRACT_DIGEST,
    )
    with store._connect() as con:
        con.execute("UPDATE operations SET payload_expires_at = ?", (time.time() - 1,))
        con.execute("UPDATE approvals SET payload_expires_at = ?", (time.time() - 1,))
    result = store.cleanup_expired(temporary_root=tmp_path / "temporary")
    retained = store.get_operation(operation.id)
    retained_approval = store.get_approval(approval.id)
    assert result["operations_scrubbed"] == 1
    assert retained and retained.status == "succeeded" and retained.action == "lyrics.translate"
    assert retained.input["small"] == "kept"
    assert retained.input["canonical_lrc"]["omitted"] is True
    assert retained_approval and retained_approval.preview["items"][0]["lyrics_text"]["omitted"] is True


def test_temporary_audio_expires_after_seven_day_policy(tmp_path):
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    source = temporary_root / "audio.mp3"
    source.write_bytes(b"audio")
    store = MusicOpsStore(tmp_path / "ledger.db", temporary_ttl_seconds=60)
    operation, _ = store.create_operation(action="download.fetch", input={}, request_id="temp-source-op")
    store.finish(operation.id, status="succeeded", result={})
    record = store.create_temporary_file(kind="downloaded_audio", path=str(source), sha256="a" * 64, size=5, media_type="audio/mpeg", source_operation_id=operation.id)
    with store._connect() as con:
        con.execute("UPDATE temporary_files SET expires_at = ? WHERE id = ?", (time.time() - 1, record.id))
    result = store.cleanup_expired(temporary_root=temporary_root)
    assert result["temporary_files_deleted"] == 1
    assert not source.exists()
    assert store.get_temporary_file(record.id).deleted_at is not None


def test_staged_import_activity_can_renew_thirty_day_retention(tmp_path):
    source = tmp_path / "staged.flac"
    source.write_bytes(b"staged")
    store = MusicOpsStore(tmp_path / "ledger.db")
    operation, _ = store.create_operation(action="library.import.prepare", input={}, request_id="staged-source-op")
    store.finish(operation.id, status="succeeded", result={})
    record = store.create_temporary_file(
        kind="normalized_staged_import",
        path=str(source),
        sha256="a" * 64,
        size=6,
        media_type="audio/flac",
        source_operation_id=operation.id,
        ttl_seconds=60,
    )

    renewed = store.renew_temporary_file(record.id, ttl_seconds=30 * 24 * 60 * 60)

    assert renewed.expires_at > time.time() + 29 * 24 * 60 * 60


def test_running_operation_heartbeat_keeps_worker_ready(tmp_path):
    store = MusicOpsStore(tmp_path / "ledger.db")
    operation, _ = store.create_operation(
        action="library.search",
        input={"query": "Future"},
        request_id="long-running-worker-heartbeat",
    )
    claimed = store.claim_next_operation("worker-1", lease_seconds=30)
    assert claimed is not None and claimed.id == operation.id

    store.heartbeat_worker("worker-1", at=time.time() - 60)
    assert store.worker_healthy(max_age_seconds=15) is False

    assert store.heartbeat(operation.id, "worker-1", lease_seconds=30) is True
    assert store.worker_healthy(max_age_seconds=15) is True
