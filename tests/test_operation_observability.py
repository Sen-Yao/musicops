from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from conftest import FakeLibrary, HEADERS, TOKEN, submit, wait_for_operation
from musicops.api import create_app


def test_queued_operation_reports_single_worker_blocker_and_position(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class BlockingLibrary(FakeLibrary):
        def search_files(self, query: str, limit: int, max_scanned: int):
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release blocking operation")
            return super().search_files(query, limit, max_scanned)

    app = create_app(tmp_path, TOKEN, library_inspector=BlockingLibrary())
    with TestClient(app) as client:
        first = submit(client, "library.search", {"query": "One"}, "queue-first-001")
        first_id = first.json()["operation"]["operation_id"]
        assert started.wait(1)
        try:
            second = submit(client, "library.search", {"query": "Two"}, "queue-second-001")
            second_payload = second.json()["operation"]

            assert second_payload["status"] == "queued"
            assert second_payload["worker_capacity"] == 1
            assert second_payload["queue"]["position"] == 1
            assert second_payload["queue"]["reason"] == "single_worker_capacity"
            assert second_payload["queue"]["blocked_by"]["operation_id"] == first_id
            assert second_payload["queue"]["blocked_by"]["action"] == "library.search"
            assert second_payload["queue"]["blocked_by"]["stage"] == "action_running"
        finally:
            release.set()

        assert wait_for_operation(client, first_id)["status"] == "succeeded"
        assert wait_for_operation(client, second_payload["operation_id"])["status"] == "succeeded"


def test_operation_events_expose_only_public_progress_fields(tmp_path):
    app = create_app(tmp_path, TOKEN, start_worker=False)
    operation, _ = app.state.store.create_operation(
        action="library.search",
        input={"query": "Song"},
        request_id="event-projection-001",
    )
    app.state.store.append_operation_event(
        operation.id,
        "progress",
        {
            "stage": "provider_connect",
            "requested_quality": "SQ",
            "candidate": {"raw_provider_text": "must-not-leak"},
            "provider_message": "must-not-leak",
        },
    )

    with TestClient(app) as client:
        response = client.get(f"/operations/{operation.id}?full=true", headers=HEADERS)

    payload = response.json()["operation"]
    assert payload["last_event"]["payload"] == {
        "stage": "provider_connect",
        "requested_quality": "SQ",
    }
    assert payload["events"][-1]["payload"] == payload["last_event"]["payload"]
    assert "must-not-leak" not in response.text


def test_failed_operation_with_string_detail_still_has_valid_projection(tmp_path):
    app = create_app(tmp_path, TOKEN, start_worker=False)
    operation, _ = app.state.store.create_operation(
        action="library.search",
        input={"query": "Song"},
        request_id="string-error-detail-001",
    )
    app.state.store.finish(
        operation.id,
        status="failed",
        error={"code": "provider_failed", "detail": "opaque", "retryable": True},
    )

    with TestClient(app) as client:
        response = client.get(f"/operations/{operation.id}", headers=HEADERS)

    assert response.status_code == 200
    payload = response.json()["operation"]
    assert payload["status"] == "failed"
    assert payload["progress"]["stage"] == "failed"
    assert "payload" not in payload["last_event"]
