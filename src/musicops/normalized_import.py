from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from musicops.actions import ActionContext, ActionFailure, StagedLyricsInput
from musicops.identity import compare_track_identities, identity_compatible as _identity_compatible
from musicops.library import LibraryInspector
from musicops.agent_translation import assemble_bilingual_lrc, validate_canonical_sha256
from musicops.store import OperationRecord
from musicops.vmomo import file_facts


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_record(context: ActionContext, operation_id: str, *, actions: set[str]) -> OperationRecord:
    operation = context.store.get_operation(operation_id)
    if operation is None:
        raise ActionFailure("evidence_operation_not_found", detail={"operation_id": operation_id})
    if operation.action not in actions:
        raise ActionFailure(
            "evidence_operation_action_mismatch",
            detail={"operation_id": operation_id, "expected_actions": sorted(actions), "actual_action": operation.action},
        )
    return operation


def _flat_target_file_path(title: str, artist: str, suffix: str) -> str:
    def clean(value: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(value or ""))
        return re.sub(r"\s+", " ", value).strip(" .-")

    clean_title = clean(title)
    clean_artist = clean(artist)
    suffix = str(suffix or "").lower()
    if not clean_title or not clean_artist or suffix not in {".flac", ".mp3", ".m4a", ".mp4", ".aac"}:
        raise ActionFailure("canonical_target_path_invalid")
    return f"{clean_title} - {clean_artist}{suffix}"


def _lyrics_search_proves_identity(
    context: ActionContext,
    *,
    fetch_operation: OperationRecord,
    evidence_ids: list[str],
    track: dict[str, Any],
) -> bool:
    result = fetch_operation.result or {}
    identity = result.get("identity") or {}
    if identity and _identity_compatible(track, identity):
        return True
    provider = str(result.get("provider") or fetch_operation.input.get("provider") or "")
    provider_id = str(result.get("provider_id") or fetch_operation.input.get("provider_id") or "")
    for evidence_id in evidence_ids:
        operation = _require_record(
            context,
            evidence_id,
            actions={"lyrics.search", "lyrics.fetch", "lyrics.resolve_masks"},
        )
        if operation.action != "lyrics.search" or operation.status != "succeeded":
            continue
        for candidate in list((operation.result or {}).get("candidates") or []):
            if str(candidate.get("provider") or "") != provider or str(candidate.get("provider_id") or "") != provider_id:
                continue
            if _identity_compatible(track, candidate):
                return True
    return False


def _masked_resolution_audit(
    context: ActionContext,
    *,
    operation_id: str,
    canonical_lrc: str,
    canonical_source_operation_id: str,
    input_canonical_lrc_sha256: str,
) -> dict[str, Any]:
    if not operation_id:
        return {}
    operation = _require_record(context, operation_id, actions={"lyrics.resolve_masks"})
    result = operation.result or {}
    if operation.status != "succeeded" or result.get("resolution_status") != "resolved":
        raise ActionFailure("masked_lyrics_resolution_not_resolved")
    canonical_sha256 = hashlib.sha256(canonical_lrc.encode("utf-8")).hexdigest()
    if (
        str(result.get("canonical_source_operation_id") or "") != canonical_source_operation_id
        or str(result.get("input_canonical_lrc_sha256") or "").casefold()
        != input_canonical_lrc_sha256.casefold()
    ):
        raise ActionFailure("masked_lyrics_source_binding_mismatch")
    if (
        str(result.get("resolved_canonical_lrc") or "").strip() != canonical_lrc
        or str(result.get("resolved_canonical_lrc_sha256") or "").lower() != canonical_sha256
    ):
        raise ActionFailure("masked_lyrics_resolved_canonical_mismatch")
    resolutions = list(result.get("resolutions") or [])
    if not resolutions:
        raise ActionFailure("masked_lyrics_resolution_entries_missing")
    return {
        "operation_id": operation.id,
        "canonical_source_operation_id": result.get("canonical_source_operation_id"),
        "input_canonical_lrc_sha256": result.get("input_canonical_lrc_sha256"),
        "resolved_canonical_lrc_sha256": canonical_sha256,
        "resolutions": resolutions,
    }


def _lyrics_for_staging(
    context: ActionContext,
    model: Any,
    *,
    track: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    evidence_rows = []
    evidence_operations: list[OperationRecord] = []
    for operation_id in model.evidence_operation_ids:
        operation = _require_record(
            context,
            operation_id,
            actions={"lyrics.search", "lyrics.fetch", "lyrics.resolve_masks"},
        )
        if operation.status not in {"succeeded", "failed", "uncertain"}:
            raise ActionFailure(
                "lyrics_evidence_operation_not_terminal",
                detail={"operation_id": operation.id, "status": operation.status},
            )
        evidence_operations.append(operation)
        evidence_rows.append({
            "operation_id": operation.id,
            "action": operation.action,
            "status": operation.status,
            "error": operation.error,
        })

    if model.status == "unavailable":
        searches = [operation for operation in evidence_operations if operation.action == "lyrics.search"]
        if not searches:
            warnings.append({"code": "lyrics_not_searched", "detail": model.reason})
        failed_fetches = {
            (str(operation.input.get("provider") or ""), str(operation.input.get("provider_id") or ""))
            for operation in evidence_operations
            if operation.action == "lyrics.fetch" and operation.status == "failed"
        }
        unresolved_candidates = []
        for operation in searches:
            if operation.status != "succeeded":
                continue
            for candidate in list((operation.result or {}).get("candidates") or []):
                key = (str(candidate.get("provider") or ""), str(candidate.get("provider_id") or ""))
                if _identity_compatible(track, candidate) and key not in failed_fetches:
                    unresolved_candidates.append({"provider": key[0], "provider_id": key[1]})
        if unresolved_candidates:
            warnings.append({"code": "lyrics_candidates_not_fetched", "candidates": unresolved_candidates})
        return "", {
            "status": model.status,
            "reason": model.reason,
            "source_language": model.source_language,
            "evidence": evidence_rows,
            "warnings": warnings,
        }

    if model.status == "instrumental":
        return "", {
            "status": model.status,
            "reason": model.reason,
            "source_language": model.source_language,
            "evidence": evidence_rows,
            "warnings": warnings,
        }

    source = _require_record(
        context,
        model.source_operation_id,
        actions={"lyrics.fetch", "lyrics.resolve_masks"},
    )
    if source.status != "succeeded" or not source.result:
        raise ActionFailure("lyrics_source_operation_not_succeeded", detail={"operation_id": source.id, "status": source.status})

    translated = ""
    masked_resolution: dict[str, Any] = {}
    fetch_source = source
    if source.action == "lyrics.resolve_masks":
        result = source.result or {}
        if result.get("resolution_status") != "resolved":
            raise ActionFailure("masked_lyrics_resolution_not_resolved")
        fetch_source = _require_record(
            context,
            str(result.get("canonical_source_operation_id") or ""),
            actions={"lyrics.fetch"},
        )
        if fetch_source.status != "succeeded" or not fetch_source.result:
            raise ActionFailure("lyrics_source_operation_not_succeeded")
        canonical = str(result.get("resolved_canonical_lrc") or "")
        source_canonical = str((fetch_source.result or {}).get("canonical_lrc") or "").strip()
        source_canonical_sha256 = hashlib.sha256(source_canonical.encode("utf-8")).hexdigest()
        masked_resolution = _masked_resolution_audit(
            context,
            operation_id=source.id,
            canonical_lrc=canonical,
            canonical_source_operation_id=fetch_source.id,
            input_canonical_lrc_sha256=source_canonical_sha256,
        )
    else:
        canonical = str((source.result or {}).get("canonical_lrc") or "")
    if not canonical:
        raise ActionFailure("canonical_lyrics_missing")
    identity_verified = _lyrics_search_proves_identity(
        context,
        fetch_operation=fetch_source,
        evidence_ids=model.evidence_operation_ids,
        track=track,
    )
    if model.status == "translated" and not identity_verified:
        raise ActionFailure("lyrics_identity_unconfirmed")
    if not identity_verified:
        warnings.append({"code": "lyrics_identity_unconfirmed", "detail": "Provider text identity was not automatically confirmed"})
    if model.status == "translated":
        try:
            validate_canonical_sha256(canonical, model.source_sha256)
            lyrics = assemble_bilingual_lrc(canonical, model.translated_lines)
        except ValueError as exc:
            raise ActionFailure(str(exc)) from exc
        translated = "\n".join(model.translated_lines)
    else:
        if model.status == "original" and model.source_language.casefold() not in {"zh", "zh-cn", "zh-hans", "zh-hant"}:
            warnings.append({"code": "lyrics_not_translated", "detail": "Original lyrics retained"})
        lyrics = canonical

    if re.search(r"(?<!\*)\*{2,}(?!\*)", lyrics):
        warnings.append({"code": "lyrics_masks_preserved", "detail": "Unresolved masks retained without guessing"})
    return lyrics, {
        "status": model.status,
        "reason": model.reason,
        "source_language": model.source_language,
        "source_operation_id": source.id,
        "canonical_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "translated_sha256": hashlib.sha256(translated.encode("utf-8")).hexdigest() if translated else "",
        "final_sha256": hashlib.sha256(lyrics.encode("utf-8")).hexdigest(),
        "line_count": len(lyrics.splitlines()),
        "evidence": evidence_rows,
        "masked_lyrics_resolution": masked_resolution,
        "warnings": warnings,
    }


def lyrics_for_existing_update(context: ActionContext, item: Any) -> tuple[str, str]:
    """Resolve a legacy complete-text update or a source-bound Agent translation."""
    if item.lyrics:
        return item.lyrics, item.lyrics_sha256

    evidence = context.library_inspector.evidence_for_file(item.file_path, include={"metadata"})
    if not evidence.get("ok") or not evidence.get("exists"):
        raise ActionFailure("lyrics_update_target_unavailable")
    track = dict(evidence.get("metadata") or {})
    if not str(track.get("title") or "").strip() or not str(track.get("artist") or "").strip():
        raise ActionFailure("lyrics_update_target_identity_missing")

    lyrics_input = StagedLyricsInput(
        status="translated",
        source_operation_id=item.source_operation_id,
        source_sha256=item.source_sha256,
        translated_lines=item.translated_lines,
        evidence_operation_ids=item.evidence_operation_ids,
        source_language="unknown",
    )
    lyrics, _audit = _lyrics_for_staging(context, lyrics_input, track=track)
    return lyrics, hashlib.sha256(lyrics.encode("utf-8")).hexdigest()


def _metadata_manifest_for_staging(context: ActionContext, model: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    operation = _require_record(context, model.metadata_operation_id, actions={"metadata.fetch"})
    if operation.status != "succeeded" or not operation.result:
        raise ActionFailure("metadata_fetch_not_succeeded", detail={"operation_id": operation.id, "status": operation.status})
    result = operation.result
    manifest = result.get("manifest") or {}
    actual_sha256 = hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()
    if actual_sha256 != model.manifest_sha256.lower() or str(result.get("manifest_sha256") or "").lower() != actual_sha256:
        raise ActionFailure("manifest_sha256_mismatch")
    matching = [
        row for row in list(manifest.get("tracks") or [])
        if str(row.get("provider_track_id") or "") == model.provider_track_id
    ]
    if len(matching) != 1:
        raise ActionFailure("manifest_track_identity_not_unique", detail={"provider_track_id": model.provider_track_id})
    return manifest, matching[0], result.get("cover") or {}


def _content_rating_for_staging(context: ActionContext, model: Any, *, source_record: Any) -> dict[str, Any]:
    candidate = dict((source_record.metadata or {}).get("candidate") or {})
    original_title = str(candidate.get("title") or "")
    title_is_explicit = bool(re.search(r"\bexplicit\b", original_title, flags=re.IGNORECASE))
    if title_is_explicit:
        if model.content_rating != "explicit":
            raise ActionFailure("vmomo_explicit_title_requires_explicit_rating")
        return {
            "value": "explicit",
            "source": "vmomo_original_title",
            "original_title": original_title,
            "reason": model.content_rating_reason or "Vmomo original title contains Explicit",
        }

    if not model.content_rating_stt_operation_id:
        if model.content_rating != "unknown":
            raise ActionFailure("content_rating_stt_required")
        return {
            "value": "unknown", "source": "not_checked",
            "reason": model.content_rating_reason or "No decisive rating evidence; STT not requested",
            "original_title": original_title,
            "warnings": [{"code": "content_rating_unknown", "detail": "STT not requested"}],
        }
    operation = _require_record(context, model.content_rating_stt_operation_id, actions={"audio.transcribe"})
    if str(operation.input.get("purpose") or "content_rating_stt") != "content_rating_stt":
        raise ActionFailure("content_rating_stt_purpose_mismatch")
    if str(operation.input.get("temporary_file_id") or "") != source_record.id:
        raise ActionFailure("content_rating_stt_source_mismatch")
    if operation.status not in {"succeeded", "failed", "uncertain"}:
        raise ActionFailure("content_rating_stt_not_terminal", detail={"status": operation.status})
    if model.content_rating in {"explicit", "clean"} and operation.status != "succeeded":
        raise ActionFailure("content_rating_stt_not_succeeded")
    succeeded_evidence = [
        row for row in list((operation.result or {}).get("evidence") or [])
        if row.get("status") == "succeeded" and str(row.get("transcript") or "").strip()
    ]
    if model.content_rating in {"explicit", "clean"} and not succeeded_evidence:
        raise ActionFailure("content_rating_positive_stt_evidence_required")
    if not model.content_rating_reason.strip():
        raise ActionFailure("content_rating_reason_required")
    return {
        "value": model.content_rating,
        "source": "bounded_stt",
        "operation_id": operation.id,
        "operation_status": operation.status,
        "purpose": "content_rating_stt",
        "reason": model.content_rating_reason,
        "original_title": original_title,
        "evidence": (operation.result or {}).get("evidence") or [],
        "error": operation.error,
    }


def prepare_staged_import(context: ActionContext, operation: OperationRecord, model: Any) -> dict[str, Any]:
    source = context.store.get_temporary_file(model.temporary_file_id)
    if source is None:
        raise ActionFailure("temporary_file_not_found")
    if source.kind != "downloaded_audio":
        raise ActionFailure("normalized_import_requires_downloaded_audio")
    if source.deleted_at is not None or source.expires_at <= time.time():
        raise ActionFailure("temporary_file_unavailable")
    source_path = Path(source.path).resolve()
    if context.temporary_dir.resolve() not in source_path.parents or not source_path.is_file():
        raise ActionFailure("temporary_source_invalid")
    if str(file_facts(source_path).get("sha256") or "") != source.sha256:
        raise ActionFailure("temporary_source_stale")

    manifest, track, cover_payload = _metadata_manifest_for_staging(context, model)
    candidate = dict((source.metadata or {}).get("candidate") or {})
    source_profile = str((source.metadata or {}).get("operation_profile") or "")
    if source_profile == "audio_only":
        raise ActionFailure("normalized_import_requires_normalized_download", detail={"temporary_file_id": source.id})
    if not candidate.get("title") or not candidate.get("artist"):
        raise ActionFailure("download_candidate_identity_missing")
    identity_match = compare_track_identities(track, candidate)
    warnings = [] if identity_match["compatible"] else [{
        "code": "identity_comparison_warning",
        "identity_match": identity_match,
    }]

    requested_quality = str((source.metadata or {}).get("vmomo_quality") or "")
    selected_quality = str(((source.metadata or {}).get("quality_selection") or {}).get("selected") or "")
    if requested_quality not in {"default", "HQ", "SQ"} or selected_quality != requested_quality:
        raise ActionFailure("download_quality_evidence_invalid")

    lyrics, lyrics_audit = _lyrics_for_staging(
        context,
        model.lyrics,
        track=track,
    )
    content_rating = _content_rating_for_staging(context, model, source_record=source)
    cover_data = b""
    cover_media_type = ""
    cover_status = str(cover_payload.get("status") or "unavailable")
    if cover_status == "available":
        try:
            cover_data = base64.b64decode(str(cover_payload.get("cover_base64") or ""), validate=True)
            cover_media_type = str(cover_payload.get("media_type") or "")
            valid_signature = (
                cover_media_type == "image/png" and cover_data.startswith(b"\x89PNG\r\n\x1a\n")
                or cover_media_type == "image/jpeg" and cover_data.startswith(b"\xff\xd8\xff")
            )
            if not valid_signature:
                raise ValueError("canonical_cover_media_invalid")
            image_probe = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-f", "null", "-"],
                input=cover_data, capture_output=True, timeout=10,
            )
            if image_probe.returncode != 0 or image_probe.stderr.strip():
                raise ValueError("canonical_cover_image_invalid")
            if hashlib.sha256(cover_data).hexdigest() != str(cover_payload.get("sha256") or "").lower():
                raise ValueError("canonical_cover_sha256_mismatch")
        except (ValueError, TypeError, OSError, subprocess.SubprocessError):
            warnings.append({"code": "cover_rejected", "detail": "Invalid optional cover omitted"})
            cover_data, cover_media_type = b"", ""
            cover_payload = {**cover_payload, "status": "unavailable", "reason": "invalid_cover_omitted"}
    elif cover_status != "unavailable":
        warnings.append({"code": "cover_rejected", "detail": "Unknown optional cover status"})
        cover_payload = {**cover_payload, "status": "unavailable", "reason": "invalid_cover_status"}

    canonical_metadata = {
        key: str(track.get(key) or "").strip()
        for key in (
            "title", "artist", "album", "album_artist", "date", "track_number",
            "track_total", "disc_number", "disc_total",
        )
    }
    canonical_metadata = {key: value for key, value in canonical_metadata.items() if value}
    warnings.extend(lyrics_audit.get("warnings") or [])
    warnings.extend(content_rating.get("warnings") or [])
    metadata_operation = context.store.get_operation(model.metadata_operation_id)
    warnings.extend((metadata_operation.result or {}).get("warnings") or [])
    missing_essential = [key for key in ("title", "artist") if not canonical_metadata.get(key)]
    if missing_essential:
        raise ActionFailure("canonical_manifest_incomplete", detail={"missing_fields": missing_essential})
    missing_optional = [key for key in ("album", "album_artist", "date", "track_number", "track_total", "disc_number", "disc_total") if not canonical_metadata.get(key)]
    if missing_optional:
        warnings.append({"code": "optional_metadata_missing", "fields": missing_optional})
    target_file_path = _flat_target_file_path(canonical_metadata["title"], canonical_metadata["artist"], source_path.suffix)
    output_dir = context.temporary_dir / operation.id
    output_path = output_dir / target_file_path
    prepared = context.library_inspector.prepare_staged_import(
        source_path=source_path,
        output_path=output_path,
        expected_source_sha256=source.sha256,
        metadata=canonical_metadata,
        lyrics=lyrics,
        cover_data=cover_data,
        cover_media_type=cover_media_type,
    )
    if not prepared.get("ok"):
        shutil.rmtree(output_dir, ignore_errors=True)
        raise ActionFailure("staged_import_prepare_failed", detail=prepared)

    cover_audit = {key: value for key, value in cover_payload.items() if key != "cover_base64"}
    audit = {
        "import_profile": "normalized_staged",
        "source_temporary_file_id": source.id,
        "source_sha256": source.sha256,
        "download_candidate": candidate,
        "identity_match": identity_match,
        "warnings": warnings,
        "vmomo_quality": requested_quality,
        "quality_selection": (source.metadata or {}).get("quality_selection") or {},
        "quality_filename_warning": (source.metadata or {}).get("quality_filename_warning"),
        "provider_labels": (source.metadata or {}).get("provider_labels") or [],
        "metadata_operation_id": model.metadata_operation_id,
        "manifest": manifest,
        "manifest_sha256": model.manifest_sha256.lower(),
        "provider_track_id": model.provider_track_id,
        "canonical_metadata": canonical_metadata,
        "target_file_path": target_file_path,
        "content_rating": content_rating,
        "lyrics": lyrics_audit,
        "cover": cover_audit,
        "staged_readback": prepared,
    }
    record = context.store.create_temporary_file(
        kind="normalized_staged_import",
        path=str(output_path),
        sha256=str(prepared["staged_sha256"]),
        size=int(prepared["size"]),
        media_type=source.media_type,
        source_operation_id=operation.id,
        metadata=audit,
        ttl_seconds=30 * 24 * 60 * 60,
    )
    return {
        "ok": True,
        "import_profile": "normalized_staged",
        "staged_file": record.to_dict(),
        "identity_match": identity_match,
        "warnings": warnings,
        "target_file_path": target_file_path,
        "manifest_sha256": model.manifest_sha256.lower(),
        "content_rating": content_rating,
        "lyrics": lyrics_audit,
        "cover": cover_audit,
    }


def _require_staged_record(context: ActionContext, staged_file_id: str) -> tuple[Any, Path, dict[str, Any]]:
    record = context.store.get_temporary_file(staged_file_id)
    if record is None:
        raise ActionFailure("staged_file_not_found")
    if record.kind != "normalized_staged_import":
        raise ActionFailure("normalized_staged_file_required")
    if record.deleted_at is not None or record.expires_at <= time.time():
        raise ActionFailure("staged_file_unavailable")
    path = Path(record.path).resolve()
    if context.temporary_dir.resolve() not in path.parents or not path.is_file():
        raise ActionFailure("staged_file_invalid")
    if str(file_facts(path).get("sha256") or "") != record.sha256:
        raise ActionFailure("staged_file_stale")
    return record, path, dict(record.metadata or {})


def preview_staged_import(context: ActionContext, model: Any) -> dict[str, Any]:
    record, path, audit = _require_staged_record(context, model.staged_file_id)
    record = context.store.renew_temporary_file(record.id, ttl_seconds=30 * 24 * 60 * 60)
    target_file_path = str(audit.get("target_file_path") or "")
    manifest_sha256 = str(audit.get("manifest_sha256") or "").lower()
    if model.target_file_path != target_file_path:
        raise ActionFailure("staged_target_path_mismatch", detail={"expected": target_file_path, "actual": model.target_file_path})
    if model.manifest_sha256.lower() != manifest_sha256:
        raise ActionFailure("staged_manifest_sha256_mismatch")

    target_before = context.library_inspector.file_snapshot(target_file_path)
    idempotent = bool(target_before.get("exists")) and str(target_before.get("sha256") or "") == record.sha256
    if target_before.get("exists") and not idempotent:
        raise ActionFailure(
            "replacement_decision_required",
            detail={
                "target_file_path": target_file_path,
                "existing_sha256": str(target_before.get("sha256") or ""),
                "staged_sha256": record.sha256,
            },
        )

    evidence = LibraryInspector(path.parent).evidence_for_file(
        path.name,
        include={"metadata", "audio", "lyrics_text", "cover_data"},
    )
    if not evidence.get("ok"):
        raise ActionFailure("staged_file_readback_failed", detail=evidence)
    lyrics_text = str((evidence.get("lyrics") or {}).get("text") or "")
    cover = dict(evidence.get("cover") or {})
    return {
        "staged_file_id": record.id,
        "import_profile": "normalized_staged",
        "target_file_path": target_file_path,
        "warnings": audit.get("warnings") or [],
        "target_before": target_before,
        "idempotent": idempotent,
        "source": {
            "temporary_file_id": audit.get("source_temporary_file_id"),
            "sha256": audit.get("source_sha256"),
            "download_candidate": audit.get("download_candidate") or {},
            "identity_match": audit.get("identity_match"),
            "warnings": audit.get("warnings") or [],
            "vmomo_quality": audit.get("vmomo_quality"),
            "quality_selection": audit.get("quality_selection") or {},
            "quality_filename_warning": audit.get("quality_filename_warning"),
            "provider_labels": audit.get("provider_labels") or [],
        },
        "staged_file": {
            "sha256": record.sha256,
            "size": record.size,
            "media_type": record.media_type,
            "expires_at": record.expires_at,
            "audio": audit.get("staged_readback", {}).get("audio") or {},
            "tag_audit": audit.get("staged_readback", {}).get("tag_audit") or {},
        },
        "manifest": audit.get("manifest") or {},
        "manifest_sha256": manifest_sha256,
        "provider_track_id": audit.get("provider_track_id"),
        "canonical_metadata": audit.get("canonical_metadata") or {},
        "content_rating": audit.get("content_rating") or {},
        "lyrics": {
            **dict(audit.get("lyrics") or {}),
            "lyrics_text": lyrics_text,
            "final_sha256": hashlib.sha256(lyrics_text.encode("utf-8")).hexdigest() if lyrics_text else "",
        },
        "cover": {
            **dict(audit.get("cover") or {}),
            "present": bool(cover.get("present")),
            "sha256": str(cover.get("sha256") or ""),
            "size": int(cover.get("size") or 0),
            "media_type": str(cover.get("mime") or cover.get("media_type") or ""),
            "data_uri": str(cover.get("data_uri") or ""),
        },
    }


def _approval_preview(context: ActionContext, operation: OperationRecord) -> dict[str, Any]:
    if operation.mutation_plan is not None:
        return operation.mutation_plan
    if not operation.approval_id:
        raise ActionFailure("approval_missing")
    approval = context.store.get_approval(operation.approval_id)
    if approval is None:
        raise ActionFailure("approval_missing")
    return approval.preview


def _consume_temporary_file(context: ActionContext, temporary_file_id: str) -> None:
    record = context.store.get_temporary_file(temporary_file_id)
    if record is None or record.deleted_at is not None:
        return
    path = Path(record.path).resolve()
    if context.temporary_dir.resolve() in path.parents:
        path.unlink(missing_ok=True)
    context.store.mark_temporary_deleted(record.id)


def publish_staged_import(context: ActionContext, operation: OperationRecord, model: Any) -> dict[str, Any]:
    preview = _approval_preview(context, operation)
    items = preview.get("items") or []
    if len(items) != 1:
        raise ActionFailure("approval_preview_invalid")
    row = items[0]
    record, path, audit = _require_staged_record(context, model.staged_file_id)
    if model.target_file_path != str(row.get("target_file_path") or ""):
        raise ActionFailure("approval_target_path_mismatch")
    if model.manifest_sha256.lower() != str(row.get("manifest_sha256") or "").lower():
        raise ActionFailure("approval_manifest_sha256_mismatch")
    if record.sha256 != str((row.get("staged_file") or {}).get("sha256") or ""):
        raise ActionFailure("stale_staged_source")
    approved_resolution = dict((row.get("lyrics") or {}).get("masked_lyrics_resolution") or {})
    staged_resolution = dict((audit.get("lyrics") or {}).get("masked_lyrics_resolution") or {})
    if approved_resolution != staged_resolution:
        raise ActionFailure("stale_masked_lyrics_resolution")

    target_before = context.library_inspector.file_snapshot(model.target_file_path)
    preview_before = row.get("target_before") or {}
    idempotent = bool(target_before.get("exists")) and str(target_before.get("sha256") or "") == record.sha256
    if target_before.get("exists") and not idempotent:
        raise ActionFailure(
            "replacement_decision_required",
            detail={
                "target_file_path": model.target_file_path,
                "existing_sha256": target_before.get("sha256"),
                "staged_sha256": record.sha256,
            },
        )
    if bool(target_before.get("exists")) != bool(preview_before.get("exists")) and not idempotent:
        raise ActionFailure("stale_target")

    if idempotent:
        result = {
            "ok": True,
            "verified": True,
            "idempotent": True,
            "target_file_path": model.target_file_path,
            "sha256": record.sha256,
            "size": target_before.get("size") or record.size,
        }
    else:
        result = context.library_inspector.add_file(
            source_path=path,
            target_file_path=model.target_file_path,
            expected_sha256=record.sha256,
        )
        if not result.get("ok"):
            raise ActionFailure("staged_import_publish_failed", detail=result)

    try:
        readback = context.library_inspector.evidence_for_file(
            model.target_file_path,
            include={"metadata", "audio", "lyrics_text", "cover_summary"},
        )
        snapshot = context.library_inspector.file_snapshot(model.target_file_path)
    except Exception as exc:
        rollback = {"attempted": False, "ok": False}
        if not idempotent:
            try:
                rollback = {"attempted": True, **context.library_inspector.delete_file(file_path=model.target_file_path)}
            except Exception as rollback_exc:
                rollback = {
                    "attempted": True,
                    "ok": False,
                    "error_type": rollback_exc.__class__.__name__,
                    "detail": str(rollback_exc),
                }
        raise ActionFailure(
            "published_file_readback_exception",
            detail={"error_type": exc.__class__.__name__, "detail": str(exc), "rollback": rollback},
            uncertain=bool(not idempotent and not rollback.get("ok")),
        ) from exc

    expected_metadata = row.get("canonical_metadata") or {}
    actual_lyrics = str((readback.get("lyrics") or {}).get("text") or "")
    expected_lyrics = str((row.get("lyrics") or {}).get("lyrics_text") or "")
    actual_cover_sha256 = str((readback.get("cover") or {}).get("sha256") or "")
    expected_cover_sha256 = str((row.get("cover") or {}).get("sha256") or "")
    verified = bool(
        readback.get("ok")
        and snapshot.get("exists")
        and str(snapshot.get("sha256") or "") == record.sha256
        and all(str((readback.get("metadata") or {}).get(key) or "") == str(value) for key, value in expected_metadata.items())
        and actual_lyrics == expected_lyrics
        and actual_cover_sha256 == expected_cover_sha256
    )
    if not verified:
        rollback = {"attempted": False, "ok": False}
        if not idempotent and str(snapshot.get("sha256") or "") == record.sha256:
            rollback = {"attempted": True, **context.library_inspector.delete_file(file_path=model.target_file_path)}
        raise ActionFailure(
            "published_file_readback_failed",
            detail={
                "target_file_path": model.target_file_path,
                "expected_sha256": record.sha256,
                "snapshot": snapshot,
                "expected_metadata": expected_metadata,
                "actual_metadata": readback.get("metadata") or {},
                "expected_lyrics_sha256": hashlib.sha256(expected_lyrics.encode("utf-8")).hexdigest() if expected_lyrics else "",
                "actual_lyrics_sha256": hashlib.sha256(actual_lyrics.encode("utf-8")).hexdigest() if actual_lyrics else "",
                "expected_cover_sha256": expected_cover_sha256,
                "actual_cover_sha256": actual_cover_sha256,
                "rollback": rollback,
            },
        )

    _consume_temporary_file(context, record.id)
    source_temporary_file_id = str(audit.get("source_temporary_file_id") or "")
    if source_temporary_file_id:
        _consume_temporary_file(context, source_temporary_file_id)
    if context.navidrome_client.configured():
        try:
            refresh = context.navidrome_client.start_scan()
        except Exception as exc:
            refresh = {"ok": False, "error_type": exc.__class__.__name__, "detail": str(exc)}
    else:
        refresh = {"ok": False, "detail": "navidrome_not_configured"}
    return {
        "ok": True,
        "overall_status": "published_pending_visibility",
        "publication_status": "published_pending_visibility",
        "import_profile": "normalized_staged",
        "target_file_path": model.target_file_path,
        "manifest_sha256": model.manifest_sha256.lower(),
        "provider_track_id": audit.get("provider_track_id"),
        "content_rating": audit.get("content_rating") or {},
        "file": {**result, "readback_verified": True},
        "navidrome_refresh": refresh,
    }
