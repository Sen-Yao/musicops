from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
import unicodedata

try:
    from mutagen import File as MutagenFile
except Exception:  # pragma: no cover - import failure is surfaced in evidence payload
    MutagenFile = None  # type: ignore[assignment]

try:
    from mutagen.flac import FLAC, Picture
except Exception:  # pragma: no cover - import failure is surfaced in patch payload
    FLAC = Picture = None  # type: ignore[assignment]

try:
    from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, USLT
except Exception:  # pragma: no cover - import failure is surfaced in patch payload
    APIC = TALB = TDRC = TIT2 = TPE1 = TPE2 = TPOS = TRCK = USLT = None  # type: ignore[assignment]

try:
    from mutagen.mp4 import MP4, MP4Cover
except Exception:  # pragma: no cover - import failure is surfaced in patch payload
    MP4 = MP4Cover = None  # type: ignore[assignment]


ALLOWED_METADATA_PATCH_FIELDS = {
    "title",
    "artist",
    "album",
    "album_artist",
    "date",
    "track_number",
    "track_total",
    "disc_number",
    "disc_total",
}


_TAG_ALIASES_BY_FORMAT = {
    "flac": {
        "title": ["title"],
        "artist": ["artist"],
        "album": ["album"],
        "album_artist": ["albumartist", "album_artist"],
        "date": ["date", "year"],
        "track_number": ["tracknumber", "track"],
        "track_total": ["tracktotal", "totaltracks"],
        "disc_number": ["discnumber", "disc"],
        "disc_total": ["disctotal", "totaldiscs"],
    },
    "mp3": {
        "title": ["TIT2", "title"],
        "artist": ["TPE1", "artist"],
        "album": ["TALB", "album"],
        "album_artist": ["TPE2", "albumartist", "album_artist"],
        "date": ["TDRC", "TYER", "date", "year"],
        "track_number": ["TRCK", "tracknumber", "track"],
        "disc_number": ["TPOS", "discnumber", "disc"],
    },
    "m4a": {
        "title": ["\xa9nam", "title"],
        "artist": ["\xa9ART", "artist"],
        "album": ["\xa9alb", "album"],
        "album_artist": ["aART", "albumartist", "album_artist"],
        "date": ["\xa9day", "date", "year"],
        "track_number": ["trkn", "tracknumber", "track"],
        "disc_number": ["disk", "discnumber", "disc"],
    },
}

_TAG_ALIASES_BY_FORMAT["mp4"] = _TAG_ALIASES_BY_FORMAT["m4a"]
_TAG_ALIASES_BY_FORMAT["aac"] = _TAG_ALIASES_BY_FORMAT["m4a"]
_TAG_ALIASES_BY_FORMAT["ogg"] = _TAG_ALIASES_BY_FORMAT["flac"]
_TAG_ALIASES_BY_FORMAT["opus"] = _TAG_ALIASES_BY_FORMAT["flac"]

_TAG_ALIASES_FALLBACK = {
    "title": ["title", "TIT2", "\xa9nam"],
    "artist": ["artist", "TPE1", "\xa9ART"],
    "album": ["album", "TALB", "\xa9alb"],
    "album_artist": ["albumartist", "album_artist", "TPE2", "aART"],
    "date": ["date", "year", "TDRC", "TYER", "\xa9day"],
    "track_number": ["tracknumber", "track", "TRCK", "trkn"],
    "track_total": ["tracktotal", "totaltracks"],
    "disc_number": ["discnumber", "disc", "TPOS", "disk"],
    "disc_total": ["disctotal", "totaldiscs"],
}

_LYRICS_KEYS = ("lyrics", "LYRICS", "unsyncedlyrics", "\xa9lyr")
_CENSORED_MARKER_RE = re.compile(r"(?<!\*)\*{2,}(?!\*)")
_TIMESTAMP_RE = re.compile(r"^\s*(\[[0-9:.]+\])")
_AUDIO_SUFFIXES = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}


class LibraryInspector:
    """Path-guarded music library evidence and restricted tag operations."""

    def __init__(self, library_root: str | Path = "/music"):
        self.library_root = Path(library_root).expanduser().resolve()

    def _resolve(self, file_path: str) -> Path:
        if not str(file_path or "").strip():
            raise ValueError("invalid_path")
        requested = Path(file_path)
        if requested.is_absolute() or ".." in requested.parts:
            raise ValueError("invalid_path")
        resolved = (self.library_root / requested).resolve()
        if resolved != self.library_root and self.library_root not in resolved.parents:
            raise ValueError("invalid_path")
        return resolved

    def resolve_path(self, file_path: str) -> dict[str, Any]:
        try:
            resolved = self._resolve(file_path)
        except ValueError:
            return {
                "ok": False,
                "error_type": "invalid_path",
                "detail": "file_path must be relative and stay under library_root",
            }
        return {
            "ok": True,
            "file_path": file_path,
            "resolved_path": str(resolved),
            "exists": resolved.exists(),
            "is_file": resolved.is_file(),
            "suffix": resolved.suffix.lower().lstrip("."),
        }

    def search_files(
        self, query: str, limit: int = 20, max_scanned: int = 5000, *,
        include: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        bounded_limit = max(1, min(int(limit), 100))
        bounded_max_scanned = max(1, min(int(max_scanned), 50000))
        if not query:
            return {
                "ok": False,
                "error_type": "empty_query",
                "query": query,
                "limit": bounded_limit,
                "max_scanned": bounded_max_scanned,
                "scanned_count": 0,
                "result_count": 0,
                "partial": False,
                "warnings": [],
                "results": [],
            }
        query_norm = " ".join(unicodedata.normalize("NFKC", query).casefold().split())
        requested = set(include or ())
        results: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        scanned_count = 0
        partial = False
        if not self.library_root.exists():
            return {
                "ok": True,
                "query": query,
                "limit": bounded_limit,
                "max_scanned": bounded_max_scanned,
                "scanned_count": 0,
                "result_count": 0,
                "partial": False,
                "warnings": [],
                "results": [],
            }
        for path in self.library_root.rglob("*"):
            if len(results) >= bounded_limit:
                break
            if not path.is_file():
                continue
            suffix = path.suffix.lower().lstrip(".")
            if suffix not in {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}:
                continue
            scanned_count += 1
            if scanned_count > bounded_max_scanned:
                scanned_count = bounded_max_scanned
                partial = True
                warnings.append({
                    "code": "scan_budget_exhausted",
                    "detail": f"stopped after scanning {bounded_max_scanned} audio files",
                })
                break
            rel = path.relative_to(self.library_root).as_posix()
            evidence = self.evidence_for_file(rel, include={"metadata", "audio", *requested})
            metadata = evidence.get("metadata") or {}
            searchable = [rel, *[str(value) for value in metadata.values() if value not in (None, "")]]
            normalized_searchable = [" ".join(unicodedata.normalize("NFKC", value).casefold().split()) for value in searchable]
            if not any(query_norm in value for value in normalized_searchable):
                continue
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = None
                warnings.append({"code": "file_stat_failed", "detail": rel})
            result = {
                "file_path": rel,
                "suffix": suffix,
                "size_bytes": size_bytes,
                "metadata": metadata,
                "duration": evidence.get("duration"),
                "warnings": evidence.get("warnings") or [],
            }
            if "lyrics_summary" in requested:
                result["lyrics"] = evidence.get("lyrics") or {"present": False, "length": 0, "line_count": 0}
            results.append(result)
        return {
            "ok": True,
            "query": query,
            "limit": bounded_limit,
            "max_scanned": bounded_max_scanned,
            "scanned_count": scanned_count,
            "result_count": len(results),
            "partial": partial,
            "warnings": warnings,
            "results": results,
        }

    def scan_missing_album(
        self,
        *,
        max_scanned: int = 50000,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        """Inspect audio files for a missing/blank ALBUM tag.

        This is deliberately a read-only inventory operation.  It reports the
        files that were actually inspected separately from files skipped by a
        scan budget, so callers cannot mistake a bounded scan for a full
        library conclusion.
        """
        bounded_max_scanned = max(1, min(int(max_scanned), 100000))
        entries: list[dict[str, Any]] = []
        missing_items: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        scanned_count = 0
        total_audio_files = 0
        interrupted = False

        if not self.library_root.exists():
            return {
                "ok": True,
                "scan_status": "complete",
                "complete": True,
                "max_scanned": bounded_max_scanned,
                "total_audio_files": 0,
                "scan_total": 0,
                "scanned_count": 0,
                "inspected_count": 0,
                "missing_album_count": 0,
                "missing_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "failed_or_skipped_count": 0,
                "entries": [],
                "missing_items": [],
                "failures": [],
                "skipped": [],
                "warnings": [],
            }

        traversal_errors: list[dict[str, Any]] = []

        def _record_traversal_error(exc: OSError) -> None:
            traversal_errors.append({"file_path": str(getattr(exc, "filename", "") or self.library_root), "reason": "directory_scan_failed", "detail": str(exc)})

        paths: list[Path] = []
        for root, directories, filenames in os.walk(self.library_root, onerror=_record_traversal_error):
            directories.sort()
            filenames.sort()
            paths.extend(Path(root) / name for name in filenames)

        if traversal_errors:
            failures.extend(traversal_errors)
            warnings.append({"code": "directory_scan_failed", "detail": f"{len(traversal_errors)} directory traversal error(s)"})

        for path in paths:
            if cancel_check is not None and cancel_check():
                interrupted = True
            if not path.is_file() or path.suffix.lower().lstrip(".") not in _AUDIO_SUFFIXES:
                continue
            total_audio_files += 1
            rel = path.relative_to(self.library_root).as_posix()
            if interrupted or scanned_count >= bounded_max_scanned:
                reason = "operation_cancelled" if interrupted else "scan_budget_exhausted"
                skipped.append({"file_path": rel, "reason": reason})
                continue

            scanned_count += 1
            try:
                evidence = self.evidence_for_file(rel, include={"metadata", "audio"})
            except Exception as exc:
                evidence = {
                    "ok": False,
                    "exists": True,
                    "metadata": {},
                    "warnings": [],
                    "error_type": exc.__class__.__name__,
                    "detail": str(exc),
                }
            metadata = dict(evidence.get("metadata") or {})
            row: dict[str, Any] = {
                "file_path": rel,
                "metadata": metadata,
                "format": evidence.get("format") or path.suffix.lower().lstrip("."),
                "duration": evidence.get("duration"),
                "warnings": list(evidence.get("warnings") or []),
            }
            if not evidence.get("ok") or not evidence.get("exists"):
                reason = str(evidence.get("error_type") or ("file_missing" if not evidence.get("exists") else "metadata_read_failed"))
                row.update({"status": "failed", "reason": reason, "error": evidence.get("detail")})
                failures.append({"file_path": rel, "reason": reason, "detail": evidence.get("detail")})
            else:
                album_value = metadata.get("album")
                missing_album = album_value is None or not str(album_value).strip()
                row.update({"status": "missing_album" if missing_album else "ok", "missing_album": missing_album})
                if missing_album:
                    missing_items.append(row)
            entries.append(row)

        if skipped:
            code = "operation_cancelled" if interrupted else "scan_budget_exhausted"
            warnings.append({"code": code, "detail": f"{len(skipped)} audio file(s) were not inspected"})
        scan_status = "partial" if failures or skipped or interrupted else "complete"
        return {
            "ok": True,
            "scan_status": scan_status,
            "complete": scan_status == "complete",
            "max_scanned": bounded_max_scanned,
            "total_audio_files": total_audio_files,
            "scan_total": total_audio_files,
            "scanned_count": scanned_count,
            "inspected_count": scanned_count,
            "missing_album_count": len(missing_items),
            "missing_count": len(missing_items),
            "failed_count": len(failures),
            "skipped_count": len(skipped),
            "failed_or_skipped_count": len(failures) + len(skipped),
            "entries": entries,
            "missing_items": missing_items,
            "failures": failures,
            "skipped": skipped,
            "warnings": warnings,
        }

    def diagnose_album(
        self,
        *,
        album: str,
        artist: str = "",
        canonical_tracks: list[dict[str, Any]] | None = None,
        candidate_paths: list[str] | None = None,
        max_scanned: int = 50000,
    ) -> dict[str, Any]:
        album_norm = (album or "").strip().casefold()
        artist_norm = (artist or "").strip().casefold()
        if not album_norm:
            return {"ok": False, "error_type": "empty_album"}
        bounded_max_scanned = max(1, min(int(max_scanned), 100000))
        local_tracks: list[dict[str, Any]] = []
        candidate_evaluations: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        scanned_count = 0
        partial = False
        if self.library_root.exists():
            path_iterable: list[Path] | Any
            if candidate_paths:
                path_iterable = []
                for candidate_path in candidate_paths:
                    try:
                        path_iterable.append(self._resolve(candidate_path))
                    except ValueError as exc:
                        warnings.append({"code": "candidate_path_invalid", "detail": f"{candidate_path}:{exc}"})
                        candidate_evaluations.append({
                            "file_path": candidate_path,
                            "classification": "unresolved_candidate",
                            "reason": "invalid_path",
                        })
            else:
                path_iterable = self.library_root.rglob("*")
            for path in path_iterable:
                if not path.is_file():
                    if candidate_paths:
                        rel = _relative_or_name(path, self.library_root)
                        candidate_evaluations.append({
                            "file_path": rel,
                            "classification": "unresolved_candidate",
                            "reason": "missing_or_not_file",
                        })
                    continue
                suffix = path.suffix.lower().lstrip(".")
                if suffix not in {"mp3", "flac", "m4a", "aac", "ogg", "opus"}:
                    if candidate_paths:
                        rel = _relative_or_name(path, self.library_root)
                        candidate_evaluations.append({
                            "file_path": rel,
                            "classification": "unresolved_candidate",
                            "reason": "unsupported_audio_format",
                        })
                    continue
                scanned_count += 1
                if scanned_count > bounded_max_scanned:
                    scanned_count = bounded_max_scanned
                    partial = True
                    warnings.append({"code": "scan_budget_exhausted", "detail": f"stopped after scanning {bounded_max_scanned} audio files"})
                    break
                rel = path.relative_to(self.library_root).as_posix()
                evidence = self.evidence_for_file(rel, include={"metadata", "audio"})
                metadata = evidence.get("metadata") or {}
                album_matches = (metadata.get("album") or "").strip().casefold() == album_norm
                artist_matches = True
                if artist_norm:
                    artist_blob = " ".join(str(metadata.get(key) or "") for key in ("artist", "album_artist")).casefold()
                    artist_matches = artist_norm in artist_blob
                if candidate_paths:
                    candidate_evaluations.append(_classify_album_candidate(
                        file_path=rel,
                        metadata=metadata,
                        album_matches=album_matches,
                        artist_matches=artist_matches,
                        canonical_tracks=_normalize_canonical_tracks(canonical_tracks or []),
                    ))
                if not album_matches:
                    continue
                if artist_norm and not artist_matches:
                    continue
                local_tracks.append({
                    "file_path": rel,
                    "format": evidence.get("format") or suffix,
                    "metadata": metadata,
                    "warnings": evidence.get("warnings") or [],
                })
        canonical = _normalize_canonical_tracks(canonical_tracks or [])
        missing_tracks = _missing_canonical_tracks(canonical, local_tracks)
        repair_plan = _album_repair_plan(local_tracks, canonical, album=album, artist=artist)
        return {
            "ok": True,
            "album": album,
            "artist": artist,
            "scanned_count": scanned_count,
            "partial": partial,
            "warnings": warnings,
            "canonical_track_count": len(canonical),
            "local_track_count": len(local_tracks),
            "local_tracks": local_tracks,
            "candidate_evaluations": candidate_evaluations,
            "missing_tracks": missing_tracks,
            "repair_plan": repair_plan,
        }

    def evidence_for_file(
        self,
        file_path: str,
        *,
        include: set[str] | list[str] | tuple[str, ...] | None = None,
        include_lyrics: bool = False,
    ) -> dict[str, Any]:
        requested = set(include or {"metadata", "audio", "lyrics_summary", "cover_summary"})
        if include_lyrics:
            requested.add("lyrics_text")
        try:
            resolved = self._resolve(file_path)
        except ValueError:
            return {
                "ok": False,
                "error_type": "invalid_path",
                "detail": "file_path must be relative and stay under library_root",
            }

        base: dict[str, Any] = {
            "ok": True,
            "file_path": file_path,
            "resolved_path": str(resolved),
            "exists": resolved.exists(),
            "metadata": {},
            "lyrics": {"present": False, "length": 0, "line_count": 0},
            "cover": {"present": False, "count": 0},
            "warnings": [],
        }
        if not resolved.exists():
            return base
        if MutagenFile is None:
            return {
                **base,
                "ok": False,
                "error_type": "mutagen_unavailable",
                "detail": "mutagen is not importable",
            }

        try:
            audio = MutagenFile(str(resolved))
        except Exception as exc:
            return {**base, "ok": False, "error_type": exc.__class__.__name__, "detail": str(exc)}
        if audio is None:
            return {**base, "ok": False, "error_type": "unsupported_audio", "detail": "mutagen could not identify file"}

        tags = getattr(audio, "tags", None) or {}
        warnings: list[dict[str, str]] = []
        audio_format = resolved.suffix.lower().lstrip(".")
        metadata = _extract_metadata(tags, audio_format=audio_format, warnings=warnings) if "metadata" in requested else {}
        wants_lyrics = bool(requested & {"lyrics_summary", "lyrics_text"})
        lyrics_text, lyrics_source = _extract_lyrics(tags, warnings=warnings) if wants_lyrics else ("", "")
        wants_cover = bool(requested & {"cover_summary", "cover_data"})
        cover_info = (
            _extract_cover(audio, tags, warnings=warnings, include_data="cover_data" in requested)
            if wants_cover
            else {"requested": False}
        )
        duration_seconds = _duration_seconds(audio) if "audio" in requested else None

        lyrics_info: dict[str, Any] = {
            "present": bool(lyrics_text),
            "length": len(lyrics_text),
            "line_count": len(lyrics_text.splitlines()) if lyrics_text else 0,
            "source": lyrics_source,
            "quality": _lyrics_quality(lyrics_text),
        }
        if "lyrics_text" in requested and lyrics_text:
            lyrics_info["text"] = lyrics_text

        return {
            **base,
            "metadata": metadata,
            "lyrics": lyrics_info,
            "cover": cover_info,
            "audio_type": audio.__class__.__name__,
            "format": resolved.suffix.lower().lstrip("."),
            "duration_seconds": duration_seconds,
            "duration": _format_duration(duration_seconds),
            "warnings": warnings,
        }

    def file_snapshot(self, file_path: str) -> dict[str, Any]:
        """Return the authoritative small snapshot used by approval previews."""
        try:
            resolved = self._resolve(file_path)
        except ValueError:
            return {"ok": False, "file_path": file_path, "error_type": "invalid_path"}
        if not resolved.is_file():
            return {"ok": True, "file_path": file_path, "exists": False}
        stat = resolved.stat()
        evidence = self.evidence_for_file(
            file_path,
            include={"metadata", "audio", "lyrics_summary", "cover_summary"},
        )
        return {
            "ok": bool(evidence.get("ok")),
            "file_path": file_path,
            "exists": True,
            "size": stat.st_size,
            "sha256": _sha256_file(resolved),
            "metadata": evidence.get("metadata") or {},
            "lyrics": evidence.get("lyrics") or {},
            "cover": evidence.get("cover") or {},
            "format": evidence.get("format") or resolved.suffix.lower().lstrip("."),
            "duration_seconds": evidence.get("duration_seconds"),
            "warnings": evidence.get("warnings") or [],
        }

    def cover_write(
        self,
        *,
        file_path: str,
        cover_data: bytes,
        media_type: str,
        cover_sha256: str,
        apply: bool = True,
    ) -> dict[str, Any]:
        actual_sha256 = hashlib.sha256(cover_data).hexdigest()
        if actual_sha256 != str(cover_sha256 or "").lower():
            return {"ok": False, "file_path": file_path, "error_type": "cover_hash_mismatch"}
        detected = _image_mime_from_bytes(cover_data)
        if not detected or detected != media_type:
            return {"ok": False, "file_path": file_path, "error_type": "cover_media_type_mismatch"}
        before = self.file_snapshot(file_path)
        if not before.get("exists"):
            return {"ok": False, "file_path": file_path, "error_type": "missing_file", "before": before}
        if not apply:
            return {"ok": True, "applied": False, "file_path": file_path, "before": before}
        resolved = self._resolve(file_path)
        try:
            _write_cover_tag(resolved, cover_data, media_type)
        except Exception as exc:
            return {
                "ok": False,
                "file_path": file_path,
                "error_type": exc.__class__.__name__,
                "detail": str(exc),
                "before": before,
            }
        after = self.file_snapshot(file_path)
        verified = str((after.get("cover") or {}).get("sha256") or "") == actual_sha256
        return {"ok": verified, "applied": verified, "verified": verified, "file_path": file_path, "before": before, "after": after}

    def move_file(self, *, file_path: str, target_file_path: str) -> dict[str, Any]:
        source = self._resolve(file_path)
        target = self._resolve(target_file_path)
        if not source.is_file():
            return {"ok": False, "file_path": file_path, "target_file_path": target_file_path, "error_type": "missing_file"}
        if target.exists():
            return {"ok": False, "file_path": file_path, "target_file_path": target_file_path, "error_type": "target_exists"}
        before_sha256 = _sha256_file(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        verified = not source.exists() and target.is_file() and _sha256_file(target) == before_sha256
        return {"ok": verified, "verified": verified, "file_path": file_path, "target_file_path": target_file_path, "sha256": before_sha256}

    def delete_file(self, *, file_path: str) -> dict[str, Any]:
        resolved = self._resolve(file_path)
        if not resolved.is_file():
            return {"ok": False, "file_path": file_path, "error_type": "missing_file"}
        sha256 = _sha256_file(resolved)
        size = resolved.stat().st_size
        resolved.unlink()
        return {"ok": not resolved.exists(), "verified": not resolved.exists(), "file_path": file_path, "sha256": sha256, "size": size}

    def add_file(self, *, source_path: Path, target_file_path: str, expected_sha256: str) -> dict[str, Any]:
        target = self._resolve(target_file_path)
        if not source_path.is_file():
            return {"ok": False, "target_file_path": target_file_path, "error_type": "source_missing"}
        source_sha256 = _sha256_file(source_path)
        if source_sha256 != expected_sha256:
            return {"ok": False, "target_file_path": target_file_path, "error_type": "source_hash_mismatch"}
        if target.exists():
            return {"ok": False, "target_file_path": target_file_path, "error_type": "target_exists"}
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".incoming", dir=str(target.parent))
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            shutil.copy2(source_path, temporary_path)
            if _sha256_file(temporary_path) != source_sha256:
                raise OSError("copy_hash_mismatch")
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        verified = target.is_file() and _sha256_file(target) == source_sha256
        return {"ok": verified, "verified": verified, "target_file_path": target_file_path, "sha256": source_sha256, "size": target.stat().st_size if target.exists() else 0}

    def prepare_staged_import(
        self,
        *,
        source_path: Path,
        output_path: Path,
        expected_source_sha256: str,
        metadata: dict[str, Any],
        lyrics: str = "",
        cover_data: bytes = b"",
        cover_media_type: str = "",
    ) -> dict[str, Any]:
        """Build and verify the exact normalized bytes later published by library.import."""
        if not source_path.is_file():
            return {"ok": False, "error_type": "source_missing"}
        source_sha256 = _sha256_file(source_path)
        if source_sha256 != str(expected_source_sha256 or "").lower():
            return {"ok": False, "error_type": "source_hash_mismatch", "source_sha256": source_sha256}
        if source_path.suffix.lower() not in {".flac", ".mp3", ".m4a", ".mp4", ".aac"}:
            return {"ok": False, "error_type": "unsupported_audio_format", "suffix": source_path.suffix.lower()}
        normalized = _normalize_patch_fields(metadata)
        missing_fields = sorted({"title", "artist"} - set(normalized))
        empty_fields = sorted(key for key, value in normalized.items() if key in {"title", "artist"} and not value)
        if missing_fields or empty_fields:
            return {
                "ok": False,
                "error_type": "canonical_metadata_incomplete",
                "missing_fields": missing_fields,
                "empty_fields": empty_fields,
            }
        if bool(cover_data) != bool(cover_media_type):
            return {"ok": False, "error_type": "cover_payload_incomplete"}

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            suffix=f".preparing{source_path.suffix.lower()}",
            dir=str(output_path.parent),
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            shutil.copy2(source_path, temporary_path)
            _clear_all_tags(temporary_path)
            _write_metadata_fields(temporary_path, normalized)
            if lyrics:
                _write_lyrics_tag(temporary_path, lyrics)
            if cover_data:
                _write_cover_tag(temporary_path, cover_data, cover_media_type)

            staged_inspector = LibraryInspector(temporary_path.parent)
            evidence = staged_inspector.evidence_for_file(
                temporary_path.name,
                include={"metadata", "audio", "lyrics_text", "cover_summary"},
            )
            actual_metadata = evidence.get("metadata") or {}
            if not evidence.get("ok") or not _metadata_patch_verified(actual_metadata, normalized):
                return {
                    "ok": False,
                    "error_type": "staged_metadata_readback_mismatch",
                    "expected_metadata": normalized,
                    "actual_metadata": actual_metadata,
                }
            actual_lyrics = str((evidence.get("lyrics") or {}).get("text") or "")
            if actual_lyrics != lyrics:
                return {
                    "ok": False,
                    "error_type": "staged_lyrics_readback_mismatch",
                    "expected_sha256": _sha256_text(lyrics) if lyrics else "",
                    "actual_sha256": _sha256_text(actual_lyrics) if actual_lyrics else "",
                }
            cover = evidence.get("cover") or {}
            expected_cover_sha256 = hashlib.sha256(cover_data).hexdigest() if cover_data else ""
            if bool(cover.get("present")) != bool(cover_data) or str(cover.get("sha256") or "") != expected_cover_sha256:
                return {
                    "ok": False,
                    "error_type": "staged_cover_readback_mismatch",
                    "expected_sha256": expected_cover_sha256,
                    "actual_sha256": str(cover.get("sha256") or ""),
                }
            tag_audit = _tag_key_audit(temporary_path)
            if tag_audit["unexpected_tag_keys"]:
                return {"ok": False, "error_type": "staged_tag_allowlist_failed", **tag_audit}

            os.replace(temporary_path, output_path)
            final_sha256 = _sha256_file(output_path)
            return {
                "ok": True,
                "verified": True,
                "source_sha256": source_sha256,
                "staged_sha256": final_sha256,
                "size": output_path.stat().st_size,
                "metadata": actual_metadata,
                "lyrics": {
                    "status": "available" if lyrics else "unavailable",
                    "sha256": _sha256_text(lyrics) if lyrics else "",
                    "length": len(lyrics),
                    "line_count": len(lyrics.splitlines()) if lyrics else 0,
                },
                "cover": {
                    "status": "available" if cover_data else "unavailable",
                    "sha256": expected_cover_sha256,
                    "size": len(cover_data),
                    "media_type": cover_media_type,
                },
                "audio": {
                    "format": evidence.get("format") or source_path.suffix.lower().lstrip("."),
                    "duration_seconds": evidence.get("duration_seconds"),
                },
                "tag_audit": tag_audit,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "detail": str(exc),
            }
        finally:
            temporary_path.unlink(missing_ok=True)

    def lyrics_diagnose(
        self,
        *,
        file_path: str = "",
        title: str = "",
        artist: str = "",
        album: str = "",
        candidate_paths: list[str] | None = None,
        max_censored_lines: int = 12,
    ) -> dict[str, Any]:
        """Read-only lyrics quality diagnosis for an already-library-managed file."""
        warnings: list[dict[str, str]] = []
        candidates = [str(item or "").strip() for item in ([file_path] + (candidate_paths or [])) if str(item or "").strip()]
        resolved_evidence: dict[str, Any] | None = None
        candidate_evaluations: list[dict[str, Any]] = []
        expected_metadata = {"title": title, "artist": artist, "album": album}

        for candidate_index, candidate in enumerate(candidates):
            evidence = self.evidence_for_file(candidate, include_lyrics=True)
            metadata = evidence.get("metadata") or {}
            matches = bool(evidence.get("exists")) and _lyrics_target_matches(metadata, expected_metadata)
            candidate_evaluations.append({
                "file_path": candidate,
                "exists": bool(evidence.get("exists")),
                "metadata_matches": matches,
                "metadata": metadata,
            })
            # An explicit file_path is already path-guarded by evidence_for_file().
            # Treat it as authoritative even when metadata is stale or display-layer
            # paths are virtual; report metadata_matches separately instead of
            # falling back to a slow full-library search.
            if evidence.get("exists") and (candidate_index == 0 or matches or not any(expected_metadata.values())):
                resolved_evidence = evidence
                break

        if resolved_evidence is None and title:
            search = self.search_files(title, limit=10, max_scanned=5000)
            warnings.extend(search.get("warnings") or [])
            for row in search.get("results") or []:
                candidate = str(row.get("file_path") or "")
                evidence = self.evidence_for_file(candidate, include_lyrics=True)
                metadata = evidence.get("metadata") or {}
                matches = bool(evidence.get("exists")) and _lyrics_target_matches(metadata, expected_metadata)
                candidate_evaluations.append({
                    "file_path": candidate,
                    "exists": bool(evidence.get("exists")),
                    "metadata_matches": matches,
                    "metadata": metadata,
                })
                if matches:
                    resolved_evidence = evidence
                    break

        if resolved_evidence is None:
            return {
                "ok": False,
                "error_type": "lyrics_target_unresolved",
                "file_path": file_path,
                "title": title,
                "artist": artist,
                "album": album,
                "candidate_evaluations": candidate_evaluations,
                "warnings": warnings,
            }

        lyrics = resolved_evidence.get("lyrics") or {}
        text = str(lyrics.get("text") or "")
        quality = _lyrics_quality(text, max_lines=max_censored_lines)
        return {
            "ok": True,
            "file_path": resolved_evidence.get("file_path") or file_path,
            "resolved_path": resolved_evidence.get("resolved_path") or "",
            "metadata": resolved_evidence.get("metadata") or {},
            "lyrics": {
                "present": bool(lyrics.get("present")),
                "source": lyrics.get("source") or "",
                "length": lyrics.get("length") or 0,
                "line_count": lyrics.get("line_count") or 0,
            },
            "censorship_detected": quality["censorship_detected"],
            "censored_marker_count": quality["censored_marker_count"],
            "censored_line_count": quality["censored_line_count"],
            "censored_lines": quality["censored_lines"],
            "candidate_evaluations": candidate_evaluations,
            "warnings": warnings,
        }

    def lyrics_write(
        self,
        *,
        file_path: str,
        lyrics: str,
        lyrics_sha256: str,
        reason: str = "",
        apply: bool = True,
    ) -> dict[str, Any]:
        warnings: list[dict[str, str]] = []
        file_path = str(file_path or "").strip()
        reason = str(reason or "").strip()
        lyrics = str(lyrics or "")
        expected_hash = str(lyrics_sha256 or "").strip().lower()
        actual_hash = _sha256_text(lyrics)

        if not expected_hash or expected_hash != actual_hash:
            return _lyrics_write_error(
                file_path=file_path,
                reason=reason,
                code="lyrics_hash_mismatch",
                detail="lyrics_sha256 must match lyrics text",
                warnings=[{"code": "lyrics_hash_mismatch", "detail": "lyrics_sha256 must match lyrics text"}],
            )
        try:
            resolved = self._resolve(file_path)
        except ValueError:
            return _lyrics_write_error(
                file_path=file_path,
                reason=reason,
                code="invalid_path",
                detail="file_path must be relative and stay under library_root",
                warnings=[{"code": "invalid_path", "detail": "file_path must be relative and stay under library_root"}],
            )
        before = _evidence_without_lyrics_text(self.evidence_for_file(file_path, include_lyrics=True))
        if not before.get("exists"):
            return _lyrics_write_error(
                file_path=file_path,
                reason=reason,
                code="missing_file",
                detail=file_path,
                before=before,
                after=before,
                warnings=[{"code": "missing_file", "detail": file_path}],
            )

        changes = [{
            "file_path": file_path,
            "field": "lyrics",
            "current_sha256": str((before.get("lyrics") or {}).get("sha256") or ""),
            "target_sha256": actual_hash,
            "current_length": str((before.get("lyrics") or {}).get("length") or 0),
            "target_length": str(len(lyrics)),
        }]
        if not apply:
            return {
                "ok": True,
                "applied": False,
                "verified": False,
                "tag_verified": False,
                "file_path": file_path,
                "reason": reason,
                "before": before,
                "after": before,
                "changes": changes,
                "warnings": warnings,
            }
        try:
            _write_lyrics_tag(resolved, lyrics)
        except Exception as exc:
            error_code = "unsupported_audio_format" if str(exc).startswith("unsupported_audio_format") else exc.__class__.__name__
            return _lyrics_write_error(
                file_path=file_path,
                reason=reason,
                code=error_code,
                detail=f"{file_path}:{exc.__class__.__name__}:{exc}",
                before=before,
                after=before,
                changes=changes,
                warnings=[{"code": "lyrics_write_failed", "detail": f"{file_path}:{exc.__class__.__name__}:{exc}"}],
            )
        after_full = self.evidence_for_file(file_path, include_lyrics=True)
        after_lyrics = after_full.get("lyrics") or {}
        text = str(after_lyrics.get("text") or "")
        tag_verified = bool(after_full.get("exists") and text == lyrics)
        after = _evidence_without_lyrics_text(after_full)
        if not tag_verified:
            warnings.append({"code": "lyrics_readback_mismatch", "detail": file_path})
        return {
            "ok": tag_verified,
            "applied": tag_verified,
            "verified": tag_verified,
            "tag_verified": tag_verified,
            "file_path": file_path,
            "reason": reason,
            "before": before,
            "after": after,
            "changes": changes,
            "warnings": warnings,
        }

    def metadata_patch(self, *, patches: list[dict[str, Any]], apply: bool = True) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        if not patches:
            return {
                "ok": False,
                "applied": False,
                "verified": False,
                "error_type": "empty_patches",
                "before": [],
                "after": [],
                "changes": [],
                "warnings": [{"code": "empty_patches", "detail": "patches must not be empty"}],
            }
        for index, patch in enumerate(patches):
            row = self._metadata_patch_one(patch=patch, apply=apply, index=index)
            rows.append(row)
            warnings.extend(row.get("warnings") or [])
        before = [row.get("before") for row in rows]
        after = [row.get("after") for row in rows]
        changes = [change for row in rows for change in (row.get("changes") or [])]
        ok = all(row.get("ok") for row in rows)
        tag_verified = bool(apply) and ok and all(row.get("tag_verified", row.get("verified")) for row in rows)
        return {
            "ok": ok,
            "applied": bool(apply) and ok,
            "verified": tag_verified,
            "tag_verified": tag_verified,
            "before": before,
            "after": after,
            "changes": changes,
            "warnings": warnings,
            "results": rows,
        }

    def _metadata_patch_one(self, *, patch: dict[str, Any], apply: bool, index: int) -> dict[str, Any]:
        warnings: list[dict[str, str]] = []
        file_path = str(patch.get("file_path") or "").strip()
        fields = patch.get("fields") or {}
        reason = str(patch.get("reason") or "").strip()
        if not isinstance(fields, dict) or not fields:
            return _metadata_patch_error(index=index, file_path=file_path, code="empty_fields", detail="fields must not be empty")
        invalid_fields = sorted(set(fields) - ALLOWED_METADATA_PATCH_FIELDS)
        if invalid_fields:
            return _metadata_patch_error(index=index, file_path=file_path, code="unsupported_fields", detail=",".join(invalid_fields))
        try:
            resolved = self._resolve(file_path)
        except ValueError:
            return _metadata_patch_error(index=index, file_path=file_path, code="invalid_path", detail="file_path must be relative and stay under library_root")
        before = self.evidence_for_file(file_path)
        if not before.get("exists"):
            return {
                "ok": False,
                "verified": False,
                "tag_verified": False,
                "index": index,
                "file_path": file_path,
                "reason": reason,
                "before": before,
                "after": before,
                "changes": [],
                "warnings": [{"code": "missing_file", "detail": file_path}],
                "error_type": "missing_file",
            }
        current_metadata = before.get("metadata") or {}
        normalized_fields = _normalize_patch_fields(fields)
        changes = _metadata_patch_changes(file_path=file_path, before=current_metadata, target=normalized_fields)
        if not apply:
            return {
                "ok": True,
                "verified": False,
                "tag_verified": False,
                "index": index,
                "file_path": file_path,
                "reason": reason,
                "before": before,
                "after": before,
                "changes": changes,
                "warnings": warnings,
            }
        try:
            _write_metadata_fields(resolved, normalized_fields)
        except Exception as exc:
            return {
                "ok": False,
                "verified": False,
                "tag_verified": False,
                "index": index,
                "file_path": file_path,
                "reason": reason,
                "before": before,
                "after": before,
                "changes": changes,
                "warnings": [{"code": "metadata_write_failed", "detail": f"{file_path}:{exc.__class__.__name__}:{exc}"}],
                "error_type": exc.__class__.__name__,
            }
        after = self.evidence_for_file(file_path)
        verified = _metadata_patch_verified(after.get("metadata") or {}, normalized_fields)
        if not verified:
            warnings.append({"code": "metadata_readback_mismatch", "detail": file_path})
        return {
            "ok": verified,
            "verified": verified,
            "tag_verified": verified,
            "index": index,
            "file_path": file_path,
            "reason": reason,
            "before": before,
            "after": after,
            "changes": changes,
            "warnings": warnings,
        }


def _stringify(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        return _stringify(value[0])
    text = getattr(value, "text", None)
    if text is not None:
        return _stringify(text)
    return str(value).strip()


def _warn(warnings: list[dict[str, str]], *, code: str, detail: str) -> None:
    warnings.append({"code": code, "detail": detail})


def _tag_value(tags: Any, key: str, *, warnings: list[dict[str, str]], field: str) -> Any:
    try:
        getter = getattr(tags, "get", None)
        if callable(getter):
            return getter(key)
    except Exception as exc:
        _warn(warnings, code="metadata_tag_read_failed", detail=f"{field}:{key}:{exc.__class__.__name__}:{exc}")
        return None
    try:
        if key in tags:
            return tags[key]
    except Exception as exc:
        _warn(warnings, code="metadata_tag_read_failed", detail=f"{field}:{key}:{exc.__class__.__name__}:{exc}")
        return None
    return None


def _split_position_total(value: Any) -> tuple[str, str]:
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
        first = value[0]
        if len(first) >= 2:
            return str(first[0] or "").strip(), str(first[1] or "").strip()
        if len(first) == 1:
            return str(first[0] or "").strip(), ""
    text = _stringify(value)
    if "/" in text:
        current, total = text.split("/", 1)
        return current.strip(), total.strip()
    return text.strip(), ""


def _extract_metadata(tags: Any, *, audio_format: str = "", warnings: list[dict[str, str]]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    aliases_by_format = _TAG_ALIASES_BY_FORMAT.get(audio_format, _TAG_ALIASES_FALLBACK)
    for field, aliases in aliases_by_format.items():
        for key in aliases:
            value = _tag_value(tags, key, warnings=warnings, field=field)
            if value is None:
                continue
            if field in {"track_number", "disc_number"}:
                current, total = _split_position_total(value)
                if current:
                    metadata[field] = current
                total_field = "track_total" if field == "track_number" else "disc_total"
                if total and total_field not in metadata:
                    metadata[total_field] = total
                if current:
                    break
            else:
                text = _stringify(value)
                if text:
                    metadata[field] = text
                    break
    return metadata


def _extract_lyrics(tags: Any, *, warnings: list[dict[str, str]]) -> tuple[str, str]:
    for key in _LYRICS_KEYS:
        try:
            if key in tags:
                text = _stringify(tags[key])
                if text:
                    return text, str(key)
        except Exception as exc:
            _warn(warnings, code="lyrics_tag_read_failed", detail=f"{key}:{exc.__class__.__name__}:{exc}")
            continue
    # ID3 lyrics frames commonly have keys like USLT::eng.
    try:
        keys = list(tags.keys())
    except Exception as exc:
        _warn(warnings, code="lyrics_keys_read_failed", detail=f"{exc.__class__.__name__}:{exc}")
        keys = []
    for key in keys:
        key_text = str(key)
        if key_text.startswith(("USLT", "SYLT")) or "lyric" in key_text.lower():
            try:
                text = _stringify(tags[key])
            except Exception as exc:
                _warn(warnings, code="lyrics_tag_read_failed", detail=f"{key_text}:{exc.__class__.__name__}:{exc}")
                continue
            if text:
                return text, key_text
    return "", ""


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(str(value).split("/", 1)[0].strip())
    except Exception:
        return None


def _metadata_patch_error(*, index: int, file_path: str, code: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "verified": False,
        "tag_verified": False,
        "index": index,
        "file_path": file_path,
        "before": None,
        "after": None,
        "changes": [],
        "warnings": [{"code": code, "detail": detail}],
        "error_type": code,
    }


def _lyrics_write_error(
    *,
    file_path: str,
    reason: str,
    code: str,
    detail: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    changes: list[dict[str, str]] | None = None,
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "applied": False,
        "verified": False,
        "tag_verified": False,
        "file_path": file_path,
        "reason": reason,
        "before": before,
        "after": after,
        "changes": changes or [],
        "warnings": warnings or [{"code": code, "detail": detail}],
        "error_type": code,
        "detail": detail,
    }


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_without_lyrics_text(evidence: dict[str, Any]) -> dict[str, Any]:
    payload = dict(evidence)
    lyrics = dict(payload.get("lyrics") or {})
    text = lyrics.pop("text", None)
    if text is not None:
        lyrics["sha256"] = _sha256_text(str(text))
    payload["lyrics"] = lyrics
    return payload


def _normalize_patch_fields(fields: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in fields.items():
        if key not in ALLOWED_METADATA_PATCH_FIELDS:
            continue
        normalized[key] = str(value or "").strip()
    return normalized


def _metadata_patch_changes(*, file_path: str, before: dict[str, Any], target: dict[str, str]) -> list[dict[str, str]]:
    changes = []
    for field, target_value in target.items():
        current_value = str(before.get(field) or "")
        if current_value != target_value:
            changes.append({"file_path": file_path, "field": field, "current": current_value, "target": target_value})
    return changes


def _metadata_patch_verified(metadata: dict[str, Any], target: dict[str, str]) -> bool:
    for field, target_value in target.items():
        if str(metadata.get(field) or "") != target_value:
            return False
    return True


def _lyrics_quality(text: str, *, max_lines: int = 12) -> dict[str, Any]:
    lines = str(text or "").splitlines()
    censored_lines: list[dict[str, Any]] = []
    marker_count = 0
    for index, line in enumerate(lines, start=1):
        matches = list(_CENSORED_MARKER_RE.finditer(line))
        if not matches:
            continue
        marker_count += len(matches)
        if len(censored_lines) >= max_lines:
            continue
        timestamp_match = _TIMESTAMP_RE.match(line)
        censored_lines.append({
            "line_number": index,
            "timestamp": timestamp_match.group(1) if timestamp_match else "",
            "marker_count": len(matches),
            "snippet": _redact_lyrics_line(line),
        })
    return {
        "censorship_detected": marker_count > 0,
        "censored_marker_count": marker_count,
        "censored_line_count": sum(1 for line in lines if _CENSORED_MARKER_RE.search(line)),
        "censored_lines": censored_lines,
    }


def _redact_lyrics_line(line: str, *, max_length: int = 160) -> str:
    text = " ".join(str(line or "").split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


def _lyrics_target_matches(metadata: dict[str, Any], expected: dict[str, Any]) -> bool:
    checks = []
    title = _norm_metadata_value(expected.get("title"))
    artist = _norm_metadata_value(expected.get("artist"))
    album = _norm_metadata_value(expected.get("album"))
    if title:
        checks.append(title == _norm_metadata_value(metadata.get("title")))
    if album:
        checks.append(album == _norm_metadata_value(metadata.get("album")))
    if artist:
        artist_blob = _norm_metadata_value(" ".join(str(metadata.get(key) or "") for key in ("artist", "album_artist")))
        checks.append(artist in artist_blob)
    return all(checks) if checks else True


def _norm_metadata_value(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _position_pair(fields: dict[str, str], current_key: str, total_key: str) -> str | None:
    current = fields.get(current_key)
    total = fields.get(total_key)
    if current is None and total is None:
        return None
    current_text = str(current or "").strip()
    total_text = str(total or "").strip()
    if total_text:
        return f"{current_text}/{total_text}"
    return current_text


def _position_tuple(fields: dict[str, str], current_key: str, total_key: str) -> tuple[int, int] | None:
    current = fields.get(current_key)
    total = fields.get(total_key)
    if current is None and total is None:
        return None
    return (_safe_int(current) or 0, _safe_int(total) or 0)


def _write_metadata_fields(path: Path, fields: dict[str, str]) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        _write_mp3_metadata(path, fields)
        return
    if suffix == ".flac":
        _write_flac_metadata(path, fields)
        return
    if suffix in {".m4a", ".mp4", ".aac"}:
        _write_mp4_metadata(path, fields)
        return
    raise ValueError(f"unsupported_audio_format:{suffix}")


def _clear_all_tags(path: Path) -> None:
    """Remove every inherited tag before writing the import allowlist."""
    suffix = path.suffix.lower()
    if suffix == ".flac":
        if FLAC is None:
            raise RuntimeError("mutagen_writer_unavailable")
        audio = FLAC(path.as_posix())
        audio.clear()
        audio.clear_pictures()
        audio.save()
        return
    if suffix == ".mp3":
        if MutagenFile is None:
            raise RuntimeError("mutagen_writer_unavailable")
        audio = MutagenFile(path.as_posix(), easy=False)
        if audio is None:
            raise ValueError("unsupported_audio")
        audio.delete()
        return
    if suffix in {".m4a", ".mp4", ".aac"}:
        if MP4 is None:
            raise RuntimeError("mutagen_writer_unavailable")
        audio = MP4(path.as_posix())
        audio.delete()
        return
    raise ValueError(f"unsupported_audio_format:{suffix}")


def _tag_key_audit(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".flac":
        if FLAC is None:
            raise RuntimeError("mutagen_reader_unavailable")
        audio = FLAC(path.as_posix())
        keys = sorted(str(key).casefold() for key in audio.keys())
        allowed = {
            "title", "artist", "album", "albumartist", "album_artist", "date",
            "tracknumber", "tracktotal", "discnumber", "disctotal", "lyrics",
        }
    elif suffix == ".mp3":
        if MutagenFile is None:
            raise RuntimeError("mutagen_reader_unavailable")
        audio = MutagenFile(path.as_posix(), easy=False)
        keys = sorted(str(key) for key in (audio.tags.keys() if audio and audio.tags else []))
        allowed = {"TIT2", "TPE1", "TALB", "TPE2", "TDRC", "TRCK", "TPOS", "USLT", "APIC"}
        normalized = {key.split(":", 1)[0] for key in keys}
        return {
            "tag_keys": keys,
            "allowed_tag_keys": sorted(allowed),
            "unexpected_tag_keys": sorted(normalized - allowed),
        }
    elif suffix in {".m4a", ".mp4", ".aac"}:
        if MP4 is None:
            raise RuntimeError("mutagen_reader_unavailable")
        audio = MP4(path.as_posix())
        keys = sorted(str(key) for key in audio.keys())
        allowed = {"\xa9nam", "\xa9ART", "\xa9alb", "aART", "\xa9day", "trkn", "disk", "\xa9lyr", "covr"}
    else:
        raise ValueError(f"unsupported_audio_format:{suffix}")
    return {
        "tag_keys": keys,
        "allowed_tag_keys": sorted(allowed),
        "unexpected_tag_keys": sorted(set(keys) - allowed),
    }


def _write_lyrics_tag(path: Path, lyrics: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        _write_mp3_lyrics(path, lyrics)
        return
    if suffix == ".flac":
        _write_flac_lyrics(path, lyrics)
        return
    if suffix in {".ogg", ".opus"}:
        _write_vorbis_lyrics(path, lyrics)
        return
    if suffix in {".m4a", ".mp4", ".aac"}:
        _write_mp4_lyrics(path, lyrics)
        return
    raise ValueError(f"unsupported_audio_format:{suffix}")


def _write_cover_tag(path: Path, cover_data: bytes, media_type: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        if MutagenFile is None or APIC is None:
            raise RuntimeError("mutagen_cover_writer_unavailable")
        audio = MutagenFile(path.as_posix(), easy=False)
        if audio is None:
            raise ValueError("unsupported_audio")
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=media_type, type=3, desc="Cover", data=cover_data))
        audio.save()
        return
    if suffix == ".flac":
        if FLAC is None or Picture is None:
            raise RuntimeError("mutagen_cover_writer_unavailable")
        audio = FLAC(path.as_posix())
        picture = Picture()
        picture.type = 3
        picture.mime = media_type
        picture.desc = "Cover"
        picture.data = cover_data
        audio.clear_pictures()
        audio.add_picture(picture)
        audio.save()
        return
    if suffix in {".m4a", ".mp4", ".aac"}:
        if MP4 is None or MP4Cover is None:
            raise RuntimeError("mutagen_cover_writer_unavailable")
        image_format = MP4Cover.FORMAT_PNG if media_type == "image/png" else MP4Cover.FORMAT_JPEG
        audio = MP4(path.as_posix())
        audio["covr"] = [MP4Cover(cover_data, imageformat=image_format)]
        audio.save()
        return
    raise ValueError(f"unsupported_audio_format:{suffix}")


def _write_mp3_lyrics(path: Path, lyrics: str) -> None:
    if MutagenFile is None:
        raise RuntimeError("mutagen_writer_unavailable")
    audio = MutagenFile(path.as_posix(), easy=False)
    if audio is None:
        raise ValueError("unsupported_audio")
    if audio.tags is None:
        audio.add_tags()
    if USLT is None:
        raise RuntimeError("mutagen_id3_uslt_unavailable")
    for key in list(audio.tags.keys()):
        if str(key).startswith("USLT"):
            del audio.tags[key]
    audio.tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
    audio.save()


def _write_flac_lyrics(path: Path, lyrics: str) -> None:
    if FLAC is None:
        raise RuntimeError("mutagen_writer_unavailable")
    audio = FLAC(path.as_posix())
    audio["lyrics"] = lyrics
    audio.save()


def _write_vorbis_lyrics(path: Path, lyrics: str) -> None:
    if MutagenFile is None:
        raise RuntimeError("mutagen_writer_unavailable")
    audio = MutagenFile(path.as_posix())
    if audio is None:
        raise ValueError("unsupported_audio")
    audio["lyrics"] = [lyrics]
    audio.save()


def _write_mp4_lyrics(path: Path, lyrics: str) -> None:
    if MP4 is None:
        raise RuntimeError("mutagen_writer_unavailable")
    audio = MP4(path.as_posix())
    audio["\xa9lyr"] = [lyrics]
    audio.save()


def _write_mp3_metadata(path: Path, fields: dict[str, str]) -> None:
    if MutagenFile is None:
        raise RuntimeError("mutagen_writer_unavailable")
    audio = MutagenFile(path.as_posix(), easy=False)
    if audio is None:
        raise ValueError("unsupported_audio")
    if audio.tags is None:
        audio.add_tags()
    class _MinimalFrame:
        def __init__(self, frame_id: str, text: str):
            self.FrameID = frame_id
            self.text = [text]

        def __str__(self) -> str:
            return str(self.text[0] if self.text else "")

    def make_frame(frame_type: Any, frame_id: str, text: str) -> Any:
        if frame_type is not None:
            return frame_type(encoding=3, text=text)
        return _MinimalFrame(frame_id, text)

    scalar_frames = {
        "title": (TIT2, "TIT2"),
        "artist": (TPE1, "TPE1"),
        "album": (TALB, "TALB"),
        "album_artist": (TPE2, "TPE2"),
        "date": (TDRC, "TDRC"),
    }
    for field, (frame_type, frame_id) in scalar_frames.items():
        if field in fields:
            audio.tags.add(make_frame(frame_type, frame_id, fields[field]))
    track_pair = _position_pair(fields, "track_number", "track_total")
    if track_pair is not None:
        audio.tags.add(make_frame(TRCK, "TRCK", track_pair))
    disc_pair = _position_pair(fields, "disc_number", "disc_total")
    if disc_pair is not None:
        audio.tags.add(make_frame(TPOS, "TPOS", disc_pair))
    audio.save()


def _write_flac_metadata(path: Path, fields: dict[str, str]) -> None:
    if FLAC is None:
        raise RuntimeError("mutagen_writer_unavailable")
    audio = FLAC(path.as_posix())
    mapping = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "album_artist": "albumartist",
        "date": "date",
        "track_number": "tracknumber",
        "track_total": "tracktotal",
        "disc_number": "discnumber",
        "disc_total": "disctotal",
    }
    for field, tag_key in mapping.items():
        if field in fields:
            audio[tag_key] = fields[field]
            if field == "album_artist":
                audio["album_artist"] = fields[field]
    audio.save()


def _write_mp4_metadata(path: Path, fields: dict[str, str]) -> None:
    if MP4 is None:
        raise RuntimeError("mutagen_writer_unavailable")
    audio = MP4(path.as_posix())
    scalar_atoms = {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "album_artist": "aART",
        "date": "\xa9day",
    }
    for field, atom in scalar_atoms.items():
        if field in fields:
            audio[atom] = [fields[field]]
    track_tuple = _position_tuple(fields, "track_number", "track_total")
    if track_tuple is not None:
        audio["trkn"] = [track_tuple]
    disc_tuple = _position_tuple(fields, "disc_number", "disc_total")
    if disc_tuple is not None:
        audio["disk"] = [disc_tuple]
    audio.save()


def _normalize_canonical_tracks(canonical_tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in canonical_tracks:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        normalized.append({
            "track_number": _safe_int(item.get("track_number")) or 0,
            "disc_number": _safe_int(item.get("disc_number")) or 1,
            "title": title,
            "disc_total": _safe_int(item.get("disc_total")) or 0,
            "track_total": _safe_int(item.get("track_total")) or 0,
        })
    return normalized


def _track_identity(metadata: dict[str, Any]) -> tuple[int, int, str]:
    return (
        _safe_int(metadata.get("disc_number")) or 1,
        _safe_int(metadata.get("track_number")) or 0,
        str(metadata.get("title") or "").strip().casefold(),
    )


def _title_key(title: Any) -> str:
    text = str(title or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*[\(（][^\)）]*(?:ft\.?|feat\.?|featuring|with|prod\.?)[^\)）]*[\)）]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _missing_canonical_tracks(canonical_tracks: list[dict[str, Any]], local_tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    local_by_number = {(_safe_int((track.get("metadata") or {}).get("disc_number")) or 1, _safe_int((track.get("metadata") or {}).get("track_number")) or 0) for track in local_tracks}
    local_titles = {_title_key((track.get("metadata") or {}).get("title")) for track in local_tracks}
    missing: list[dict[str, Any]] = []
    for track in canonical_tracks:
        number_key = (track.get("disc_number") or 1, track.get("track_number") or 0)
        title_key = _title_key(track.get("title"))
        if number_key in local_by_number or title_key in local_titles:
            continue
        missing.append({"track_number": track.get("track_number") or 0, "disc_number": track.get("disc_number") or 1, "title": track.get("title") or ""})
    return missing


def _canonical_for_local(local_track: dict[str, Any], canonical_tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    metadata = local_track.get("metadata") or {}
    title = _title_key(metadata.get("title"))
    track_number = _safe_int(metadata.get("track_number")) or 0
    disc_number = _safe_int(metadata.get("disc_number")) or 1
    for candidate in canonical_tracks:
        if title and title == _title_key(candidate.get("title")):
            return candidate
    for candidate in canonical_tracks:
        if (candidate.get("disc_number") or 1, candidate.get("track_number") or 0) == (disc_number, track_number):
            return candidate
    return None


def _relative_or_name(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except Exception:
        return path.name


def _classify_album_candidate(
    *,
    file_path: str,
    metadata: dict[str, Any],
    album_matches: bool,
    artist_matches: bool,
    canonical_tracks: list[dict[str, Any]],
) -> dict[str, Any]:
    canonical = _canonical_for_local({"metadata": metadata}, canonical_tracks) if canonical_tracks else None
    if album_matches and artist_matches and canonical:
        classification = "confirmed_album_track"
        reason = "album_artist_and_canonical_track_match"
    elif album_matches and artist_matches and not canonical:
        classification = "unresolved_candidate"
        reason = "album_matches_but_no_canonical_track_match"
    elif not album_matches:
        classification = "candidate_duplicate_or_single"
        reason = "album_mismatch"
    else:
        classification = "candidate_duplicate_or_single"
        reason = "artist_mismatch"
    return {
        "file_path": file_path,
        "classification": classification,
        "reason": reason,
        "metadata": metadata,
        "canonical_track": canonical,
    }


def _album_repair_plan(local_tracks: list[dict[str, Any]], canonical_tracks: list[dict[str, Any]], *, album: str = "", artist: str = "") -> dict[str, Any]:
    changes: list[dict[str, str]] = []
    patches: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, str]] = []
    for local in sorted(local_tracks, key=lambda t: _track_identity(t.get("metadata") or {})):
        metadata = local.get("metadata") or {}
        canonical = _canonical_for_local(local, canonical_tracks) if canonical_tracks else None
        if canonical_tracks and not canonical:
            out_of_scope.append({
                "file_path": str(local.get("file_path") or ""),
                "field": "canonical_track",
                "current": str(metadata.get("title") or ""),
                "target": "",
                "reason": "local_track_not_matched_to_canonical_track",
            })
            continue

        target_fields: dict[str, str] = {}
        if album and str(metadata.get("album") or "") != album:
            target_fields["album"] = album
        if artist and str(metadata.get("album_artist") or "") != artist:
            target_fields["album_artist"] = artist
        if canonical:
            canonical_title = str(canonical.get("title") or "").strip()
            if canonical_title and str(metadata.get("title") or "") != canonical_title:
                target_fields["title"] = canonical_title
            for field in ("track_number", "track_total", "disc_number", "disc_total"):
                value = canonical.get(field)
                if value:
                    target_fields[field] = str(value)
        target_disc = str(target_fields.get("disc_number") or metadata.get("disc_number") or "1")
        target_disc_total = str(target_fields.get("disc_total") or metadata.get("disc_total") or "1")
        current_disc = str(metadata.get("disc_number") or "")
        current_disc_total = str(metadata.get("disc_total") or "")
        current_disc_pair = f"{current_disc}/{current_disc_total}" if current_disc or current_disc_total else ""
        target_disc_pair = f"{target_disc}/{target_disc_total}"
        if current_disc_pair != target_disc_pair:
            changes.append({
                "file_path": str(local.get("file_path") or ""),
                "field": "disc",
                "current": current_disc_pair,
                "target": target_disc_pair,
                "format": str(local.get("format") or ""),
            })
        for field, target in sorted(target_fields.items()):
            current = str(metadata.get(field) or "")
            if current == target:
                continue
            if field not in {"disc_number", "disc_total"}:
                changes.append({
                    "file_path": str(local.get("file_path") or ""),
                    "field": field,
                    "current": current,
                    "target": target,
                    "format": str(local.get("format") or ""),
                })
        patch_fields = {field: value for field, value in target_fields.items() if str(metadata.get(field) or "") != value}
        if patch_fields:
            patches.append({
                "file_path": str(local.get("file_path") or ""),
                "fields": patch_fields,
                "reason": "album_repair_saga_confirmed_track_metadata_normalization",
            })
    return {
        "dry_run": True,
        "apply_action": "library.update_metadata",
        "change_count": len(changes),
        "changes": changes,
        "patch_count": len(patches),
        "patches": patches,
        "out_of_scope_findings": out_of_scope,
        "requires_user_confirmation": True,
    }


def _duration_seconds(audio: Any) -> int | None:
    info = getattr(audio, "info", None)
    length = getattr(info, "length", None)
    if not length:
        return None
    try:
        return int(length)
    except Exception:
        return None


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _image_mime_from_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _extract_cover(
    audio: Any,
    tags: Any,
    *,
    warnings: list[dict[str, str]],
    include_data: bool = False,
) -> dict[str, Any]:
    pictures: list[tuple[bytes, str]] = []
    seen: set[str] = set()

    def add_picture(value: Any, *, source: str, allow_base64: bool = False) -> None:
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            data = getattr(item, "data", None)
            if data is None and isinstance(item, (bytes, bytearray, memoryview)):
                data = bytes(item)
            elif data is not None:
                try:
                    data = bytes(data)
                except Exception:
                    data = None
            if data is None and allow_base64 and isinstance(item, str):
                try:
                    data = base64.b64decode(item, validate=True)
                except Exception as exc:
                    _warn(warnings, code="cover_base64_decode_failed", detail=f"{source}:{exc.__class__.__name__}:{exc}")
                    continue
            if not data:
                continue
            mime = _image_mime_from_bytes(data)
            if not mime:
                _warn(warnings, code="cover_image_invalid", detail=f"{source}:unsupported_or_invalid_image_signature")
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            pictures.append((data, mime))

    getall = getattr(tags, "getall", None)
    if callable(getall):
        try:
            for frame in getall("APIC"):
                add_picture(frame, source="APIC")
        except Exception as exc:
            _warn(warnings, code="cover_apic_read_failed", detail=f"{exc.__class__.__name__}:{exc}")
    try:
        for pic in getattr(audio, "pictures", None) or []:
            add_picture(pic, source="pictures")
    except Exception as exc:
        _warn(warnings, code="cover_pictures_read_failed", detail=f"{exc.__class__.__name__}:{exc}")
    try:
        keys = list(tags.keys())
    except Exception as exc:
        _warn(warnings, code="cover_keys_read_failed", detail=f"{exc.__class__.__name__}:{exc}")
        keys = []
    for key in keys:
        key_text = str(key)
        if not (key_text.startswith("APIC") or key_text.lower() in {"covr", "metadata_block_picture"}):
            continue
        try:
            value = tags[key]
        except Exception as exc:
            _warn(warnings, code="cover_tag_read_failed", detail=f"{key_text}:{exc.__class__.__name__}:{exc}")
            continue
        add_picture(value, source=key_text, allow_base64=key_text.lower() == "metadata_block_picture")
    info: dict[str, Any] = {"present": bool(pictures), "count": len(pictures)}
    if pictures:
        data, mime = pictures[0]
        info.update({"mime": mime, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        if include_data:
            info["data_uri"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return info
