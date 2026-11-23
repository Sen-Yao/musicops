from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import httpx


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_row(payload: dict[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    row["evidence_sha256"] = hashlib.sha256(encoded).hexdigest()
    return row


def run_remote_clip_stt(audio_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    purpose = str(payload.get("purpose") or "content_rating_stt")
    if not audio_path.is_file():
        return {
            "ok": False,
            "detail": "audio_source_missing",
            "purpose": purpose,
            "source": payload.get("source") or {},
        }
    audio_sha256 = _sha256_file(audio_path)
    endpoint = os.environ.get("MUSIC_STT_ENDPOINT", "http://192.168.1.6:8000/v1/audio/transcriptions")
    quality = str(payload.get("quality") or "balanced").upper()
    model = os.environ.get(
        f"MUSIC_STT_MODEL_{quality}",
        os.environ.get("MUSIC_STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2"),
    )
    timeout = max(1, int(os.environ.get("MUSIC_STT_TIMEOUT", "120")))
    evidence: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="musicops-stt-") as temporary_dir:
        markers = list(payload.get("markers") or [])
        if not markers:
            start_seconds = max(0.0, float(payload.get("start_seconds") or 0.0))
            end_seconds = payload.get("end_seconds")
            duration_seconds = (
                max(0.5, float(end_seconds) - start_seconds)
                if end_seconds is not None
                else max(0.5, float(payload.get("duration_seconds") or 15.0))
            )
            markers = [{
                "marker_id": "clip-1",
                "start_seconds": start_seconds,
                "duration_seconds": min(60.0, duration_seconds),
            }]
        for marker in markers:
            marker_id = str(marker.get("marker_id") or "")
            start_seconds = max(0.0, float(marker.get("start_seconds") or 0.0))
            duration_seconds = min(15.0, max(0.5, float(marker.get("duration_seconds") or 4.5)))
            clip_path = Path(temporary_dir) / f"{marker_id or len(evidence)}.wav"
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start_seconds:.3f}", "-t", f"{duration_seconds:.3f}",
                "-i", str(audio_path), "-ac", "1", "-ar", "16000", str(clip_path),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
                with clip_path.open("rb") as clip:
                    response = httpx.post(
                        endpoint,
                        data={"model": model},
                        files={"file": (clip_path.name, clip, "audio/wav")},
                        timeout=timeout,
                    )
                response.raise_for_status()
                body = response.json()
                transcript = str(body.get("text") or "").strip()
                evidence.append(_evidence_row({
                    **marker,
                    "purpose": purpose,
                    "status": "succeeded" if transcript else "failed",
                    "transcript": transcript,
                    "provider": "openai_compatible_stt",
                    "model": model,
                    "audio_sha256": audio_sha256,
                    "clip_sha256": _sha256_file(clip_path),
                    "error": "" if transcript else "empty_transcript",
                }))
            except Exception as exc:
                evidence.append(_evidence_row({
                    **marker,
                    "purpose": purpose,
                    "status": "failed",
                    "transcript": "",
                    "provider": "openai_compatible_stt",
                    "model": model,
                    "audio_sha256": audio_sha256,
                    "error": f"{exc.__class__.__name__}:{exc}",
                }))

    succeeded = sum(item.get("status") == "succeeded" for item in evidence)
    return {
        "ok": succeeded == len(evidence) and bool(evidence),
        "request_id": payload.get("request_id"),
        "purpose": purpose,
        "canonical_lrc_sha256": payload.get("canonical_lrc_sha256"),
        "file_path": payload.get("file_path"),
        "temporary_file_id": payload.get("temporary_file_id"),
        "source": {**dict(payload.get("source") or {}), "sha256": audio_sha256},
        "audio_sha256": audio_sha256,
        "provider": "openai_compatible_stt",
        "model": model,
        "quality": quality.casefold(),
        "marker_count": len(evidence),
        "succeeded_marker_count": succeeded,
        "evidence": evidence,
    }
