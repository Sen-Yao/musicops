from __future__ import annotations

import asyncio
import errno
import hashlib
import json
from pathlib import Path
import stat
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from conftest import TOKEN, submit, wait_for_operation
import musicops.actions as actions_module
from musicops.api import create_app
from musicops.vmomo import TelethonVmomoClient


class FakeVmomo:
    def search(self, query: str, timeout: int = 90):
        return {
            "success": True,
            "query": query,
            "sent_msg_id": 10,
            "candidates": [{"index": 1, "title": "Song"}],
            "messages": [{"id": 10, "text": "raw provider response"}],
        }

    def request_media(
        self,
        search_msg_id: int,
        index: int,
        vmomo_quality: str,
        keywords: list[str],
        timeout: int = 120,
        on_progress=None,
        cancel_requested=None,
    ):
        return {
            "success": True,
            "media_msg_id": 12,
            "candidate": {"index": index, "title": "Song (Explicit)", "artist": "Artist"},
            "candidate_identity": {
                "search_msg_id": search_msg_id,
                "candidate_index": index,
                "snapshot": {"index": index, "title": "Song (Explicit)", "artist": "Artist"},
            },
            "provider_labels": ["Explicit"],
            "quality_selection": {
                "requested": vmomo_quality,
                "selected": vmomo_quality,
                "available": ["HQ", "SQ"],
                "quality_button_clicked": True,
                "quality_menu_msg_id": 11,
                "observed_button_labels": ["HQ", "SQ"],
            },
        }

    def stage_download(
        self,
        media_msg_id: int,
        output_dir: Path,
        timeout: float = 300,
        retries: int = 2,
        deadline_monotonic=None,
        on_progress=None,
        cancel_requested=None,
    ):
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "Song.mp3"
        target.write_bytes(b"downloaded-audio")
        return {"success": True, "media_msg_id": media_msg_id, "file_path": str(target)}


def test_normalized_download_binds_candidate_and_accepts_separator_variant(tmp_path):
    candidate = {
        "index": 1,
        "title": "turn it up",
        "artist": "Orton&班克西 8ank$Yy",
        "msg_id": 42,
        "song_id": "song-1",
    }

    class SelectedCandidateVmomo(FakeVmomo):
        def request_media(self, *args, **kwargs):
            payload = super().request_media(*args, **kwargs)
            payload["candidate"] = candidate
            return payload

    app = create_app(tmp_path, TOKEN, vmomo_client=SelectedCandidateVmomo())
    manifest = {
        "provider_release_id": "release-1",
        "tracks": [{
            "provider_track_id": "track-1",
            "title": "turn it up",
            "artist": "Orton; 班克西 8ank$Yy",
            "album": "Album",
        }],
    }
    manifest_sha256 = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    search, _ = app.state.store.create_operation(
        action="download.search",
        input={"query": "turn it up"},
        request_id="seed-download-search-variant",
    )
    app.state.store.finish(
        search.id,
        status="succeeded",
        result={"provider": "vmomo", "candidates": [candidate]},
    )
    metadata, _ = app.state.store.create_operation(
        action="metadata.fetch",
        input={"provider": "musicbrainz", "provider_release_id": "release-1"},
        request_id="seed-metadata-fetch-variant",
    )
    app.state.store.finish(
        metadata.id,
        status="succeeded",
        result={"manifest": manifest, "manifest_sha256": manifest_sha256},
    )
    with TestClient(app) as client:
        response = submit(client, "download.fetch", {
            "search_operation_id": search.id,
            "candidate_index": 1,
            "metadata_operation_id": metadata.id,
            "manifest_sha256": manifest_sha256,
            "provider_track_id": "track-1",
            "vmomo_quality": "SQ",
        }, "download-fetch-variant-001")
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
    assert operation["status"] == "succeeded", operation
    assert operation["result"]["identity_match"]["classification"] == "equivalent_display_variant"
    assert operation["result"]["identity_match"]["fields"]["artist"]["status"] == "equivalent_display_variant"
    assert operation["result"]["search_msg_id"] == 42


def test_normalized_download_requires_preflight_evidence(tmp_path):
    app = create_app(tmp_path, TOKEN, vmomo_client=FakeVmomo())
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"candidate_index": 1, "vmomo_quality": "SQ"},
            "download-fetch-without-validation-001",
        )
    assert response.status_code == 422


@pytest.mark.parametrize("canonical_artist, provider_artist, classification", [
    ("Orton; 班克西 8ank$Yy", "Other Artist", "mismatch"),
    ("K/DA", "K-DA&英雄联盟&i-dle&Mimi We..", "ambiguous"),
])
def test_download_identity_differences_warn_and_allow_bound_transfer(tmp_path, canonical_artist, provider_artist, classification):
    candidate = {"index": 1, "title": "turn it up", "artist": provider_artist, "msg_id": 42}

    class SelectedCandidateVmomo(FakeVmomo):
        def request_media(self, *args, **kwargs):
            payload = super().request_media(*args, **kwargs)
            payload["candidate"] = candidate
            return payload

    app = create_app(tmp_path, TOKEN, vmomo_client=SelectedCandidateVmomo())
    manifest = {
        "provider_release_id": "release-1",
        "tracks": [{"provider_track_id": "track-1", "title": "turn it up", "artist": canonical_artist, "album": "Album"}],
    }
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    search, _ = app.state.store.create_operation(action="download.search", input={"query": "turn it up"}, request_id="seed-download-search-mismatch")
    app.state.store.finish(search.id, status="succeeded", result={"provider": "vmomo", "candidates": [candidate]})
    metadata, _ = app.state.store.create_operation(action="metadata.fetch", input={"provider": "musicbrainz", "provider_release_id": "release-1"}, request_id="seed-metadata-fetch-mismatch")
    app.state.store.finish(metadata.id, status="succeeded", result={"manifest": manifest, "manifest_sha256": digest})
    with TestClient(app) as client:
        response = submit(client, "download.fetch", {
            "search_operation_id": search.id,
            "candidate_index": 1,
            "metadata_operation_id": metadata.id,
            "manifest_sha256": digest,
            "provider_track_id": "track-1",
            "vmomo_quality": "SQ",
        }, "download-fetch-warning-001")
        fetched = wait_for_operation(client, response.json()["operation"]["operation_id"])
        assert fetched["status"] == "succeeded", fetched
        assert fetched["result"]["identity_match"]["classification"] == classification
        warnings = fetched["result"]["warnings"]
        assert warnings[0]["code"] == "identity_comparison_warning"
        assert fetched["result"]["warnings"] == warnings
        record = app.state.store.get_temporary_file(fetched["result"]["temporary_file"]["temporary_file_id"])
        assert record.metadata["warnings"] == warnings


def test_normalized_download_rejects_candidate_changed_since_search_before_transfer(tmp_path):
    class ChangedCandidateVmomo(FakeVmomo):
        staged = False

        def request_media(self, search_msg_id, index, vmomo_quality, keywords, **kwargs):
            payload = super().request_media(search_msg_id, index, vmomo_quality, keywords, **kwargs)
            payload["candidate"] = {"index": index, "title": "turn it up", "artist": "Other Artist", "msg_id": search_msg_id}
            return payload

        def stage_download(self, *args, **kwargs):
            self.staged = True
            return super().stage_download(*args, **kwargs)

    vmomo = ChangedCandidateVmomo()
    app = create_app(tmp_path, TOKEN, vmomo_client=vmomo)
    selected_candidate = {"index": 1, "title": "turn it up", "artist": "Orton&班克西 8ank$Yy", "msg_id": 10}
    search, _ = app.state.store.create_operation(action="download.search", input={"query": "turn it up"}, request_id="seed-search-changed-candidate")
    app.state.store.finish(search.id, status="succeeded", result={"candidates": [selected_candidate]})
    manifest = {"provider_release_id": "release-1", "tracks": [{"provider_track_id": "track-1", "title": "turn it up", "artist": "Orton&班克西 8ank$Yy"}]}
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    metadata, _ = app.state.store.create_operation(action="metadata.fetch", input={}, request_id="seed-metadata-changed-candidate")
    app.state.store.finish(metadata.id, status="succeeded", result={"manifest": manifest, "manifest_sha256": digest})
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {
                "search_operation_id": search.id,
                "candidate_index": 1,
                "metadata_operation_id": metadata.id,
                "manifest_sha256": digest,
                "provider_track_id": "track-1",
                "vmomo_quality": "SQ",
            },
            "download-fetch-changed-candidate-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "download_candidate_changed_since_search"
    assert vmomo.staged is False


def test_download_search_and_fetch_return_temporary_reference(tmp_path):
    app = create_app(tmp_path, TOKEN, vmomo_client=FakeVmomo())
    with TestClient(app) as client:
        search_response = submit(client, "download.search", {"query": "Song"}, "download-search-001")
        search = wait_for_operation(client, search_response.json()["operation"]["operation_id"])
        assert search["result"]["provider"] == "vmomo"
        assert "messages" not in search["result"]

        fetch_response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only", "keywords": ["Song"]},
            "download-fetch-001",
        )
        fetch = wait_for_operation(client, fetch_response.json()["operation"]["operation_id"])
        temporary = fetch["result"]["temporary_file"]
        assert temporary["available"] is True
        assert len(temporary["sha256"]) == 64
        assert "path" not in temporary
        assert fetch["result"]["quality_selection"]["selected"] == "SQ"
        assert fetch["result"]["quality_selection"]["quality_button_clicked"] is True
        assert fetch["result"]["candidate_identity"]["search_msg_id"] == 10
        assert fetch["result"]["candidate_identity"]["candidate_index"] == 1
        assert fetch["result"]["provider_labels"] == ["Explicit"]

        inspect_response = submit(client, "temporary.inspect", {"temporary_file_id": temporary["temporary_file_id"]}, "temporary-inspect-001")
        inspected = wait_for_operation(client, inspect_response.json()["operation"]["operation_id"])
        assert inspected["result"]["temporary_file"]["exists"] is True

        delete_response = submit(client, "temporary.delete", {"temporary_file_id": temporary["temporary_file_id"]}, "temporary-delete-001")
        deleted = wait_for_operation(client, delete_response.json()["operation"]["operation_id"])
        assert deleted["result"]["temporary_file"]["available"] is False


def test_download_contract_rejects_removed_fields_and_missing_vmomo_quality(tmp_path):
    app = create_app(tmp_path, TOKEN, vmomo_client=FakeVmomo())
    with TestClient(app) as client:
        old_search = submit(client, "download.search", {"query": "Song", "quality": "fast"}, "old-search-001")
        direct_fetch = submit(client, "download.fetch", {"media_msg_id": 12, "vmomo_quality": "SQ", "operation_profile": "audio_only"}, "direct-fetch-001")
        missing_quality = submit(client, "download.fetch", {"search_msg_id": 10, "candidate_index": 1, "operation_profile": "audio_only"}, "missing-quality-001")

    assert old_search.status_code == 422
    assert direct_fetch.status_code == 422
    assert missing_quality.status_code == 422


def test_download_quality_selection_gap_is_a_terminal_failure(tmp_path):
    class MissingQualityVmomo(FakeVmomo):
        def request_media(self, search_msg_id, index, vmomo_quality, keywords, timeout=120, **_kwargs):
            return {
                "success": False,
                "error": "quality_selection_unavailable",
                "failure_stage": "quality_selection",
                "requested_quality": vmomo_quality,
                "available_qualities": ["HQ"],
            }

    app = create_app(tmp_path, TOKEN, vmomo_client=MissingQualityVmomo())
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only"},
            "missing-sq-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "quality_selection_unavailable"
    assert operation["error"]["detail"]["available_qualities"] == ["HQ"]


def test_download_rejects_a_mismatched_selected_quality_before_staging(tmp_path):
    class MismatchedQualityVmomo(FakeVmomo):
        staged = False

        def request_media(self, search_msg_id, index, vmomo_quality, keywords, timeout=120, **kwargs):
            payload = super().request_media(search_msg_id, index, vmomo_quality, keywords, timeout, **kwargs)
            payload["quality_selection"]["selected"] = "HQ"
            return payload

        def stage_download(self, media_msg_id, output_dir, timeout=300, retries=2, **kwargs):
            self.staged = True
            return super().stage_download(media_msg_id, output_dir, timeout, retries, **kwargs)

    vmomo = MismatchedQualityVmomo()
    app = create_app(tmp_path, TOKEN, vmomo_client=vmomo)
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only"},
            "mismatched-sq-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "quality_selection_unavailable"
    assert vmomo.staged is False


def test_download_trusts_selected_quality_not_provider_filename_label(tmp_path):
    class ConflictingFilenameVmomo(FakeVmomo):
        def stage_download(self, media_msg_id, output_dir, timeout=300, retries=2, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            target = output_dir / "Song HQ.flac"
            target.write_bytes(b"downloaded-audio")
            return {"success": True, "media_msg_id": media_msg_id, "file_path": str(target)}

    app = create_app(tmp_path, TOKEN, vmomo_client=ConflictingFilenameVmomo())
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only"},
            "sq-hq-filename-conflict-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    assert operation["status"] == "succeeded"
    assert operation["result"]["quality_selection"]["selected"] == "SQ"
    assert operation["result"]["quality_filename_warning"] == {
        "code": "filename_quality_untrusted",
        "filename_quality": "HQ",
        "selected_quality": "SQ",
    }


def test_download_failure_removes_partial_staging_files(tmp_path):
    class PartialDownloadVmomo(FakeVmomo):
        def stage_download(self, media_msg_id, output_dir, timeout=300, retries=2, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "partial.flac").write_bytes(b"partial")
            return {"success": False, "error": "download_failed"}

    app = create_app(tmp_path, TOKEN, vmomo_client=PartialDownloadVmomo())
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only"},
            "partial-download-001",
        )
        operation_id = response.json()["operation"]["operation_id"]
        operation = wait_for_operation(client, operation_id)

    assert operation["status"] == "failed"
    assert not (app.state.action_context.temporary_dir / operation_id).exists()


def test_vmomo_parser_accepts_telegram_keycap_candidate_numbers(tmp_path):
    client = TelethonVmomoClient(tmp_path / "session", "123", "hash")
    message = SimpleNamespace(
        id=41881,
        message="1️⃣ Pray 4 Love - Travis Scott&The Weeknd\n2️⃣ THE SCOTTS (Explicit) - THE SCOTTS&Travis Scott\n🔟 TKN - ROSALIA&Travis Scott",
        buttons=[[SimpleNamespace(data=b"one", text="1"), SimpleNamespace(data=b"two", text="2")]],
    )
    candidates = client._parse_candidates(message)
    assert [(row["index"], row["title"]) for row in candidates] == [(1, "Pray 4 Love"), (2, "THE SCOTTS (Explicit)"), (10, "TKN")]


def test_vmomo_request_media_clicks_the_exact_quality_control(tmp_path, monkeypatch):
    class Button:
        def __init__(self, text, data=b""):
            self.text = text
            self.data = data
            self.clicked = False

        async def click(self):
            self.clicked = True

    candidate_button = Button("1", b"candidate")
    hq_button = Button("HQ 高品质")
    sq_button = Button("SQ 无损")
    search = SimpleNamespace(
        id=10,
        message="1️⃣ Song (Explicit) - Artist",
        buttons=[[candidate_button]],
        media=None,
    )
    quality_menu = SimpleNamespace(id=101, message="请选择音质", buttons=[[hq_button, sq_button]], media=None)
    media = SimpleNamespace(
        id=102,
        message="Song (Explicit) - Artist",
        buttons=[],
        media=object(),
        file=SimpleNamespace(name="Song.flac", size=100, mime_type="audio/flac"),
        date="",
    )

    class Telegram:
        async def get_messages(self, _bot, ids):
            assert ids == 10
            return search

        async def iter_messages(self, _bot, limit, min_id=None):
            if limit == 1 and min_id is None:
                yield SimpleNamespace(id=100)
            elif sq_button.clicked:
                yield media
            else:
                yield quality_menu

        async def disconnect(self):
            return None

    async def fake_client():
        return Telegram()

    async def no_sleep(_seconds):
        return None

    client = TelethonVmomoClient(tmp_path / "session", "123", "hash")
    monkeypatch.setattr(client, "_client", fake_client)
    monkeypatch.setattr("musicops.vmomo.asyncio.sleep", no_sleep)

    result = client.request_media(10, 1, "SQ", ["Song"], timeout=1)

    assert result["success"] is True
    assert candidate_button.clicked is True
    assert sq_button.clicked is True
    assert hq_button.clicked is False
    assert result["quality_selection"] == {
        "requested": "SQ",
        "selected": "SQ",
        "available": ["HQ", "SQ"],
        "quality_button_clicked": True,
        "quality_menu_msg_id": 101,
        "observed_button_labels": ["HQ 高品质", "SQ 无损"],
    }
    assert result["provider_labels"] == ["Explicit"]


def test_vmomo_quality_click_exception_is_safe_provider_failure(tmp_path, monkeypatch):
    class Button:
        def __init__(self, text, *, fail=False):
            self.text = text
            self.data = b"callback"
            self.fail = fail

        async def click(self):
            if self.fail:
                raise ConnectionResetError(errno.ECONNRESET, "provider-secret-detail")

    candidate_button = Button("1")
    sq_button = Button("SQ 无损", fail=True)
    search = SimpleNamespace(id=10, message="1️⃣ Song - Artist", buttons=[[candidate_button]], media=None)
    quality_menu = SimpleNamespace(id=101, message="请选择音质", buttons=[[sq_button]], media=None)

    class Telegram:
        async def get_messages(self, _bot, ids):
            return search

        async def iter_messages(self, _bot, limit, min_id=None):
            if limit == 1 and min_id is None:
                yield SimpleNamespace(id=100)
            else:
                yield quality_menu

        async def disconnect(self):
            return None

    async def fake_client():
        return Telegram()

    client = TelethonVmomoClient(tmp_path / "session", "123", "hash")
    monkeypatch.setattr(client, "_client", fake_client)

    result = client.request_media(10, 1, "SQ", [], timeout=1)

    assert result["success"] is False
    assert result["error"] == "provider_connection_failed"
    assert result["failure_stage"] == "provider_io"
    assert result["provider_error"] == {
        "error": "provider_connection_failed",
        "error_type": "ConnectionResetError",
        "errno": errno.ECONNRESET,
    }
    assert "provider-secret-detail" not in str(result)


def test_download_transfer_failure_retains_explicit_quality_and_candidate_evidence(tmp_path):
    class FailedTransferVmomo(FakeVmomo):
        def stage_download(self, media_msg_id, output_dir, timeout=300, retries=2, **_kwargs):
            return {
                "success": False,
                "error": "download_failed",
                "failure_stage": "media_download",
                "media_msg_id": media_msg_id,
                "attempts": 3,
                "errors": [
                    {
                        "stage": "media_download",
                        "attempt": 1,
                        "error": "attempt_timeout",
                        "error_type": "TimeoutError",
                        "duration_seconds": 10.0,
                    }
                ],
            }

    app = create_app(tmp_path, TOKEN, vmomo_client=FailedTransferVmomo())
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only"},
            "download-evidence-failure-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    detail = operation["error"]["detail"]
    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "download_fetch_failed"
    assert detail["failure_stage"] == "media_download"
    assert detail["media_msg_id"] == 12
    assert detail["requested_quality"] == "SQ"
    assert detail["selected_quality"] == "SQ"
    assert detail["quality_selection"]["quality_button_clicked"] is True
    assert detail["candidate_identity"]["search_msg_id"] == 10
    assert detail["candidate_identity"]["candidate_index"] == 1
    assert detail["errors"][0]["error"] == "attempt_timeout"


def test_download_fetch_uses_one_total_deadline_across_provider_stages(tmp_path, monkeypatch):
    class Clock:
        monotonic_value = 100.0

        @classmethod
        def monotonic(cls):
            return cls.monotonic_value

        @staticmethod
        def time():
            return 1_000.0

    class BudgetVmomo(FakeVmomo):
        transfer_call = None

        def request_media(self, *args, **kwargs):
            result = super().request_media(*args, **kwargs)
            Clock.monotonic_value += 12.0
            return result

        def stage_download(self, media_msg_id, output_dir, **kwargs):
            self.transfer_call = {"media_msg_id": media_msg_id, **kwargs}
            return {
                "success": False,
                "error": "download_failed",
                "failure_stage": "media_download",
                "media_msg_id": media_msg_id,
                "errors": [],
            }

    vmomo = BudgetVmomo()
    monkeypatch.setattr(actions_module, "time", Clock)
    app = create_app(tmp_path, TOKEN, vmomo_client=vmomo)
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only", "timeout": 30, "retries": 2},
            "download-total-deadline-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    assert operation["status"] == "failed"
    assert vmomo.transfer_call is not None
    assert vmomo.transfer_call["timeout"] == 18.0
    assert vmomo.transfer_call["deadline_monotonic"] == 130.0
    assert vmomo.transfer_call["retries"] == 2


def test_running_download_cancel_request_reaches_provider(tmp_path):
    started = threading.Event()

    class CancellableVmomo(FakeVmomo):
        def request_media(
            self,
            search_msg_id,
            index,
            vmomo_quality,
            keywords,
            timeout=120,
            cancel_requested=None,
            **_kwargs,
        ):
            started.set()
            deadline = time.time() + 2
            while time.time() < deadline:
                if cancel_requested and cancel_requested():
                    return {
                        "success": False,
                        "error": "operation_cancelled",
                        "failure_stage": "quality_selection",
                    }
                time.sleep(0.01)
            raise AssertionError("cancel request did not reach provider")

    app = create_app(tmp_path, TOKEN, vmomo_client=CancellableVmomo())
    with TestClient(app) as client:
        response = submit(
            client,
            "download.fetch",
            {"search_msg_id": 10, "candidate_index": 1, "vmomo_quality": "SQ", "operation_profile": "audio_only"},
            "download-cancel-001",
        )
        operation_id = response.json()["operation"]["operation_id"]
        assert started.wait(1)
        cancelled = client.post(f"/operations/{operation_id}/cancel", headers={"Authorization": f"Bearer {TOKEN}"})
        assert cancelled.status_code == 202
        assert cancelled.json()["cancel_requested"] is True
        operation = wait_for_operation(client, operation_id)

    assert operation["status"] == "cancelled"
    assert operation["error"]["code"] == "operation_cancelled"


def test_vmomo_quality_failure_reports_only_sanitized_button_observations(tmp_path, monkeypatch):
    class Button:
        def __init__(self, text):
            self.text = text
            self.data = b"callback-must-not-be-returned"

        async def click(self):
            return None

    candidate_button = Button("1")
    unknown_quality = Button("无损下载")
    search = SimpleNamespace(id=10, message="1️⃣ Song - Artist", buttons=[[candidate_button]], media=None)
    response = SimpleNamespace(
        id=102,
        message="provider message body must not be returned",
        buttons=[[unknown_quality]],
        media=object(),
        file=SimpleNamespace(name="Song.flac", size=100, mime_type="audio/flac"),
        date="",
    )

    class Telegram:
        async def get_messages(self, _bot, ids):
            return search

        async def iter_messages(self, _bot, limit, min_id=None):
            if limit == 1 and min_id is None:
                yield SimpleNamespace(id=100)
            else:
                yield response

        async def disconnect(self):
            return None

    async def fake_client():
        return Telegram()

    client = TelethonVmomoClient(tmp_path / "session", "123", "hash")
    monkeypatch.setattr(client, "_client", fake_client)

    result = client.request_media(10, 1, "SQ", [], timeout=1)

    assert result["success"] is False
    assert result["observed_button_labels"] == ["无损下载"]
    assert result["media_received_before_selection"] is True
    assert "provider message body" not in str(result)
    assert "callback-must-not-be-returned" not in str(result)


def test_vmomo_missing_quality_controls_is_retryable_provider_timeout(tmp_path, monkeypatch):
    candidate_button = SimpleNamespace(click=lambda: None, text="1", data=b"candidate")
    search = SimpleNamespace(id=10, message="1️⃣ Song - Artist", buttons=[[candidate_button]], media=None)

    async def click_candidate():
        return None

    candidate_button.click = click_candidate

    class Telegram:
        async def get_messages(self, _bot, ids):
            return search

        async def iter_messages(self, _bot, limit, min_id=None):
            if limit == 1 and min_id is None:
                yield SimpleNamespace(id=100)

        async def disconnect(self):
            return None

    async def fake_client():
        return Telegram()

    async def no_sleep(_seconds):
        return None

    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("musicops.vmomo.time", SimpleNamespace(monotonic=lambda: next(ticks, 2.0)))
    monkeypatch.setattr("musicops.vmomo.asyncio.sleep", no_sleep)
    monkeypatch.setattr(client := TelethonVmomoClient(tmp_path / "session", "123", "hash"), "_client", fake_client)

    result = client.request_media(10, 1, "SQ", [], timeout=1)

    assert result["success"] is False
    assert result["error"] == "provider_timeout"
    assert result["failure_stage"] == "quality_selection"


def test_vmomo_transfer_timeout_has_nonempty_attempt_diagnostics(tmp_path, monkeypatch):
    media = SimpleNamespace(
        media=object(),
        file=SimpleNamespace(name="Song.flac", size=100, mime_type="audio/flac"),
    )

    class Telegram:
        async def get_messages(self, _bot, ids):
            return media

        def iter_download(self, _media, offset, request_size):
            async def fail():
                if False:
                    yield b""
                raise asyncio.TimeoutError()

            return fail()

        async def disconnect(self):
            return None

    async def fake_client():
        return Telegram()

    client = TelethonVmomoClient(tmp_path / "session", "123", "hash")
    monkeypatch.setattr(client, "_client", fake_client)

    result = client.stage_download(12, tmp_path / "output", timeout=30, retries=2)

    assert result["success"] is False
    assert result["error"] == "download_failed"
    assert result["attempts"] == 3
    assert [row["error"] for row in result["errors"]] == ["attempt_timeout"] * 3
    assert all(row["error_type"] == "TimeoutError" for row in result["errors"])


def test_vmomo_copies_read_only_session_to_private_runtime(tmp_path, monkeypatch):
    source = tmp_path / "mounted.session"
    source.write_bytes(b"telegram-session-seed")
    source.chmod(0o400)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("MUSICOPS_TELEGRAM_SESSION_PATH", str(source))

    client = TelethonVmomoClient.from_env(tmp_path / "data")

    assert client.session_path != source
    assert client.session_path.read_bytes() == b"telegram-session-seed"
    assert stat.S_IMODE(client.session_path.stat().st_mode) == 0o600
    assert client.availability() == ("available", "")


def test_vmomo_preserves_existing_runtime_session(tmp_path, monkeypatch):
    source = tmp_path / "mounted.session"
    source.write_bytes(b"original-seed")
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("MUSICOPS_TELEGRAM_SESSION_PATH", str(source))
    first = TelethonVmomoClient.from_env(tmp_path / "data")
    first.session_path.write_bytes(b"runtime-state")
    source.write_bytes(b"replacement-seed")

    second = TelethonVmomoClient.from_env(tmp_path / "data")

    assert second.session_path.read_bytes() == b"runtime-state"


def test_vmomo_requires_existing_session_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("MUSICOPS_TELEGRAM_SESSION_PATH", str(tmp_path / "missing.session"))

    with pytest.raises(RuntimeError, match="telegram_session_file_missing"):
        TelethonVmomoClient.from_env(tmp_path / "data")
