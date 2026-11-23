from __future__ import annotations

import hashlib
import base64
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from conftest import FakeLibrary, FakeNavidrome, HEADERS, TOKEN, approve, submit, wait_for_operation
from musicops.api import create_app


def test_mutation_returns_authoritative_preview_and_rejects_stale_file(tmp_path):
    library = FakeLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, navidrome_client=FakeNavidrome())
    lyrics = "[00:01.00]new line"
    input = {"items": [{"file_path": "Artist/Album/One.flac", "lyrics": lyrics, "lyrics_sha256": hashlib.sha256(lyrics.encode()).hexdigest()}]}
    with TestClient(app) as client:
        response = submit(client, "library.update_lyrics", input, "lyrics-preview-0001")
        assert response.status_code == 202
        body = response.json()
        assert body["approval_required"] is True
        assert "operation" not in body
        approval = body["approval"]
        assert approval["preview"]["authoritative_source"] == "musicops_server_current_state"
        assert approval["preview"]["recommended_display"] == "html"
        assert approval["preview"]["items"][0]["before"]["sha256"] == library.files["Artist/Album/One.flac"]["sha256"]
        assert approval["preview"]["items"][0]["target"]["lyrics_text"]["omitted"] is True
        full_approval = client.get(f"/approvals/{approval['approval_id']}?full=true", headers=HEADERS).json()["approval"]
        assert full_approval["preview"]["items"][0]["target"]["lyrics_text"] == lyrics

        library.mutate_outside_ops("Artist/Album/One.flac")
        operation = approve(client, approval["approval_id"], "lyrics-execute-0001")
        assert operation["status"] == "failed"
        assert operation["error"]["code"] == "action_result_not_ok"
        assert operation["result"]["overall_status"] == "failed"
        assert operation["result"]["results"][0]["error"] == "stale_approval"


def test_approval_review_is_public_read_only_and_execute_stays_authenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICOPS_PUBLIC_BASE_URL", "https://musicops.example.test")
    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary(), start_worker=False)
    lyrics = "[00:01.00]review line"
    input = {
        "items": [{
            "file_path": "Artist/Album/One.flac",
            "lyrics": lyrics,
            "lyrics_sha256": hashlib.sha256(lyrics.encode()).hexdigest(),
        }],
    }
    with TestClient(app) as client:
        response = submit(client, "library.update_lyrics", input, "public-review-0001")
        approval = response.json()["approval"]
        review_path = approval["review_path"]
        assert approval["review_url"] == f"https://musicops.example.test{review_path}"

        review = client.get(review_path)
        assert review.status_code == 200
        assert "review line" in review.text
        assert review.headers["cache-control"] == "no-store"
        assert review.headers["referrer-policy"] == "no-referrer"
        assert review.headers["x-content-type-options"] == "nosniff"
        assert "Content-Security-Policy" in review.headers

        unauthenticated = client.post(
            f"/approvals/{approval['approval_id']}/execute",
            json={"request_id": "public-review-execute-0001", "actor": "user", "reason": "reviewed", "approved_at": time.time()},
        )
        assert unauthenticated.status_code == 401

        assert client.get(f"/approvals/{approval['approval_id']}/review").status_code == 200
        assert client.get(f"/approvals/{approval['approval_id']}").status_code == 401
        missing_review = client.get("/approvals/approval_missing/review")
        assert missing_review.status_code == 404
        assert missing_review.headers["cache-control"] == "no-store"


def test_single_file_metadata_and_lyrics_updates_write_and_read_back(tmp_path):
    library = FakeLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, navidrome_client=FakeNavidrome())
    path = "Artist/Album/One.flac"
    lyrics = "[00:01.00]verified line"
    with TestClient(app) as client:
        metadata_approval = submit(
            client,
            "library.update_metadata",
            {"patches": [{"file_path": path, "fields": {"album": "Verified Album"}}]},
            "single-metadata-preview-001",
        ).json()["approval"]
        metadata_operation = approve(client, metadata_approval["approval_id"], "single-metadata-execute-001")
        metadata_result = metadata_operation["result"]["results"][0]
        assert metadata_result["ok"] is True
        assert metadata_result["after"]["metadata"]["album"] == "Verified Album"

        lyrics_approval = submit(
            client,
            "library.update_lyrics",
            {
                "items": [{
                    "file_path": path,
                    "lyrics": lyrics,
                    "lyrics_sha256": hashlib.sha256(lyrics.encode()).hexdigest(),
                }],
            },
            "single-lyrics-preview-001",
        ).json()["approval"]
        lyrics_operation = approve(client, lyrics_approval["approval_id"], "single-lyrics-execute-001")
        lyrics_result = lyrics_operation["result"]["results"][0]
        assert lyrics_result["ok"] is True
        assert lyrics_result["after"]["lyrics"]["sha256"] == hashlib.sha256(lyrics.encode()).hexdigest()


def test_fixed_batch_reports_partial_success_per_file_and_refreshes_once(tmp_path):
    library = FakeLibrary()
    navidrome = FakeNavidrome()
    app = create_app(tmp_path, TOKEN, library_inspector=library, navidrome_client=navidrome)
    input = {"patches": [
        {"file_path": "Artist/Album/One.flac", "fields": {"album": "Updated"}},
        {"file_path": "Artist/Album/Two.flac", "fields": {"album": "Updated"}},
    ]}
    with TestClient(app) as client:
        approval = submit(client, "library.update_metadata", input, "metadata-preview-001").json()["approval"]
        assert approval["preview"]["item_count"] == 2
        fixed_hash = approval["preview"]["fixed_batch_sha256"]
        assert len(fixed_hash) == 64
        library.mutate_outside_ops("Artist/Album/Two.flac")

        operation = approve(client, approval["approval_id"], "metadata-execute-001")
        result = operation["result"]
        assert operation["status"] == "failed"
        assert operation["error"]["code"] == "action_result_not_ok"
        assert result["overall_status"] == "partial_success"
        assert result["succeeded_count"] == result["failed_count"] == 1
        assert result["results"][0]["ok"] is True
        assert result["results"][1]["error"] == "stale_approval"
        assert navidrome.scans == 1


def test_permanent_delete_preview_and_readback(tmp_path):
    library = FakeLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, navidrome_client=FakeNavidrome())
    path = "Artist/Album/One.flac"
    with TestClient(app) as client:
        approval = submit(client, "library.delete", {"items": [{"file_path": path}]}, "delete-preview-0001").json()["approval"]
        item = approval["preview"]["items"][0]
        assert approval["preview"]["permanent_delete"] is True
        assert item["permanent"] is True
        assert item["before"]["metadata"]["title"] == "One"
        assert item["before"]["size"] > 0 and len(item["before"]["sha256"]) == 64

        operation = approve(client, approval["approval_id"], "delete-execute-0001")
        assert operation["result"]["overall_status"] == "succeeded"
        assert path not in library.files

        second = client.post(
            f"/approvals/{approval['approval_id']}/execute",
            headers=HEADERS,
            json={"request_id": "delete-execute-0002", "actor": "user", "reason": "repeat", "approved_at": time.time()},
        )
        assert second.status_code == 409
        assert second.json()["detail"] == "approval_already_consumed"


def test_approval_request_id_is_idempotent_and_bound_to_exact_input(tmp_path):
    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary())
    one = {"items": [{"file_path": "Artist/Album/One.flac"}]}
    two = {"items": [{"file_path": "Artist/Album/Two.flac"}]}
    with TestClient(app) as client:
        first = submit(client, "library.delete", one, "delete-idempotent-001")
        replay = submit(client, "library.delete", one, "delete-idempotent-001")
        conflict = submit(client, "library.delete", two, "delete-idempotent-001")
        assert replay.status_code == 200 and replay.json()["reused"] is True
        assert replay.json()["approval"]["approval_id"] == first.json()["approval"]["approval_id"]
        assert conflict.status_code == 409


def test_approval_execution_validates_input_and_provenance_before_consumption(tmp_path):
    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary())
    cases = [
        ("invalid-input", "approval_stored_input_invalid"),
        ("schema-change", "approval_action_schema_changed"),
        ("contract-change", "approval_action_contract_changed"),
    ]
    with TestClient(app) as client:
        for index, (case, expected_detail) in enumerate(cases):
            response = submit(
                client,
                "library.delete",
                {"items": [{"file_path": "Artist/Album/One.flac"}]},
                f"preconsume-preview-{index:04d}",
            )
            approval = response.json()["approval"]
            assert len(approval["action_schema_digest"]) == 64
            assert len(approval["action_contract_digest"]) == 64
            with app.state.store._connect() as con:
                if case == "invalid-input":
                    con.execute("UPDATE approvals SET input_json = ? WHERE id = ?", ('{"items":[]}', approval["approval_id"]))
                elif case == "schema-change":
                    con.execute("UPDATE approvals SET action_schema_digest = ? WHERE id = ?", ("0" * 64, approval["approval_id"]))
                else:
                    con.execute("UPDATE approvals SET action_contract_digest = ? WHERE id = ?", ("0" * 64, approval["approval_id"]))

            execute_request_id = f"preconsume-execute-{index:04d}"
            rejected = client.post(
                f"/approvals/{approval['approval_id']}/execute",
                headers=HEADERS,
                json={
                    "request_id": execute_request_id,
                    "actor": "user",
                    "reason": "reviewed exact preview",
                    "approved_at": time.time(),
                },
            )

            assert rejected.status_code == 409
            assert rejected.json()["detail"] == expected_detail
            assert app.state.store.get_operation_by_request_id(execute_request_id) is None
            persisted = app.state.store.get_approval(approval["approval_id"])
            assert persisted is not None and persisted.consumed_by_operation_id is None


def test_move_and_cover_use_approval_and_readback(tmp_path):
    library = FakeLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, navidrome_client=FakeNavidrome())
    source = "Artist/Album/One.flac"
    target = "Artist/Album/Renamed.flac"
    cover = b"\x89PNG\r\n\x1a\nfixture-cover"
    encoded = base64.b64encode(cover).decode("ascii")
    with TestClient(app) as client:
        move_approval = submit(client, "library.move", {"items": [{"file_path": source, "target_file_path": target}]}, "move-preview-001").json()["approval"]
        moved = approve(client, move_approval["approval_id"], "move-execute-001")
        assert moved["result"]["overall_status"] == "succeeded"
        assert source not in library.files and target in library.files

        cover_input = {"items": [{"file_path": target, "cover_base64": encoded, "media_type": "image/png", "cover_sha256": hashlib.sha256(cover).hexdigest()}]}
        cover_approval = submit(client, "library.update_cover", cover_input, "cover-preview-001").json()["approval"]
        assert cover_approval["preview"]["items"][0]["target"]["data_uri"]["omitted"] is True
        covered = approve(client, cover_approval["approval_id"], "cover-execute-001")
        assert covered["result"]["overall_status"] == "succeeded"
        assert library.files[target]["cover_sha256"] == hashlib.sha256(cover).hexdigest()


def test_downloaded_temporary_file_can_be_approved_and_added(tmp_path):
    library = FakeLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, navidrome_client=FakeNavidrome())
    temporary_path = app.state.action_context.temporary_dir / "source.mp3"
    temporary_path.write_bytes(b"new-audio")
    sha256 = hashlib.sha256(b"new-audio").hexdigest()
    source_operation, _ = app.state.store.create_operation(action="download.fetch", input={}, request_id="seed-download-op")
    app.state.store.finish(source_operation.id, status="succeeded", result={})
    temporary = app.state.store.create_temporary_file(
        kind="downloaded_audio",
        path=str(temporary_path),
        sha256=sha256,
        size=temporary_path.stat().st_size,
        media_type="audio/mpeg",
        source_operation_id=source_operation.id,
    )
    target = "Artist/New Album/New Song.mp3"
    with TestClient(app) as client:
        approval = submit(client, "library.add", {"items": [{"temporary_file_id": temporary.id, "target_file_path": target}]}, "add-preview-001").json()["approval"]
        assert approval["preview"]["items"][0]["source"]["sha256"] == sha256
        added = approve(client, approval["approval_id"], "add-execute-001")
        assert added["result"]["overall_status"] == "succeeded"
        assert target in library.files
        assert app.state.store.get_temporary_file(temporary.id).deleted_at is not None
        assert not temporary_path.exists()


def test_library_add_preview_rejects_a_changed_temporary_source(tmp_path):
    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary(), navidrome_client=FakeNavidrome())
    temporary_path = app.state.action_context.temporary_dir / "changed.mp3"
    temporary_path.write_bytes(b"original")
    source_operation, _ = app.state.store.create_operation(action="download.fetch", input={}, request_id="changed-source-op")
    app.state.store.finish(source_operation.id, status="succeeded", result={})
    temporary = app.state.store.create_temporary_file(
        kind="downloaded_audio",
        path=str(temporary_path),
        sha256=hashlib.sha256(b"original").hexdigest(),
        size=temporary_path.stat().st_size,
        media_type="audio/mpeg",
        source_operation_id=source_operation.id,
    )
    temporary_path.write_bytes(b"changed")

    with TestClient(app) as client:
        response = submit(
            client,
            "library.add",
            {"items": [{"temporary_file_id": temporary.id, "target_file_path": "Artist/Album/Changed.mp3"}]},
            "changed-add-preview-001",
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "temporary_source_stale"


def test_library_update_lyrics_preserves_unresolved_masks_with_warning(tmp_path):
    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary())
    lyrics = "[00:01.00]say **** now"
    item = {"file_path": "Artist/Album/One.flac", "lyrics": lyrics, "lyrics_sha256": hashlib.sha256(lyrics.encode()).hexdigest()}
    with TestClient(app) as client:
        rejected = submit(client, "library.update_lyrics", {"items": [item]}, "masked-no-evidence-001")
        assert rejected.status_code == 202
        preview = rejected.json()["approval"]["preview"]
        assert preview["items"][0]["warnings"][0]["code"] == "lyrics_masks_preserved"
        assert preview["items"][0]["target"]["lyrics_text"]["sha256"] == hashlib.sha256(lyrics.encode()).hexdigest()


def test_personal_auto_lyrics_derives_hash(tmp_path):
    library = FakeLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, mutation_mode="personal_auto")
    lyrics = "[00:01.00]keep ****"
    with TestClient(app) as client:
        response = submit(client,"library.update_lyrics",{"items":[{"file_path":"Artist/Album/One.flac","lyrics":lyrics}]},"auto-lyrics-nohash")
        assert response.status_code == 202, response.text
        operation=wait_for_operation(client,response.json()["operation"]["operation_id"])
        assert operation["status"] == "succeeded", operation
        assert operation["result"]["ok"] is True
        assert operation["mutation_plan"]["items"][0]["target"]["lyrics_sha256"] == hashlib.sha256(lyrics.encode()).hexdigest()
        assert library.files["Artist/Album/One.flac"]["lyrics"] == lyrics
        catalog=client.get("/actions",headers=HEADERS).json()
        by_name={a["name"]:a for a in catalog["actions"]}
        assert by_name["library.update_lyrics"]["requires_approval"] is False
        assert all(not by_name[a]["requires_approval"] for a in ["library.import","library.delete","library.update_metadata"])
        wrong=submit(client,"library.update_lyrics",{"items":[{"file_path":"Artist/Album/One.flac","lyrics":lyrics,"lyrics_sha256":"a"*64}]},"auto-lyrics-wronghash")
        assert wrong.status_code == 422


@pytest.mark.parametrize("mode", ["strict", "personal_auto"])
def test_catalog_exposes_runtime_policy_for_every_mutation(tmp_path, mode):
    app = create_app(tmp_path, TOKEN, library_inspector=FakeLibrary(), mutation_mode=mode)
    with TestClient(app) as client:
        catalog = client.get("/actions", headers=HEADERS).json()["actions"]
    mutations = {"library.add", "library.import", "library.update_metadata", "library.update_lyrics",
                 "library.update_cover", "library.move", "library.delete"}
    rows = {row["name"]: row for row in catalog}
    for name in mutations:
        assert rows[name]["requires_approval"] is (mode == "strict")
        assert rows[name]["confirmation_policy"] == ("none" if mode == "personal_auto" else "explicit_preview_approval")
        assert rows[name]["execution_mode"] == rows[name]["mutation_policy"] == ("auto_mutation" if mode == "personal_auto" else "strict_approval")
    for name in {"download.fetch", "library.import.prepare", "library.search"}:
        assert rows[name]["execution_mode"] == "asynchronous"
        assert rows[name]["requires_approval"] is False


@pytest.mark.parametrize("action", ["library.add", "library.update_metadata", "library.update_lyrics",
                                    "library.update_cover", "library.move", "library.delete"])
def test_personal_auto_mutations_persist_snapshot_execute_and_retry_once(tmp_path, action):
    library = FakeLibrary()
    navidrome = FakeNavidrome()
    app = create_app(tmp_path, TOKEN, library_inspector=library, navidrome_client=navidrome, mutation_mode="personal_auto")
    path = "Artist/Album/One.flac"
    target = "Artist/Album/New.flac"
    cover = b"\x89PNG\r\n\x1a\nfixture-cover"
    payloads = {
        "library.update_metadata": {"patches": [{"file_path": path, "fields": {"album": "Updated"}}]},
        "library.update_lyrics": {"items": [{"file_path": path, "lyrics": "[00:01.00]synthetic"}]},
        "library.update_cover": {"items": [{"file_path": path, "cover_base64": base64.b64encode(cover).decode(), "media_type": "image/png", "cover_sha256": hashlib.sha256(cover).hexdigest()}]},
        "library.move": {"items": [{"file_path": path, "target_file_path": target}]},
        "library.delete": {"items": [{"file_path": path}]},
    }
    source = app.state.action_context.temporary_dir / "synthetic.flac"
    source.write_bytes(b"synthetic-audio")
    seeded, _ = app.state.store.create_operation(action="download.fetch", input={}, request_id="seed-auto-source")
    app.state.store.finish(seeded.id, status="succeeded", result={})
    temporary = app.state.store.create_temporary_file(kind="downloaded_audio", path=str(source),
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(), size=source.stat().st_size, media_type="audio/flac", source_operation_id=seeded.id)
    payloads["library.add"] = {"items": [{"temporary_file_id": temporary.id, "target_file_path": target}]}
    with TestClient(app) as client:
        response = submit(client, action, payloads[action], "auto-mutation-once")
        assert response.status_code == 202, response.text
        assert "approval" not in response.json()
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
        assert operation["status"] == "succeeded", operation
        assert operation["approval_id"] is None
        assert operation["mutation_plan"]["authoritative_source"] == "musicops_server_current_state"
        persisted = app.state.store.get_operation(operation["operation_id"])
        assert persisted.mutation_plan == operation["mutation_plan"]
        if action == "library.update_metadata":
            assert library.files[path]["metadata"]["album"] == "Updated"
        elif action == "library.update_lyrics":
            assert library.files[path]["lyrics"] == "[00:01.00]synthetic"
        elif action == "library.update_cover":
            assert library.files[path]["cover_sha256"] == hashlib.sha256(cover).hexdigest()
        elif action in {"library.move", "library.add"}:
            assert target in library.files
        else:
            assert path not in library.files
            assert operation["mutation_plan"]["permanent_delete"] is True
        replay = submit(client, action, payloads[action], "auto-mutation-once")
        assert replay.status_code == 200, replay.text
        assert replay.json()["reused"] is True
        assert replay.json()["operation"]["operation_id"] == operation["operation_id"]
        assert replay.json()["operation"]["mutation_plan"] == response.json()["operation"]["mutation_plan"]
        assert navidrome.scans == 1
        conflict = submit(client, "library.search", {"query": "different intent"}, "auto-mutation-once")
        assert conflict.status_code == 409
        with app.state.store._connect() as con:
            assert con.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


def test_personal_auto_rejects_a_file_changed_after_snapshot(tmp_path):
    class RacingLibrary(FakeLibrary):
        def file_snapshot(self, file_path):
            snapshot = super().file_snapshot(file_path)
            self.mutate_outside_ops(file_path)
            return snapshot

    library = RacingLibrary()
    app = create_app(tmp_path, TOKEN, library_inspector=library, mutation_mode="personal_auto")
    path = "Artist/Album/One.flac"
    with TestClient(app) as client:
        response = submit(client, "library.delete", {"items": [{"file_path": path}]}, "auto-stale-delete")
        operation = wait_for_operation(client, response.json()["operation"]["operation_id"])
        assert operation["status"] == "failed", operation
        assert operation["result"]["results"][0]["error"] == "stale_approval"
        assert path in library.files
