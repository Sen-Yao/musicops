from pathlib import Path

from musicops import remote_stt


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"text": "say pussy now"}


def test_remote_stt_records_source_and_audio_clip_hashes(tmp_path, monkeypatch):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio-bytes")

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"clip-bytes")

    monkeypatch.setattr(remote_stt.subprocess, "run", fake_run)
    monkeypatch.setattr(remote_stt.httpx, "post", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setenv("MUSIC_STT_MODEL_ACCURATE", "server-selected-stt")
    payload = {
        "request_id": "request-1",
        "file_path": "song.mp3",
        "source": {"kind": "library", "file_path": "song.mp3"},
        "canonical_lrc_sha256": "a" * 64,
        "quality": "accurate",
        "purpose": "masked_lyrics_stt",
        "markers": [{
            "marker_id": "marker-1",
            "timestamp": "00:01.000",
            "marker_text": "****",
            "line_sha256": "b" * 64,
            "start_seconds": 0,
            "duration_seconds": 4.5,
            "candidates": ["pussy", "fuck"],
        }],
    }

    result = remote_stt.run_remote_clip_stt(audio, payload)
    rejected = remote_stt.run_remote_clip_stt(tmp_path / "missing.mp3", payload)

    assert result["ok"] is True
    assert len(result["audio_sha256"]) == 64
    assert len(result["evidence"][0]["clip_sha256"]) == 64
    assert result["evidence"][0]["transcript"] == "say pussy now"
    assert result["model"] == "server-selected-stt"
    assert result["purpose"] == "masked_lyrics_stt"
    assert result["evidence"][0]["purpose"] == "masked_lyrics_stt"
    assert result["evidence"][0]["provider"] == "openai_compatible_stt"
    assert len(result["evidence"][0]["evidence_sha256"]) == 64
    assert result["source"]["kind"] == "library"
    assert result["source"]["sha256"] == result["audio_sha256"]
    assert rejected["ok"] is False
