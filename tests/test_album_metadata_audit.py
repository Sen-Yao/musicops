from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from conftest import TOKEN, submit, wait_for_operation
from musicops.api import create_app
from musicops.library import LibraryInspector


def _audio_files(root: Path, names: list[str]) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")


def test_scan_missing_album_is_complete_and_treats_whitespace_as_missing(tmp_path, monkeypatch):
    _audio_files(tmp_path, ["Artist/one.flac", "Artist/two.flac", "Artist/three.flac"])
    inspector = LibraryInspector(tmp_path)
    evidence = {
        "Artist/one.flac": {"ok": True, "exists": True, "format": "flac", "metadata": {"title": "One", "artist": "Artist", "album": "Album"}, "warnings": []},
        "Artist/two.flac": {"ok": True, "exists": True, "format": "flac", "metadata": {"title": "Two", "artist": "Artist", "album": "   ", "date": "2024", "track_number": "2"}, "warnings": []},
        "Artist/three.flac": {"ok": True, "exists": True, "format": "flac", "metadata": {"title": "Three", "artist": "Artist", "album_artist": "Artist"}, "warnings": []},
    }
    monkeypatch.setattr(inspector, "evidence_for_file", lambda path, **_: evidence[path])

    result = inspector.scan_missing_album()

    assert result["scan_status"] == "complete"
    assert result["complete"] is True
    assert result["total_audio_files"] == result["scanned_count"] == 3
    assert result["missing_album_count"] == 2
    assert {row["file_path"] for row in result["missing_items"]} == {"Artist/two.flac", "Artist/three.flac"}
    assert result["missing_items"][0]["metadata"]


def test_scan_reports_budget_and_read_errors_as_partial(tmp_path, monkeypatch):
    _audio_files(tmp_path, ["one.flac", "0-two.flac", "three.flac", "four.flac"])
    inspector = LibraryInspector(tmp_path)

    def evidence(path, **_):
        if path == "0-two.flac":
            return {"ok": False, "exists": True, "error_type": "PermissionError", "detail": "denied", "metadata": {}, "warnings": []}
        return {"ok": True, "exists": True, "format": "flac", "metadata": {"title": path, "album": "Album"}, "warnings": []}

    monkeypatch.setattr(inspector, "evidence_for_file", evidence)
    result = inspector.scan_missing_album(max_scanned=3)

    assert result["scan_status"] == "partial"
    assert result["complete"] is False
    assert result["total_audio_files"] == 4
    assert result["scanned_count"] == 3
    assert result["failed_count"] == 1
    assert result["skipped_count"] == 1
    assert result["skipped"][0]["reason"] == "scan_budget_exhausted"
    assert result["warnings"][0]["code"] == "scan_budget_exhausted"


def test_scan_reports_cancellation_without_claiming_full_library(tmp_path, monkeypatch):
    _audio_files(tmp_path, ["one.flac", "two.flac"])
    inspector = LibraryInspector(tmp_path)
    monkeypatch.setattr(inspector, "evidence_for_file", lambda path, **_: {"ok": True, "exists": True, "metadata": {"album": "Album"}, "warnings": []})
    result = inspector.scan_missing_album(cancel_check=lambda: True)

    assert result["scan_status"] == "partial"
    assert result["complete"] is False
    assert result["scanned_count"] == 0
    assert result["skipped_count"] == 2
    assert all(row["reason"] == "operation_cancelled" for row in result["skipped"])


def test_audit_returns_traceable_ambiguous_candidates_without_writing(tmp_path):
    class AuditLibrary:
        def __init__(self):
            self.writes = 0

        def scan_missing_album(self, **_):
            return {
                "ok": True,
                "scan_status": "complete",
                "complete": True,
                "total_audio_files": 1,
                "scanned_count": 1,
                "missing_album_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "entries": [],
                "missing_items": [{"file_path": "Artist/song.flac", "metadata": {"title": "Song", "artist": "Artist"}}],
                "failures": [],
                "skipped": [],
                "warnings": [],
            }

        def metadata_patch(self, **_):
            self.writes += 1
            raise AssertionError("audit must not write")

    class Provider:
        def configured(self):
            return True

        def search(self, **_):
            return {
                "ok": True,
                "source_sha256": "s" * 64,
                "fetched_at": 1.0,
                "candidates": [
                    {"provider": "musicbrainz", "provider_release_id": "r1", "title": "Album One", "album_artist": "Artist"},
                    {"provider": "musicbrainz", "provider_release_id": "r2", "title": "Album Two", "album_artist": "Artist"},
                ],
            }

        def fetch(self, **kwargs):
            return {
                "ok": True,
                "manifest_sha256": "m" * 64,
                "manifest": {"tracks": [{"title": "Song", "artist": "Artist", "track_number": 1}]},
            }

    library = AuditLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, metadata_provider=Provider())
    with TestClient(app) as client:
        response = submit(client, "library.album_metadata_audit", {}, "album-audit-ambiguous-001")
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
    assert operation["status"] == "succeeded"
    item = operation["result"]["missing_items"][0]
    assert item["candidate_status"] == "needs_manual_review"
    assert len(item["candidates"]) == 2
    assert item["candidates"][0]["source"]["provider_release_id"] == "r1"
    assert item["candidates"][0]["match_evidence"]["track_matches"]
    assert operation["result"]["write_back"]["performed"] is False
    assert library.writes == 0
