from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import hashlib
import logging
import os
from pathlib import Path
import secrets
import threading
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from musicops.actions import ActionContext, ActionFailure, build_registry, registry_digest
from musicops.approval_renderer import render_approval_html
from musicops.library import LibraryInspector
from musicops.lyrics_provider import LyricsProviderClient
from musicops.metadata_provider import MetadataProviderClient
from musicops.navidrome import NavidromeClient
from musicops.operation_types import OperationStatus
from musicops.provenance import verify_source_manifest
from musicops.remote_stt import run_remote_clip_stt
from musicops.store import ApprovalError, MusicOpsStore, OperationRecord, RequestConflict
from musicops.vmomo import UnconfiguredVmomoClient, VmomoClient


LEASE_SECONDS = 30.0
WORKER_HEARTBEAT_SECONDS = 5.0
LOGGER = logging.getLogger("musicops.worker")
PUBLIC_PROGRESS_KEYS = {
    "stage",
    "provider",
    "search_msg_id",
    "candidate_index",
    "requested_quality",
    "selected_quality",
    "available_qualities",
    "observed_button_labels",
    "quality_button_clicked",
    "quality_menu_msg_id",
    "media_msg_id",
    "attempt",
    "attempts_total",
    "resumed_from",
    "downloaded_bytes",
    "expected_size",
    "partial_size",
    "percent",
    "error",
    "size",
    "elapsed_seconds",
    "remaining_seconds",
    "deadline_at",
    "total_budget_seconds",
}


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationCreateRequest(ApiModel):
    action: str = Field(min_length=1, max_length=200)
    input: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(min_length=8, max_length=200)


class ApprovalExecuteRequest(ApiModel):
    request_id: str = Field(min_length=8, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    approved_at: float


def _check_token(expected_token: str):
    def dependency(authorization: str | None = Header(default=None)) -> None:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_or_invalid_token")
        token = authorization[len(prefix) :]
        if not token or len(token) > 512 or not secrets.compare_digest(token, expected_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_or_invalid_token")

    return dependency


def _directory_writable(path: Path) -> bool:
    probe = path / f".ready-{uuid.uuid4().hex}"
    try:
        probe.write_text("ready", encoding="utf-8")
        return probe.read_text(encoding="utf-8") == "ready"
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


def _decode_cover_data_uri(data_uri: Any) -> bytes:
    prefix, separator, encoded = str(data_uri or "").partition(",")
    if not separator or not prefix.startswith("data:image/") or ";base64" not in prefix:
        raise ValueError("invalid_cover_data_uri")
    return base64.b64decode(encoded, validate=True)


def create_app(
    data_dir: str | Path,
    api_token: str,
    library_inspector: LibraryInspector | None = None,
    navidrome_client: NavidromeClient | None = None,
    vmomo_client: VmomoClient | None = None,
    lyrics_provider: LyricsProviderClient | None = None,
    metadata_provider: MetadataProviderClient | None = None,
    remote_stt_runner: Any | None = None,
    start_worker: bool = True,
    mutation_mode: str | None = None,
) -> FastAPI:
    mutation_mode = (mutation_mode or os.environ.get("MUSICOPS_MUTATION_MODE") or "strict").strip().lower()
    if mutation_mode not in {"strict", "personal_auto"}:
        raise ValueError("mutation_mode must be strict or personal_auto")
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    temporary_dir = data_path / "temporary"
    temporary_dir.mkdir(parents=True, exist_ok=True)

    store = MusicOpsStore(data_path / "musicops-v2.db")
    context = ActionContext(
        store=store,
        data_path=data_path,
        temporary_dir=temporary_dir,
        vmomo_client=vmomo_client or UnconfiguredVmomoClient(),
        library_inspector=library_inspector or LibraryInspector(),
        navidrome_client=navidrome_client or NavidromeClient(),
        lyrics_provider=lyrics_provider or LyricsProviderClient(),
        metadata_provider=metadata_provider or MetadataProviderClient(),
        remote_stt_runner=remote_stt_runner or run_remote_clip_stt,
    )
    registry = build_registry()
    action_schema_digest = registry_digest(registry, context)
    expected_source_manifest = os.environ.get("MUSICOPS_SOURCE_MANIFEST_SHA256", "").lower()
    source_provenance = verify_source_manifest(
        os.environ.get("MUSICOPS_SOURCE_ROOT", "/app"),
        os.environ.get("MUSICOPS_SOURCE_MANIFEST_PATH", "/app/.musicops-source-manifest"),
        expected_sha256=expected_source_manifest,
    ) if expected_source_manifest else {"verified": True, "expected_sha256": "", "error": "not_configured"}
    worker_id = f"worker-{uuid.uuid4().hex}"
    worker_stop = threading.Event()
    worker_thread: threading.Thread | None = None

    def recover_expired() -> None:
        for operation in store.expired_running_operations():
            action = registry.get(operation.action)
            safe = bool(action and action.lease_recovery_safe and operation.attempts < operation.max_attempts)
            store.recover_expired(operation.id, safe_to_retry=safe)

    def execute_operation(operation: OperationRecord) -> None:
        action = registry.get(operation.action)
        if action is None:
            store.finish(operation.id, status=OperationStatus.FAILED.value, error={"code": "action_removed"}, worker_id=worker_id)
            return
        try:
            model = action.input_model.model_validate(operation.input)
        except ValidationError as exc:
            store.finish(
                operation.id,
                status=OperationStatus.FAILED.value,
                error={"code": "stored_input_invalid", "detail": exc.errors(include_context=False, include_input=False)},
                worker_id=worker_id,
            )
            return
        availability, reason = action.availability(context)
        if availability == "unavailable":
            store.finish(operation.id, status=OperationStatus.FAILED.value, error={"code": "action_unavailable", "detail": reason}, worker_id=worker_id)
            return
        expires = time.time() + LEASE_SECONDS
        if not store.acquire_resources(operation.id, action.resources(model), lease_expires_at=expires):
            store.requeue(operation.id, worker_id, reason="resource_busy")
            return

        heartbeat_stop = threading.Event()

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(WORKER_HEARTBEAT_SECONDS):
                if not store.heartbeat(operation.id, worker_id, lease_seconds=LEASE_SECONDS):
                    return

        heartbeat = threading.Thread(target=heartbeat_loop, name=f"musicops-heartbeat-{operation.id}", daemon=True)
        heartbeat.start()
        try:
            result = action.handler(context, operation, model)
            result_failed = result.payload.get("ok") is False
            result_status = OperationStatus.FAILED.value if result_failed else OperationStatus.SUCCEEDED.value
            result_error = None
            if result_status == OperationStatus.FAILED.value:
                result_error = {
                    "code": "action_result_not_ok",
                    "detail": {
                        key: result.payload[key]
                        for key in ("overall_status", "succeeded_count", "failed_count")
                        if key in result.payload
                    },
                }
            store.finish(
                operation.id,
                status=result_status,
                result=result.payload,
                error=result_error,
                worker_id=worker_id,
            )
        except ActionFailure as exc:
            current = store.get_operation(operation.id)
            if exc.code == "operation_cancelled":
                store.finish(
                    operation.id,
                    status=OperationStatus.CANCELLED.value,
                    error=exc.to_dict(),
                    worker_id=worker_id,
                )
            elif exc.retryable and not exc.uncertain and current and current.attempts < current.max_attempts:
                store.requeue(operation.id, worker_id, reason=exc.code)
            else:
                terminal = OperationStatus.UNCERTAIN.value if exc.uncertain else OperationStatus.FAILED.value
                store.finish(operation.id, status=terminal, error=exc.to_dict(), worker_id=worker_id)
        except Exception as exc:
            terminal = OperationStatus.FAILED.value if action.lease_recovery_safe else OperationStatus.UNCERTAIN.value
            store.finish(
                operation.id,
                status=terminal,
                error={
                    "code": "action_exception",
                    "error_type": exc.__class__.__name__,
                    "detail": str(exc),
                    "uncertain": terminal == OperationStatus.UNCERTAIN.value,
                },
                worker_id=worker_id,
            )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)

    def worker_loop() -> None:
        store.heartbeat_worker(worker_id)
        next_cleanup = 0.0
        while not worker_stop.is_set():
            try:
                store.heartbeat_worker(worker_id)
                recover_expired()
                if time.time() >= next_cleanup:
                    store.cleanup_expired(temporary_root=temporary_dir)
                    next_cleanup = time.time() + 60
                operation = store.claim_next_operation(worker_id, lease_seconds=LEASE_SECONDS)
                if operation is None:
                    worker_stop.wait(0.1)
                    continue
                execute_operation(operation)
            except Exception:
                LOGGER.exception("worker_loop_failed", extra={"worker_id": worker_id})
                worker_stop.wait(0.25)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal worker_thread
        if start_worker:
            worker_thread = threading.Thread(target=worker_loop, name="musicops-operation-worker", daemon=True)
            worker_thread.start()
        try:
            yield
        finally:
            worker_stop.set()
            if worker_thread is not None:
                worker_thread.join(timeout=2)

    app = FastAPI(title="musicops", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.action_context = context
    app.state.action_registry = registry
    app.state.worker_id = worker_id
    app.state.mutation_mode = mutation_mode

    def approval_payload(approval: Any, *, full: bool = False) -> dict[str, Any]:
        payload = approval.to_dict(include_input=full, full=full)
        review_path = f"/approvals/{approval.id}/review"
        payload["review_path"] = review_path
        public_base_url = os.environ.get("MUSICOPS_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public_base_url:
            payload["review_url"] = f"{public_base_url}{review_path}"
        return payload

    def review_cover_assets(approval: dict[str, Any]) -> dict[str, dict[str, Any]]:
        if str(approval.get("action") or "") != "library.update_lyrics":
            return {}
        try:
            budget = max(0, int(os.environ.get("MUSICOPS_REVIEW_COVER_BUDGET_BYTES", "10000000")))
        except ValueError:
            budget = 10_000_000
        assets: dict[str, dict[str, Any]] = {}
        for item in list((approval.get("preview") or {}).get("items") or []):
            file_path = str(item.get("file_path") or "")
            cover_preview = ((item.get("before") or {}).get("cover") or {})
            expected = str(cover_preview.get("sha256") or "").lower()
            if not file_path or not cover_preview.get("present") or not expected:
                assets[file_path] = {"verified": False, "status": "missing"}
                continue
            try:
                evidence = context.library_inspector.evidence_for_file(
                    file_path,
                    include={"cover_data"},
                )
                cover = evidence.get("cover") or {}
                data_uri = str(cover.get("data_uri") or "")
                data = _decode_cover_data_uri(data_uri)
                actual = hashlib.sha256(data).hexdigest()
                reported = str(cover.get("sha256") or actual).lower()
            except Exception:
                assets[file_path] = {"verified": False, "status": "read_failed"}
                continue
            if actual != expected or reported != expected:
                assets[file_path] = {"verified": False, "status": "hash_mismatch"}
                continue
            if len(data) > budget:
                assets[file_path] = {
                    "verified": False,
                    "status": "budget_exceeded",
                    "preview_bytes": len(data),
                }
                continue
            budget -= len(data)
            assets[file_path] = {
                "verified": True,
                "status": "verified",
                "data_uri": data_uri,
                "embedded_bytes": len(data),
            }
        return assets

    def operation_budget(operation: OperationRecord) -> float | None:
        if operation.action == "download.fetch":
            try:
                return float(operation.input.get("timeout", 300))
            except (TypeError, ValueError):
                return 300.0
        return None

    def public_operation_event(event: dict[str, Any] | None) -> dict[str, Any] | None:
        if event is None:
            return None
        event_type = str(event.get("event_type") or "")
        public = {
            "event_id": event.get("event_id"),
            "operation_id": event.get("operation_id"),
            "event_type": event_type,
            "created_at": event.get("created_at"),
        }
        raw_payload = event.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        if event_type == "progress":
            public["payload"] = {key: payload[key] for key in PUBLIC_PROGRESS_KEYS if key in payload}
        elif event_type in {"queued", "requeued"}:
            public["payload"] = {
                key: payload[key]
                for key in ("action", "reason", "request_id", "approval_id")
                if key in payload
            }
        elif event_type == "cancel_requested":
            public["payload"] = {"requested_at": payload.get("requested_at")}
        return public

    def operation_payload(operation: OperationRecord, *, full: bool = False) -> dict[str, Any]:
        def strip_paths(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: strip_paths(item) for key, item in value.items() if key != "resolved_path"}
            if isinstance(value, list):
                return [strip_paths(item) for item in value]
            return value
        payload = strip_paths(operation.to_dict(full=full))
        last_event = public_operation_event(store.latest_operation_event(operation.id))
        progress_event = public_operation_event(store.latest_operation_event(operation.id, event_type="progress"))
        progress = dict((progress_event or {}).get("payload") or {})
        if not progress.get("stage"):
            if operation.status == OperationStatus.QUEUED.value:
                progress["stage"] = "queued"
            elif operation.status == OperationStatus.RUNNING.value:
                progress["stage"] = "action_running"
            else:
                progress["stage"] = operation.status
        progress["operation_attempt"] = operation.attempts
        progress["operation_max_attempts"] = operation.max_attempts
        if operation.started_at is not None:
            progress["elapsed_seconds"] = round(max(0.0, time.time() - operation.started_at), 3)
        deadline_at = progress.get("deadline_at")
        if isinstance(deadline_at, (int, float)):
            progress["remaining_seconds"] = round(max(0.0, float(deadline_at) - time.time()), 3)
        if operation.status == OperationStatus.FAILED.value:
            error = operation.error if isinstance(operation.error, dict) else {}
            detail = error.get("detail")
            failure_stage = detail.get("failure_stage") if isinstance(detail, dict) else None
            if failure_stage:
                progress["stage"] = failure_stage
        payload["worker_capacity"] = 1
        payload["progress"] = progress
        payload["last_event"] = last_event
        if operation.action == "download.fetch":
            payload["execution_budget"] = {
                "total_timeout_seconds": operation_budget(operation),
                "provider_transfer_attempts_max": int(operation.input.get("retries", 2)) + 1,
                "operation_max_attempts": operation.max_attempts,
            }
        if operation.status == OperationStatus.QUEUED.value:
            position = store.queue_position(operation.id)
            running = store.current_running_operation()
            reason = "single_worker_capacity" if running is not None or (position or 0) > 1 else "awaiting_worker"
            queue: dict[str, Any] = {"position": position, "reason": reason, "worker_capacity": 1}
            estimates: list[float] = []
            if running is not None:
                running_progress = dict((store.latest_operation_event(running.id, event_type="progress") or {}).get("payload") or {})
                running_deadline = running_progress.get("deadline_at")
                running_remaining = (
                    max(0.0, float(running_deadline) - time.time())
                    if isinstance(running_deadline, (int, float))
                    else None
                )
                queue["blocked_by"] = {
                    "operation_id": running.id,
                    "action": running.action,
                    "owner": running.lease_owner,
                    "stage": running_progress.get("stage") or "action_running",
                    "elapsed_seconds": round(max(0.0, time.time() - (running.started_at or running.created_at)), 3),
                    "estimated_remaining_seconds": round(running_remaining, 3) if running_remaining is not None else None,
                }
                if running_remaining is not None:
                    estimates.append(running_remaining)
            for queued in store.queued_operations_ahead(operation.id):
                budget = operation_budget(queued)
                if budget is None:
                    estimates = []
                    break
                estimates.append(budget)
            if estimates:
                queue["estimated_wait_upper_bound_seconds"] = round(sum(estimates), 3)
            payload["queue"] = queue
        if operation.status == OperationStatus.RUNNING.value:
            payload["cancel_requested"] = store.is_cancel_requested(operation.id)
        if operation.status == OperationStatus.FAILED.value and bool((operation.error or {}).get("retryable")):
            payload["retry"] = {
                "retryable": True,
                "same_request_id_returns_existing_operation": True,
                "request_id_strategy": "Choose one new stable request_id for the retry and reuse it only for transport retries of that submission.",
                "diagnostic_path": f"/operations/{operation.id}?full=true",
            }
        if full:
            payload["events"] = [
                public
                for event in store.list_operation_events(operation.id, limit=100)
                if (public := public_operation_event(event)) is not None
            ]
        return payload

    @app.get("/health/live")
    def health_live() -> dict[str, Any]:
        return {"ok": True, "service": "musicops"}

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        checks = {
            "database": store.ready(),
            "temporary_storage": _directory_writable(temporary_dir),
            "worker": store.worker_healthy(max_age_seconds=max(15, WORKER_HEARTBEAT_SECONDS * 3)) if start_worker else False,
            "source_provenance": bool(source_provenance.get("verified")),
            "action_schema_provenance": (
                not os.environ.get("MUSICOPS_ACTION_SCHEMA_DIGEST")
                or os.environ.get("MUSICOPS_ACTION_SCHEMA_DIGEST", "").lower() == action_schema_digest
            ),
        }
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "ok": ready,
                "service": "musicops",
                "checks": checks,
                "build": {
                    "git_commit": os.environ.get("MUSICOPS_BUILD_COMMIT", "unknown"),
                    "git_tree": os.environ.get("MUSICOPS_BUILD_TREE", "unknown"),
                    "built_at": os.environ.get("MUSICOPS_BUILD_TIME", "unknown"),
                    "source_manifest_sha256": expected_source_manifest or "unknown",
                    "action_schema_digest": action_schema_digest,
                    "image_id": os.environ.get("MUSICOPS_IMAGE_ID", "unknown"),
                    "source_provenance": source_provenance,
                },
            },
        )

    @app.get("/actions", dependencies=[Depends(_check_token(api_token))])
    def actions(name: str | None = None, full: bool = False) -> Any:
        if name is not None and name not in registry:
            return JSONResponse(status_code=404, content={"ok": False, "detail": "action_not_found"})
        names = [name] if name is not None else sorted(registry)
        rows = [registry[action_name].effective_descriptor(context, mutation_mode) for action_name in names]
        if not full:
            rows = [{key: value for key, value in row.items() if key != "input_schema"} for row in rows]
        return {
            "ok": True,
            "schema_digest": action_schema_digest,
            "actions": rows,
            "build": {
                "git_commit": os.environ.get("MUSICOPS_BUILD_COMMIT", "unknown"),
                "git_tree": os.environ.get("MUSICOPS_BUILD_TREE", "unknown"),
                "built_at": os.environ.get("MUSICOPS_BUILD_TIME", "unknown"),
                "source_manifest_sha256": expected_source_manifest or "unknown",
                "image_id": os.environ.get("MUSICOPS_IMAGE_ID", "unknown"),
            },
        }

    @app.post("/operations", dependencies=[Depends(_check_token(api_token))])
    def create_operation(request: OperationCreateRequest) -> JSONResponse:
        action = registry.get(request.action)
        if action is None:
            return JSONResponse(status_code=404, content={"ok": False, "detail": "action_not_found"})
        try:
            validated = action.input_model.model_validate(request.input)
        except ValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "detail": "action_input_invalid",
                    "errors": exc.errors(include_context=False, include_input=False),
                },
            )
        normalized_input = validated.model_dump(mode="json")
        availability, reason = action.availability(context)
        if availability == "unavailable":
            return JSONResponse(status_code=503, content={"ok": False, "detail": "action_unavailable", "reason": reason})
        try:
            existing = store.get_operation_by_request_id(request.request_id)
            if existing is not None:
                if existing.action != action.name or existing.input != normalized_input:
                    raise RequestConflict("request_id_reused_with_different_intent")
                return JSONResponse(
                    status_code=200,
                    content={"ok": True, "reused": True, "operation": operation_payload(existing)},
                )
            if action.requires_approval and mutation_mode == "strict":
                assert action.preview is not None
                preview = action.preview(context, validated)
                approval, reused = store.create_approval(
                    action=action.name,
                    input=normalized_input,
                    preview=preview,
                    request_id=request.request_id,
                    action_schema_digest=action.schema_digest(),
                    action_contract_digest=action.contract_digest(),
                )
                return JSONResponse(
                    status_code=200 if reused else 202,
                    content={"ok": True, "reused": reused, "approval_required": True, "approval": approval_payload(approval)},
                )
            mutation_plan = action.preview(context, validated) if action.requires_approval and action.preview else None
            operation, reused = store.create_operation(
                action=action.name,
                input=normalized_input,
                request_id=request.request_id,
                max_attempts=action.max_attempts,
                mutation_plan=mutation_plan,
            )
        except RequestConflict as exc:
            return JSONResponse(status_code=409, content={"ok": False, "detail": str(exc)})
        except ActionFailure as exc:
            return JSONResponse(status_code=422, content={"ok": False, "detail": exc.code, "error": exc.to_dict()})
        return JSONResponse(
            status_code=200 if reused else 202,
            content={"ok": True, "reused": reused, "operation": operation_payload(operation)},
        )

    @app.get("/operations", dependencies=[Depends(_check_token(api_token))])
    def list_operations(
        action: str = "",
        operation_status: str = Query(default="", alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        rows = store.list_operations(action=action, status=operation_status, limit=limit)
        return {"ok": True, "operations": [operation_payload(row) for row in rows]}

    @app.get("/operations/{operation_id}", dependencies=[Depends(_check_token(api_token))])
    def get_operation(operation_id: str, full: bool = False) -> dict[str, Any]:
        operation = store.get_operation(operation_id)
        if operation is None:
            raise HTTPException(status_code=404, detail="operation_not_found")
        return {"ok": True, "operation": operation_payload(operation, full=full)}

    @app.post("/operations/{operation_id}/cancel", dependencies=[Depends(_check_token(api_token))])
    def cancel_operation(operation_id: str) -> JSONResponse:
        try:
            operation = store.cancel(operation_id)
        except ValueError as exc:
            code = 404 if str(exc) == "operation_not_found" else 409
            return JSONResponse(status_code=code, content={"ok": False, "detail": str(exc)})
        cancel_requested = operation.status == OperationStatus.RUNNING.value
        return JSONResponse(
            status_code=202 if cancel_requested else 200,
            content={
                "ok": True,
                "cancel_requested": cancel_requested,
                "operation": operation_payload(operation),
            },
        )

    @app.get("/approvals/{approval_id}", dependencies=[Depends(_check_token(api_token))])
    def get_approval(approval_id: str, full: bool = False) -> dict[str, Any]:
        approval = store.get_approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval_not_found")
        return {"ok": True, "approval": approval_payload(approval, full=full)}

    @app.get("/approvals/{approval_id}/review", response_class=HTMLResponse)
    def review_approval(approval_id: str) -> HTMLResponse:
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "img-src data:; base-uri 'none'; frame-ancestors 'none'"
            ),
        }
        approval = store.get_approval(approval_id)
        if approval is None:
            return HTMLResponse("Not found", status_code=404, headers=headers)
        try:
            approval_dict = approval_payload(approval, full=True)
            page = render_approval_html(approval_dict, cover_assets=review_cover_assets(approval_dict))
        except Exception:
            LOGGER.exception("approval_review_render_failed", extra={"approval_id": approval_id})
            return HTMLResponse("Review unavailable", status_code=500, headers=headers)
        return HTMLResponse(page, headers=headers)

    @app.post("/approvals/{approval_id}/execute", dependencies=[Depends(_check_token(api_token))])
    def execute_approval(approval_id: str, request: ApprovalExecuteRequest) -> JSONResponse:
        approval = store.get_approval(approval_id)
        if approval is None:
            return JSONResponse(status_code=404, content={"ok": False, "detail": "approval_not_found"})
        action = registry.get(approval.action)
        if action is None:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "action_removed"})
        current_schema_digest = action.schema_digest()
        current_contract_digest = action.contract_digest()
        if not approval.action_schema_digest or not approval.action_contract_digest:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "approval_provenance_missing"})
        if approval.action_schema_digest != current_schema_digest:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "approval_action_schema_changed"})
        if approval.action_contract_digest != current_contract_digest:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "approval_action_contract_changed"})
        try:
            validated = action.input_model.model_validate(approval.input)
        except ValidationError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "detail": "approval_stored_input_invalid",
                    "errors": exc.errors(include_context=False, include_input=False),
                },
            )
        if validated.model_dump(mode="json") != approval.input:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "detail": "approval_stored_input_normalization_changed"},
            )
        try:
            operation, reused = store.create_operation_from_approval(
                approval_id=approval_id,
                request_id=request.request_id,
                actor=request.actor,
                reason=request.reason,
                approved_at=request.approved_at,
                action_schema_digest=current_schema_digest,
                action_contract_digest=current_contract_digest,
                max_attempts=action.max_attempts,
            )
        except RequestConflict as exc:
            return JSONResponse(status_code=409, content={"ok": False, "detail": str(exc)})
        except ApprovalError as exc:
            return JSONResponse(status_code=409, content={"ok": False, "detail": str(exc)})
        return JSONResponse(
            status_code=200 if reused else 202,
            content={"ok": True, "reused": reused, "operation": operation_payload(operation)},
        )

    return app
