from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from conftest import FakeLibrary, FakeNavidrome, HEADERS, TOKEN, submit, wait_for_operation
from musicops.api import create_app


EXPECTED_ACTIONS = {
    "library.search", "library.read", "library.album_diagnose", "library.album_metadata_audit",
    "library.add", "library.update_metadata", "library.update_lyrics", "library.update_cover", "library.move", "library.delete",
    "download.search", "download.fetch", "lyrics.search", "lyrics.fetch", "lyrics.resolve_masks", "audio.transcribe",
    "metadata.search", "metadata.fetch", "library.import.prepare", "library.import",
    "navidrome.search", "navidrome.verify", "navidrome.refresh", "temporary.inspect", "temporary.delete",
}


def test_action_catalog_is_authenticated_complete_and_manager_free(tmp_path):
    app = create_app(tmp_path, TOKEN)
    with TestClient(app) as client:
        assert client.get("/actions").status_code == 401
        payload = client.get("/actions", headers=HEADERS).json()
        assert {row["name"] for row in payload["actions"]} == EXPECTED_ACTIONS
        assert all("input_schema" not in row for row in payload["actions"])
        full_payload = client.get("/actions?full=true", headers=HEADERS).json()
        assert all("input_schema" in row for row in full_payload["actions"])
        selected = client.get("/actions?name=library.search&full=true", headers=HEADERS).json()
        assert [row["name"] for row in selected["actions"]] == ["library.search"]
        assert client.get("/actions?name=missing", headers=HEADERS).status_code == 404
        serialized = json.dumps(full_payload)
        for forbidden in ("run_id", "todo_item_id", "retry_of", "acceptance", "completion"):
            assert forbidden not in serialized
        assert all(len(row["schema_digest"]) == 64 for row in full_payload["actions"])
        assert all(
            row["confirmation_policy"] == "explicit_preview_approval"
            for row in full_payload["actions"]
            if row["requires_approval"]
        )

        by_name = {row["name"]: row for row in full_payload["actions"]}
        assert "lyrics.translate" not in by_name

        transcription = by_name["audio.transcribe"]
        assert transcription["requires_approval"] is False
        assert transcription["confirmation_policy"] == "explicit_intent_sufficient"
        assert transcription["data_handling"] == {
            "boundary": "configured_external_processor",
            "payload_classes": ["bounded_audio_clip"],
            "purposes": ["content_rating_stt", "masked_lyrics_stt"],
        }

        search_schema = by_name["download.search"]["input_schema"]
        fetch_schema = by_name["download.fetch"]["input_schema"]
        metadata_fetch_schema = by_name["metadata.fetch"]["input_schema"]
        album_audit_schema = by_name["library.album_metadata_audit"]["input_schema"]
        assert {"max_scanned", "lookup_candidates", "candidate_limit"} <= set(album_audit_schema["properties"])
        assert by_name["library.album_metadata_audit"]["requires_approval"] is False
        assert "quality" not in search_schema["properties"]
        assert "quality" not in fetch_schema["properties"]
        assert "media_msg_id" not in fetch_schema["properties"]
        assert {"candidate_index", "vmomo_quality"} <= set(fetch_schema["required"])
        assert {"search_operation_id", "metadata_operation_id", "manifest_sha256", "provider_track_id"} <= set(fetch_schema["properties"])
        assert "identity_validation_operation_id" not in fetch_schema["properties"]
        assert "download.validate" not in by_name
        assert "Required for normalized_staged" in fetch_schema["properties"]["search_operation_id"]["description"]
        assert "derives it from search_operation_id" in fetch_schema["properties"]["search_msg_id"]["description"]
        assert fetch_schema["properties"]["vmomo_quality"]["enum"] == ["default", "HQ", "SQ"]
        assert "Total download.fetch deadline" in fetch_schema["properties"]["timeout"]["description"]
        assert "additional media-transfer attempts" in fetch_schema["properties"]["retries"]["description"]
        assert by_name["download.fetch"]["operation_max_attempts"] == 1
        assert metadata_fetch_schema["properties"]["provider"]["enum"] == [
            "musicbrainz", "netease", "qq", "apple_music", "spotify",
        ]
        assert "supplemental_sources" in metadata_fetch_schema["properties"]

        transcription_schema = transcription["input_schema"]
        assert {"file_path", "temporary_file_id"} <= set(transcription_schema["properties"])
        assert transcription_schema["properties"]["purpose"]["default"] == "content_rating_stt"
        assert {"canonical_source_operation_id", "canonical_lrc_sha256", "music_identity"} <= set(
            transcription_schema["properties"]
        )
        assert by_name["lyrics.resolve_masks"]["operation_profile"] == "masked_lyrics_resolution"
        assert set(by_name["lyrics.resolve_masks"]["input_schema"]["required"]) == {
            "canonical_source_operation_id",
            "canonical_lrc_sha256",
            "identity_evidence_operation_id",
            "music_identity",
            "resolutions",
        }
        assert by_name["library.add"]["operation_profile"] == "audio_only"
        assert by_name["library.import"]["operation_profile"] == "normalized_staged"
        assert by_name["library.import"]["requires_approval"] is True
        assert by_name["library.import.prepare"]["requires_approval"] is False
        assert set(by_name["library.import"]["input_schema"]["required"]) == {
            "staged_file_id", "target_file_path", "manifest_sha256",
        }


def test_read_action_is_durable_and_request_id_is_idempotent(tmp_path):
    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary())
    with TestClient(app) as client:
        first = submit(client, "library.search", {"query": "One"}, "read-search-0001")
        assert first.status_code == 202
        operation = wait_for_operation(client, first.json()["operation"]["operation_id"])
        assert operation["status"] == "succeeded"
        assert operation["result"]["result_count"] == 1

        replay = submit(client, "library.search", {"query": "One"}, "read-search-0001")
        assert replay.status_code == 200
        assert replay.json()["reused"] is True
        assert replay.json()["operation"]["operation_id"] == operation["operation_id"]

        conflict = submit(client, "library.search", {"query": "Two"}, "read-search-0001")
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "request_id_reused_with_different_intent"


def test_metadata_fetch_accepts_explicit_cross_provider_supplement(tmp_path):
    calls = []

    class Provider:
        def configured(self):
            return True

        def search(self, **kwargs):
            return {"ok": True, "candidates": [], **kwargs}

        def fetch(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "provider": "mixed",
                "provider_release_id": kwargs["provider_release_id"],
                "manifest": {"provider": "mixed", "tracks": []},
                "manifest_sha256": "a" * 64,
                "cover": {"status": "unavailable"},
            }

    app = create_app(tmp_path, TOKEN, metadata_provider=Provider())
    with TestClient(app) as client:
        response = submit(
            client,
            "metadata.fetch",
            {
                "provider": "musicbrainz",
                "provider_release_id": "release-1",
                "supplemental_sources": [{
                    "provider": "netease",
                    "provider_release_id": "album-2",
                    "market": "cn",
                    "fields": ["date"],
                }],
            },
            "metadata-mixed-fetch-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    assert operation["status"] == "succeeded"
    assert calls == [{
        "provider": "musicbrainz",
        "provider_release_id": "release-1",
        "supplemental_sources": [{
            "provider": "netease",
            "provider_release_id": "album-2",
            "market": "cn",
            "fields": ["date"],
        }],
    }]


def test_server_translation_action_is_removed(tmp_path):
    app = create_app(tmp_path, TOKEN)
    with TestClient(app) as client:
        assert client.get("/actions?name=lyrics.translate", headers=HEADERS).status_code == 404


def test_service_actions_share_the_operation_envelope(tmp_path):
    class Lyrics:
        def search(self, **kwargs):
            return {"ok": True, "candidates": [{"provider": "lrclib", "provider_id": "1"}], **kwargs}

        def fetch(self, **kwargs):
            return {"ok": True, "canonical_lrc": "[00:01]line", "canonical_lrc_sha256": "a" * 64, **kwargs}

    def stt(_audio_path, payload):
        return {"ok": True, "provider": "fake-stt", "model": "internal-model", "transcript": "line", **payload}

    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary(), lyrics_provider=Lyrics(), remote_stt_runner=stt, navidrome_client=FakeNavidrome())
    cases = [
        ("lyrics.search", {"title": "Song"}, "candidates"),
        ("lyrics.fetch", {"provider": "lrclib", "provider_id": "1"}, "canonical_lrc"),
        ("audio.transcribe", {"file_path": "Artist/Album/One.flac", "start_seconds": 1, "end_seconds": 5}, "transcript"),
        ("navidrome.refresh", {}, "started"),
    ]
    with TestClient(app) as client:
        for index, (action, input, key) in enumerate(cases):
            response = submit(client, action, input, f"service-action-{index:04d}")
            operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
            assert operation["status"] == "succeeded"
            assert key in operation["result"]


def test_audio_transcribe_accepts_one_managed_source_and_guards_temporary_hash(tmp_path):
    calls = []

    def stt(audio_path, payload):
        calls.append((audio_path, payload))
        return {"ok": True, "provider": "fake-stt", "model": "internal-model", "transcript": "heard word", **payload}

    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary(), remote_stt_runner=stt)
    temporary_path = app.state.action_context.temporary_dir / "candidate.mp3"
    temporary_path.write_bytes(b"temporary-audio")
    source_operation, _ = app.state.store.create_operation(action="download.fetch", input={}, request_id="temp-stt-source")
    app.state.store.finish(source_operation.id, status="succeeded", result={})
    temporary = app.state.store.create_temporary_file(
        kind="downloaded_audio",
        path=str(temporary_path),
        sha256=hashlib.sha256(b"temporary-audio").hexdigest(),
        size=temporary_path.stat().st_size,
        media_type="audio/mpeg",
        source_operation_id=source_operation.id,
    )

    with TestClient(app) as client:
        neither = submit(client, "audio.transcribe", {"start_seconds": 1, "end_seconds": 5}, "stt-neither-001")
        unbounded_mask = submit(
            client,
            "audio.transcribe",
            {"file_path": "Artist/Album/One.flac", "purpose": "masked_lyrics_stt"},
            "stt-mask-unbounded-001",
        )
        both = submit(
            client,
            "audio.transcribe",
            {"file_path": "Artist/Album/One.flac", "temporary_file_id": temporary.id},
            "stt-both-001",
        )
        response = submit(
            client,
            "audio.transcribe",
            {"temporary_file_id": temporary.id, "start_seconds": 1, "end_seconds": 5},
            "stt-temporary-001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

        temporary_path.write_bytes(b"tampered")
        stale_response = submit(
            client,
            "audio.transcribe",
            {"temporary_file_id": temporary.id, "start_seconds": 1, "end_seconds": 5},
            "stt-temporary-stale-001",
        )
        stale = wait_for_operation(client, stale_response.json()["operation"]["operation_id"])

    assert neither.status_code == 422
    assert unbounded_mask.status_code == 422
    assert both.status_code == 422
    assert operation["status"] == "succeeded"
    assert operation["result"]["source"] == {
        "kind": "temporary",
        "temporary_file_id": temporary.id,
        "sha256": temporary.sha256,
    }
    assert calls[0][0] == temporary_path.resolve()
    assert "path" not in operation["result"]
    assert stale["status"] == "failed"
    assert stale["error"]["code"] == "temporary_source_stale"


def test_mask_resolution_action_is_atomic_and_returns_source_bound_output(tmp_path):
    app = create_app(tmp_path, TOKEN)
    canonical = "[00:42.000]say **** now"
    canonical_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
    fetch, _ = app.state.store.create_operation(
        action="lyrics.fetch",
        input={"provider": "netease", "provider_id": "1"},
        request_id="seed-mask-fetch-001",
    )
    app.state.store.finish(
        fetch.id,
        status="succeeded",
        result={
            "ok": True,
            "provider": "netease",
            "provider_id": "1",
            "canonical_lrc": canonical,
            "canonical_lrc_sha256": canonical_sha256,
        },
    )
    identity = {
        "identity_status": "confirmed",
        "title": "Song",
        "artist": "Artist",
        "album": "",
        "source_kind": "provider",
        "source_ref": "netease:1",
    }
    identity_search, _ = app.state.store.create_operation(
        action="lyrics.search",
        input={"title": "Song", "artist": "Artist"},
        request_id="seed-mask-identity-search",
    )
    app.state.store.finish(
        identity_search.id,
        status="succeeded",
        result={"candidates": [{"provider": "netease", "provider_id": "1", "title": "Song", "artist": "Artist"}]},
    )
    stt, _ = app.state.store.create_operation(
        action="audio.transcribe",
        input={
            "temporary_file_id": "temp-source",
            "start_seconds": 40.0,
            "end_seconds": 45.0,
            "quality": "accurate",
            "purpose": "masked_lyrics_stt",
            "canonical_source_operation_id": fetch.id,
            "canonical_lrc_sha256": canonical_sha256,
            "music_identity": identity,
        },
        request_id="seed-mask-stt",
    )
    app.state.store.finish(
        stt.id,
        status="succeeded",
        result={"ok": True, "evidence": [{"transcript": "unreliable hint"}]},
    )

    with TestClient(app) as client:
        response = submit(
            client,
            "lyrics.resolve_masks",
            {
                "canonical_source_operation_id": fetch.id,
                "canonical_lrc_sha256": canonical_sha256,
                "identity_evidence_operation_id": identity_search.id,
                "music_identity": identity,
                "resolutions": [{
                    "canonical_timestamp": "00:42.000",
                    "original_text": "****",
                    "replacement_text": "heard",
                    "method": "agent_resolved_with_stt_hint",
                    "stt_operation_id": stt.id,
                }],
            },
            "resolve-masks-001",
        )
        resolution = wait_for_operation(client, response.json()["operation"]["operation_id"])
        assert resolution["status"] == "succeeded"
        assert resolution["result"]["resolution_status"] == "resolved"
        assert resolution["result"]["resolved_canonical_lrc"] == "[00:42.000]say heard now"
        assert resolution["result"]["canonical_source_operation_id"] == fetch.id
        assert resolution["result"]["input_canonical_lrc_sha256"] == canonical_sha256
        assert client.get("/actions?name=lyrics.translate", headers=HEADERS).status_code == 404


def test_mask_resolution_requires_lyric_stt_purpose_and_accepts_agent_inference(tmp_path):
    app = create_app(tmp_path, TOKEN)
    canonical = "[00:42.000]say **** now"
    canonical_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
    fetch, _ = app.state.store.create_operation(
        action="lyrics.fetch",
        input={"provider": "netease", "provider_id": "1"},
        request_id="seed-minimal-mask-fetch",
    )
    app.state.store.finish(fetch.id, status="succeeded", result={
        "provider": "netease",
        "provider_id": "1",
        "canonical_lrc": canonical,
        "canonical_lrc_sha256": canonical_sha256,
    })
    identity = {
        "identity_status": "confirmed",
        "title": "Song",
        "artist": "Artist",
        "album": "",
        "source_kind": "provider",
        "source_ref": "netease:1",
    }
    identity_search, _ = app.state.store.create_operation(
        action="lyrics.search",
        input={"title": "Song", "artist": "Artist"},
        request_id="seed-minimal-mask-identity-search",
    )
    app.state.store.finish(
        identity_search.id,
        status="succeeded",
        result={"candidates": [{"provider": "netease", "provider_id": "1", "title": "Song", "artist": "Artist"}]},
    )
    rating_stt, _ = app.state.store.create_operation(
        action="audio.transcribe",
        input={"purpose": "content_rating_stt"},
        request_id="seed-rating-stt",
    )
    app.state.store.finish(rating_stt.id, status="succeeded", result={"ok": True})

    base_input = {
        "canonical_source_operation_id": fetch.id,
        "canonical_lrc_sha256": canonical_sha256,
        "identity_evidence_operation_id": identity_search.id,
        "music_identity": identity,
    }
    with TestClient(app) as client:
        rejected = submit(client, "lyrics.resolve_masks", {
            **base_input,
            "resolutions": [{
                "canonical_timestamp": "00:42.000",
                "original_text": "****",
                "replacement_text": "heard",
                "method": "agent_resolved_with_stt_hint",
                "stt_operation_id": rating_stt.id,
            }],
        }, "reject-rating-stt")
        rejected_operation = wait_for_operation(client, rejected.json()["operation"]["operation_id"])

        inferred = submit(client, "lyrics.resolve_masks", {
            **base_input,
            "resolutions": [{
                "canonical_timestamp": "00:42.000",
                "original_text": "****",
                "replacement_text": "heard",
                "method": "agent_inferred",
            }],
        }, "accept-agent-inference")
        inferred_operation = wait_for_operation(client, inferred.json()["operation"]["operation_id"])

        invalid_shape = submit(client, "lyrics.resolve_masks", {
            **base_input,
            "resolutions": [{
                "canonical_timestamp": "00:42.000",
                "original_text": "****",
                "replacement_text": "two words",
                "method": "agent_inferred",
            }],
        }, "reject-multi-token")

    assert rejected_operation["status"] == "failed"
    assert rejected_operation["error"]["code"] == "stt_purpose_mismatch"
    assert inferred_operation["status"] == "succeeded"
    assert inferred_operation["result"]["resolution_status"] == "resolved"
    assert invalid_shape.status_code == 422


def test_mask_resolution_rejects_cross_track_stt_provenance(tmp_path):
    app = create_app(tmp_path, TOKEN)
    canonical = "[00:42.000]say **** now"
    canonical_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
    identity = {
        "identity_status": "confirmed",
        "title": "Song",
        "artist": "Artist",
        "album": "",
        "source_kind": "provider",
        "source_ref": "netease:1",
    }
    fetch, _ = app.state.store.create_operation(
        action="lyrics.fetch",
        input={"provider": "netease", "provider_id": "1"},
        request_id="cross-track-fetch",
    )
    app.state.store.finish(fetch.id, status="succeeded", result={
        "provider": "netease",
        "provider_id": "1",
        "canonical_lrc": canonical,
        "canonical_lrc_sha256": canonical_sha256,
    })
    search, _ = app.state.store.create_operation(
        action="lyrics.search",
        input={"title": "Song", "artist": "Artist"},
        request_id="cross-track-search",
    )
    app.state.store.finish(search.id, status="succeeded", result={
        "candidates": [{"provider": "netease", "provider_id": "1", "title": "Song", "artist": "Artist"}],
    })
    stt, _ = app.state.store.create_operation(
        action="audio.transcribe",
        input={
            "file_path": "Artist/Album/One.flac",
            "start_seconds": 40,
            "end_seconds": 45,
            "quality": "accurate",
            "purpose": "masked_lyrics_stt",
            "canonical_source_operation_id": fetch.id,
            "canonical_lrc_sha256": "0" * 64,
            "music_identity": identity,
        },
        request_id="cross-track-stt",
    )
    app.state.store.finish(stt.id, status="succeeded", result={"ok": True})

    with TestClient(app) as client:
        submitted = submit(client, "lyrics.resolve_masks", {
            "canonical_source_operation_id": fetch.id,
            "canonical_lrc_sha256": canonical_sha256,
            "identity_evidence_operation_id": search.id,
            "music_identity": identity,
            "resolutions": [{
                "canonical_timestamp": "00:42.000",
                "original_text": "****",
                "replacement_text": "heard",
                "method": "agent_resolved_with_stt_hint",
                "stt_operation_id": stt.id,
            }],
        }, "cross-track-resolver")
        operation = wait_for_operation(client, submitted.json()["operation"]["operation_id"])

    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "stt_lyrics_provenance_mismatch"


def test_navidrome_verify_enforces_expected_presence(tmp_path):
    class MissingNavidrome(FakeNavidrome):
        def path_evidence(self, **kwargs):
            return {"ok": True, "matched": False, "path_matched": False, **kwargs}

    app = create_app(tmp_path, TOKEN, navidrome_client=MissingNavidrome())
    input_payload = {"file_path": "Artist/Album/Missing.flac", "query": "Missing"}
    with TestClient(app) as client:
        absent = submit(client, "navidrome.verify", {**input_payload, "expected_present": False}, "navidrome-absent-0001")
        absent_operation = wait_for_operation(client, absent.json()["operation"]["operation_id"])
        assert absent_operation["status"] == "succeeded"
        assert absent_operation["result"]["verified"] is True
        assert absent_operation["result"]["expected_present"] is False

        present = submit(client, "navidrome.verify", input_payload, "navidrome-present-0001")
        present_operation = wait_for_operation(client, present.json()["operation"]["operation_id"])
        assert present_operation["status"] == "failed"
        assert present_operation["error"]["code"] == "navidrome_expectation_not_met"
        assert present_operation["error"]["detail"]["verified"] is False


def test_navidrome_verify_requires_metadata_for_unique_strategy(tmp_path):
    app = create_app(tmp_path, TOKEN, navidrome_client=FakeNavidrome())
    with TestClient(app) as client:
        response = submit(
            client,
            "navidrome.verify",
            {"file_path": "Artist/Album/Song.flac", "match_strategy": "unique_metadata"},
            "navidrome-unique-without-metadata-0001",
        )

    assert response.status_code == 422
    assert "expected_metadata_required_for_unique_metadata" in str(response.json()["errors"])


def test_navidrome_verify_never_treats_ambiguous_metadata_as_absent(tmp_path):
    class AmbiguousNavidrome(FakeNavidrome):
        def path_evidence(self, **kwargs):
            return {
                "ok": True,
                "matched": False,
                "metadata_ambiguous": True,
                "metadata_candidate_count": 2,
                **kwargs,
            }

    app = create_app(tmp_path, TOKEN, navidrome_client=AmbiguousNavidrome())
    with TestClient(app) as client:
        response = submit(
            client,
            "navidrome.verify",
            {
                "file_path": "Artist/Album/Song.flac",
                "expected_metadata": {"title": "Song", "artist": "Artist", "album": "Album"},
                "expected_present": False,
                "match_strategy": "unique_metadata",
            },
            "navidrome-ambiguous-is-not-absent-0001",
        )
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])

    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "navidrome_metadata_ambiguous"
    assert operation["error"]["detail"]["verified"] is False
