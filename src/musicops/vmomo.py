from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class VmomoClient(Protocol):
    def availability(self) -> tuple[str, str]: ...
    def search(self, query: str, timeout: int = 90) -> dict[str, Any]: ...
    def request_media(
        self,
        search_msg_id: int,
        index: int,
        vmomo_quality: str,
        keywords: list[str],
        timeout: int = 120,
        on_progress: ProgressCallback | None = None,
        cancel_requested: CancelCheck | None = None,
    ) -> dict[str, Any]: ...
    def stage_download(
        self,
        media_msg_id: int,
        output_dir: Path,
        timeout: float = 300,
        retries: int = 2,
        deadline_monotonic: float | None = None,
        on_progress: ProgressCallback | None = None,
        cancel_requested: CancelCheck | None = None,
    ) -> dict[str, Any]: ...


def _emit_progress(callback: ProgressCallback | None, stage: str, **payload: Any) -> None:
    if callback is not None:
        callback({"stage": stage, **payload})


def _cancelled(callback: CancelCheck | None) -> bool:
    return bool(callback and callback())


def _safe_download_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TimeoutError):
        return {"error": "attempt_timeout", "error_type": exc.__class__.__name__}
    if isinstance(exc, OSError):
        return {
            "error": "os_error",
            "error_type": exc.__class__.__name__,
            "errno": getattr(exc, "errno", None),
        }
    return {"error": "provider_exception", "error_type": exc.__class__.__name__}


def _safe_provider_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TimeoutError):
        return {"error": "provider_timeout", "error_type": exc.__class__.__name__}
    if isinstance(exc, OSError):
        return {
            "error": "provider_connection_failed",
            "error_type": exc.__class__.__name__,
            "errno": getattr(exc, "errno", None),
        }
    return {"error": "provider_exception", "error_type": exc.__class__.__name__}


def safe_candidate_id(raw: Any, default_id: str) -> str:
    value = str(raw or default_id).strip() or default_id
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value)
    return value.strip("-")[:96] or default_id


def file_facts(path: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return facts
    facts["size"] = path.stat().st_size
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    facts["sha256"] = h.hexdigest()
    facts["mime_type"] = mimetypes.guess_type(str(path))[0]
    return facts


class UnconfiguredVmomoClient:
    def __init__(self, reason: str = "telegram_provider_not_configured") -> None:
        self.reason = reason

    def availability(self) -> tuple[str, str]:
        return "unavailable", self.reason

    def _raise(self) -> None:
        raise RuntimeError(self.reason)

    def search(self, query: str, timeout: int = 90) -> dict[str, Any]:
        self._raise()

    def request_media(
        self,
        search_msg_id: int,
        index: int,
        vmomo_quality: str,
        keywords: list[str],
        timeout: int = 120,
        on_progress: ProgressCallback | None = None,
        cancel_requested: CancelCheck | None = None,
    ) -> dict[str, Any]:
        self._raise()

    def stage_download(
        self,
        media_msg_id: int,
        output_dir: Path,
        timeout: float = 300,
        retries: int = 2,
        deadline_monotonic: float | None = None,
        on_progress: ProgressCallback | None = None,
        cancel_requested: CancelCheck | None = None,
    ) -> dict[str, Any]:
        self._raise()


@dataclass
class TelethonVmomoClient:
    session_path: Path
    api_id: str
    api_hash: str
    bot: str = "VmomoVBot"
    proxy: tuple[Any, ...] | None = None

    @classmethod
    def from_env(cls, data_dir: str | Path, session_name: str = "senyao_userbot") -> "TelethonVmomoClient":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(session_name or "")):
            raise ValueError("invalid_session_name")
        safe_name = session_name
        api_id = os.environ.get("TELEGRAM_API_ID", "")
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        if not api_id or not api_hash:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
        configured_path = os.environ.get("MUSICOPS_TELEGRAM_SESSION_PATH")
        mounted_path = Path("/sessions") / f"{safe_name}.session"
        runtime_dir = Path(data_dir) / "telegram_sessions"
        runtime_path = runtime_dir / f"{safe_name}.session"
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(runtime_dir, 0o700)
        if not runtime_path.is_file():
            source_path = Path(configured_path) if configured_path else mounted_path
            if not source_path.is_file():
                raise RuntimeError("telegram_session_file_missing")
            fd, temporary_name = tempfile.mkstemp(prefix=f".{safe_name}.", suffix=".session", dir=runtime_dir)
            try:
                with os.fdopen(fd, "wb") as destination, source_path.open("rb") as source:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.chmod(temporary_name, 0o600)
                os.replace(temporary_name, runtime_path)
            except Exception:
                Path(temporary_name).unlink(missing_ok=True)
                raise
        os.chmod(runtime_path, 0o600)
        session_path = runtime_path
        proxy = None
        proxy_host = os.environ.get("PROXY_HOST")
        proxy_port = os.environ.get("PROXY_PORT")
        if proxy_host and proxy_port:
            try:
                proxy = (
                    2,  # SOCKS5; Telethon accepts the PySocks-compatible numeric value.
                    proxy_host,
                    int(proxy_port),
                    True,
                    os.environ.get("PROXY_USER") or None,
                    os.environ.get("PROXY_PASS") or None,
                )
            except Exception as exc:
                raise RuntimeError(f"invalid proxy configuration: {exc}") from exc
        return cls(session_path=session_path, api_id=api_id, api_hash=api_hash, proxy=proxy)

    def availability(self) -> tuple[str, str]:
        try:
            with self.session_path.open("r+b"):
                pass
        except OSError:
            return "unavailable", "telegram_runtime_session_not_writable"
        if not os.access(self.session_path.parent, os.W_OK):
            return "unavailable", "telegram_runtime_directory_not_writable"
        return "available", ""

    def _run(self, coro):
        return asyncio.run(coro)

    async def _client(self):
        from telethon import TelegramClient  # type: ignore

        client = TelegramClient(str(self.session_path), int(self.api_id), self.api_hash, proxy=self.proxy)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("telegram_session_not_authorized")
        return client

    def _message_to_dict(self, msg: Any) -> dict[str, Any]:
        text = getattr(msg, "message", "") or ""
        data: dict[str, Any] = {
            "id": getattr(msg, "id", None),
            "text": text,
            "date": str(getattr(msg, "date", "")),
        }
        candidates = self._parse_candidates(msg)
        if candidates:
            data["type"] = "search_results"
            data["candidates"] = candidates
        elif getattr(msg, "media", None):
            data["type"] = "media_file"
            data["file_info"] = self._file_info(msg)
        else:
            data["type"] = "message"
        return data

    def _parse_candidates(self, msg: Any) -> list[dict[str, Any]]:
        text = getattr(msg, "message", "") or ""
        rows: list[dict[str, Any]] = []
        buttons = getattr(msg, "buttons", None) or []
        button_flat = [button for row in buttons for button in row]
        for line in text.splitlines():
            normalized_line = re.sub(r"^\s*🔟", "10)", line)
            m = re.match(r"\s*(\d+)(?:\ufe0f?\u20e3|[\).、])\s*(.+)", normalized_line)
            if not m:
                continue
            idx = int(m.group(1))
            body = m.group(2).strip()
            title, artist = body, ""
            if " - " in body:
                title, artist = body.split(" - ", 1)
            elif " — " in body:
                title, artist = body.split(" — ", 1)
            song_id = ""
            if idx - 1 < len(button_flat):
                data = getattr(button_flat[idx - 1], "data", b"") or b""
                song_id = hashlib.sha1(data).hexdigest()[:16] if data else ""
            rows.append({
                "index": idx,
                "title": title.strip(),
                "artist": artist.strip(),
                "song_id": song_id,
                "msg_id": getattr(msg, "id", None),
            })
        return rows

    def _file_info(self, msg: Any) -> dict[str, Any]:
        file_obj = getattr(msg, "file", None)
        return {
            "file_name": getattr(file_obj, "name", None),
            "size": getattr(file_obj, "size", None),
            "mime_type": getattr(file_obj, "mime_type", None),
        }

    @staticmethod
    def _quality_tier(label: str) -> str | None:
        value = " ".join(str(label or "").strip().split())
        upper = value.upper()
        for tier in ("SQ", "HQ"):
            if re.search(rf"(^|[^A-Z0-9]){tier}([^A-Z0-9]|$)", upper):
                return tier
        normalized = re.sub(r"\s+", "", upper)
        if any(alias in normalized for alias in ("DEFAULT", "STANDARD", "默认", "标准", "普通")):
            return "default"
        return None

    @classmethod
    def _quality_buttons(cls, msg: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for button_row in getattr(msg, "buttons", None) or []:
            for button in button_row:
                label = str(getattr(button, "text", "") or "").strip()
                tier = cls._quality_tier(label)
                if tier:
                    rows.append({"tier": tier, "label": label, "button": button})
        return rows

    @staticmethod
    def _provider_labels(candidate: dict[str, Any]) -> list[str]:
        text = " ".join(str(candidate.get(key) or "") for key in ("title", "artist"))
        labels: list[str] = []
        if re.search(r"(^|[^A-Za-z])explicit([^A-Za-z]|$)", text, flags=re.IGNORECASE):
            labels.append("Explicit")
        return labels

    def search(self, query: str, timeout: int = 90) -> dict[str, Any]:
        async def _search():
            client = await self._client()
            try:
                sent = await client.send_message(self.bot, query)
                deadline = time.time() + timeout
                messages = []
                seen_message_ids: set[int] = set()
                while time.time() < deadline:
                    async for msg in client.iter_messages(self.bot, limit=10, min_id=sent.id):
                        data = self._message_to_dict(msg)
                        message_id = data.get("id")
                        if isinstance(message_id, int) and message_id in seen_message_ids:
                            continue
                        if isinstance(message_id, int):
                            seen_message_ids.add(message_id)
                        messages.append(data)
                        if data.get("type") == "search_results":
                            candidates = data.get("candidates") or []
                            return {
                                "success": True,
                                "query": query,
                                "sent_msg_id": sent.id,
                                "messages": messages,
                                "candidates": candidates,
                            }
                    await asyncio.sleep(2)
                return {"success": False, "query": query, "sent_msg_id": sent.id, "messages": messages[-10:], "candidates": [], "error": "search_timeout"}
            finally:
                await client.disconnect()

        return self._run(_search())

    def request_media(
        self,
        search_msg_id: int,
        index: int,
        vmomo_quality: str,
        keywords: list[str],
        timeout: int = 120,
        on_progress: ProgressCallback | None = None,
        cancel_requested: CancelCheck | None = None,
    ) -> dict[str, Any]:
        candidate_evidence: dict[str, Any] = {
            "requested_quality": vmomo_quality,
            "search_msg_id": search_msg_id,
            "candidate_index": index,
        }

        async def _request():
            _emit_progress(on_progress, "provider_connect", provider="vmomo")
            client = await self._client()
            try:
                if _cancelled(cancel_requested):
                    return {"success": False, "error": "operation_cancelled", "failure_stage": "provider_connect"}
                msg = await client.get_messages(self.bot, ids=search_msg_id)
                if not msg:
                    return {
                        "success": False,
                        "error": "search_message_not_found",
                        "failure_stage": "candidate_lookup",
                        "search_msg_id": search_msg_id,
                    }
                buttons = getattr(msg, "buttons", None) or []
                flat = [button for row in buttons for button in row]
                if index < 1 or index > len(flat):
                    return {
                        "success": False,
                        "error": "candidate_button_not_found",
                        "failure_stage": "candidate_lookup",
                        "candidate_index": index,
                        "button_count": len(flat),
                    }
                candidates = self._parse_candidates(msg)
                candidate = next((row for row in candidates if row.get("index") == index), {})
                if not candidate:
                    return {
                        "success": False,
                        "error": "candidate_identity_unavailable",
                        "failure_stage": "candidate_lookup",
                        "search_msg_id": search_msg_id,
                        "candidate_index": index,
                    }
                provider_labels = self._provider_labels(candidate)
                candidate_evidence.update(
                    {
                        "candidate": candidate,
                        "candidate_identity": {
                            "search_msg_id": search_msg_id,
                            "candidate_index": index,
                            "snapshot": candidate,
                        },
                        "provider_labels": provider_labels,
                    }
                )
                _emit_progress(
                    on_progress,
                    "candidate_selected",
                    search_msg_id=search_msg_id,
                    candidate_index=index,
                    candidate=candidate,
                    provider_labels=provider_labels,
                    requested_quality=vmomo_quality,
                )
                latest = None
                async for recent in client.iter_messages(self.bot, limit=1):
                    latest = getattr(recent, "id", None)
                if _cancelled(cancel_requested):
                    return {
                        "success": False,
                        "error": "operation_cancelled",
                        "failure_stage": "candidate_selection",
                        **candidate_evidence,
                    }
                await flat[index - 1].click()
                watermark = int(latest or search_msg_id)
                deadline = time.monotonic() + timeout
                seen_message_ids: set[int] = set()
                lowered = [k.lower() for k in keywords if k]
                selected_quality: str | None = None
                available_qualities: list[str] = []
                observed_button_labels: list[str] = []
                quality_button_clicked = False
                quality_menu_msg_id: int | None = None
                identity_mismatch: dict[str, Any] | None = None
                _emit_progress(on_progress, "quality_selection", requested_quality=vmomo_quality)
                while time.monotonic() < deadline:
                    if _cancelled(cancel_requested):
                        return {
                            "success": False,
                            "error": "operation_cancelled",
                            "failure_stage": "media_wait",
                            "selected_quality": selected_quality,
                            **candidate_evidence,
                        }
                    polled = []
                    async for response in client.iter_messages(self.bot, limit=20, min_id=watermark):
                        response_id = getattr(response, "id", None)
                        if not isinstance(response_id, int) or response_id in seen_message_ids:
                            continue
                        seen_message_ids.add(response_id)
                        polled.append(response)
                    for response in sorted(polled, key=lambda item: int(getattr(item, "id", 0))):
                        for button_row in getattr(response, "buttons", None) or []:
                            for button in button_row:
                                label = str(getattr(button, "text", "") or "").strip()
                                if label and label not in observed_button_labels:
                                    observed_button_labels.append(label)
                        controls = self._quality_buttons(response)
                        if controls and selected_quality is None:
                            available_qualities = list(dict.fromkeys(str(row["tier"]) for row in controls))
                            quality_menu_msg_id = int(getattr(response, "id", 0) or 0) or None
                            _emit_progress(
                                on_progress,
                                "quality_controls_observed",
                                requested_quality=vmomo_quality,
                                available_qualities=available_qualities,
                                observed_button_labels=observed_button_labels,
                            )
                            target = next((row for row in controls if row["tier"] == vmomo_quality), None)
                            if target is None:
                                return {
                                    "success": False,
                                    "error": "quality_selection_unavailable",
                                    "failure_stage": "quality_selection",
                                    "available_qualities": available_qualities,
                                    "observed_button_labels": observed_button_labels,
                                    **candidate_evidence,
                                }
                            await target["button"].click()
                            selected_quality = vmomo_quality
                            quality_button_clicked = True
                            _emit_progress(
                                on_progress,
                                "quality_selected",
                                requested_quality=vmomo_quality,
                                selected_quality=selected_quality,
                                quality_button_clicked=True,
                                available_qualities=available_qualities,
                            )
                            watermark = max(watermark, int(getattr(response, "id", watermark)))
                            continue
                        data = self._message_to_dict(response)
                        if data.get("type") != "media_file":
                            continue
                        if vmomo_quality in {"HQ", "SQ"} and selected_quality != vmomo_quality:
                            return {
                                "success": False,
                                "error": "quality_selection_unavailable",
                                "failure_stage": "quality_selection",
                                "available_qualities": available_qualities,
                                "observed_button_labels": observed_button_labels,
                                "media_received_before_selection": True,
                                **candidate_evidence,
                            }
                        haystack = " ".join([
                            str(data.get("text") or ""),
                            str((data.get("file_info") or {}).get("file_name") or ""),
                        ]).lower()
                        matched = [k for k in lowered if k in haystack]
                        if lowered and not matched:
                            identity_mismatch = {
                                "file_info": data.get("file_info") or {},
                                "required_keywords": keywords,
                            }
                            continue
                        selected = selected_quality or "default"
                        evidence = {
                            "requested": vmomo_quality,
                            "selected": selected,
                            "available": available_qualities,
                            "quality_button_clicked": quality_button_clicked,
                            "quality_menu_msg_id": quality_menu_msg_id,
                            "observed_button_labels": observed_button_labels,
                        }
                        _emit_progress(
                            on_progress,
                            "media_received",
                            media_msg_id=data.get("id"),
                            requested_quality=vmomo_quality,
                            selected_quality=selected,
                            quality_button_clicked=quality_button_clicked,
                        )
                        return {
                            "success": True,
                            "media_msg_id": data.get("id"),
                            "file_info": data.get("file_info") or {},
                            "matched_keywords": matched,
                            **candidate_evidence,
                            "quality_selection": evidence,
                        }
                    await asyncio.sleep(2)
                if vmomo_quality in {"HQ", "SQ"} and selected_quality is None:
                    error = "provider_timeout"
                    failure_stage = "quality_selection"
                elif identity_mismatch is not None:
                    error = "media_identity_mismatch"
                    failure_stage = "media_wait"
                else:
                    error = "media_timeout"
                    failure_stage = "media_wait"
                return {
                    "success": False,
                    "error": error,
                    "failure_stage": failure_stage,
                    "selected_quality": selected_quality,
                    "available_qualities": available_qualities,
                    "observed_button_labels": observed_button_labels,
                    "quality_button_clicked": quality_button_clicked,
                    "identity_mismatch": identity_mismatch,
                    **candidate_evidence,
                }
            finally:
                await client.disconnect()

        async def _bounded_request():
            try:
                return await asyncio.wait_for(_request(), timeout=max(0.001, float(timeout)))
            except Exception as exc:
                provider_error = _safe_provider_error(exc)
                return {
                    "success": False,
                    "error": provider_error["error"],
                    "failure_stage": "provider_io",
                    "provider_error": provider_error,
                    **candidate_evidence,
                }

        return self._run(_bounded_request())

    def stage_download(
        self,
        media_msg_id: int,
        output_dir: Path,
        timeout: float = 300,
        retries: int = 2,
        deadline_monotonic: float | None = None,
        on_progress: ProgressCallback | None = None,
        cancel_requested: CancelCheck | None = None,
    ) -> dict[str, Any]:
        async def _download():
            started_at = time.monotonic()
            deadline = deadline_monotonic or (started_at + max(0.0, float(timeout)))
            attempts_total = retries + 1
            errors: list[dict[str, Any]] = []
            _emit_progress(on_progress, "media_download_prepare", media_msg_id=media_msg_id, attempts_total=attempts_total)
            client = await self._client()
            try:
                if _cancelled(cancel_requested):
                    return {"success": False, "error": "operation_cancelled", "failure_stage": "media_download"}
                msg = await client.get_messages(self.bot, ids=media_msg_id)
                if not msg or not getattr(msg, "media", None):
                    return {
                        "success": False,
                        "error": "media_message_not_found",
                        "failure_stage": "media_download",
                        "media_msg_id": media_msg_id,
                    }
                output_dir.mkdir(parents=True, exist_ok=True)
                info = self._file_info(msg)
                expected_size = int(info.get("size") or 0) or None
                name = (info.get("file_name") or f"vmomo_{media_msg_id}").strip()
                name = re.sub(r'[\\/:*?"<>|]+', "-", name) or f"vmomo_{media_msg_id}"
                path = output_dir / name
                part_path = path.with_name(f"{path.name}.part")
                for attempt in range(1, attempts_total + 1):
                    if _cancelled(cancel_requested):
                        return {
                            "success": False,
                            "error": "operation_cancelled",
                            "failure_stage": "media_download",
                            "media_msg_id": media_msg_id,
                            "attempts": attempt - 1,
                            "errors": errors,
                        }
                    remaining_total = deadline - time.monotonic()
                    if remaining_total <= 0:
                        break
                    if expected_size and part_path.exists() and part_path.stat().st_size > expected_size:
                        part_path.unlink()
                    offset = part_path.stat().st_size if part_path.exists() else 0
                    if expected_size and offset == expected_size:
                        part_path.replace(path)
                        facts = file_facts(path)
                        return {
                            "success": True,
                            "file_path": str(path),
                            "media_msg_id": media_msg_id,
                            "attempts": attempt - 1,
                            "errors": errors,
                            "file": facts,
                            "file_info": info,
                        }
                    attempts_left = attempts_total - attempt + 1
                    attempt_budget = remaining_total / attempts_left
                    attempt_deadline = time.monotonic() + attempt_budget
                    attempt_started = time.monotonic()
                    _emit_progress(
                        on_progress,
                        "media_download",
                        media_msg_id=media_msg_id,
                        attempt=attempt,
                        attempts_total=attempts_total,
                        resumed_from=offset,
                        expected_size=expected_size,
                    )
                    try:
                        last_progress_at = 0.0
                        with part_path.open("ab" if offset else "wb") as handle:
                            iterator = client.iter_download(msg.media, offset=offset, request_size=512 * 1024)
                            while True:
                                if _cancelled(cancel_requested):
                                    return {
                                        "success": False,
                                        "error": "operation_cancelled",
                                        "failure_stage": "media_download",
                                        "media_msg_id": media_msg_id,
                                        "attempts": attempt,
                                        "errors": errors,
                                    }
                                remaining_attempt = attempt_deadline - time.monotonic()
                                if remaining_attempt <= 0:
                                    raise TimeoutError()
                                try:
                                    chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining_attempt)
                                except StopAsyncIteration:
                                    break
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                downloaded = handle.tell()
                                now = time.monotonic()
                                if now - last_progress_at >= 5 or (expected_size and downloaded >= expected_size):
                                    _emit_progress(
                                        on_progress,
                                        "media_download",
                                        media_msg_id=media_msg_id,
                                        attempt=attempt,
                                        attempts_total=attempts_total,
                                        downloaded_bytes=downloaded,
                                        expected_size=expected_size,
                                        percent=round(downloaded * 100 / expected_size, 1) if expected_size else None,
                                    )
                                    last_progress_at = now
                        actual_size = part_path.stat().st_size if part_path.exists() else 0
                        if expected_size and actual_size != expected_size:
                            errors.append(
                                {
                                    "stage": "media_download",
                                    "attempt": attempt,
                                    "error": "size_mismatch",
                                    "error_type": "DownloadSizeMismatch",
                                    "duration_seconds": round(time.monotonic() - attempt_started, 3),
                                    "partial_size": actual_size,
                                    "expected_size": expected_size,
                                }
                            )
                            continue
                        part_path.replace(path)
                        facts = file_facts(path)
                        _emit_progress(
                            on_progress,
                            "media_download_complete",
                            media_msg_id=media_msg_id,
                            attempt=attempt,
                            attempts_total=attempts_total,
                            downloaded_bytes=facts.get("size"),
                            expected_size=expected_size,
                        )
                        return {
                            "success": True,
                            "file_path": str(path),
                            "media_msg_id": media_msg_id,
                            "attempts": attempt,
                            "errors": errors,
                            "duration_seconds": round(time.monotonic() - started_at, 3),
                            "file": facts,
                            "file_info": info,
                        }
                    except Exception as exc:
                        partial_size = part_path.stat().st_size if part_path.exists() else 0
                        errors.append(
                            {
                                "stage": "media_download",
                                "attempt": attempt,
                                **_safe_download_error(exc),
                                "duration_seconds": round(time.monotonic() - attempt_started, 3),
                                "partial_size": partial_size,
                                "expected_size": expected_size,
                            }
                        )
                        _emit_progress(
                            on_progress,
                            "media_download_retry",
                            media_msg_id=media_msg_id,
                            attempt=attempt,
                            attempts_total=attempts_total,
                            error=errors[-1]["error"],
                            partial_size=partial_size,
                            expected_size=expected_size,
                        )
                deadline_exceeded = time.monotonic() >= deadline
                return {
                    "success": False,
                    "error": "download_deadline_exceeded" if deadline_exceeded else "download_failed",
                    "failure_stage": "media_download",
                    "media_msg_id": media_msg_id,
                    "attempts": len(errors),
                    "attempts_total": attempts_total,
                    "errors": errors,
                    "duration_seconds": round(time.monotonic() - started_at, 3),
                    "expected_size": expected_size,
                    "partial_size": part_path.stat().st_size if part_path.exists() else 0,
                    "file_info": info,
                }
            finally:
                await client.disconnect()

        async def _bounded_download():
            remaining = (
                deadline_monotonic - time.monotonic()
                if deadline_monotonic is not None
                else max(0.0, float(timeout))
            )
            if remaining <= 0:
                return {
                    "success": False,
                    "error": "download_deadline_exceeded",
                    "failure_stage": "media_download",
                    "media_msg_id": media_msg_id,
                    "attempts": 0,
                    "attempts_total": retries + 1,
                    "errors": [],
                }
            try:
                return await asyncio.wait_for(_download(), timeout=max(0.001, remaining))
            except Exception as exc:
                provider_error = _safe_provider_error(exc)
                error = "download_deadline_exceeded" if provider_error["error"] == "provider_timeout" else "download_failed"
                return {
                    "success": False,
                    "error": error,
                    "failure_stage": "media_download",
                    "media_msg_id": media_msg_id,
                    "attempts_total": retries + 1,
                    "errors": [
                        {
                            "stage": "media_download",
                            **provider_error,
                        }
                    ],
                }

        return self._run(_bounded_download())
