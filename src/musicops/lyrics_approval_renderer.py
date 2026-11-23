from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
from importlib.resources import files
import json
from pathlib import Path
import re
import time
from typing import Any


_TIMESTAMP = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>[0-5]\d)(?:[.:](?P<fraction>\d{1,3}))?\]")
_LEADING_TIMESTAMPS = re.compile(
    r"^(?P<timestamps>(?:\[\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?\])+)(?P<text>.*)$"
)


@dataclass(frozen=True)
class LyricGroup:
    key_ms: int
    timestamp: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class UntimedLine:
    line_number: int
    text: str


@dataclass(frozen=True)
class ParsedLyrics:
    groups: tuple[LyricGroup, ...]
    untimed: tuple[UntimedLine, ...]


@dataclass(frozen=True)
class DiffRow:
    timestamp: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    change: str


@dataclass(frozen=True)
class ReviewItem:
    source: dict[str, Any]
    file_path: str
    metadata: dict[str, Any]
    duration_label: str
    current_text: str
    target_text: str
    lyrics_status: str
    lyrics_reason: str
    show_diff: bool
    show_current_raw: bool
    import_item: bool


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _timestamp_parts(value: str) -> tuple[int, str]:
    match = _TIMESTAMP.fullmatch(value)
    if not match:
        raise ValueError(f"invalid timestamp: {value}")
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    fraction = match.group("fraction") or "0"
    milliseconds = int(fraction.ljust(3, "0")[:3])
    key_ms = ((minutes * 60) + seconds) * 1000 + milliseconds
    return key_ms, value[1:-1]


def parse_lrc(text: str) -> ParsedLyrics:
    grouped: dict[int, dict[str, Any]] = {}
    untimed: list[UntimedLine] = []
    for line_number, raw_line in enumerate(str(text or "").split("\n"), start=1):
        match = _LEADING_TIMESTAMPS.match(raw_line)
        if not match:
            untimed.append(UntimedLine(line_number=line_number, text=raw_line))
            continue
        lyric_text = match.group("text").strip()
        timestamp_tokens = [token.group(0) for token in _TIMESTAMP.finditer(match.group("timestamps"))]
        if not timestamp_tokens:
            untimed.append(UntimedLine(line_number=line_number, text=raw_line))
            continue
        for token in timestamp_tokens:
            key_ms, display = _timestamp_parts(token)
            group = grouped.setdefault(key_ms, {"timestamp": display, "lines": []})
            group["lines"].append(lyric_text)
    groups = tuple(
        LyricGroup(key_ms=key, timestamp=row["timestamp"], lines=tuple(row["lines"]))
        for key, row in sorted(grouped.items())
    )
    return ParsedLyrics(groups=groups, untimed=tuple(untimed))


def diff_lrc(before: ParsedLyrics, after: ParsedLyrics) -> tuple[DiffRow, ...]:
    before_by_key = {group.key_ms: group for group in before.groups}
    after_by_key = {group.key_ms: group for group in after.groups}
    rows: list[DiffRow] = []
    for key in sorted(set(before_by_key) | set(after_by_key)):
        old = before_by_key.get(key)
        new = after_by_key.get(key)
        if old is None:
            change = "added"
        elif new is None:
            change = "removed"
        elif old.lines != new.lines:
            change = "changed"
        else:
            change = "unchanged"
        rows.append(
            DiffRow(
                timestamp=(new or old).timestamp,
                before=old.lines if old else (),
                after=new.lines if new else (),
                change=change,
            )
        )
    return tuple(rows)


def _line_stack(lines: tuple[str, ...]) -> str:
    if not lines:
        return '<p class="diff-empty">—</p>'
    rendered = []
    for index, line in enumerate(lines):
        css = "lyric-primary" if index == 0 else "lyric-companion"
        rendered.append(f'<p class="{css}">{_e(line)}</p>')
    return "".join(rendered)


def _render_final(parsed: ParsedLyrics, *, empty_message: str = "") -> str:
    if not parsed.groups:
        message = empty_message or "没有可按时间戳展示的歌词；请检查下方未定位内容和原始数据。"
        return f'<div class="empty-state">{_e(message)}</div>'
    blocks = []
    for group in parsed.groups:
        blocks.append(
            f'<section class="lyric-pair" data-timestamp="{_e(group.timestamp)}">'
            f'<span class="timestamp">{_e(group.timestamp)}</span>'
            f'<div class="lyric-copy">{_line_stack(group.lines)}</div>'
            "</section>"
        )
    return "".join(blocks)


def _render_untimed(parsed: ParsedLyrics) -> str:
    meaningful = list(parsed.untimed)
    while meaningful and not meaningful[0].text:
        meaningful.pop(0)
    while meaningful and not meaningful[-1].text:
        meaningful.pop()
    if not meaningful:
        return ""
    rows = "".join(
        '<div class="untimed-row">'
        f'<span class="untimed-number">L{row.line_number}</span>'
        f'<code>{_e(row.text) if row.text else "空行"}</code>'
        "</div>"
        for row in meaningful
    )
    return (
        '<section class="format-warning" role="note">'
        "<div><strong>格式提醒</strong><p>以下内容没有可识别的时间戳，将按原始顺序写入。</p></div>"
        f'<details><summary>未定位内容 · {len(meaningful)} 行</summary><div class="untimed-list">{rows}</div></details>'
        "</section>"
    )


def _render_diff(rows: tuple[DiffRow, ...], panel_id: str) -> str:
    labels = {"added": "新增", "removed": "删除", "changed": "变化", "unchanged": "未变化"}
    body = []
    for row in rows:
        body.append(
            f'<article class="diff-row" data-change="{row.change}">'
            '<header class="diff-time">'
            f'<span class="timestamp">{_e(row.timestamp)}</span>'
            f'<span class="change-label">{labels[row.change]}</span>'
            "</header>"
            '<div class="diff-columns">'
            f'<section><h4>当前</h4>{_line_stack(row.before)}</section>'
            f'<section><h4>目标</h4>{_line_stack(row.after)}</section>'
            "</div></article>"
        )
    diff_list = "".join(body) or '<div class="empty-state">没有时间戳歌词可比较。</div>'
    return (
        f'<div class="diff-view" id="{panel_id}-diff-view" data-mode="changes">'
        '<div class="diff-toolbar" aria-label="变更显示范围">'
        f'<button type="button" class="mode-button active" data-diff-mode="changes" data-diff-target="{panel_id}-diff-view">仅变更</button>'
        f'<button type="button" class="mode-button" data-diff-mode="all" data-diff-target="{panel_id}-diff-view">全部</button>'
        "</div>"
        f'<div class="diff-list">{diff_list}</div>'
        "</div>"
    )


def _format_duration(value: Any) -> str:
    try:
        seconds = max(0, int(value))
    except Exception:
        return "—"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _cover_markup(file_path: str, cover_assets: dict[str, dict[str, Any]]) -> str:
    asset = cover_assets.get(file_path) or {}
    if asset.get("verified") and str(asset.get("data_uri") or "").startswith("data:image/"):
        return (
            '<div class="cover-wrapper">'
            f'<img class="cover" src="{_e(asset["data_uri"])}" alt="已校验的专辑封面">'
            '<span class="cover-state verified">封面已校验</span>'
            "</div>"
        )
    status = str(asset.get("status") or "not_requested")
    labels = {
        "hash_mismatch": "封面与审批快照不一致",
        "budget_exceeded": "批次封面预算已用尽",
        "read_failed": "封面读取失败",
        "invalid_data": "封面数据无法校验",
        "missing": "没有嵌入封面",
        "pillow_unavailable": "缩略图组件不可用",
        "not_requested": "没有嵌入封面",
    }
    return (
        '<div class="cover-wrapper">'
        '<div class="cover cover-placeholder" role="img" aria-label="无可用封面"><span>♪</span></div>'
        f'<span class="cover-state">{_e(labels.get(status, status))}</span>'
        "</div>"
    )


def _lyrics_review_item(item: dict[str, Any]) -> ReviewItem:
    before = item.get("before") or {}
    target = item.get("target") or {}
    return ReviewItem(
        source=item,
        file_path=str(item.get("file_path") or "item"),
        metadata=dict(before.get("metadata") or {}),
        duration_label=str(before.get("duration") or _format_duration(before.get("duration_seconds"))),
        current_text=str((before.get("lyrics") or {}).get("text") or ""),
        target_text=str(target.get("lyrics_text") or ""),
        lyrics_status="updated",
        lyrics_reason="",
        show_diff=True,
        show_current_raw=True,
        import_item=False,
    )


def _import_review_item(item: dict[str, Any]) -> ReviewItem:
    staged = item.get("staged_file") or {}
    lyrics = item.get("lyrics") or {}
    return ReviewItem(
        source=item,
        file_path=str(item.get("target_file_path") or "item"),
        metadata=dict(item.get("canonical_metadata") or {}),
        duration_label=_format_duration((staged.get("audio") or {}).get("duration_seconds")),
        current_text="",
        target_text=str(lyrics.get("lyrics_text") or ""),
        lyrics_status=str(lyrics.get("status") or "unavailable"),
        lyrics_reason=str(lyrics.get("reason") or ""),
        show_diff=False,
        show_current_raw=False,
        import_item=True,
    )


def _import_cover_asset(item: dict[str, Any]) -> dict[str, Any]:
    cover = item.get("cover") or {}
    if not cover.get("present"):
        return {"verified": False, "status": "missing"}
    data_uri = str(cover.get("data_uri") or "")
    expected = str(cover.get("sha256") or "").lower()
    prefix, separator, encoded = data_uri.partition(",")
    if not separator or not prefix.startswith("data:image/") or ";base64" not in prefix:
        return {"verified": False, "status": "invalid_data"}
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except Exception:
        return {"verified": False, "status": "invalid_data"}
    if not expected or hashlib.sha256(decoded).hexdigest() != expected:
        return {"verified": False, "status": "hash_mismatch"}
    return {"verified": True, "status": "verified", "data_uri": data_uri}


def _detail_table(rows: list[tuple[str, Any]]) -> str:
    body = "".join(f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>" for label, value in rows)
    return f'<table class="evidence-table"><tbody>{body}</tbody></table>'


def _joined(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "")


def _render_masked_lyrics_resolutions(resolution: dict[str, Any]) -> str:
    resolutions = list(resolution.get("resolutions") or [])
    if not resolution and not resolutions:
        return ""
    overview = _detail_table([
        ("Resolution operation", resolution.get("operation_id", "")),
        ("原 Canonical SHA-256", resolution.get("input_canonical_lrc_sha256", "")),
        ("解析后 Canonical SHA-256", resolution.get("resolved_canonical_lrc_sha256", "")),
    ])
    rows = []
    for index, item in enumerate(resolutions, start=1):
        detail_rows = [
            ("原始遮罩", item.get("original_text", "")),
            ("解析词", item.get("replacement_text", "")),
            ("Resolution method", item.get("method", "")),
            ("STT operation", item.get("stt_operation_id") or "—"),
        ]
        rows.append(
            '<section class="evidence-section">'
            f'<h3>遮罩 {index} · {_e(item.get("canonical_timestamp") or "未定位")}</h3>'
            f'{_detail_table(detail_rows)}</section>'
        )
    return (
        '<section class="mask-evidence"><h2>歌词遮罩还原</h2>'
        f'<section class="evidence-section"><h3>Canonical 绑定</h3>{overview}</section>{"".join(rows)}</section>'
    )


def _render_import_details(item: dict[str, Any], cover_asset: dict[str, Any]) -> str:
    source = item.get("source") or {}
    candidate = source.get("download_candidate") or {}
    quality = source.get("quality_selection") or {}
    staged = item.get("staged_file") or {}
    audio = staged.get("audio") or {}
    tag_audit = staged.get("tag_audit") or {}
    metadata = item.get("canonical_metadata") or {}
    manifest = item.get("manifest") or {}
    manifest_sources = list(manifest.get("sources") or [])
    if not manifest_sources and manifest.get("provider"):
        manifest_sources = [{
            "provider": manifest.get("provider"),
            "provider_release_id": manifest.get("provider_release_id"),
        }]
    source_names = list(dict.fromkeys(str(row.get("provider") or "") for row in manifest_sources if row.get("provider")))
    source_ids = [
        f"{row.get('provider')}:{row.get('provider_release_id')}"
        for row in manifest_sources
        if row.get("provider") and row.get("provider_release_id")
    ]
    rating = item.get("content_rating") or {}
    lyrics = item.get("lyrics") or {}
    masked_resolution = lyrics.get("masked_lyrics_resolution") or {}
    cover = item.get("cover") or {}
    target_before = item.get("target_before") or {}

    warnings: list[str] = []
    identity_match = source.get("identity_match") or {}
    if identity_match and not identity_match.get("compatible"):
        differences = "; ".join(
            f"{field}: {row.get('actual') or '空'} → {row.get('expected') or '空'} ({row.get('status')})"
            for field, row in (identity_match.get("fields") or {}).items()
            if row.get("status") in {"mismatch", "ambiguous_truncation"}
        )
        warnings.append(f"下载候选与正式元数据的文字身份比较存在差异，未经自动确认。{differences}")
    if str(rating.get("value") or "") == "unknown":
        warnings.append("Explicit/Clean 状态无法确定；不得将该音频描述为 Clean。")
    if str(lyrics.get("status") or "") in {"unavailable", "translation_failed"}:
        warnings.append(f"歌词状态：{lyrics.get('status')}。{lyrics.get('reason') or '没有完整双语歌词。'}")
    if not cover.get("present"):
        warnings.append(f"封面不可用。{cover.get('reason') or '本次发布不会写入封面。'}")
    elif not cover_asset.get("verified"):
        warnings.append("封面数据未通过本地 SHA-256 校验，审计页不会显示该图片。")
    if source.get("quality_filename_warning"):
        warning = source["quality_filename_warning"]
        warnings.append(
            f"文件名包含 {warning.get('filename_quality')}，但音质以 Vmomo 实际选中的 "
            f"{warning.get('selected_quality')} 为准。"
        )
    for warning in item.get("warnings") or []:
        warnings.append(f"{warning.get("code")}: {json.dumps(warning, ensure_ascii=False)}")
    warning_markup = "".join(f'<div class="import-warning">{_e(value)}</div>' for value in warnings)

    metadata_rows = [(str(key), value) for key, value in metadata.items()]
    evidence_ids = [row.get("operation_id") for row in list(lyrics.get("evidence") or []) if row.get("operation_id")]
    sections = [
        ("发布目标", [
            ("Action", "library.import"),
            ("导入配置", item.get("import_profile", "")),
            ("目标路径", item.get("target_file_path", "")),
            ("目标当前状态", "已存在" if target_before.get("exists") else "不存在"),
            ("Staged file ID", item.get("staged_file_id", "")),
            ("Staged 到期时间", _format_timestamp(staged.get("expires_at"))),
        ]),
        ("下载来源与音质", [
            ("候选标题", candidate.get("title", "")),
            ("候选艺术家", candidate.get("artist", "")),
            ("Song ID", candidate.get("song_id", "")),
            ("候选索引", candidate.get("index", "")),
            ("Provider labels", _joined(source.get("provider_labels") or [])),
            ("Vmomo 请求音质", quality.get("requested", "")),
            ("Vmomo 选中音质", source.get("vmomo_quality") or quality.get("selected", "")),
            ("可用音质", _joined(quality.get("available") or [])),
            ("文件名质量提示", (source.get("quality_filename_warning") or {}).get("code", "")),
            ("原始音频 SHA-256", source.get("sha256", "")),
            ("源临时文件", source.get("temporary_file_id", "")),
        ]),
        ("最终文件", [
            ("Staged SHA-256", staged.get("sha256", "")),
            ("文件大小", staged.get("size", "")),
            ("格式", audio.get("format", "")),
            ("时长", _format_duration(audio.get("duration_seconds"))),
            ("写入标签", _joined(tag_audit.get("tag_keys") or [])),
            ("非允许标签", _joined(tag_audit.get("unexpected_tag_keys") or []) or "无"),
        ]),
        ("Canonical metadata", metadata_rows),
        ("Content rating", [
            ("结论", rating.get("value", "")),
            ("证据来源", rating.get("source", "")),
            ("理由", rating.get("reason", "")),
            ("STT operation", rating.get("operation_id", "")),
        ]),
        ("歌词", [
            ("状态", lyrics.get("status", "")),
            ("行数", lyrics.get("line_count", "")),
            ("最终歌词 SHA-256", lyrics.get("final_sha256", "")),
            ("Canonical SHA-256", lyrics.get("canonical_sha256", "")),
            ("译文 SHA-256", lyrics.get("translated_sha256", "")),
            ("遮罩解析 SHA-256", masked_resolution.get("resolved_canonical_lrc_sha256", "")),
            ("来源 operation", lyrics.get("source_operation_id", "")),
            ("证据 operations", _joined(evidence_ids)),
            ("说明", lyrics.get("reason", "")),
        ]),
        ("封面", [
            ("状态", cover.get("status", "")),
            ("审计页校验", cover_asset.get("status", "")),
            ("SHA-256", cover.get("sha256", "")),
            ("媒体类型", cover.get("media_type", "")),
            ("大小", cover.get("size", "")),
        ]),
        ("Canonical 实体", [
            ("Manifest SHA-256", item.get("manifest_sha256", "")),
            ("Metadata providers", _joined(source_names)),
            ("Provider entity IDs", _joined(source_ids)),
            ("Release ID", manifest.get("provider_release_id", "")),
            ("Release Group ID", manifest.get("release_group_id", "")),
            ("Track ID", item.get("provider_track_id", "")),
            ("Manifest provider", manifest.get("provider", "")),
        ]),
    ]
    section_markup = "".join(
        f'<section class="evidence-section"><h3>{_e(title)}</h3>{_detail_table(rows)}</section>'
        for title, rows in sections
    )
    return (
        '<section class="import-review">'
        '<header class="import-heading"><p class="song-kicker">library.import</p><h2>导入审批</h2></header>'
        f'{warning_markup}<div class="evidence-sections">{section_markup}'
        f'{_render_masked_lyrics_resolutions(masked_resolution)}</div></section>'
    )


def _render_song(
    review: ReviewItem,
    *,
    index: int,
    active: bool,
    cover_assets: dict[str, dict[str, Any]],
) -> str:
    title = review.metadata.get("title") or Path(review.file_path).stem
    artist = review.metadata.get("artist") or "未知艺术家"
    current = parse_lrc(review.current_text)
    final = parse_lrc(review.target_text)
    diff = diff_lrc(current, final)
    panel_id = f"song-{index}"
    active_class = " active" if active else ""
    hidden = "" if active else " hidden"
    raw_sections = [
        f'<section><h3>目标 LRC</h3><pre class="raw-lyrics">{_e(review.target_text)}</pre></section>'
    ]
    if review.show_current_raw:
        raw_sections.append(
            f'<section><h3>当前 LRC</h3><pre class="raw-lyrics">{_e(review.current_text)}</pre></section>'
        )
    raw_grid_class = "raw-grid" if len(raw_sections) > 1 else "raw-grid single"
    item_json = ""
    if not review.import_item:
        raw_item = json.dumps(review.source, ensure_ascii=False, indent=2)
        item_json = (
            '<details class="item-json"><summary>本项目原始审批数据</summary>'
            f'<pre class="raw-lyrics">{_e(raw_item)}</pre></details>'
        )
    diff_tab = (
        f'<button type="button" class="tab" role="tab" aria-selected="false" '
        f'data-tab-target="{panel_id}-diff">变更对比</button>'
        if review.show_diff
        else ""
    )
    diff_panel = (
        f'<div class="lyrics-panel" id="{panel_id}-diff" data-tab-panel hidden>{_render_diff(diff, panel_id)}</div>'
        if review.show_diff
        else ""
    )
    empty_message = ""
    if not review.target_text:
        reason = review.lyrics_reason or "没有可写入的歌词。"
        empty_message = f"没有歌词写入。歌词状态：{review.lyrics_status}。{reason}"
    lyrics_review = (
        f'{_render_untimed(final)}'
        '<nav class="tabs lyrics-tabs" role="tablist" aria-label="歌词审阅视图">'
        f'<button type="button" class="tab active" role="tab" aria-selected="true" data-tab-target="{panel_id}-final">最终歌词</button>'
        f'{diff_tab}'
        f'<button type="button" class="tab" role="tab" aria-selected="false" data-tab-target="{panel_id}-raw">原始数据</button>'
        "</nav>"
        '<section class="lyrics-container">'
        f'<div class="lyrics-panel active" id="{panel_id}-final" data-tab-panel>'
        f'<div class="lyrics-stack">{_render_final(final, empty_message=empty_message)}</div></div>'
        f'{diff_panel}'
        f'<div class="lyrics-panel" id="{panel_id}-raw" data-tab-panel hidden>'
        f'<div class="{raw_grid_class}">{"".join(raw_sections)}</div>{item_json}</div>'
        "</section>"
    )
    header = (
        '<header class="header song-header">'
        f'{_cover_markup(review.file_path, cover_assets)}'
        '<div class="song-info">'
        f'<p class="song-kicker">歌词审批 · ITEM {index + 1:03d}</p>'
        f'<h1 class="info-title">{_e(title)}</h1>'
        f'<p class="info-artist">{_e(artist)}</p>'
        '<dl class="info-meta">'
        f'<div><dt>专辑</dt><dd>{_e(review.metadata.get("album") or "—")}</dd></div>'
        f'<div><dt>年份</dt><dd>{_e(review.metadata.get("date") or "—")}</dd></div>'
        f'<div><dt>时长</dt><dd>{_e(review.duration_label)}</dd></div>'
        f'<div><dt>文件</dt><dd>{_e(review.file_path)}</dd></div>'
        "</dl></div></header>"
    )
    if review.import_item:
        lyrics_active = bool(review.target_text)
        lyrics_selected = "true" if lyrics_active else "false"
        import_selected = "false" if lyrics_active else "true"
        lyrics_class = "review-mode-tab active" if lyrics_active else "review-mode-tab"
        import_class = "review-mode-tab" if lyrics_active else "review-mode-tab active"
        lyrics_panel_class = "review-view-panel active" if lyrics_active else "review-view-panel"
        import_panel_class = "review-view-panel" if lyrics_active else "review-view-panel active"
        lyrics_hidden = "" if lyrics_active else " hidden"
        import_hidden = " hidden" if lyrics_active else ""
        content = (
            '<nav class="review-mode-tabs" role="tablist" aria-label="审批视图">'
            f'<button type="button" class="{lyrics_class}" role="tab" aria-selected="{lyrics_selected}" '
            f'data-review-view-target="{panel_id}-lyrics-review">歌词审计</button>'
            f'<button type="button" class="{import_class}" role="tab" aria-selected="{import_selected}" '
            f'data-review-view-target="{panel_id}-import-review">导入审批</button>'
            "</nav>"
            f'<div class="{lyrics_panel_class}" id="{panel_id}-lyrics-review" data-review-view-panel{lyrics_hidden}>'
            f'{lyrics_review}</div>'
            f'<div class="{import_panel_class}" id="{panel_id}-import-review" data-review-view-panel{import_hidden}>'
            f'{_render_import_details(review.source, cover_assets.get(review.file_path) or {})}</div>'
        )
    else:
        content = lyrics_review
    return (
        f'<article class="song-panel{active_class}" id="{panel_id}" data-song-panel="{panel_id}"{hidden}>'
        f'{header}{content}</article>'
    )


def _batch_navigation(items: list[ReviewItem]) -> str:
    if len(items) <= 1:
        return ""
    buttons = []
    options = []
    for index, item in enumerate(items):
        title = item.metadata.get("title") or Path(item.file_path or f"item-{index + 1}").stem
        artist = item.metadata.get("artist") or "未知艺术家"
        active = " active" if index == 0 else ""
        selected = " selected" if index == 0 else ""
        buttons.append(
            f'<button type="button" class="song-nav-button{active}" data-song-target="song-{index}">'
            f'<span>{_e(title)}</span><small>{_e(artist)}</small></button>'
        )
        options.append(f'<option value="song-{index}"{selected}>{_e(title)} · {_e(artist)}</option>')
    return (
        '<aside class="batch-nav" aria-label="批次歌曲">'
        '<div class="batch-nav-heading"><span>固定批次</span><strong>'
        f'{len(items)} 首</strong></div>{"".join(buttons)}</aside>'
        '<label class="batch-select-label" for="batch-song-select">选择歌曲</label>'
        f'<select id="batch-song-select">{"".join(options)}</select>'
    )


def _format_timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return "—"


def _audit_proof(approval: dict[str, Any], generated_at: float) -> str:
    consumed = approval.get("consumed_by_operation_id") or approval.get("operation_id")
    if consumed:
        status = "已使用"
    else:
        status = "等待批准"
    preview = approval.get("preview") or {}
    raw = json.dumps(approval, ensure_ascii=False, indent=2)
    return (
        '<details class="audit-proof">'
        "<summary>审批凭证</summary>"
        '<div class="audit-grid">'
        f'<div><span>生成时状态</span><strong>{_e(status)}</strong></div>'
        f'<div><span>Approval ID</span><code>{_e(approval.get("approval_id"))}</code></div>'
        f'<div><span>Intent SHA-256</span><code>{_e(approval.get("intent_sha256"))}</code></div>'
        f'<div><span>Fixed Batch SHA-256</span><code>{_e(preview.get("fixed_batch_sha256"))}</code></div>'
        f'<div><span>页面生成时间</span><code>{_format_timestamp(generated_at)}</code></div>'
        '<div><span>审批期限</span><code>无时间限制</code></div>'
        "</div>"
        f'<details class="approval-json"><summary>完整审批 JSON</summary><pre class="raw-lyrics">{_e(raw)}</pre></details>'
        "</details>"
    )


def render_lyrics_approval(
    approval: dict[str, Any],
    *,
    cover_assets: dict[str, dict[str, Any]] | None = None,
    generated_at: float | None = None,
) -> str:
    items = [_lyrics_review_item(item) for item in list((approval.get("preview") or {}).get("items") or [])]
    return _render_review_page(
        approval,
        items=items,
        cover_assets=cover_assets or {},
        generated_at=generated_at,
    )


def render_import_approval(approval: dict[str, Any], *, generated_at: float | None = None) -> str:
    source_items = list((approval.get("preview") or {}).get("items") or [])
    items = [_import_review_item(item) for item in source_items]
    assets = {review.file_path: _import_cover_asset(review.source) for review in items}
    return _render_review_page(approval, items=items, cover_assets=assets, generated_at=generated_at)


def _render_review_page(
    approval: dict[str, Any],
    *,
    items: list[ReviewItem],
    cover_assets: dict[str, dict[str, Any]],
    generated_at: float | None,
) -> str:
    template = files("musicops").joinpath("templates", "lyrics-review.html").read_text(encoding="utf-8")
    timestamp = time.time() if generated_at is None else generated_at
    title = "歌词审批"
    if len(items) == 1:
        title = f"歌词审批 - {items[0].metadata.get('title') or Path(items[0].file_path).stem}"
    page = template.replace("{{page_title}}", _e(title))
    page = page.replace("{{batch_label}}", _e(f"固定批次 · {len(items)} 首"))
    page = page.replace("{{layout_class}}", "with-batch" if len(items) > 1 else "single")
    page = page.replace("{{batch_navigation}}", _batch_navigation(items))
    page = page.replace(
        "{{song_panels}}",
        "".join(
            _render_song(item, index=index, active=index == 0, cover_assets=cover_assets)
            for index, item in enumerate(items)
        ),
    )
    page = page.replace("{{audit_proof}}", _audit_proof(approval, timestamp))
    return page
