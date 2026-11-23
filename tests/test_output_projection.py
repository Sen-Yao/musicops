from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from conftest import TOKEN, submit, wait_for_operation
from musicops.api import create_app
from musicops.library import LibraryInspector


def test_library_search_requests_only_metadata_and_audio(tmp_path, monkeypatch):
    root = tmp_path / "music"
    root.mkdir()
    (root / "One.flac").write_bytes(b"fixture")
    inspector = LibraryInspector(root)
    requested = []

    def evidence(file_path, *, include=None, **_kwargs):
        requested.append(set(include or []))
        return {"metadata": {"title": "World", "artist": "Artist", "album": "Album"}, "duration": "03:00", "warnings": []}

    monkeypatch.setattr(inspector, "evidence_for_file", evidence)
    result = inspector.search_files("ＷＯＲＬＤ")
    assert result["result_count"] == 1
    assert requested == [{"metadata", "audio"}]


def test_library_summary_omits_cover_data_and_lyrics_text(tmp_path, monkeypatch):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "One.mp3"
    path.write_bytes(b"fixture")

    class Tags(dict):
        def keys(self):
            return super().keys()

    class Info:
        length = 123

    class Audio:
        tags = Tags({"title": ["One"], "lyrics": ["[00:01]line"], "covr": [b"\x89PNG\r\n\x1a\ncontent"]})
        info = Info()

    monkeypatch.setattr("musicops.library.MutagenFile", lambda _path: Audio())
    inspector = LibraryInspector(root)
    summary = inspector.evidence_for_file("One.mp3")
    expanded = inspector.evidence_for_file("One.mp3", include={"metadata", "lyrics_text", "cover_data"})
    assert "text" not in summary["lyrics"]
    assert "data_uri" not in summary["cover"]
    assert summary["cover"]["sha256"]
    assert expanded["lyrics"]["text"] == "[00:01]line"
    assert expanded["cover"]["data_uri"].startswith("data:image/png;base64,")


def test_operation_default_output_summarizes_large_service_payload(tmp_path):
    lyrics = "[00:01.00]" + "x" * 2000

    class Provider:
        def fetch(self, **kwargs):
            return {"ok": True, "canonical_lrc": lyrics, "canonical_lrc_sha256": "a" * 64, **kwargs}

        def search(self, **kwargs):
            return {"ok": True, "candidates": [], **kwargs}

    app = create_app(tmp_path, TOKEN, lyrics_provider=Provider())
    with TestClient(app) as client:
        response = submit(client, "lyrics.fetch", {"provider": "lrclib", "provider_id": "1"}, "large-output-0001")
        operation_id = response.json()["operation"]["operation_id"]
        wait_for_operation(client, operation_id)
        summary = client.get(f"/operations/{operation_id}", headers={"Authorization": f"Bearer {TOKEN}"}).json()["operation"]
        full = client.get(f"/operations/{operation_id}?full=true", headers={"Authorization": f"Bearer {TOKEN}"}).json()["operation"]
        assert summary["result"]["canonical_lrc"]["omitted"] is True
        assert full["result"]["canonical_lrc"] == lyrics


def test_metadata_cover_bytes_require_full_operation_projection(tmp_path):
    cover_base64 = "a" * 2000

    class Provider:
        def configured(self):
            return True

        def fetch(self, **kwargs):
            manifest = {"provider": "musicbrainz", "provider_release_id": kwargs["provider_release_id"], "tracks": []}
            return {
                "ok": True,
                "manifest": manifest,
                "manifest_sha256": "b" * 64,
                "cover": {"status": "available", "cover_base64": cover_base64},
                **kwargs,
            }

        def search(self, **kwargs):
            return {"ok": True, "candidates": [], **kwargs}

    app = create_app(tmp_path, TOKEN, metadata_provider=Provider())
    with TestClient(app) as client:
        response = submit(
            client,
            "metadata.fetch",
            {"provider": "musicbrainz", "provider_release_id": "release-1"},
            "metadata-cover-projection-0001",
        )
        operation_id = response.json()["operation"]["operation_id"]
        wait_for_operation(client, operation_id)
        summary = client.get(f"/operations/{operation_id}", headers={"Authorization": f"Bearer {TOKEN}"}).json()["operation"]
        full = client.get(f"/operations/{operation_id}?full=true", headers={"Authorization": f"Bearer {TOKEN}"}).json()["operation"]

    assert summary["result"]["cover"]["cover_base64"]["omitted"] is True
    assert full["result"]["cover"]["cover_base64"] == cover_base64
