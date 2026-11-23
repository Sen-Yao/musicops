from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from musicops.identity import artist_identity_compatible, compare_track_identities, identity_compatible as _identity_compatible
from musicops.library import LibraryInspector
from musicops.lyrics_provider import LyricsProviderClient
from musicops.masked_lyrics import MASK_PATTERN, resolve_masked_lyrics
from musicops.store import MusicOpsStore, OperationRecord
from musicops.vmomo import VmomoClient, file_facts


Quality = Literal["fast", "balanced", "accurate"]
VmomoQuality = Literal["default", "HQ", "SQ"]
DownloadProfile = Literal["normalized_staged", "audio_only"]
MetadataProvider = Literal["musicbrainz", "netease", "qq", "apple_music", "spotify"]
MetadataSupplementField = Literal["title", "artist", "album", "album_artist", "date", "cover"]
ConfirmationPolicy = Literal["none", "explicit_intent_sufficient", "explicit_preview_approval"]
Availability = Callable[["ActionContext"], tuple[str, str]]
ResourceResolver = Callable[[BaseModel], list[tuple[str, str]]]
Handler = Callable[["ActionContext", OperationRecord, BaseModel], "ActionResult"]
PreviewBuilder = Callable[["ActionContext", BaseModel], dict[str, Any]]


class ActionFailure(RuntimeError):
    def __init__(self, code: str, *, detail: Any = None, retryable: bool = False, uncertain: bool = False,
                 error_type: str | None = None, field: str | None = None, message: str | None = None,
                 suggested_action: str | None = None, retry_with: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.uncertain = uncertain
        self.error_type = error_type
        self.field = field
        self.message = message
        self.suggested_action = suggested_action
        self.retry_with = retry_with

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "detail": self.detail, "retryable": self.retryable, "uncertain": self.uncertain}
        for key in ("error_type", "field", "message", "suggested_action", "retry_with"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


@dataclass
class ActionResult:
    payload: dict[str, Any]


@dataclass
class ActionContext:
    store: MusicOpsStore
    data_path: Path
    temporary_dir: Path
    vmomo_client: VmomoClient
    library_inspector: LibraryInspector
    navidrome_client: Any
    lyrics_provider: LyricsProviderClient
    metadata_provider: Any
    remote_stt_runner: Any

    def progress(self, operation_id: str, stage: str, **details: Any) -> dict[str, Any]:
        payload = {"stage": stage, **details}
        self.store.append_operation_event(operation_id, "progress", payload)
        return payload

    def raise_if_cancel_requested(self, operation_id: str) -> None:
        if self.store.is_cancel_requested(operation_id):
            raise ActionFailure("operation_cancelled")


def _available(_context: ActionContext) -> tuple[str, str]:
    return "available", ""


@dataclass
class Action:
    name: str
    input_model: type[BaseModel]
    handler: Handler
    risk: str = "read_only"
    requires_approval: bool = False
    confirmation_policy: ConfirmationPolicy = "none"
    data_handling: dict[str, Any] | None = None
    operation_profile: str | None = None
    lease_recovery_safe: bool = True
    max_attempts: int = 2
    availability: Availability = _available
    resources: ResourceResolver = field(default=lambda _model: [])
    preview: PreviewBuilder | None = None

    def contract_descriptor(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        confirmation_policy: ConfirmationPolicy = (
            "explicit_preview_approval" if self.requires_approval else self.confirmation_policy
        )
        row = {
            "name": self.name,
            "input_schema": schema,
            "schema_digest": hashlib.sha256(_canonical_json(schema).encode("utf-8")).hexdigest(),
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "confirmation_policy": confirmation_policy,
            "execution": "asynchronous",
            "operation_max_attempts": self.max_attempts,
            "lease_recovery_safe": self.lease_recovery_safe,
        }
        if self.data_handling is not None:
            row["data_handling"] = self.data_handling
        if self.operation_profile is not None:
            row["operation_profile"] = self.operation_profile
        return row

    def schema_digest(self) -> str:
        schema = self.input_model.model_json_schema()
        return hashlib.sha256(_canonical_json(schema).encode("utf-8")).hexdigest()

    def contract_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.contract_descriptor()).encode("utf-8")).hexdigest()

    def descriptor(self, context: ActionContext) -> dict[str, Any]:
        row = self.contract_descriptor()
        row["contract_digest"] = self.contract_digest()
        availability, reason = self.availability(context)
        row["availability"] = availability
        if reason:
            row["availability_reason"] = reason
        return row


    def effective_descriptor(self, context: ActionContext, mutation_mode: str) -> dict[str, Any]:
        row = self.descriptor(context)
        row.update({"mutation_mode": mutation_mode, "execution_mode": "asynchronous"})
        if self.requires_approval:
            auto = mutation_mode == "personal_auto"
            policy = "auto_mutation" if auto else "strict_approval"
            row.update({
                "requires_approval": not auto,
                "confirmation_policy": "none" if auto else "explicit_preview_approval",
                "execution_mode": policy,
                "mutation_policy": policy,
            })
        return row


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(ActionInput):
    pass


class LibrarySearchInput(ActionInput):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
    max_scanned: int = Field(default=5000, ge=1, le=50000)
    include: list[Literal["lyrics_summary"]] = Field(default_factory=list)


class LibraryReadInput(ActionInput):
    file_path: str = Field(min_length=1, max_length=2000)
    include: list[Literal["metadata", "audio", "lyrics_summary", "lyrics_text", "cover_summary", "cover_data"]] = Field(
        default_factory=lambda: ["metadata", "audio", "lyrics_summary", "cover_summary"]
    )


class AlbumDiagnoseInput(ActionInput):
    album: str = Field(min_length=1, max_length=500)
    artist: str = Field(default="", max_length=500)
    canonical_tracks: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    candidate_paths: list[str] = Field(default_factory=list, max_length=500)
    max_scanned: int = Field(default=50000, ge=1, le=100000)


class AlbumMetadataAuditInput(ActionInput):
    """Read-only full-library ALBUM completeness audit."""

    max_scanned: int = Field(default=50000, ge=1, le=100000)
    lookup_candidates: bool = True
    candidate_limit: int = Field(default=5, ge=1, le=20)


class NavidromeSearchInput(ActionInput):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class NavidromeVerifyInput(ActionInput):
    file_path: str = Field(min_length=1, max_length=2000)
    query: str = Field(default="", max_length=500)
    expected_metadata: dict[str, Any] = Field(default_factory=dict)
    expected_present: bool = True
    match_strategy: Literal["exact_path", "unique_metadata"] = "exact_path"

    @model_validator(mode="after")
    def validate_match_strategy(self) -> "NavidromeVerifyInput":
        if self.match_strategy == "unique_metadata" and not self.expected_metadata:
            raise ValueError("expected_metadata_required_for_unique_metadata")
        return self


class DownloadSearchInput(ActionInput):
    query: str = Field(min_length=1, max_length=500)
    timeout: int = Field(default=90, ge=5, le=300)


class DownloadFetchInput(ActionInput):
    search_msg_id: int | None = Field(
        default=None,
        gt=0,
        description="Required only for audio_only. normalized_staged derives it from search_operation_id.",
    )
    search_operation_id: str = Field(
        default="",
        max_length=200,
        description="Required for normalized_staged; binds the selected candidate to a succeeded download.search operation.",
    )
    candidate_index: int = Field(ge=1, le=100)
    vmomo_quality: VmomoQuality
    operation_profile: DownloadProfile = Field(
        default="normalized_staged",
        description="normalized_staged requires search and canonical metadata bindings; audio_only uses search_msg_id only.",
    )
    metadata_operation_id: str = Field(default="", max_length=200, description="Required for normalized_staged.")
    manifest_sha256: str = Field(default="", max_length=64, description="Required for normalized_staged.")
    provider_track_id: str = Field(default="", max_length=200, description="Required for normalized_staged.")
    keywords: list[str] = Field(default_factory=list, max_length=20)
    timeout: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="Total download.fetch deadline in seconds across quality selection, media wait, and all transfer attempts.",
    )
    retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Maximum additional media-transfer attempts within the total timeout; this is separate from operation_max_attempts.",
    )

    @model_validator(mode="after")
    def validate_download_binding(self) -> "DownloadFetchInput":
        if self.operation_profile == "normalized_staged":
            if self.search_msg_id is not None:
                raise ValueError("normalized_download_uses_search_operation")
            if not self.search_operation_id or not self.metadata_operation_id or not self.provider_track_id:
                raise ValueError("normalized_download_requires_search_and_metadata_binding")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", self.manifest_sha256):
                raise ValueError("normalized_download_manifest_sha256_required")
        elif self.operation_profile == "audio_only":
            if self.search_msg_id is None:
                raise ValueError("audio_only_download_requires_search_msg_id")
            if self.search_operation_id or self.metadata_operation_id or self.manifest_sha256 or self.provider_track_id:
                raise ValueError("audio_only_download_cannot_use_normalized_binding")
        return self


class MetadataSearchInput(ActionInput):
    query: str = Field(min_length=1, max_length=500)
    artist: str = Field(default="", max_length=500)
    album: str = Field(default="", max_length=500)
    limit: int = Field(default=10, ge=1, le=50)


class MetadataSupplementalSource(ActionInput):
    provider: MetadataProvider
    provider_release_id: str = Field(min_length=1, max_length=200)
    market: str = Field(default="", max_length=20)
    fields: list[MetadataSupplementField] = Field(min_length=1, max_length=6)


class MetadataFetchInput(ActionInput):
    provider: MetadataProvider
    provider_release_id: str = Field(min_length=1, max_length=200)
    market: str = Field(default="", max_length=20)
    supplemental_sources: list[MetadataSupplementalSource] = Field(default_factory=list, max_length=4)


class LyricsSearchInput(ActionInput):
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field(default="", max_length=500)
    album: str = Field(default="", max_length=500)
    quality: Quality = "balanced"
    limit: int = Field(default=10, ge=1, le=50)


class LyricsFetchInput(ActionInput):
    provider: Literal["lrclib", "netease", "qq"]
    provider_id: str = Field(min_length=1, max_length=200)


class MusicIdentity(ActionInput):
    identity_status: Literal["confirmed", "external_confirmed"]
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field(min_length=1, max_length=500)
    album: str = Field(default="", max_length=500)
    source_kind: Literal["library", "provider", "user_approved"]
    source_ref: str = Field(min_length=1, max_length=2000)


class AudioTranscribeInput(ActionInput):
    file_path: str | None = Field(default=None, min_length=1, max_length=2000)
    temporary_file_id: str | None = Field(default=None, min_length=1, max_length=200)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    quality: Quality = "balanced"
    purpose: Literal["content_rating_stt", "masked_lyrics_stt"] = "content_rating_stt"
    canonical_source_operation_id: str = Field(default="", max_length=200)
    canonical_lrc_sha256: str = Field(default="", pattern=r"^(?:[0-9a-fA-F]{64})?$")
    music_identity: MusicIdentity | None = None

    @model_validator(mode="after")
    def validate_window(self) -> "AudioTranscribeInput":
        if (self.file_path is None) == (self.temporary_file_id is None):
            raise ValueError("provide_file_path_or_temporary_file_id")
        if self.end_seconds is not None:
            if self.end_seconds <= self.start_seconds:
                raise ValueError("end_seconds_must_exceed_start_seconds")
            if self.end_seconds - self.start_seconds > 60:
                raise ValueError("transcription_window_exceeds_60_seconds")
        if self.purpose == "masked_lyrics_stt":
            if self.end_seconds is None:
                raise ValueError("masked_lyrics_stt_requires_bounded_window")
            if self.end_seconds - self.start_seconds > 15:
                raise ValueError("masked_lyrics_stt_window_exceeds_15_seconds")
            if not self.canonical_source_operation_id or not self.canonical_lrc_sha256 or self.music_identity is None:
                raise ValueError("masked_lyrics_stt_provenance_required")
        return self


class MaskedLyricResolution(ActionInput):
    canonical_timestamp: str = Field(pattern=r"^\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?$")
    original_text: str = Field(min_length=1, max_length=200)
    replacement_text: str = Field(min_length=1, max_length=200)
    method: Literal["agent_inferred", "agent_resolved_with_stt_hint"]
    stt_operation_id: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_resolution(self) -> "MaskedLyricResolution":
        if any(character.isspace() for character in self.original_text + self.replacement_text):
            raise ValueError("masked_lyrics_resolution_must_use_single_tokens")
        if self.original_text == self.replacement_text:
            raise ValueError("masked_lyrics_resolution_must_change_text")
        if MASK_PATTERN.search(self.replacement_text) or re.search(r"\[\d{1,3}:[0-5]\d", self.replacement_text):
            raise ValueError("masked_lyrics_replacement_text_invalid")
        if self.method == "agent_inferred" and self.stt_operation_id:
            raise ValueError("agent_inferred_must_not_reference_stt")
        if self.method == "agent_resolved_with_stt_hint" and not self.stt_operation_id:
            raise ValueError("stt_hint_operation_required")
        return self


class ResolveMaskedLyricsInput(ActionInput):
    canonical_source_operation_id: str = Field(min_length=1, max_length=200)
    canonical_lrc_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    identity_evidence_operation_id: str = Field(min_length=1, max_length=200)
    music_identity: MusicIdentity
    resolutions: list[MaskedLyricResolution] = Field(min_length=1, max_length=100)


class TemporaryInput(ActionInput):
    temporary_file_id: str = Field(min_length=1, max_length=200)


class MetadataPatchItem(ActionInput):
    file_path: str = Field(min_length=1, max_length=2000)
    fields: dict[str, Any] = Field(min_length=1, max_length=20)


class UpdateMetadataInput(ActionInput):
    patches: list[MetadataPatchItem] = Field(min_length=1, max_length=500)


class LyricsUpdateItem(ActionInput):
    file_path: str = Field(min_length=1, max_length=2000)
    lyrics: str = Field(default="", max_length=500000)
    lyrics_sha256: str = Field(default="", pattern=r"^$|^[0-9a-fA-F]{64}$")
    source_operation_id: str = Field(default="", max_length=200)
    source_sha256: str = Field(default="", pattern=r"^$|^[0-9a-fA-F]{64}$")
    translated_lines: list[str] = Field(default_factory=list, max_length=5000)
    evidence_operation_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_payload(self) -> "LyricsUpdateItem":
        agent_payload = bool(self.source_operation_id or self.source_sha256 or self.translated_lines or self.evidence_operation_ids)
        if bool(self.lyrics) == agent_payload:
            raise ValueError("lyrics_update_payload_must_be_exactly_one_mode")
        if agent_payload and (not self.source_operation_id or not self.source_sha256):
            raise ValueError("lyrics_source_binding_required")
        if agent_payload and self.lyrics_sha256:
            raise ValueError("lyrics_hash_not_allowed_with_agent_payload")
        if self.lyrics_sha256 and hashlib.sha256(self.lyrics.encode("utf-8")).hexdigest() != self.lyrics_sha256.lower():
            raise ValueError("lyrics_sha256_mismatch")
        if self.lyrics:
            self.lyrics_sha256 = hashlib.sha256(self.lyrics.encode("utf-8")).hexdigest()
        return self


class UpdateLyricsInput(ActionInput):
    items: list[LyricsUpdateItem] = Field(min_length=1, max_length=500)


class CoverUpdateItem(ActionInput):
    file_path: str = Field(min_length=1, max_length=2000)
    cover_base64: str = Field(min_length=1, max_length=20_000_000)
    media_type: Literal["image/jpeg", "image/png"]
    cover_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @model_validator(mode="after")
    def validate_cover(self) -> "CoverUpdateItem":
        try:
            data = base64.b64decode(self.cover_base64, validate=True)
        except Exception as exc:
            raise ValueError("invalid_cover_base64") from exc
        if hashlib.sha256(data).hexdigest() != self.cover_sha256.lower():
            raise ValueError("cover_sha256_mismatch")
        return self


class UpdateCoverInput(ActionInput):
    items: list[CoverUpdateItem] = Field(min_length=1, max_length=100)


class MoveItem(ActionInput):
    file_path: str = Field(min_length=1, max_length=2000)
    target_file_path: str = Field(min_length=1, max_length=2000)


class MoveInput(ActionInput):
    items: list[MoveItem] = Field(min_length=1, max_length=500)


class DeleteItem(ActionInput):
    file_path: str = Field(min_length=1, max_length=2000)


class DeleteInput(ActionInput):
    items: list[DeleteItem] = Field(min_length=1, max_length=500)


class AddItem(ActionInput):
    temporary_file_id: str = Field(min_length=1, max_length=200)
    target_file_path: str = Field(min_length=1, max_length=2000)


class AddInput(ActionInput):
    items: list[AddItem] = Field(min_length=1, max_length=500)


class StagedLyricsInput(ActionInput):
    status: Literal["translated", "original", "translation_failed", "unavailable", "instrumental"]
    source_operation_id: str = Field(default="", max_length=200)
    source_sha256: str = Field(default="", pattern=r"^$|^[0-9a-fA-F]{64}$")
    translated_lines: list[str] = Field(default_factory=list, max_length=5000)
    evidence_operation_ids: list[str] = Field(default_factory=list, max_length=20)
    source_language: str = Field(default="unknown", min_length=2, max_length=30)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_evidence(self) -> "StagedLyricsInput":
        if self.status in {"translated", "original", "translation_failed"} and not self.source_operation_id:
            raise ValueError("lyrics_source_operation_required")
        if self.status == "translated" and not self.source_sha256:
            raise ValueError("lyrics_source_sha256_required")
        if self.status != "translated" and (self.source_sha256 or self.translated_lines):
            raise ValueError("translated_payload_requires_translated_status")
        if self.status in {"translation_failed", "unavailable", "instrumental"} and not self.reason.strip():
            raise ValueError("lyrics_status_reason_required")
        return self


class StagedImportPrepareInput(ActionInput):
    temporary_file_id: str = Field(min_length=1, max_length=200)
    metadata_operation_id: str = Field(min_length=1, max_length=200)
    manifest_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    provider_track_id: str = Field(min_length=1, max_length=200)
    lyrics: StagedLyricsInput
    content_rating: Literal["explicit", "clean", "unknown"]
    content_rating_stt_operation_id: str = Field(default="", max_length=200)
    content_rating_reason: str = Field(default="", max_length=1000)


class StagedImportInput(ActionInput):
    staged_file_id: str = Field(min_length=1, max_length=200)
    target_file_path: str = Field(min_length=1, max_length=2000)
    manifest_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _vmomo_available(context: ActionContext) -> tuple[str, str]:
    check = getattr(context.vmomo_client, "availability", None)
    if callable(check):
        return check()
    return "available", ""


def _navidrome_available(context: ActionContext) -> tuple[str, str]:
    return ("available", "") if context.navidrome_client.configured() else ("unavailable", "navidrome_not_configured")


def _metadata_available(context: ActionContext) -> tuple[str, str]:
    configured = getattr(context.metadata_provider, "configured", None)
    return ("available", "") if callable(configured) and configured() else ("unavailable", "metadata_provider_not_configured")


def _library_resource(model: BaseModel, mode: str) -> list[tuple[str, str]]:
    paths: list[str] = []
    if hasattr(model, "file_path"):
        paths.append(str(getattr(model, "file_path")))
    for name in ("items", "patches"):
        for item in list(getattr(model, name, []) or []):
            paths.append(str(getattr(item, "file_path", "") or getattr(item, "target_file_path", "")))
            target = str(getattr(item, "target_file_path", "") or "")
            if target:
                paths.append(target)
    return [(mode, f"library:{path}") for path in sorted(set(paths)) if path]


def _audio_resource(model: BaseModel) -> list[tuple[str, str]]:
    temporary_file_id = str(getattr(model, "temporary_file_id", "") or "")
    if temporary_file_id:
        return [("read", f"temporary:{temporary_file_id}")]
    return _library_resource(model, "read")


def _add_resources(model: BaseModel) -> list[tuple[str, str]]:
    resources = _library_resource(model, "write")
    resources.extend(
        ("read", f"temporary:{item.temporary_file_id}")
        for item in list(getattr(model, "items", []) or [])
        if getattr(item, "temporary_file_id", None)
    )
    return sorted(set(resources))


def _prepare_import_resources(model: BaseModel) -> list[tuple[str, str]]:
    temporary_file_id = str(getattr(model, "temporary_file_id", "") or "")
    return [("read", f"temporary:{temporary_file_id}")] if temporary_file_id else []


def _staged_import_resources(model: BaseModel) -> list[tuple[str, str]]:
    staged_file_id = str(getattr(model, "staged_file_id", "") or "")
    target_file_path = str(getattr(model, "target_file_path", "") or "")
    resources = []
    if staged_file_id:
        resources.append(("write", f"temporary:{staged_file_id}"))
    if target_file_path:
        resources.append(("write", f"library:{target_file_path}"))
    return sorted(set(resources))


def _resolve_audio_source(context: ActionContext, model: AudioTranscribeInput) -> tuple[Path, dict[str, Any]]:
    if model.file_path is not None:
        resolved = context.library_inspector.resolve_path(model.file_path)
        if not resolved.get("ok") or not resolved.get("exists") or not resolved.get("is_file"):
            raise ActionFailure(
                "audio_source_unavailable",
                detail={"source_kind": "library", "file_path": model.file_path, "path_evidence": resolved},
            )
        return Path(str(resolved["resolved_path"])), {"kind": "library", "file_path": model.file_path}

    assert model.temporary_file_id is not None
    record = context.store.get_temporary_file(model.temporary_file_id)
    if record is None:
        raise ActionFailure("temporary_file_not_found")
    if record.deleted_at is not None or record.expires_at <= time.time():
        raise ActionFailure("temporary_file_unavailable", detail={"temporary_file_id": record.id})
    path = Path(record.path).resolve()
    root = context.temporary_dir.resolve()
    if root not in path.parents or not path.is_file():
        raise ActionFailure("temporary_source_invalid", detail={"temporary_file_id": record.id})
    current = file_facts(path)
    if str(current.get("sha256") or "") != record.sha256:
        raise ActionFailure("temporary_source_stale", detail={"temporary_file_id": record.id})
    return path, {"kind": "temporary", "temporary_file_id": record.id, "sha256": record.sha256}


def _require_ok(payload: dict[str, Any], code: str, *, retryable: bool = False) -> dict[str, Any]:
    if payload.get("ok") is False or payload.get("success") is False:
        raise ActionFailure(code, detail=payload, retryable=retryable)
    return payload


def _library_search(context: ActionContext, _operation: OperationRecord, model: LibrarySearchInput) -> ActionResult:
    kwargs = {"include": set(model.include)} if model.include else {}
    return ActionResult(context.library_inspector.search_files(model.query, model.limit, model.max_scanned, **kwargs))


def _library_read(context: ActionContext, _operation: OperationRecord, model: LibraryReadInput) -> ActionResult:
    return ActionResult(_require_ok(context.library_inspector.evidence_for_file(model.file_path, include=set(model.include)), "library_read_failed"))


def _album_diagnose(context: ActionContext, _operation: OperationRecord, model: AlbumDiagnoseInput) -> ActionResult:
    return ActionResult(_require_ok(context.library_inspector.diagnose_album(**model.model_dump()), "album_diagnose_failed"))


def _candidate_match_evidence(item: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") or {}
    expected_artist = str(metadata.get("artist") or metadata.get("album_artist") or "").strip()
    candidate_artist = str(candidate.get("album_artist") or "").strip()
    artist_match = artist_identity_compatible(expected_artist, candidate_artist)
    return {
        "query_title": str(metadata.get("title") or ""),
        "query_artist": expected_artist,
        "artist_match": artist_match,
        "identity_basis": "track title and artist were used as the provider search query; provider returned an album candidate",
        "evidence_sufficient_for_write": False,
        "track_matches": [],
    }


def _audit_album_metadata(context: ActionContext, operation: OperationRecord, model: AlbumMetadataAuditInput) -> ActionResult:
    payload = context.library_inspector.scan_missing_album(
        max_scanned=model.max_scanned,
        cancel_check=lambda: context.store.is_cancel_requested(operation.id),
    )
    _require_ok(payload, "library_album_metadata_audit_failed")
    lookup_failed = 0
    lookup_attempted = 0
    for item in payload.get("missing_items") or []:
        if not model.lookup_candidates:
            item["candidate_status"] = "not_requested"
            item["candidates"] = []
            continue
        lookup_attempted += 1
        metadata = item.get("metadata") or {}
        title = str(metadata.get("title") or "").strip()
        artist = str(metadata.get("artist") or metadata.get("album_artist") or "").strip()
        if not title and not artist:
            item["candidate_status"] = "insufficient_identity"
            item["candidate_reason"] = "title_and_artist_missing"
            item["candidates"] = []
            lookup_failed += 1
            continue
        try:
            result = context.metadata_provider.search(query=title or artist, artist=artist, album="", limit=model.candidate_limit)
        except Exception as exc:
            result = {"ok": False, "error": f"{exc.__class__.__name__}:{exc}", "candidates": []}
        candidates = []
        for candidate in list(result.get("candidates") or []):
            row = dict(candidate)
            evidence = _candidate_match_evidence(item, row)
            fetch_fn = getattr(context.metadata_provider, "fetch", None)
            provider_id = str(row.get("provider_release_id") or "")
            if callable(fetch_fn) and provider_id:
                try:
                    fetched = fetch_fn(
                        provider=str(row.get("provider") or ""),
                        provider_release_id=provider_id,
                        market=str(row.get("market") or ""),
                    )
                except Exception as exc:
                    fetched = {"ok": False, "error": f"{exc.__class__.__name__}:{exc}"}
                row["source_fetch"] = {
                    "ok": bool(fetched.get("ok")),
                    "manifest_sha256": fetched.get("manifest_sha256") or "",
                    "error": fetched.get("error") if not fetched.get("ok") else "",
                }
                tracks = list((fetched.get("manifest") or {}).get("tracks") or []) if fetched.get("ok") else []
                expected_title = str(metadata.get("title") or "")
                expected_artist = str(metadata.get("artist") or metadata.get("album_artist") or "")
                evidence["track_matches"] = [
                    {
                        "track_number": track.get("track_number"),
                        "disc_number": track.get("disc_number"),
                        "identity": compare_track_identities(
                            {"title": expected_title, "artist": expected_artist},
                            {"title": track.get("title"), "artist": track.get("artist")},
                        ),
                    }
                    for track in tracks
                    if compare_track_identities(
                        {"title": expected_title, "artist": expected_artist},
                        {"title": track.get("title"), "artist": track.get("artist")},
                    ).get("compatible")
                ]
                evidence["evidence_sufficient_for_write"] = len(evidence["track_matches"]) == 1 and evidence["artist_match"]
            row["match_evidence"] = evidence
            row["source"] = {
                "provider": row.get("provider"),
                "provider_release_id": row.get("provider_release_id"),
                "market": row.get("market") or "",
                "search_source_sha256": result.get("source_sha256") or "",
                "search_fetched_at": result.get("fetched_at"),
            }
            candidates.append(row)
        compatible = [
            row for row in candidates
            if (row.get("match_evidence") or {}).get("evidence_sufficient_for_write")
        ]
        if not result.get("ok"):
            item["candidate_status"] = "lookup_failed"
            item["candidate_reason"] = result.get("error") or "metadata_provider_search_failed"
            lookup_failed += 1
        elif len(compatible) == 1:
            item["candidate_status"] = "suggested"
            item["candidate_reason"] = "single_track_identity_match_requires_existing_approval_flow"
        elif len(compatible) > 1:
            item["candidate_status"] = "needs_manual_review"
            item["candidate_reason"] = "multiple_artist_compatible_candidates"
        else:
            item["candidate_status"] = "needs_manual_review"
            item["candidate_reason"] = "no_artist_compatible_candidate"
        item["candidates"] = candidates
        item["candidate_count"] = len(candidates)
    payload["candidate_lookup_attempted"] = lookup_attempted
    payload["candidate_lookup_failed_count"] = lookup_failed
    payload["candidate_lookup_status"] = (
        "not_requested" if not model.lookup_candidates else ("partial" if lookup_failed else "complete")
    )
    payload["overall_status"] = "complete" if payload.get("complete") and not lookup_failed else "partial"
    payload["write_back"] = {"performed": False, "reason": "audit_is_read_only; use library.update_metadata approval flow"}
    return ActionResult(payload)


def _navidrome_search(context: ActionContext, _operation: OperationRecord, model: NavidromeSearchInput) -> ActionResult:
    return ActionResult(_require_ok(context.navidrome_client.db_search_evidence(query=model.query, limit=model.limit), "navidrome_search_failed", retryable=True))


def _navidrome_verify(context: ActionContext, _operation: OperationRecord, model: NavidromeVerifyInput) -> ActionResult:
    payload = _require_ok(
        context.navidrome_client.path_evidence(
            file_path=model.file_path,
            query=model.query,
            expected_metadata=model.expected_metadata,
            match_strategy=model.match_strategy,
        ),
        "navidrome_verify_failed",
        retryable=True,
    )
    payload["expected_present"] = model.expected_present
    if model.match_strategy == "unique_metadata" and payload.get("metadata_ambiguous"):
        payload["verified"] = False
        raise ActionFailure("navidrome_metadata_ambiguous", detail=payload, retryable=False)
    payload["verified"] = bool(payload.get("matched")) == model.expected_present
    if not payload["verified"]:
        raise ActionFailure("navidrome_expectation_not_met", detail=payload, retryable=True)
    return ActionResult(payload)


def _navidrome_refresh(context: ActionContext, _operation: OperationRecord, _model: NoInput) -> ActionResult:
    return ActionResult(_require_ok(context.navidrome_client.start_scan(), "navidrome_refresh_failed", retryable=True))


def _download_search(context: ActionContext, _operation: OperationRecord, model: DownloadSearchInput) -> ActionResult:
    payload = _require_ok(context.vmomo_client.search(model.query, timeout=model.timeout), "download_search_failed", retryable=True)
    payload.pop("messages", None)
    payload["provider"] = "vmomo"
    return ActionResult(payload)


def _download_candidate_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in ("index", "title", "artist", "song_id", "msg_id")
    }


def _download_candidate_sha256(candidate: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(_download_candidate_snapshot(candidate)).encode("utf-8")).hexdigest()


def _download_binding_for_fetch(context: ActionContext, model: DownloadFetchInput) -> dict[str, Any] | None:
    if model.operation_profile == "audio_only":
        return None

    search = context.store.get_operation(model.search_operation_id)
    if search is None:
        raise ActionFailure("download_search_operation_not_found")
    if search.action != "download.search":
        raise ActionFailure("download_search_operation_action_mismatch")
    if search.status != "succeeded" or not search.result:
        raise ActionFailure("download_search_operation_not_succeeded", detail={"status": search.status})
    candidates = [
        dict(candidate)
        for candidate in list(search.result.get("candidates") or [])
        if int(candidate.get("index") or 0) == model.candidate_index
    ]
    if len(candidates) != 1:
        raise ActionFailure("download_candidate_identity_not_unique", detail={"candidate_index": model.candidate_index})
    candidate = candidates[0]
    if not candidate.get("title") or not candidate.get("artist") or int(candidate.get("msg_id") or 0) <= 0:
        raise ActionFailure("download_candidate_identity_missing", detail={"candidate": candidate})

    metadata = context.store.get_operation(model.metadata_operation_id)
    if metadata is None:
        raise ActionFailure("metadata_operation_not_found")
    if metadata.action != "metadata.fetch":
        raise ActionFailure("metadata_operation_action_mismatch")
    if metadata.status != "succeeded" or not metadata.result:
        raise ActionFailure("metadata_operation_not_succeeded", detail={"status": metadata.status})
    manifest = dict(metadata.result.get("manifest") or {})
    actual_manifest_sha256 = hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()
    if (
        actual_manifest_sha256 != model.manifest_sha256.lower()
        or str(metadata.result.get("manifest_sha256") or "").lower() != actual_manifest_sha256
    ):
        raise ActionFailure("manifest_sha256_mismatch")
    tracks = [
        dict(track)
        for track in list(manifest.get("tracks") or [])
        if str(track.get("provider_track_id") or "") == model.provider_track_id
    ]
    if len(tracks) != 1:
        raise ActionFailure("manifest_track_identity_not_unique", detail={"provider_track_id": model.provider_track_id})
    identity_match = compare_track_identities(tracks[0], candidate)
    warnings = [] if identity_match["compatible"] else [{
        "code": "identity_comparison_warning",
        "identity_match": identity_match,
    }]
    return {
        "search_operation_id": search.id,
        "search_msg_id": int(candidate["msg_id"]),
        "candidate_index": model.candidate_index,
        "candidate": candidate,
        "candidate_sha256": _download_candidate_sha256(candidate),
        "metadata_operation_id": metadata.id,
        "manifest_sha256": actual_manifest_sha256,
        "provider_track_id": model.provider_track_id,
        "canonical_track": tracks[0],
        "identity_match": identity_match,
        "warnings": warnings,
    }


def _download_fetch(context: ActionContext, operation: OperationRecord, model: DownloadFetchInput) -> ActionResult:
    download_binding = _download_binding_for_fetch(context, model)
    search_msg_id = int((download_binding or {}).get("search_msg_id") or model.search_msg_id or 0)
    output_dir = context.temporary_dir / operation.id
    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + model.timeout
    deadline_at = time.time() + model.timeout

    def report_progress(event: dict[str, Any]) -> None:
        payload = dict(event)
        stage = str(payload.pop("stage", "running"))
        context.progress(
            operation.id,
            stage,
            **payload,
            elapsed_seconds=round(time.monotonic() - started_monotonic, 3),
            remaining_seconds=round(max(0.0, deadline_monotonic - time.monotonic()), 3),
            deadline_at=deadline_at,
            total_budget_seconds=model.timeout,
        )

    context.progress(
        operation.id,
        "quality_selection",
        search_msg_id=search_msg_id,
        candidate_index=model.candidate_index,
        requested_quality=model.vmomo_quality,
        attempts_total=model.retries + 1,
        remaining_seconds=float(model.timeout),
        deadline_at=deadline_at,
        total_budget_seconds=model.timeout,
    )
    requested = context.vmomo_client.request_media(
        search_msg_id,
        model.candidate_index,
        model.vmomo_quality,
        model.keywords,
        timeout=max(1, int(deadline_monotonic - time.monotonic())),
        on_progress=report_progress,
        cancel_requested=lambda: context.store.is_cancel_requested(operation.id),
    )
    if requested.get("success") is False or requested.get("ok") is False:
        code = str(requested.get("error") or "download_candidate_failed")
        if code == "operation_cancelled":
            raise ActionFailure("operation_cancelled", detail=requested)
        deterministic = {
            "candidate_button_not_found",
            "candidate_identity_unavailable",
            "media_identity_mismatch",
            "quality_selection_unavailable",
            "search_message_not_found",
        }
        raise ActionFailure(code, detail=requested, retryable=code not in deterministic)
    media_msg_id = int(requested.get("media_msg_id") or 0)
    if media_msg_id <= 0:
        raise ActionFailure("download_media_id_missing", detail=requested)
    quality_selection = dict(requested.get("quality_selection") or {})
    candidate = dict(requested.get("candidate") or {})
    candidate_identity = dict(requested.get("candidate_identity") or {})
    provider_labels = list(requested.get("provider_labels") or [])
    if download_binding is not None:
        actual_candidate_sha256 = _download_candidate_sha256(candidate)
        if actual_candidate_sha256 != str(download_binding.get("candidate_sha256") or ""):
            raise ActionFailure(
                "download_candidate_changed_since_search",
                detail={
                    "failure_stage": "candidate_binding",
                    "selected_candidate": download_binding.get("candidate") or {},
                    "downloaded_candidate": candidate,
                    "selected_candidate_sha256": download_binding.get("candidate_sha256"),
                    "downloaded_candidate_sha256": actual_candidate_sha256,
                    "search_operation_id": model.search_operation_id,
                },
            )
    if str(quality_selection.get("selected") or "") != model.vmomo_quality:
        raise ActionFailure(
            "quality_selection_unavailable",
            detail={
                "failure_stage": "quality_selection",
                "requested_quality": model.vmomo_quality,
                "selected_quality": quality_selection.get("selected"),
                "quality_selection": quality_selection,
                "candidate": candidate,
                "candidate_identity": candidate_identity,
                "provider_labels": provider_labels,
                "media_msg_id": media_msg_id,
            },
        )
    if model.vmomo_quality in {"HQ", "SQ"} and quality_selection.get("quality_button_clicked") is not True:
        raise ActionFailure(
            "quality_selection_evidence_missing",
            detail={
                "failure_stage": "quality_selection",
                "requested_quality": model.vmomo_quality,
                "selected_quality": quality_selection.get("selected"),
                "media_msg_id": media_msg_id,
                "quality_selection": quality_selection,
                "candidate": candidate,
                "candidate_identity": candidate_identity,
                "provider_labels": provider_labels,
            },
        )
    try:
        context.raise_if_cancel_requested(operation.id)
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise ActionFailure(
                "download_deadline_exceeded",
                detail={
                    "failure_stage": "media_download",
                    "media_msg_id": media_msg_id,
                    "requested_quality": model.vmomo_quality,
                    "selected_quality": quality_selection.get("selected"),
                    "quality_selection": quality_selection,
                    "candidate_identity": candidate_identity,
                    "provider_labels": provider_labels,
                },
                retryable=True,
            )
        payload = context.vmomo_client.stage_download(
            media_msg_id,
            output_dir,
            timeout=remaining,
            retries=model.retries,
            deadline_monotonic=deadline_monotonic,
            on_progress=report_progress,
            cancel_requested=lambda: context.store.is_cancel_requested(operation.id),
        )
        if payload.get("success") is False or payload.get("ok") is False:
            error = str(payload.get("error") or "download_failed")
            if error == "operation_cancelled":
                raise ActionFailure("operation_cancelled", detail=payload)
            detail = {
                **payload,
                "failure_stage": str(payload.get("failure_stage") or "media_download"),
                "requested_quality": model.vmomo_quality,
                "selected_quality": quality_selection.get("selected"),
                "quality_selection": quality_selection,
                "candidate": candidate,
                "candidate_identity": candidate_identity,
                "provider_labels": provider_labels,
                "media_msg_id": media_msg_id,
            }
            code = "download_deadline_exceeded" if error == "download_deadline_exceeded" else "download_fetch_failed"
            raise ActionFailure(code, detail=detail, retryable=True)
        path = Path(str(payload.get("file_path") or "")).resolve()
        root = context.temporary_dir.resolve()
        if root not in path.parents or not path.is_file():
            raise ActionFailure("download_output_invalid", detail={"path": str(path)})
        report_progress({"stage": "hashing", "media_msg_id": media_msg_id})
        facts = file_facts(path)
        report_progress({"stage": "staging", "media_msg_id": media_msg_id, "size": facts.get("size")})
        filename_quality = next(
            (value for value in ("SQ", "HQ") if re.search(rf"(^|[^A-Z0-9]){value}([^A-Z0-9]|$)", path.name.upper())),
            "",
        )
        quality_filename_warning = None
        if model.vmomo_quality in {"HQ", "SQ"} and filename_quality and filename_quality != model.vmomo_quality:
            quality_filename_warning = {
                "code": "filename_quality_untrusted",
                "filename_quality": filename_quality,
                "selected_quality": model.vmomo_quality,
            }
        record = context.store.create_temporary_file(
            kind="downloaded_audio",
            path=str(path),
            sha256=str(facts["sha256"]),
            size=int(facts["size"]),
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            source_operation_id=operation.id,
            metadata={
                "provider": "vmomo",
                "media_msg_id": media_msg_id,
                "search_msg_id": search_msg_id,
                "candidate_index": model.candidate_index,
                "vmomo_quality": model.vmomo_quality,
                "quality_selection": quality_selection,
                "provider_labels": provider_labels,
                "candidate": candidate,
                "candidate_identity": candidate_identity,
                "filename": path.name,
                "quality_filename_warning": quality_filename_warning,
                "operation_profile": model.operation_profile,
                "search_operation_id": model.search_operation_id,
                "metadata_operation_id": model.metadata_operation_id,
                "manifest_sha256": model.manifest_sha256.lower(),
                "provider_track_id": model.provider_track_id,
                "candidate_sha256": (download_binding or {}).get("candidate_sha256"),
                "identity_match": (download_binding or {}).get("identity_match"),
                "warnings": (download_binding or {}).get("warnings") or [],
            },
        )
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    return ActionResult({
        "ok": True,
        "provider": "vmomo",
        "candidate": candidate,
        "candidate_identity": candidate_identity,
        "provider_labels": provider_labels,
        "quality_selection": quality_selection,
        "transfer": {
            "attempts": payload.get("attempts"),
            "attempts_total": model.retries + 1,
            "duration_seconds": payload.get("duration_seconds"),
            "errors": payload.get("errors") or [],
            "total_budget_seconds": model.timeout,
        },
        "quality_filename_warning": quality_filename_warning,
        "operation_profile": model.operation_profile,
        "search_msg_id": search_msg_id,
        "candidate_index": model.candidate_index,
        "search_operation_id": model.search_operation_id,
        "metadata_operation_id": model.metadata_operation_id,
        "manifest_sha256": model.manifest_sha256.lower(),
        "provider_track_id": model.provider_track_id,
        "candidate_sha256": (download_binding or {}).get("candidate_sha256"),
        "identity_match": (download_binding or {}).get("identity_match"),
        "warnings": (download_binding or {}).get("warnings") or [],
        "temporary_file": record.to_dict(),
    })


def _metadata_search(context: ActionContext, _operation: OperationRecord, model: MetadataSearchInput) -> ActionResult:
    payload = context.metadata_provider.search(**model.model_dump())
    return ActionResult(_require_ok(payload, "metadata_search_failed", retryable=True))


def _metadata_fetch(context: ActionContext, _operation: OperationRecord, model: MetadataFetchInput) -> ActionResult:
    payload = context.metadata_provider.fetch(**model.model_dump(exclude_defaults=True))
    return ActionResult(_require_ok(payload, "metadata_fetch_failed", retryable=True))


def _lyrics_search(context: ActionContext, _operation: OperationRecord, model: LyricsSearchInput) -> ActionResult:
    payload = context.lyrics_provider.search(**model.model_dump())
    if payload.get("ok") is False and not payload.get("warnings"):
        raise ActionFailure("lyrics_search_failed", detail=payload, retryable=True)
    return ActionResult(payload)


def _lyrics_fetch(context: ActionContext, _operation: OperationRecord, model: LyricsFetchInput) -> ActionResult:
    return ActionResult(_require_ok(context.lyrics_provider.fetch(**model.model_dump()), "lyrics_fetch_failed", retryable=True))


def _lyrics_resolve_masks(
    context: ActionContext,
    _operation: OperationRecord,
    model: ResolveMaskedLyricsInput,
) -> ActionResult:
    source = context.store.get_operation(model.canonical_source_operation_id)
    if source is None:
        raise ActionFailure("canonical_lyrics_operation_not_found")
    if source.action != "lyrics.fetch":
        raise ActionFailure("canonical_lyrics_operation_action_mismatch")
    if source.status != "succeeded" or not source.result:
        raise ActionFailure("canonical_lyrics_operation_not_succeeded")
    canonical_lrc = str(source.result.get("canonical_lrc") or "").strip()
    actual_sha256 = hashlib.sha256(canonical_lrc.encode("utf-8")).hexdigest()
    if (
        not canonical_lrc
        or actual_sha256 != model.canonical_lrc_sha256.lower()
        or str(source.result.get("canonical_lrc_sha256") or "").lower() != actual_sha256
    ):
        raise ActionFailure("canonical_lrc_sha256_mismatch")

    provider = str(source.result.get("provider") or source.input.get("provider") or "")
    provider_id = str(source.result.get("provider_id") or source.input.get("provider_id") or "")
    identity = model.music_identity.model_dump(mode="json")
    if (
        identity.get("source_kind") != "provider"
        or identity.get("source_ref") != f"{provider}:{provider_id}"
    ):
        raise ActionFailure("canonical_music_identity_source_mismatch")
    identity_operation = context.store.get_operation(model.identity_evidence_operation_id)
    if identity_operation is None:
        raise ActionFailure("lyrics_identity_operation_not_found")
    if identity_operation.action != "lyrics.search":
        raise ActionFailure("lyrics_identity_operation_action_mismatch")
    if identity_operation.status != "succeeded" or not identity_operation.result:
        raise ActionFailure("lyrics_identity_operation_not_succeeded")
    candidates = list(identity_operation.result.get("candidates") or [])
    matching_candidates = [
        candidate
        for candidate in candidates
        if str(candidate.get("provider") or "") == provider
        and str(candidate.get("provider_id") or "") == provider_id
    ]
    if len(matching_candidates) != 1:
        raise ActionFailure("lyrics_identity_evidence_mismatch")

    identity_warnings = [] if _identity_compatible(identity, matching_candidates[0]) else [{"code": "lyrics_identity_warning", "identity_match": compare_track_identities(identity, matching_candidates[0])}]

    for resolution in model.resolutions:
        if resolution.method != "agent_resolved_with_stt_hint":
            continue
        stt_operation = context.store.get_operation(resolution.stt_operation_id)
        if stt_operation is None:
            raise ActionFailure("stt_operation_not_found")
        if stt_operation.action != "audio.transcribe":
            raise ActionFailure("stt_operation_action_mismatch")
        if stt_operation.status != "succeeded":
            raise ActionFailure("stt_operation_not_succeeded")
        if stt_operation.input.get("purpose") != "masked_lyrics_stt":
            raise ActionFailure("stt_purpose_mismatch")
        if (
            stt_operation.input.get("canonical_source_operation_id") != source.id
            or str(stt_operation.input.get("canonical_lrc_sha256") or "").lower() != actual_sha256
            or stt_operation.input.get("music_identity") != identity
        ):
            raise ActionFailure("stt_lyrics_provenance_mismatch")
    try:
        payload = resolve_masked_lyrics(
            canonical_lrc=canonical_lrc,
            canonical_lrc_sha256=model.canonical_lrc_sha256,
            resolutions=[resolution.model_dump(mode="json") for resolution in model.resolutions],
        )
    except ValueError as exc:
        raise ActionFailure(str(exc)) from exc
    payload.update({
        "canonical_source_operation_id": source.id,
        "canonical_provider": provider,
        "canonical_provider_id": provider_id,
        "warnings": identity_warnings,
        "identity_evidence_operation_id": identity_operation.id,
        "music_identity": identity,
    })
    return ActionResult(payload)


def _audio_transcribe(context: ActionContext, operation: OperationRecord, model: AudioTranscribeInput) -> ActionResult:
    audio_path, source = _resolve_audio_source(context, model)
    payload = context.remote_stt_runner(
        audio_path,
        {"request_id": operation.request_id, "source": source, **model.model_dump()},
    )
    return ActionResult(_require_ok(payload, "audio_transcribe_failed", retryable=True))


def _prepare_staged_import(
    context: ActionContext,
    operation: OperationRecord,
    model: StagedImportPrepareInput,
) -> ActionResult:
    from musicops.normalized_import import prepare_staged_import

    return ActionResult(prepare_staged_import(context, operation, model))


def _temporary_inspect(context: ActionContext, _operation: OperationRecord, model: TemporaryInput) -> ActionResult:
    record = context.store.get_temporary_file(model.temporary_file_id)
    if record is None:
        raise ActionFailure("temporary_file_not_found")
    path = Path(record.path)
    payload = record.to_dict()
    payload["exists"] = path.is_file()
    if path.is_file():
        payload["current_sha256"] = file_facts(path)["sha256"]
    return ActionResult({"ok": True, "temporary_file": payload})


def _temporary_delete(context: ActionContext, _operation: OperationRecord, model: TemporaryInput) -> ActionResult:
    record = context.store.get_temporary_file(model.temporary_file_id)
    if record is None:
        raise ActionFailure("temporary_file_not_found")
    path = Path(record.path).resolve()
    if context.temporary_dir.resolve() not in path.parents:
        raise ActionFailure("temporary_path_outside_root")
    path.unlink(missing_ok=True)
    updated = context.store.mark_temporary_deleted(record.id)
    return ActionResult({"ok": not path.exists(), "temporary_file": updated.to_dict()})


def _staged_preview_item(context: ActionContext, model: StagedImportInput) -> dict[str, Any]:
    from musicops.normalized_import import preview_staged_import

    return preview_staged_import(context, model)


def _approval_for(context: ActionContext, operation: OperationRecord) -> dict[str, Any]:
    if operation.mutation_plan is not None:
        return operation.mutation_plan
    if not operation.approval_id:
        raise ActionFailure("approval_missing")
    approval = context.store.get_approval(operation.approval_id)
    if approval is None:
        raise ActionFailure("approval_missing")
    return approval.preview


def _preview_item(preview: dict[str, Any], index: int) -> dict[str, Any]:
    items = preview.get("items") or []
    if index >= len(items):
        raise ActionFailure("approval_preview_invalid")
    return items[index]


def _snapshot_matches(context: ActionContext, expected: dict[str, Any], file_path: str) -> bool:
    current = context.library_inspector.file_snapshot(file_path)
    return bool(current.get("exists")) == bool(expected.get("exists")) and str(current.get("sha256") or "") == str(expected.get("sha256") or "")


def _finish_batch(context: ActionContext, results: list[dict[str, Any]]) -> ActionResult:
    succeeded = sum(bool(row.get("ok")) for row in results)
    navidrome: dict[str, Any] | None = None
    if succeeded and context.navidrome_client.configured():
        try:
            navidrome = context.navidrome_client.start_scan()
        except Exception as exc:
            navidrome = {"ok": False, "detail": f"{exc.__class__.__name__}:{exc}"}
    return ActionResult({
        "ok": succeeded == len(results),
        "overall_status": "succeeded" if succeeded == len(results) else "partial_success" if succeeded else "failed",
        "item_count": len(results),
        "succeeded_count": succeeded,
        "failed_count": len(results) - succeeded,
        "results": results,
        "navidrome_refresh": navidrome,
    })


def _update_metadata(context: ActionContext, operation: OperationRecord, model: UpdateMetadataInput) -> ActionResult:
    preview = _approval_for(context, operation)
    results = []
    for index, patch in enumerate(model.patches):
        expected = _preview_item(preview, index)["before"]
        if not _snapshot_matches(context, expected, patch.file_path):
            results.append({"ok": False, "index": index, "file_path": patch.file_path, "error": "stale_approval"})
            continue
        result = context.library_inspector.metadata_patch(patches=[patch.model_dump()], apply=True)
        row = (result.get("results") or [result])[0]
        results.append({"index": index, **row})
    return _finish_batch(context, results)


def _update_lyrics(context: ActionContext, operation: OperationRecord, model: UpdateLyricsInput) -> ActionResult:
    from musicops.normalized_import import lyrics_for_existing_update

    preview = _approval_for(context, operation)
    results = []
    for index, item in enumerate(model.items):
        expected = _preview_item(preview, index)["before"]
        if not _snapshot_matches(context, expected, item.file_path):
            results.append({"ok": False, "index": index, "file_path": item.file_path, "error": "stale_approval"})
            continue
        lyrics, lyrics_sha256 = lyrics_for_existing_update(context, item)
        expected_sha256 = str((_preview_item(preview, index).get("target") or {}).get("lyrics_sha256") or "")
        if not expected_sha256 or lyrics_sha256 != expected_sha256:
            results.append({"ok": False, "index": index, "file_path": item.file_path, "error": "stale_lyrics_payload"})
            continue
        row = context.library_inspector.lyrics_write(
            file_path=item.file_path,
            lyrics=lyrics,
            lyrics_sha256=lyrics_sha256,
            reason="approved_musicops_operation",
            apply=True,
        )
        if MASK_PATTERN.search(lyrics):
            row.setdefault("warnings", []).append({"code": "lyrics_masks_preserved"})
        results.append({"index": index, **row})
    return _finish_batch(context, results)


def _update_cover(context: ActionContext, operation: OperationRecord, model: UpdateCoverInput) -> ActionResult:
    preview = _approval_for(context, operation)
    results = []
    for index, item in enumerate(model.items):
        expected = _preview_item(preview, index)["before"]
        if not _snapshot_matches(context, expected, item.file_path):
            results.append({"ok": False, "index": index, "file_path": item.file_path, "error": "stale_approval"})
            continue
        try:
            cover_data = base64.b64decode(item.cover_base64, validate=True)
        except Exception:
            results.append({"ok": False, "index": index, "file_path": item.file_path, "error": "invalid_cover_base64"})
            continue
        row = context.library_inspector.cover_write(
            file_path=item.file_path,
            cover_data=cover_data,
            media_type=item.media_type,
            cover_sha256=item.cover_sha256,
        )
        results.append({"index": index, **row})
    return _finish_batch(context, results)


def _move(context: ActionContext, operation: OperationRecord, model: MoveInput) -> ActionResult:
    preview = _approval_for(context, operation)
    results = []
    for index, item in enumerate(model.items):
        row_preview = _preview_item(preview, index)
        if not _snapshot_matches(context, row_preview["before"], item.file_path):
            results.append({"ok": False, "index": index, "file_path": item.file_path, "error": "stale_approval"})
            continue
        target = context.library_inspector.file_snapshot(item.target_file_path)
        if bool(target.get("exists")) != bool((row_preview.get("target_before") or {}).get("exists")):
            results.append({"ok": False, "index": index, "file_path": item.file_path, "error": "stale_target"})
            continue
        results.append({"index": index, **context.library_inspector.move_file(**item.model_dump())})
    return _finish_batch(context, results)


def _delete(context: ActionContext, operation: OperationRecord, model: DeleteInput) -> ActionResult:
    preview = _approval_for(context, operation)
    results = []
    for index, item in enumerate(model.items):
        expected = _preview_item(preview, index)["before"]
        if not _snapshot_matches(context, expected, item.file_path):
            results.append({"ok": False, "index": index, "file_path": item.file_path, "error": "stale_approval"})
            continue
        results.append({"index": index, **context.library_inspector.delete_file(file_path=item.file_path)})
    return _finish_batch(context, results)


def _add(context: ActionContext, operation: OperationRecord, model: AddInput) -> ActionResult:
    preview = _approval_for(context, operation)
    results = []
    for index, item in enumerate(model.items):
        row_preview = _preview_item(preview, index)
        record = context.store.get_temporary_file(item.temporary_file_id)
        target = context.library_inspector.file_snapshot(item.target_file_path)
        if (
            record is None
            or record.deleted_at is not None
            or record.expires_at <= time.time()
            or record.sha256 != str((row_preview.get("source") or {}).get("sha256") or "")
        ):
            results.append({"ok": False, "index": index, "target_file_path": item.target_file_path, "error": "stale_temporary_source"})
            continue
        if bool(target.get("exists")) != bool((row_preview.get("target_before") or {}).get("exists")):
            results.append({"ok": False, "index": index, "target_file_path": item.target_file_path, "error": "stale_target"})
            continue
        result = context.library_inspector.add_file(source_path=Path(record.path), target_file_path=item.target_file_path, expected_sha256=record.sha256)
        if result.get("ok"):
            Path(record.path).unlink(missing_ok=True)
            context.store.mark_temporary_deleted(record.id)
        results.append({"index": index, **result})
    return _finish_batch(context, results)


def _staged_import(context: ActionContext, operation: OperationRecord, model: StagedImportInput) -> ActionResult:
    from musicops.normalized_import import publish_staged_import

    return ActionResult(publish_staged_import(context, operation, model))


def build_mutation_preview(context: ActionContext, model: BaseModel, *, action: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if action == "library.update_metadata":
        for item in getattr(model, "patches"):
            before = context.library_inspector.file_snapshot(item.file_path)
            rows.append({"file_path": item.file_path, "before": before, "target": {"metadata": item.fields}})
    elif action == "library.update_lyrics":
        from musicops.normalized_import import lyrics_for_existing_update

        for item in getattr(model, "items"):
            lyrics, lyrics_sha256 = lyrics_for_existing_update(context, item)
            before = context.library_inspector.file_snapshot(item.file_path)
            current = context.library_inspector.evidence_for_file(item.file_path, include={"lyrics_text"})
            current_text = str((current.get("lyrics") or {}).get("text") or "")
            if current_text:
                before.setdefault("lyrics", {})["text"] = current_text
            rows.append({
                "file_path": item.file_path,
                "before": before,
                "warnings": [{"code": "lyrics_masks_preserved"}] if MASK_PATTERN.search(lyrics) else [],
                "target": {
                    "lyrics_sha256": lyrics_sha256,
                    "length": len(lyrics),
                    "line_count": len(lyrics.splitlines()),
                    "lyrics_text": lyrics,
                },
            })
    elif action == "library.update_cover":
        for item in getattr(model, "items"):
            before = context.library_inspector.file_snapshot(item.file_path)
            rows.append({
                "file_path": item.file_path,
                "before": before,
                "target": {
                    "cover_sha256": item.cover_sha256.lower(),
                    "media_type": item.media_type,
                    "data_uri": f"data:{item.media_type};base64,{item.cover_base64}",
                },
            })
    elif action == "library.move":
        for item in getattr(model, "items"):
            rows.append({
                "file_path": item.file_path,
                "before": context.library_inspector.file_snapshot(item.file_path),
                "target_file_path": item.target_file_path,
                "target_before": context.library_inspector.file_snapshot(item.target_file_path),
            })
    elif action == "library.delete":
        for item in getattr(model, "items"):
            rows.append({"file_path": item.file_path, "before": context.library_inspector.file_snapshot(item.file_path), "permanent": True})
    elif action == "library.import":
        rows.append(_staged_preview_item(context, model))
    elif action == "library.add":
        for item in getattr(model, "items"):
            temporary = context.store.get_temporary_file(item.temporary_file_id)
            if temporary is None:
                raise ActionFailure("temporary_file_not_found", detail={"temporary_file_id": item.temporary_file_id})
            if temporary.deleted_at is not None or temporary.expires_at <= time.time():
                raise ActionFailure("temporary_file_unavailable", detail={"temporary_file_id": item.temporary_file_id})
            path = Path(temporary.path).resolve()
            if context.temporary_dir.resolve() not in path.parents or not path.is_file():
                raise ActionFailure("temporary_source_invalid", detail={"temporary_file_id": item.temporary_file_id})
            current = file_facts(path)
            if str(current.get("sha256") or "") != temporary.sha256:
                raise ActionFailure("temporary_source_stale", detail={"temporary_file_id": item.temporary_file_id})
            rows.append({
                "temporary_file_id": item.temporary_file_id,
                "import_profile": "audio_only",
                "source": temporary.to_dict(),
                "target_file_path": item.target_file_path,
                "target_before": context.library_inspector.file_snapshot(item.target_file_path),
            })
    else:
        raise ActionFailure("preview_not_supported")
    batch_sha256 = hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()
    return {
        "action": action,
        "authoritative_source": "musicops_server_current_state",
        "item_count": len(rows),
        "fixed_batch_sha256": batch_sha256,
        "recommended_display": "html" if len(rows) > 1 or action in {"library.import", "library.update_lyrics", "library.update_cover"} else "chat",
        "permanent_delete": action == "library.delete",
        "items": rows,
    }


def build_registry() -> dict[str, Action]:
    actions = [
        Action("library.search", LibrarySearchInput, _library_search),
        Action("library.read", LibraryReadInput, _library_read, resources=lambda model: _library_resource(model, "read")),
        Action("library.album_diagnose", AlbumDiagnoseInput, _album_diagnose),
        Action("library.album_metadata_audit", AlbumMetadataAuditInput, _audit_album_metadata),
        Action("navidrome.search", NavidromeSearchInput, _navidrome_search, availability=_navidrome_available),
        Action("navidrome.verify", NavidromeVerifyInput, _navidrome_verify, availability=_navidrome_available),
        Action("navidrome.refresh", NoInput, _navidrome_refresh, risk="service_refresh", availability=_navidrome_available),
        Action("download.search", DownloadSearchInput, _download_search, risk="external_service", availability=_vmomo_available),
        Action("download.fetch", DownloadFetchInput, _download_fetch, risk="temporary_download", availability=_vmomo_available, lease_recovery_safe=False, max_attempts=1),
        Action("metadata.search", MetadataSearchInput, _metadata_search, risk="external_service", availability=_metadata_available),
        Action("metadata.fetch", MetadataFetchInput, _metadata_fetch, risk="external_service", availability=_metadata_available),
        Action("lyrics.search", LyricsSearchInput, _lyrics_search, risk="external_service"),
        Action("lyrics.fetch", LyricsFetchInput, _lyrics_fetch, risk="external_service"),
        Action(
            "lyrics.resolve_masks",
            ResolveMaskedLyricsInput,
            _lyrics_resolve_masks,
            operation_profile="masked_lyrics_resolution",
        ),
        Action(
            "audio.transcribe",
            AudioTranscribeInput,
            _audio_transcribe,
            risk="external_service",
            confirmation_policy="explicit_intent_sufficient",
            data_handling={
                "boundary": "configured_external_processor",
                "payload_classes": ["bounded_audio_clip"],
                "purposes": ["content_rating_stt", "masked_lyrics_stt"],
            },
            resources=_audio_resource,
        ),
        Action("temporary.inspect", TemporaryInput, _temporary_inspect),
        Action("temporary.delete", TemporaryInput, _temporary_delete, risk="temporary_delete", lease_recovery_safe=False, max_attempts=1),
        Action(
            "library.import.prepare",
            StagedImportPrepareInput,
            _prepare_staged_import,
            risk="temporary_mutation",
            operation_profile="normalized_staged",
            lease_recovery_safe=False,
            max_attempts=1,
            resources=_prepare_import_resources,
        ),
        Action(
            "library.import",
            StagedImportInput,
            _staged_import,
            risk="library_mutation",
            requires_approval=True,
            operation_profile="normalized_staged",
            lease_recovery_safe=False,
            max_attempts=1,
            resources=_staged_import_resources,
        ),
        Action("library.update_metadata", UpdateMetadataInput, _update_metadata, risk="library_mutation", requires_approval=True, lease_recovery_safe=False, max_attempts=1, resources=lambda model: _library_resource(model, "write")),
        Action("library.update_lyrics", UpdateLyricsInput, _update_lyrics, risk="library_mutation", requires_approval=True, lease_recovery_safe=False, max_attempts=1, resources=lambda model: _library_resource(model, "write")),
        Action("library.update_cover", UpdateCoverInput, _update_cover, risk="library_mutation", requires_approval=True, lease_recovery_safe=False, max_attempts=1, resources=lambda model: _library_resource(model, "write")),
        Action("library.move", MoveInput, _move, risk="library_mutation", requires_approval=True, lease_recovery_safe=False, max_attempts=1, resources=lambda model: _library_resource(model, "write")),
        Action("library.delete", DeleteInput, _delete, risk="permanent_delete", requires_approval=True, lease_recovery_safe=False, max_attempts=1, resources=lambda model: _library_resource(model, "write")),
        Action(
            "library.add",
            AddInput,
            _add,
            risk="library_mutation",
            requires_approval=True,
            operation_profile="audio_only",
            lease_recovery_safe=False,
            max_attempts=1,
            resources=_add_resources,
        ),
    ]
    for action in actions:
        if action.requires_approval:
            action.preview = lambda context, model, action_name=action.name: build_mutation_preview(context, model, action=action_name)
    return {action.name: action for action in actions}


def registry_digest(registry: dict[str, Action], _context: ActionContext | None = None) -> str:
    rows = [registry[name].contract_descriptor() for name in sorted(registry)]
    return hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()
