from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

from conftest import FakeNavidrome, HEADERS, TOKEN, approve, submit, wait_for_operation
from musicops.api import create_app
from musicops.approval_renderer import render_approval
from musicops.actions import ActionFailure
from musicops.library import LibraryInspector
from musicops.normalized_import import _identity_compatible, _masked_resolution_audit


@pytest.mark.parametrize("canonical_artist", ["The Weeknd & Playboi Carti", "The Weeknd with Playboi Carti"])
def test_download_identity_accepts_normalized_collaborator_artist(canonical_artist):
    assert _identity_compatible(
        {"title": "Timeless", "artist": canonical_artist},
        {"title": "Timeless (Explicit)", "artist": "The Weeknd&Playboi Carti"},
    )


@pytest.mark.parametrize("provider_artist", ["The Weeknd&Playboi Cart", "Different Artist Name.."])
def test_download_identity_rejects_unmarked_or_unrelated_partial_artist(provider_artist):
    assert not _identity_compatible(
        {"title": "Timeless", "artist": "The Weeknd with Playboi Carti"},
        {"title": "Timeless (Explicit)", "artist": provider_artist},
    )


def test_download_identity_accepts_explicit_truncated_collaborator_artist():
    result = _identity_compatible(
        {"title": "Timeless", "artist": "The Weeknd with Playboi Carti"},
        {"title": "Timeless (Explicit)", "artist": "The Weeknd&Playboi Cart.."},
    )
    assert result is True


def _sha256_json(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _audio_fixture(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
            "-metadata", "title=Untrusted Download Title",
            "-metadata", "artist=Untrusted Artist",
            "-metadata", "comment=provider-private-comment",
            "-metadata", "lyrics=[00:01.00]unreviewed lyrics",
            str(path),
        ],
        check=True,
    )


def test_identity_comparison_accepts_traditional_simplified_but_not_fuzzy_artist_matches():
    assert _identity_compatible(
        {"title": "淚海", "artist": "許茹芸", "album": "淚海"},
        {"title": "泪海", "artist": "许茹芸", "album": "泪海"},
    )
    assert not _identity_compatible(
        {"title": "淚海", "artist": "許茹芸"},
        {"title": "泪海", "artist": "许茹芸 & 另一位艺人"},
    )
    assert not _identity_compatible(
        {"title": "淚海", "artist": "許茹芸"},
        {"title": "泪海 Remix", "artist": "许茹芸"},
    )


@pytest.mark.parametrize("suffix", ["mp3", "m4a"])
def test_allowlist_normalization_supports_common_download_formats(tmp_path, suffix):
    source = tmp_path / f"source.{suffix}"
    output = tmp_path / "staged" / f"Song - Artist.{suffix}"
    _audio_fixture(source)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata = {
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "album_artist": "Artist",
        "date": "2026",
        "track_number": "1",
        "track_total": "2",
        "disc_number": "1",
        "disc_total": "1",
    }

    result = LibraryInspector(tmp_path / "library").prepare_staged_import(
        source_path=source,
        output_path=output,
        expected_source_sha256=source_sha256,
        metadata=metadata,
        lyrics="[00:01.00]Reviewed",
    )

    assert result["ok"] is True, result
    evidence = LibraryInspector(output.parent).evidence_for_file(
        output.name,
        include={"metadata", "lyrics_text", "cover_summary"},
    )
    assert evidence["metadata"] == metadata
    assert evidence["lyrics"]["text"] == "[00:01.00]Reviewed"
    assert evidence["cover"]["present"] is False
    assert result["tag_audit"]["unexpected_tag_keys"] == []


def _manifest(*, cover: bool) -> tuple[dict, dict, str]:
    cover_data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4l8AAAAASUVORK5CYII=") if cover else b""
    cover_payload = (
        {
            "status": "available",
            "provider": "cover_art_archive",
            "source_url": "https://cover.test/release/release-1/front",
            "media_type": "image/png",
            "size": len(cover_data),
            "sha256": hashlib.sha256(cover_data).hexdigest(),
            "cover_base64": base64.b64encode(cover_data).decode("ascii"),
        }
        if cover
        else {
            "status": "unavailable",
            "provider": "cover_art_archive",
            "source_url": "https://cover.test/release/release-1/front",
            "reason": "not_found",
            "media_type": "",
            "size": 0,
            "sha256": "",
        }
    )
    track = {
        "provider_track_id": "track-1",
        "provider_recording_id": "recording-1",
        "title": "Canonical Song",
        "artist": "Future",
        "album": "Canonical Album",
        "album_artist": "Future",
        "date": "2026-07-20",
        "track_number": 1,
        "track_total": 2,
        "disc_number": 1,
        "disc_total": 1,
    }
    manifest = {
        "provider": "musicbrainz",
        "provider_release_id": "release-1",
        "release_group_id": "release-group-1",
        "album": "Canonical Album",
        "album_artist": "Future",
        "date": "2026-07-20",
        "disc_total": 1,
        "track_count": 2,
        "tracks": [
            track,
            {
                **track,
                "provider_track_id": "track-2",
                "provider_recording_id": "recording-2",
                "title": "Second Song",
                "track_number": 2,
            },
        ],
        "cover": {key: value for key, value in cover_payload.items() if key != "cover_base64"},
        "source": {
            "provider": "musicbrainz",
            "provider_release_id": "release-1",
            "source_sha256": "a" * 64,
        },
    }
    return manifest, cover_payload, _sha256_json(manifest)


class MetadataProvider:
    def __init__(self, *, cover: bool = True):
        self.manifest, self.cover, self.digest = _manifest(cover=cover)

    def configured(self):
        return True

    def search(self, **kwargs):
        return {
            "ok": True,
            "provider": "musicbrainz",
            "candidates": [{"provider_release_id": "release-1", "title": "Canonical Album"}],
            **kwargs,
        }

    def fetch(self, *, provider, provider_release_id):
        assert provider == "musicbrainz"
        assert provider_release_id == "release-1"
        return {
            "ok": True,
            "provider": provider,
            "provider_release_id": provider_release_id,
            "manifest": self.manifest,
            "manifest_sha256": self.digest,
            "cover": self.cover,
        }


class LyricsProvider:
    def search(self, **kwargs):
        return {"ok": False, "candidates": [], "warnings": [{"code": "not_found", "detail": "all configured providers"}], **kwargs}

    def fetch(self, **_kwargs):
        raise AssertionError("not used")


def _app(tmp_path: Path, *, cover: bool = True, library_type=LibraryInspector, navidrome_client=None, mutation_mode=None):
    library_root = tmp_path / "library"
    library_root.mkdir()
    provider = MetadataProvider(cover=cover)
    navidrome = navidrome_client or FakeNavidrome()

    app = create_app(
        tmp_path / "data",
        TOKEN,
        library_inspector=library_type(library_root),
        navidrome_client=navidrome,
        metadata_provider=provider,
        lyrics_provider=LyricsProvider(),
        mutation_mode=mutation_mode,
        remote_stt_runner=lambda _path, payload: {
            "ok": True,
            "source": payload["source"],
            "evidence": [{"status": "succeeded", "transcript": "inconclusive words"}],
        },
    )
    return app, library_root, provider, navidrome


def _seed_download(app, *, explicit: bool, candidate_artist: str = "Future") -> str:
    path = app.state.action_context.temporary_dir / ("source-explicit.flac" if explicit else "source.flac")
    _audio_fixture(path)
    operation, _ = app.state.store.create_operation(action="download.fetch", input={}, request_id=f"seed-download-{explicit}")
    app.state.store.finish(operation.id, status="succeeded", result={})
    data = path.read_bytes()
    record = app.state.store.create_temporary_file(
        kind="downloaded_audio",
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        media_type="audio/flac",
        source_operation_id=operation.id,
        metadata={
            "candidate": {
                "title": "Canonical Song (Explicit)" if explicit else "Canonical Song",
                "artist": candidate_artist,
            },
            "vmomo_quality": "SQ",
            "quality_selection": {"requested": "SQ", "selected": "SQ"},
            "provider_labels": ["Explicit"] if explicit else [],
        },
    )
    return record.id


def test_normalized_prepare_rejects_explicit_audio_only_source(tmp_path):
    app, _library_root, provider, _navidrome = _app(tmp_path, cover=False)
    source_id = _seed_download(app, explicit=True)
    source = app.state.store.get_temporary_file(source_id)
    assert source is not None
    app.state.store.mark_temporary_deleted(source_id, at=source.created_at)
    replacement = app.state.store.create_temporary_file(
        kind="downloaded_audio",
        path=source.path,
        sha256=source.sha256,
        size=source.size,
        media_type=source.media_type,
        source_operation_id=source.source_operation_id,
        metadata={**source.metadata, "operation_profile": "audio_only"},
    )
    with TestClient(app) as client:
        metadata_operation_id = _metadata_fetch(client, provider)
        search_operation, _ = app.state.store.create_operation(
            action="lyrics.search",
            input={"title": "Canonical Song"},
            request_id="audio-only-guard-lyrics-search-001",
        )
        app.state.store.finish(
            search_operation.id,
            status="succeeded",
            result={"ok": False, "candidates": [], "warnings": [{"code": "not_found"}]},
        )
        response = submit(client, "library.import.prepare", {
            "temporary_file_id": replacement.id,
            "metadata_operation_id": metadata_operation_id,
            "manifest_sha256": provider.digest,
            "provider_track_id": "track-1",
            "lyrics": {
                "status": "unavailable",
                "evidence_operation_ids": [search_operation.id],
                "source_language": "en",
                "reason": "none",
            },
            "content_rating": "explicit",
            "content_rating_reason": "Vmomo original title contains Explicit",
        }, "prepare-audio-only-rejected-001")
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "normalized_import_requires_normalized_download"


def test_staged_import_rechecks_mask_resolution_canonical_binding(tmp_path):
    app, _library_root, _provider, _navidrome = _app(tmp_path)
    original = "[00:42.000]say **** word now"
    resolved = "[00:42.000]say heard word now"
    fetch, _ = app.state.store.create_operation(
        action="lyrics.fetch",
        input={"provider": "lrclib", "provider_id": "1"},
        request_id="seed-resolution-source-001",
    )
    app.state.store.finish(fetch.id, status="succeeded", result={"canonical_lrc": original})
    original_sha256 = hashlib.sha256(original.encode()).hexdigest()
    resolutions = [{
        "canonical_timestamp": "00:42.000",
        "original_text": "****",
        "replacement_text": "heard",
        "method": "agent_inferred",
        "stt_operation_id": "",
    }]
    operation, _ = app.state.store.create_operation(
        action="lyrics.resolve_masks",
        input={},
        request_id="seed-resolution-audit-001",
    )
    app.state.store.finish(
        operation.id,
        status="succeeded",
        result={
            "resolution_status": "resolved",
            "canonical_source_operation_id": fetch.id,
            "input_canonical_lrc_sha256": original_sha256,
            "resolved_canonical_lrc": resolved,
            "resolved_canonical_lrc_sha256": hashlib.sha256(resolved.encode()).hexdigest(),
            "resolutions": resolutions,
        },
    )

    audit = _masked_resolution_audit(
        app.state.action_context,
        operation_id=operation.id,
        canonical_lrc=resolved,
        canonical_source_operation_id=fetch.id,
        input_canonical_lrc_sha256=original_sha256,
    )
    assert audit["resolutions"] == resolutions

    with pytest.raises(ActionFailure) as exc_info:
        _masked_resolution_audit(
            app.state.action_context,
            operation_id=operation.id,
            canonical_lrc="[00:42.000]different canonical",
            canonical_source_operation_id=fetch.id,
            input_canonical_lrc_sha256=original_sha256,
        )
    assert exc_info.value.code == "masked_lyrics_resolved_canonical_mismatch"


def _metadata_fetch(client, provider: MetadataProvider) -> str:
    response = submit(
        client,
        "metadata.fetch",
        {"provider": "musicbrainz", "provider_release_id": "release-1"},
        "metadata-fetch-0001",
    )
    operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
    assert operation["status"] == "succeeded"
    assert operation["result"]["manifest_sha256"] == provider.digest
    return operation["operation_id"]


def _translated_lyrics(app) -> dict:
    canonical = "[00:01.00]Hello\n[00:02.00]World"
    fetch, _ = app.state.store.create_operation(
        action="lyrics.fetch",
        input={"provider": "lrclib", "provider_id": "1"},
        request_id="lyrics-fetch-agent-0001",
    )
    app.state.store.finish(fetch.id, status="succeeded", result={
        "provider": "lrclib",
        "provider_id": "1",
        "identity": {"title": "Canonical Song", "artist": "Future", "album": "Canonical Album"},
        "canonical_lrc": canonical,
    })
    search, _ = app.state.store.create_operation(
        action="lyrics.search",
        input={"title": "Canonical Song", "artist": "Future", "album": "Canonical Album"},
        request_id="lyrics-search-agent-0001",
    )
    app.state.store.finish(search.id, status="succeeded", result={"candidates": [{
        "provider": "lrclib", "provider_id": "1", "title": "Canonical Song",
        "artist": "Future", "album": "Canonical Album",
    }]})
    return {
        "status": "translated",
        "source_operation_id": fetch.id,
        "source_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "translated_lines": ["你好", "世界"],
        "evidence_operation_ids": [search.id],
        "source_language": "en",
    }


def _prepare_unavailable_explicit(client, app, provider: MetadataProvider, *, suffix: str):
    source_id = _seed_download(app, explicit=True)
    metadata_operation_id = _metadata_fetch(client, provider)
    search_operation, _ = app.state.store.create_operation(
        action="lyrics.search",
        input={"title": "Canonical Song"},
        request_id=f"empty-lyrics-search-{suffix}",
    )
    app.state.store.finish(
        search_operation.id,
        status="succeeded",
        result={"ok": False, "candidates": [], "warnings": [{"code": "not_found"}]},
    )
    response = submit(
        client,
        "library.import.prepare",
        {
            "temporary_file_id": source_id,
            "metadata_operation_id": metadata_operation_id,
            "manifest_sha256": provider.digest,
            "provider_track_id": "track-1",
            "lyrics": {
                "status": "unavailable",
                "evidence_operation_ids": [search_operation.id],
                "source_language": "en",
                "reason": "No compatible lyrics found",
            },
            "content_rating": "explicit",
            "content_rating_reason": "Vmomo original title contains Explicit",
        },
        f"prepare-unavailable-{suffix}",
    )
    operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
    assert operation["status"] == "succeeded", operation
    return operation["result"]["staged_file"]


def test_personal_auto_normalized_import_publishes_exact_bytes_without_approval(tmp_path):
    app, library_root, provider, navidrome = _app(tmp_path, cover=False, mutation_mode="personal_auto")
    with TestClient(app) as client:
        staged = _prepare_unavailable_explicit(client, app, provider, suffix="auto")
        record = app.state.store.get_temporary_file(staged["temporary_file_id"])
        payload = {"staged_file_id": record.id, "target_file_path": "Canonical Song - Future.flac", "manifest_sha256": provider.digest}
        response = submit(client, "library.import", payload, "auto-normalized-import")
        assert response.status_code == 202, response.text
        assert "approval" not in response.json()
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
        assert operation["status"] == "succeeded", operation
        assert operation["approval_id"] is None
        assert operation["mutation_plan"]["items"][0]["staged_file_id"] == record.id
        published = library_root / payload["target_file_path"]
        assert hashlib.sha256(published.read_bytes()).hexdigest() == record.sha256
        assert operation["result"]["ok"] is True
        assert operation["result"]["publication_status"] == "published_pending_visibility"
        assert "next_action" not in operation["result"]
        replay = submit(client, "library.import", payload, "auto-normalized-import")
        assert replay.status_code == 200, replay.text
        assert replay.json()["operation"]["operation_id"] == operation["operation_id"]
        assert navidrome.scans == 1
        with app.state.store._connect() as con:
            assert con.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize("candidate_artist", ["Future", "Other Artist", "Future&Guest..", "<script>alert(1)</script>"])
def test_normalized_import_prepares_exact_bytes_renders_html_and_publishes_independently(tmp_path, candidate_artist):
    app, library_root, provider, navidrome = _app(tmp_path, cover=True)
    source_id = _seed_download(app, explicit=True, candidate_artist=candidate_artist)
    with TestClient(app) as client:
        metadata_operation_id = _metadata_fetch(client, provider)
        lyrics = _translated_lyrics(app)
        prepared_response = submit(
            client,
            "library.import.prepare",
            {
                "temporary_file_id": source_id,
                "metadata_operation_id": metadata_operation_id,
                "manifest_sha256": provider.digest,
                "provider_track_id": "track-1",
                "lyrics": lyrics,
                "content_rating": "explicit",
                "content_rating_reason": "Vmomo original title contains Explicit",
            },
            "prepare-import-0001",
        )
        prepared = wait_for_operation(client, prepared_response.json()["operation"]["operation_id"])
        assert prepared["status"] == "succeeded", prepared
        staged = prepared["result"]["staged_file"]
        assert prepared["result"]["target_file_path"] == "Canonical Song - Future.flac"
        assert staged["expires_at"] > time.time() + 29 * 24 * 60 * 60
        assert list(library_root.iterdir()) == []
        assert navidrome.scans == 0

        record = app.state.store.get_temporary_file(staged["temporary_file_id"])
        assert record is not None
        staged_path = Path(record.path)
        staged_sha256 = hashlib.sha256(staged_path.read_bytes()).hexdigest()
        assert staged_sha256 == record.sha256
        staged_evidence = LibraryInspector(staged_path.parent).evidence_for_file(
            staged_path.name,
            include={"metadata", "lyrics_text", "cover_summary"},
        )
        assert staged_evidence["metadata"] == {
            "title": "Canonical Song",
            "artist": "Future",
            "album": "Canonical Album",
            "album_artist": "Future",
            "date": "2026-07-20",
            "track_number": "1",
            "track_total": "2",
            "disc_number": "1",
            "disc_total": "1",
        }
        assert staged_evidence["lyrics"]["text"] == (
            "[00:01.00]Hello\n[00:01.00]你好\n[00:02.00]World\n[00:02.00]世界"
        )
        assert staged_evidence["cover"]["present"] is True
        assert "comment" not in record.metadata["staged_readback"]["tag_audit"]["tag_keys"]

        preview_response = submit(
            client,
            "library.import",
            {
                "staged_file_id": record.id,
                "target_file_path": "Canonical Song - Future.flac",
                "manifest_sha256": provider.digest,
            },
            "preview-import-0001",
        )
        assert preview_response.status_code == 202, preview_response.text
        approval_id = preview_response.json()["approval"]["approval_id"]
        approval = client.get(f"/approvals/{approval_id}?full=true", headers=HEADERS).json()["approval"]
        item = approval["preview"]["items"][0]
        assert approval["preview"]["recommended_display"] == "html"
        assert item["staged_file"]["sha256"] == staged_sha256
        assert item["manifest_sha256"] == provider.digest
        assert item["content_rating"]["value"] == "explicit"
        if candidate_artist == "Future":
            assert item["source"]["warnings"] == []
        else:
            assert prepared["result"]["warnings"][0]["code"] == "identity_comparison_warning"
            assert item["source"]["warnings"] == prepared["result"]["warnings"]
            assert item["source"]["identity_match"]["compatible"] is False
        assert item["lyrics"]["lyrics_text"].startswith("[00:01.00]Hello")
        assert item["cover"]["data_uri"].startswith("data:image/png;base64,")
        assert list(library_root.iterdir()) == []
        assert navidrome.scans == 0

        output = tmp_path / "import-review.html"
        render_approval(approval, output)
        page = output.read_text(encoding="utf-8")
        assert 'class="lyrics-review"' in page
        assert 'data-review-view-target="song-0-lyrics-review"' in page
        assert 'data-review-view-target="song-0-import-review"' in page
        assert "normalized_staged" in page
        assert "Canonical Song - Future.flac" in page
        assert "最终歌词" in page
        assert "变更对比" not in page
        assert "导入审批" in page
        assert "data:image/png;base64," in page
        assert staged_sha256 in page and provider.digest in page
        if candidate_artist != "Future":
            assert "下载候选与正式元数据的文字身份比较存在差异" in page
            assert "<script>alert(1)</script>" not in page

        published = approve(client, approval_id, "execute-import-0001")
        assert published["status"] == "succeeded", published
        assert published["result"]["publication_status"] == "published_pending_visibility"
        target = library_root / "Canonical Song - Future.flac"
        assert hashlib.sha256(target.read_bytes()).hexdigest() == staged_sha256
        assert navidrome.scans == 1
        assert app.state.store.get_temporary_file(record.id).deleted_at is not None
        assert app.state.store.get_temporary_file(source_id).deleted_at is not None


def test_missing_lyrics_cover_and_unknown_rating_are_explicitly_approvable(tmp_path):
    app, library_root, provider, _navidrome = _app(tmp_path, cover=False)
    source_id = _seed_download(app, explicit=False)
    with TestClient(app) as client:
        metadata_operation_id = _metadata_fetch(client, provider)
        search_response = submit(
            client,
            "lyrics.search",
            {"title": "Canonical Song", "artist": "Future", "album": "Canonical Album"},
            "lyrics-search-empty-0001",
        )
        search_operation = wait_for_operation(client, search_response.json()["operation"]["operation_id"])
        stt_response = submit(
            client,
            "audio.transcribe",
            {"temporary_file_id": source_id, "start_seconds": 0, "end_seconds": 10, "quality": "balanced"},
            "rating-stt-0001",
        )
        stt_operation = wait_for_operation(client, stt_response.json()["operation"]["operation_id"])

        prepared_response = submit(
            client,
            "library.import.prepare",
            {
                "temporary_file_id": source_id,
                "metadata_operation_id": metadata_operation_id,
                "manifest_sha256": provider.digest,
                "provider_track_id": "track-1",
                "lyrics": {
                    "status": "unavailable",
                    "evidence_operation_ids": [search_operation["operation_id"]],
                    "source_language": "en",
                    "reason": "No configured lyric provider returned a compatible result",
                },
                "content_rating": "unknown",
                "content_rating_stt_operation_id": stt_operation["operation_id"],
                "content_rating_reason": "STT did not cover a decisive censored or uncensored token",
            },
            "prepare-import-missing-0001",
        )
        prepared = wait_for_operation(client, prepared_response.json()["operation"]["operation_id"])
        assert prepared["status"] == "succeeded", prepared
        staged = prepared["result"]["staged_file"]
        preview = submit(
            client,
            "library.import",
            {
                "staged_file_id": staged["temporary_file_id"],
                "target_file_path": "Canonical Song - Future.flac",
                "manifest_sha256": provider.digest,
            },
            "preview-import-missing-0001",
        )
        approval_id = preview.json()["approval"]["approval_id"]
        approval = client.get(f"/approvals/{approval_id}?full=true", headers=HEADERS).json()["approval"]
        output = tmp_path / "missing-review.html"
        render_approval(approval, output)
        page = output.read_text(encoding="utf-8")
        assert "没有歌词写入" in page
        assert "封面不可用" in page
        assert "无法确定" in page
        assert "rating-stt-0001" not in page  # Operation IDs, not request IDs, are bound.
        assert stt_operation["operation_id"] in page
        assert "inconclusive words" in page
        assert "STT did not cover a decisive censored or uncensored token" in page
        assert list(library_root.iterdir()) == []


def test_unknown_rating_needs_no_stt_but_collision_requires_replacement_decision(tmp_path):
    app, library_root, provider, _navidrome = _app(tmp_path, cover=False)
    source_id = _seed_download(app, explicit=False)
    with TestClient(app) as client:
        metadata_operation_id = _metadata_fetch(client, provider)
        search_response = submit(
            client,
            "lyrics.search",
            {"title": "Canonical Song", "artist": "Future", "album": "Canonical Album"},
            "lyrics-search-empty-0002",
        )
        search_operation = wait_for_operation(client, search_response.json()["operation"]["operation_id"])
        payload = {
            "temporary_file_id": source_id,
            "metadata_operation_id": metadata_operation_id,
            "manifest_sha256": provider.digest,
            "provider_track_id": "track-1",
            "lyrics": {
                "status": "unavailable",
                "evidence_operation_ids": [search_operation["operation_id"]],
                "source_language": "en",
                "reason": "not found",
            },
            "content_rating": "unknown",
            "content_rating_reason": "no decisive evidence",
        }
        missing_stt = submit(client, "library.import.prepare", payload, "prepare-without-stt-0001")
        failed = wait_for_operation(client, missing_stt.json()["operation"]["operation_id"])
        assert failed["status"] == "succeeded", failed
        assert "content_rating_unknown" in {w["code"] for w in failed["result"]["warnings"]}

        stt_response = submit(
            client,
            "audio.transcribe",
            {"temporary_file_id": source_id, "start_seconds": 0, "end_seconds": 10, "quality": "balanced"},
            "rating-stt-0002",
        )
        stt_operation = wait_for_operation(client, stt_response.json()["operation"]["operation_id"])
        prepared_response = submit(
            client,
            "library.import.prepare",
            {**payload, "content_rating_stt_operation_id": stt_operation["operation_id"]},
            "prepare-with-stt-0001",
        )
        prepared = wait_for_operation(client, prepared_response.json()["operation"]["operation_id"])
        assert prepared["status"] == "succeeded", prepared
        staged = prepared["result"]["staged_file"]

        target = library_root / "Canonical Song - Future.flac"
        shutil.copyfile(Path(app.state.store.get_temporary_file(source_id).path), target)
        preview = submit(
            client,
            "library.import",
            {
                "staged_file_id": staged["temporary_file_id"],
                "target_file_path": target.name,
                "manifest_sha256": provider.digest,
            },
            "preview-collision-0001",
        )
        assert preview.status_code == 422
        assert preview.json()["detail"] == "replacement_decision_required"


def test_unavailable_lyrics_warns_for_unfetched_compatible_candidate(tmp_path):
    app, _library_root, provider, _navidrome = _app(tmp_path, cover=False)
    source_id = _seed_download(app, explicit=True)
    with TestClient(app) as client:
        metadata_operation_id = _metadata_fetch(client, provider)
        search_operation, _ = app.state.store.create_operation(
            action="lyrics.search",
            input={"title": "Canonical Song"},
            request_id="lyrics-candidate-evidence-0001",
        )
        app.state.store.finish(
            search_operation.id,
            status="succeeded",
            result={
                "ok": True,
                "candidates": [{
                    "provider": "lrclib",
                    "provider_id": "1",
                    "title": "Canonical Song",
                    "artist": "Future",
                    "album": "Canonical Album",
                }],
            },
        )
        response = submit(
            client,
            "library.import.prepare",
            {
                "temporary_file_id": source_id,
                "metadata_operation_id": metadata_operation_id,
                "manifest_sha256": provider.digest,
                "provider_track_id": "track-1",
                "lyrics": {
                    "status": "unavailable",
                    "evidence_operation_ids": [search_operation.id],
                    "source_language": "en",
                    "reason": "incorrectly skipped candidate",
                },
                "content_rating": "explicit",
                "content_rating_reason": "Vmomo original title contains Explicit",
            },
            "prepare-skipped-lyrics-candidate-0001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    assert operation["status"] == "succeeded", operation
    assert "lyrics_candidates_not_fetched" in {w["code"] for w in operation["result"]["warnings"]}


def test_agent_translation_failure_keeps_canonical_lyrics_and_review_warning(tmp_path):
    app, _library_root, provider, _navidrome = _app(tmp_path, cover=False)
    source_id = _seed_download(app, explicit=True)
    canonical = "[00:01.00]Hello\n[00:02.00]World"
    identity = {
        "identity_status": "confirmed",
        "title": "Canonical Song",
        "artist": "Future",
        "album": "Canonical Album",
        "source_kind": "provider",
        "source_ref": "lrclib:1",
    }
    with TestClient(app) as client:
        metadata_operation_id = _metadata_fetch(client, provider)
        fetch_operation, _ = app.state.store.create_operation(
            action="lyrics.fetch",
            input={"provider": "lrclib", "provider_id": "1"},
            request_id="canonical-lyrics-fetch-0001",
        )
        app.state.store.finish(
            fetch_operation.id,
            status="succeeded",
            result={
                "ok": True,
                "provider": "lrclib",
                "provider_id": "1",
                "identity": {"title": "Canonical Song", "artist": "Future", "album": "Canonical Album"},
                "canonical_lrc": canonical,
                "canonical_lrc_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            },
        )
        response = submit(
            client,
            "library.import.prepare",
            {
                "temporary_file_id": source_id,
                "metadata_operation_id": metadata_operation_id,
                "manifest_sha256": provider.digest,
                "provider_track_id": "track-1",
                "lyrics": {
                    "status": "translation_failed",
                    "source_operation_id": fetch_operation.id,
                    "evidence_operation_ids": [],
                    "source_language": "en",
                    "reason": "Agent translation was not produced",
                },
                "content_rating": "explicit",
                "content_rating_reason": "Vmomo original title contains Explicit",
            },
            "prepare-translation-failed-0001",
        )
        prepared = wait_for_operation(client, response.json()["operation"]["operation_id"])
        assert prepared["status"] == "succeeded", prepared
        staged = prepared["result"]["staged_file"]
        preview = submit(
            client,
            "library.import",
            {
                "staged_file_id": staged["temporary_file_id"],
                "target_file_path": "Canonical Song - Future.flac",
                "manifest_sha256": provider.digest,
            },
            "preview-translation-failed-0001",
        )
        approval_id = preview.json()["approval"]["approval_id"]
        approval = client.get(f"/approvals/{approval_id}?full=true", headers=HEADERS).json()["approval"]
        item = approval["preview"]["items"][0]

    assert item["lyrics"]["status"] == "translation_failed"
    assert item["lyrics"]["lyrics_text"] == canonical
    output = tmp_path / "translation-failed-review.html"
    render_approval(approval, output)
    assert "translation_failed" in output.read_text(encoding="utf-8")


class ReadbackFailureLibrary(LibraryInspector):
    def evidence_for_file(self, file_path, *, include=None, include_lyrics=False):
        if self._resolve(file_path).exists():
            return {"ok": False, "file_path": file_path, "exists": True, "error_type": "forced_readback_failure"}
        return super().evidence_for_file(file_path, include=include, include_lyrics=include_lyrics)


def test_publish_readback_failure_withdraws_only_the_new_file(tmp_path):
    app, library_root, provider, navidrome = _app(
        tmp_path,
        cover=False,
        library_type=ReadbackFailureLibrary,
    )
    with TestClient(app) as client:
        staged = _prepare_unavailable_explicit(client, app, provider, suffix="rollback")
        preview = submit(
            client,
            "library.import",
            {
                "staged_file_id": staged["temporary_file_id"],
                "target_file_path": "Canonical Song - Future.flac",
                "manifest_sha256": provider.digest,
            },
            "preview-import-rollback-0001",
        )
        operation = approve(client, preview.json()["approval"]["approval_id"], "execute-import-rollback-0001")

    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "published_file_readback_failed"
    assert operation["error"]["detail"]["rollback"]["ok"] is True
    assert not (library_root / "Canonical Song - Future.flac").exists()
    assert navidrome.scans == 0


class FailingRefreshNavidrome(FakeNavidrome):
    def start_scan(self):
        self.scans += 1
        return {"ok": False, "detail": "scan_temporarily_unavailable"}


def test_navidrome_refresh_failure_retains_verified_file_as_pending_visibility(tmp_path):
    navidrome = FailingRefreshNavidrome()
    app, library_root, provider, _ = _app(tmp_path, cover=False, navidrome_client=navidrome)
    with TestClient(app) as client:
        staged = _prepare_unavailable_explicit(client, app, provider, suffix="visibility")
        preview = submit(
            client,
            "library.import",
            {
                "staged_file_id": staged["temporary_file_id"],
                "target_file_path": "Canonical Song - Future.flac",
                "manifest_sha256": provider.digest,
            },
            "preview-import-visibility-0001",
        )
        operation = approve(client, preview.json()["approval"]["approval_id"], "execute-import-visibility-0001")

    assert operation["status"] == "succeeded"
    assert operation["result"]["publication_status"] == "published_pending_visibility"
    assert operation["result"]["navidrome_refresh"]["ok"] is False
    assert (library_root / "Canonical Song - Future.flac").is_file()
    assert navidrome.scans == 1


def test_same_final_hash_is_idempotent_without_overwrite(tmp_path):
    app, library_root, provider, navidrome = _app(tmp_path, cover=False)
    with TestClient(app) as client:
        staged = _prepare_unavailable_explicit(client, app, provider, suffix="idempotent")
        staged_record = app.state.store.get_temporary_file(staged["temporary_file_id"])
        target = library_root / "Canonical Song - Future.flac"
        shutil.copyfile(Path(staged_record.path), target)
        before_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        preview = submit(
            client,
            "library.import",
            {
                "staged_file_id": staged_record.id,
                "target_file_path": target.name,
                "manifest_sha256": provider.digest,
            },
            "preview-import-idempotent-0001",
        )
        approval = preview.json()["approval"]
        assert approval["preview"]["items"][0]["idempotent"] is True
        operation = approve(client, approval["approval_id"], "execute-import-idempotent-0001")

    assert operation["status"] == "succeeded"
    assert operation["result"]["file"]["idempotent"] is True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before_sha256
    assert navidrome.scans == 1


@pytest.mark.parametrize("lyrics_status", ["original", "translation_failed", "unavailable"])
def test_best_effort_warnings_survive_prepare_review_publish(tmp_path, lyrics_status):
    app, library_root, provider, _ = _app(tmp_path, cover=False)
    source_id = _seed_download(app, explicit=False, candidate_artist="K-DA & truncated...")
    for track in provider.manifest["tracks"]:
        for field in ("album", "album_artist", "date", "track_number", "track_total", "disc_number", "disc_total"):
            track.pop(field, None)
    provider.digest = _sha256_json(provider.manifest)
    provider.cover = {"status": "available", "cover_base64": "%%%", "media_type": "image/png", "sha256": "a"*64}
    canonical = "[00:01.00]Keep **** unchanged"
    source, _ = app.state.store.create_operation(action="lyrics.fetch", input={"provider": "lrclib", "provider_id": "1"}, request_id="best-effort-fetch")
    app.state.store.finish(source.id, status="succeeded", result={"canonical_lrc": canonical})
    lyrics = {"status": lyrics_status, "source_language": "en", "reason": "best effort"}
    if lyrics_status != "unavailable":
        lyrics["source_operation_id"] = source.id
    with TestClient(app) as client:
        metadata_id = _metadata_fetch(client, provider)
        response = submit(client, "library.import.prepare", {"temporary_file_id":source_id,"metadata_operation_id":metadata_id,"manifest_sha256":provider.digest,"provider_track_id":"track-1","lyrics":lyrics,"content_rating":"unknown"}, "best-effort-prepare")
        prepared = wait_for_operation(client,response.json()["operation"]["operation_id"])
        assert prepared["status"] == "succeeded", prepared
        result = prepared["result"]
        codes = {w["code"] for w in result["warnings"]}
        assert {"identity_comparison_warning","content_rating_unknown","optional_metadata_missing","cover_rejected"} <= codes
        if lyrics_status != "unavailable":
            assert "lyrics_masks_preserved" in codes
        else:
            assert "lyrics_not_searched" in codes
        staged = result["staged_file"]
        preview = submit(client,"library.import", {"staged_file_id":staged["temporary_file_id"],"target_file_path":"Canonical Song - Future.flac","manifest_sha256":provider.digest}, "best-effort-preview")
        assert preview.status_code == 202, preview.text
        approval = preview.json()["approval"]
        html = client.get(approval["review_path"])
        assert html.status_code == 200
        assert "cover_rejected" in html.text and "content_rating_unknown" in html.text
        published = approve(client,approval["approval_id"],"best-effort-publish")
        assert published["status"] == "succeeded", published
        evidence = LibraryInspector(library_root).evidence_for_file("Canonical Song - Future.flac",include={"metadata","lyrics_text","cover_summary"})
        assert evidence["metadata"]["title"] == "Canonical Song"
        assert evidence["cover"]["present"] is False
        assert (evidence.get("lyrics") or {}).get("text", "") == ("" if lyrics_status == "unavailable" else canonical)


@pytest.mark.parametrize("rating", ["explicit", "clean"])
def test_positive_rating_without_evidence_still_rejected(tmp_path, rating):
    app, _, provider, _ = _app(tmp_path, cover=False)
    source_id = _seed_download(app, explicit=False)
    with TestClient(app) as client:
        metadata_id = _metadata_fetch(client, provider)
        response=submit(client,"library.import.prepare",{"temporary_file_id":source_id,"metadata_operation_id":metadata_id,"manifest_sha256":provider.digest,"provider_track_id":"track-1","lyrics":{"status":"unavailable","reason":"not requested"},"content_rating":rating},"rating-no-proof")
        operation=wait_for_operation(client,response.json()["operation"]["operation_id"])
        assert operation["status"] == "failed"
        assert operation["error"]["code"] == "content_rating_stt_required"
