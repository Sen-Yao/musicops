from __future__ import annotations

import base64
import copy
import hashlib
import os
import time

import pytest

from musicops.approval_renderer import render_approval
from musicops.approval_renderer import cleanup_default_reviews, default_review_path


def test_renderer_shows_fixed_hashes_permanent_delete_and_raw_data(tmp_path):
    output = tmp_path / "delete-review.html"
    approval = {
        "approval_id": "approval_1",
        "action": "library.delete",
        "intent_sha256": "a" * 64,
        "preview": {
            "fixed_batch_sha256": "b" * 64,
            "items": [{
                "file_path": "Artist/Album/Song.flac",
                "permanent": True,
                "before": {"sha256": "c" * 64, "size": 123, "metadata": {"title": "Song", "artist": "Artist"}},
            }],
        },
    }
    render_approval(approval, output)
    page = output.read_text(encoding="utf-8")
    assert "永久删除" in page
    assert "Artist/Album/Song.flac" in page
    assert "a" * 64 in page and "b" * 64 in page and "c" * 64 in page
    assert "{{" not in page


def test_renderer_renders_full_target_lyrics_without_modifying_payload(tmp_path):
    output = tmp_path / "lyrics-review.html"
    approval = {
        "approval_id": "approval_lyrics",
        "action": "library.update_lyrics",
        "intent_sha256": "a" * 64,
        "preview": {
            "fixed_batch_sha256": "b" * 64,
            "items": [{
                "file_path": "Song.flac",
                "before": {"lyrics": {"text": "[00:01]before", "sha256": "c" * 64, "line_count": 1, "length": 15}},
                "target": {"lyrics_text": "[00:01]after", "lyrics_sha256": "d" * 64, "line_count": 1, "length": 14},
            }],
        },
    }
    render_approval(approval, output)
    page = output.read_text(encoding="utf-8")
    assert "[00:01]before" in page
    assert "[00:01]after" in page
    assert approval["preview"]["items"][0]["target"]["lyrics_text"] == "[00:01]after"


def _lyrics_item(name: str = "Song.flac") -> dict:
    return {
        "file_path": name,
        "before": {
            "sha256": "e" * 64,
            "metadata": {
                "title": "Love Song",
                "artist": "方大同",
                "album": "The Soulboy Collection",
                "date": "2013",
            },
            "duration_seconds": 269,
            "lyrics": {
                "text": "[00:01.00]旧句\n[00:02.00]保留句\n[00:04.00]删除句",
                "sha256": "c" * 64,
                "line_count": 3,
                "length": 44,
            },
            "cover": {"present": True, "sha256": "f" * 64, "size": 1000, "mime": "image/jpeg"},
        },
        "target": {
            "lyrics_text": "[ar:方大同]\n[00:01.00]新句\n[00:01.00]New line\n[00:02.00]保留句\n[00:03.00]新增句\n未定位尾行",
            "lyrics_sha256": "d" * 64,
            "line_count": 6,
            "length": 85,
        },
    }


def _lyrics_approval(*items: dict) -> dict:
    return {
        "approval_id": "approval_lyrics",
        "action": "library.update_lyrics",
        "intent_sha256": "a" * 64,
        "expires_at": 2_000_000_000,
        "created_at": 1_900_000_000,
        "preview": {
            "fixed_batch_sha256": "b" * 64,
            "items": list(items or [_lyrics_item()]),
        },
    }


def _import_item(
    *,
    lyrics_status: str = "translated",
    lyrics_text: str = "[00:01.000]Original\n[00:01.000]译文\n[00:02.000]",
    cover_status: str = "valid",
    content_rating: str = "explicit",
    mask_resolution: bool = False,
) -> dict:
    cover_bytes = b"verified-cover"
    cover_sha256 = hashlib.sha256(cover_bytes).hexdigest()
    cover = {
        "present": cover_status != "missing",
        "status": "available" if cover_status != "missing" else "unavailable",
        "data_uri": "data:image/jpeg;base64," + base64.b64encode(cover_bytes).decode("ascii"),
        "sha256": cover_sha256 if cover_status != "mismatch" else "f" * 64,
        "media_type": "image/jpeg",
        "size": len(cover_bytes),
    }
    if cover_status == "missing":
        cover.update({"data_uri": "", "sha256": "", "reason": "No cover returned"})
    masked_resolution = {}
    if mask_resolution:
        masked_resolution = {
            "operation_id": "op_mask_resolution",
            "input_canonical_lrc_sha256": "7" * 64,
            "resolved_canonical_lrc_sha256": "8" * 64,
            "resolutions": [{
                "canonical_timestamp": "00:42.000",
                "original_text": "****",
                "replacement_text": "heard",
                "method": "agent_resolved_with_stt_hint",
                "stt_operation_id": "op_stt_hint",
            }],
        }
    return {
        "target_file_path": "Fukk a Interview - Future.flac",
        "target_before": {"exists": False, "file_path": "Fukk a Interview - Future.flac"},
        "import_profile": "normalized_staged",
        "staged_file_id": "temp_staged",
        "staged_file": {
            "sha256": "1" * 64,
            "size": 123456,
            "expires_at": 2_000_000_000,
            "audio": {"format": "flac", "duration_seconds": 157},
            "tag_audit": {"tag_keys": ["title", "artist", "lyrics"], "unexpected_tag_keys": []},
        },
        "source": {
            "sha256": "2" * 64,
            "temporary_file_id": "temp_source",
            "vmomo_quality": "SQ",
            "provider_labels": ["Explicit"],
            "quality_selection": {"requested": "SQ", "selected": "SQ", "available": ["SQ"]},
            "download_candidate": {
                "title": "Fukk A Interview (Explicit)",
                "artist": "Future",
                "song_id": "song-1",
                "index": 5,
            },
        },
        "canonical_metadata": {
            "title": "Fukk a Interview",
            "artist": "Future",
            "album": "The Real Me",
            "album_artist": "Future",
            "date": "2026-07-10",
            "track_number": "1",
            "track_total": "22",
            "disc_number": "1",
            "disc_total": "1",
        },
        "content_rating": {
            "value": content_rating,
            "source": "vmomo_original_title" if content_rating == "explicit" else "bounded_stt",
            "reason": "Bound evidence",
            "operation_id": "op_rating" if content_rating == "unknown" else "",
        },
        "lyrics": {
            "status": lyrics_status,
            "lyrics_text": lyrics_text,
            "line_count": len(lyrics_text.splitlines()),
            "final_sha256": "3" * 64,
            "canonical_sha256": "4" * 64,
            "translated_sha256": "5" * 64 if lyrics_status == "translated" else "",
            "source_operation_id": "op_lyrics",
            "reason": "No compatible lyrics found" if not lyrics_text else "Reviewed lyrics evidence",
            "evidence": [{"operation_id": "op_lyrics", "action": "lyrics.fetch", "status": "succeeded"}],
            "masked_lyrics_resolution": masked_resolution,
        },
        "cover": cover,
        "manifest_sha256": "6" * 64,
        "provider_track_id": "track-1",
        "manifest": {
            "provider": "musicbrainz",
            "provider_release_id": "release-1",
            "release_group_id": "release-group-1",
            "sources": [
                {"provider": "musicbrainz", "provider_release_id": "release-1"},
                {"provider": "netease", "provider_release_id": "album-2"},
            ],
        },
    }


def _import_approval(item: dict | None = None) -> dict:
    return {
        "approval_id": "approval_import",
        "action": "library.import",
        "intent_sha256": "a" * 64,
        "expires_at": 2_000_000_000,
        "created_at": 1_900_000_000,
        "preview": {
            "fixed_batch_sha256": "b" * 64,
            "items": [item or _import_item()],
        },
    }


def test_lyrics_renderer_restores_content_first_template_and_structured_diff(tmp_path):
    approval = _lyrics_approval(_lyrics_item())
    original = copy.deepcopy(approval)
    output = tmp_path / "lyrics-review.html"
    render_approval(
        approval,
        output,
        cover_assets={
            "Song.flac": {
                "data_uri": "data:image/jpeg;base64,/9j/2Q==",
                "verified": True,
                "status": "verified",
            }
        },
    )

    page = output.read_text(encoding="utf-8")
    assert 'class="lyrics-review"' in page
    assert "最终歌词" in page
    assert "变更对比" in page
    assert "原始数据" in page
    assert "审批凭证" in page
    assert "<details" in page and "approval_lyrics" in page
    assert 'data-change="changed"' in page
    assert 'data-change="added"' in page
    assert 'data-change="removed"' in page
    assert 'data-change="unchanged"' in page
    assert "仅变更" in page and "全部" in page
    assert page.index("新句") < page.index("New line")
    assert "未定位内容" in page
    assert "[ar:方大同]" in page
    assert "未定位尾行" in page
    assert 'src="data:image/jpeg;base64,/9j/2Q=="' in page
    assert "{{" not in page
    assert approval == original


def test_lyrics_renderer_keeps_exact_target_in_raw_view(tmp_path):
    target = "[00:01.00]A & B\n\n[offset:100]\n<script>alert(1)</script>"
    item = _lyrics_item()
    item["target"]["lyrics_text"] = target
    approval = _lyrics_approval(item)
    output = tmp_path / "lyrics-review.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert target not in page
    assert "[00:01.00]A &amp; B" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    assert "格式提醒" in page


def test_lyrics_renderer_builds_one_batch_page_with_song_navigation(tmp_path):
    first = _lyrics_item("One.flac")
    first["before"]["metadata"]["title"] = "One"
    second = _lyrics_item("Two.flac")
    second["before"]["metadata"]["title"] = "Two"
    approval = _lyrics_approval(first, second)
    output = tmp_path / "batch-review.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert page.count('class="song-panel') == 2
    assert 'data-song-target="song-0"' in page
    assert 'data-song-target="song-1"' in page
    assert 'id="batch-song-select"' in page
    assert "固定批次 · 2 首" in page


def test_import_renderer_uses_lyrics_template_with_dual_views_and_no_diff(tmp_path):
    approval = _import_approval()
    original = copy.deepcopy(approval)
    output = tmp_path / "import-review.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert 'class="lyrics-review"' in page
    assert "<title>歌词审批 - Fukk a Interview</title>" in page
    assert page.count('data-review-view-target="') == 2
    assert "歌词审计" in page and "导入审批" in page
    assert "最终歌词" in page and "原始数据" in page
    assert "变更对比" not in page
    assert 'data-timestamp="00:01.000"' in page
    assert 'data-timestamp="00:02.000"' in page
    assert "空白歌词行" not in page
    assert '<p class="lyric-primary"></p>' in page
    assert "[00:01.000]Original" in page
    assert "normalized_staged" in page
    for expected in (
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "4" * 64,
        "5" * 64,
        "6" * 64,
        hashlib.sha256(b"verified-cover").hexdigest(),
        "release-1",
        "netease",
        "album-2",
        "track-1",
        "SQ",
        "explicit",
    ):
        assert expected in page
    assert 'src="data:image/jpeg;base64,' in page
    assert "approval_import" in page and "a" * 64 in page and "b" * 64 in page
    assert "审批期限" in page and "无时间限制" in page
    assert "已过期" not in page
    assert "{{" not in page
    assert approval == original


def test_import_raw_view_escapes_exact_target_lrc(tmp_path):
    target = "[00:01.000]A & B\n[offset:100]\n<script>alert(1)</script>"
    approval = _import_approval(_import_item(lyrics_text=target))
    output = tmp_path / "escaped-import.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert target not in page
    assert "[00:01.000]A &amp; B" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page


def test_import_renderer_shows_compact_mask_resolution_audit(tmp_path):
    approval = _import_approval(_import_item(mask_resolution=True))
    output = tmp_path / "mask-evidence.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    for expected in (
        "歌词遮罩还原",
        "Canonical 绑定",
        "****",
        "heard",
        "agent_resolved_with_stt_hint",
        "op_stt_hint",
        "7" * 64,
        "8" * 64,
    ):
        assert expected in page
    for removed in ("Evidence bundle", "Confidence", "STT clip", "Provider / model", "STT 原始结果"):
        assert removed not in page


@pytest.mark.parametrize("lyrics_status", ["unavailable", "instrumental"])
def test_import_without_lyrics_defaults_to_import_view_and_explains_reason(tmp_path, lyrics_status):
    approval = _import_approval(_import_item(lyrics_status=lyrics_status, lyrics_text="", cover_status="missing"))
    output = tmp_path / f"{lyrics_status}.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert 'class="review-mode-tab active" role="tab" aria-selected="true" data-review-view-target="song-0-import-review"' in page
    assert "没有歌词写入" in page
    assert lyrics_status in page
    assert "No compatible lyrics found" in page
    assert "没有嵌入封面" in page


@pytest.mark.parametrize("lyrics_status", ["original", "translation_failed"])
def test_import_with_original_lyrics_keeps_lyrics_view_as_default(tmp_path, lyrics_status):
    approval = _import_approval(_import_item(lyrics_status=lyrics_status, lyrics_text="[00:01.000]Original"))
    output = tmp_path / f"{lyrics_status}.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert 'class="review-mode-tab active" role="tab" aria-selected="true" data-review-view-target="song-0-lyrics-review"' in page
    assert "[00:01.000]Original" in page


def test_import_cover_hash_mismatch_uses_placeholder_and_reports_warning(tmp_path):
    approval = _import_approval(_import_item(cover_status="mismatch", content_rating="unknown"))
    output = tmp_path / "mismatch.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert 'src="data:image/jpeg;base64,' not in page
    assert "封面与审批快照不一致" in page
    assert "封面数据未通过本地 SHA-256 校验" in page
    assert "Explicit/Clean 状态无法确定" in page


def test_renderer_writes_private_files_and_cleanup_only_removes_default_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICOPS_REVIEW_DIR", str(tmp_path / "reviews"))
    approval = _lyrics_approval(_lyrics_item())
    default_output = default_review_path("approval_lyrics")
    explicit_output = tmp_path / "keep.html"

    render_approval(approval, default_output)
    render_approval(approval, explicit_output)

    assert os.stat(default_output).st_mode & 0o777 == 0o600
    assert os.stat(explicit_output).st_mode & 0o777 == 0o600
    old = time.time() - 25 * 3600
    os.utime(default_output, (old, old))
    os.utime(explicit_output, (old, old))
    result = cleanup_default_reviews(max_age_hours=24, now=time.time())

    assert result["deleted_count"] == 1
    assert not default_output.exists()
    assert explicit_output.exists()


def test_audio_only_add_renderer_warns_that_normalization_is_not_included(tmp_path):
    approval = {
        "approval_id": "approval_audio_only",
        "action": "library.add",
        "intent_sha256": "a" * 64,
        "preview": {
            "fixed_batch_sha256": "b" * 64,
            "items": [{
                "temporary_file_id": "temp_1",
                "import_profile": "audio_only",
                "target_file_path": "Song - Artist.flac",
                "source": {"sha256": "c" * 64, "size": 123},
            }],
        },
    }
    output = tmp_path / "audio-only-review.html"

    render_approval(approval, output)

    page = output.read_text(encoding="utf-8")
    assert "Audio-only import" in page
    assert "audio_only" in page
    assert "不会规范化 metadata、歌词或封面" in page
