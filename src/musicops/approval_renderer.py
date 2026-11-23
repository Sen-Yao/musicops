from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

from musicops.lyrics_approval_renderer import render_import_approval, render_lyrics_approval


PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}}</title>
<style>
:root{color-scheme:light;--ink:#171717;--muted:#686868;--line:#d8d8d4;--paper:#f7f7f4;--white:#fff;--danger:#a42323;--accent:#12634f;--soft:#ecece8}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}
header{border-bottom:1px solid var(--line);background:var(--white)}.shell{width:min(1120px,calc(100% - 32px));margin:auto}
.mast{display:grid;grid-template-columns:1fr auto;gap:24px;padding:28px 0 22px;align-items:end}h1{font-size:26px;line-height:1.15;margin:0 0 8px}h2{font-size:18px;margin:0}p{margin:0}.muted{color:var(--muted)}
.state{border-left:4px solid var(--accent);padding:8px 12px;background:#edf5f1}.state.danger{border-color:var(--danger);background:#f8eded}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);margin:20px 0}.fact{background:var(--white);padding:12px}.label{display:block;color:var(--muted);font-size:12px;margin-bottom:3px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
main{padding:4px 0 56px}.item{border-top:1px solid var(--line);padding:26px 0}.item:first-child{border-top:0}.item-head{display:flex;justify-content:space-between;gap:20px;align-items:start;margin-bottom:16px}.index{color:var(--muted);font:12px ui-monospace,SFMono-Regular,monospace}
table{width:100%;border-collapse:collapse;background:var(--white);table-layout:fixed}th,td{border:1px solid var(--line);padding:9px 11px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:var(--soft);font-size:12px;color:var(--muted);font-weight:600}.change{color:var(--accent);font-weight:600}.removed{color:var(--danger)}
.lyrics{display:grid;grid-template-columns:1fr 1fr;border:1px solid var(--line);background:var(--line);gap:1px}.pane{background:var(--white);min-width:0}.pane h3{font-size:12px;color:var(--muted);margin:0;padding:9px 12px;border-bottom:1px solid var(--line);background:var(--soft)}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:14px;font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:560px;overflow:auto}
.cover-grid{display:grid;grid-template-columns:220px 1fr;gap:24px;align-items:start}.cover{width:220px;aspect-ratio:1;object-fit:contain;background:var(--white);border:1px solid var(--line)}
.warning{border:1px solid #dbb5b5;background:#fff5f5;color:#651818;padding:13px 15px;margin:12px 0}.raw{margin-top:28px}.raw summary{cursor:pointer;color:var(--muted)}
@media(max-width:760px){.mast{grid-template-columns:1fr}.facts{grid-template-columns:1fr}.lyrics{grid-template-columns:1fr}.cover-grid{grid-template-columns:1fr}.item-head{display:block}.cover{width:min(100%,320px)}}
</style>
</head>
<body><header><div class="shell mast">{{header}}</div></header><main class="shell">{{facts}}{{items}}{{raw}}</main></body>
</html>"""


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _metadata_table(before: dict[str, Any], target: dict[str, Any]) -> str:
    current = before.get("metadata") or {}
    desired = target.get("metadata") or {}
    keys = sorted(set(current) | set(desired))
    body = []
    for key in keys:
        old = current.get(key, "")
        new = desired.get(key, old)
        css = " class=\"change\"" if str(old) != str(new) else ""
        body.append(f"<tr><td>{_e(key)}</td><td>{_e(old)}</td><td{css}>{_e(new)}</td></tr>")
    return "<table><thead><tr><th>字段</th><th>当前</th><th>目标</th></tr></thead><tbody>" + "".join(body) + "</tbody></table>"


def _summary_table(rows: list[tuple[str, Any, Any]]) -> str:
    body = "".join(
        f"<tr><td>{_e(label)}</td><td>{_e(before)}</td><td class=\"change\">{_e(after)}</td></tr>"
        for label, before, after in rows
    )
    return "<table><thead><tr><th>项目</th><th>当前</th><th>目标</th></tr></thead><tbody>" + body + "</tbody></table>"


def _render_item(action: str, item: dict[str, Any], index: int) -> str:
    path = item.get("file_path") or item.get("target_file_path") or item.get("temporary_file_id") or "-"
    before = item.get("before") or item.get("target_before") or {}
    target = item.get("target") or {}
    heading = f'<div class="item-head"><h2>{_e(path)}</h2><span class="index">ITEM {index + 1:03d}</span></div>'
    if action == "library.update_metadata":
        content = _metadata_table(before, target)
    elif action == "library.update_lyrics":
        old = before.get("lyrics") or {}
        content = _summary_table([
            ("SHA-256", old.get("sha256", ""), target.get("lyrics_sha256", "")),
            ("行数", old.get("line_count", 0), target.get("line_count", 0)),
            ("字符数", old.get("length", 0), target.get("length", 0)),
        ])
        current_text = str(old.get("text") or "当前完整歌词未内联；以当前 SHA-256 为准。")
        content += (
            '<div class="lyrics"><section class="pane"><h3>当前</h3><pre>' + _e(current_text) +
            '</pre></section><section class="pane"><h3>目标</h3><pre>' + _e(target.get("lyrics_text") or "") + "</pre></section></div>"
        )
    elif action == "library.update_cover":
        uri = str(target.get("data_uri") or "")
        image = f'<img class="cover" alt="目标封面" src="{_e(uri)}">' if uri.startswith("data:image/") else ""
        content = '<div class="cover-grid">' + image + _summary_table([
            ("SHA-256", (before.get("cover") or {}).get("sha256", ""), target.get("cover_sha256", "")),
            ("媒体类型", (before.get("cover") or {}).get("mime", ""), target.get("media_type", "")),
        ]) + "</div>"
    elif action == "library.delete":
        metadata = before.get("metadata") or {}
        content = '<div class="warning"><strong>永久删除</strong><br>批准后不进入回收站，也不提供恢复操作。</div>'
        content += _summary_table([
            ("标题", metadata.get("title", ""), "删除"),
            ("歌手", metadata.get("artist", ""), "删除"),
            ("文件大小", before.get("size", ""), "删除"),
            ("SHA-256", before.get("sha256", ""), "删除"),
        ])
    elif action == "library.move":
        content = _summary_table([
            ("路径", item.get("file_path", ""), item.get("target_file_path", "")),
            ("SHA-256", before.get("sha256", ""), before.get("sha256", "")),
        ])
    elif action == "library.add":
        source = item.get("source") or {}
        content = '<div class="warning"><strong>Audio-only import</strong><br>本审批不会规范化 metadata、歌词或封面。</div>'
        content += _summary_table([
            ("导入配置", "", item.get("import_profile", "audio_only")),
            ("临时文件", item.get("temporary_file_id", ""), item.get("target_file_path", "")),
            ("SHA-256", source.get("sha256", ""), source.get("sha256", "")),
            ("文件大小", source.get("size", ""), source.get("size", "")),
        ])
    else:
        content = f"<pre>{_e(json.dumps(item, ensure_ascii=False, indent=2))}</pre>"
    return f'<article class="item">{heading}{content}</article>'


def default_review_dir() -> Path:
    configured = os.environ.get("MUSICOPS_REVIEW_DIR")
    return Path(configured).expanduser().resolve() if configured else Path(tempfile.gettempdir()) / "musicops" / "reviews"


def default_review_path(approval_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(approval_id or "approval")).strip("-") or "approval"
    return default_review_dir() / f"review-{safe_id}.html"


def cleanup_default_reviews(*, max_age_hours: float = 24, now: float | None = None) -> dict[str, Any]:
    root = default_review_dir()
    cutoff = (time.time() if now is None else now) - max(0, max_age_hours) * 3600
    deleted: list[str] = []
    if root.exists():
        for candidate in root.glob("review-*.html"):
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                deleted.append(str(candidate))
    return {"ok": True, "review_dir": str(root), "deleted_count": len(deleted), "deleted": deleted}


def _write_private_page(page: str, output_path: str | Path) -> Path:
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(page)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        os.chmod(target, 0o600)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return target


def render_approval_html(
    approval: dict[str, Any],
    *,
    cover_assets: dict[str, dict[str, Any]] | None = None,
) -> str:
    preview = approval.get("preview") or {}
    action = str(approval.get("action") or preview.get("action") or "unknown")
    if action == "library.update_lyrics":
        return render_lyrics_approval(approval, cover_assets=cover_assets)
    if action == "library.import":
        return render_import_approval(approval)
    danger = action == "library.delete"
    header = (
        f'<div><h1>{_e(action)}</h1><p class="muted">服务器当前状态生成的固定审批清单</p></div>'
        f'<div class="state{" danger" if danger else ""}"><span class="label">状态</span>{"永久删除" if danger else "等待批准"}</div>'
    )
    facts = '<section class="facts">' + "".join([
        f'<div class="fact"><span class="label">Approval ID</span><span class="mono">{_e(approval.get("approval_id"))}</span></div>',
        f'<div class="fact"><span class="label">Intent SHA-256</span><span class="mono">{_e(approval.get("intent_sha256"))}</span></div>',
        f'<div class="fact"><span class="label">Fixed Batch SHA-256</span><span class="mono">{_e(preview.get("fixed_batch_sha256"))}</span></div>',
    ]) + "</section>"
    items = "".join(_render_item(action, row, index) for index, row in enumerate(preview.get("items") or []))
    raw_json = json.dumps(approval, ensure_ascii=False, indent=2)
    raw = f'<details class="raw"><summary>原始审批数据</summary><pre>{_e(raw_json)}</pre></details>'
    page = PAGE_TEMPLATE.replace("{{title}}", _e(f"MusicOps 审批 - {action}"))
    page = page.replace("{{header}}", header).replace("{{facts}}", facts).replace("{{items}}", items).replace("{{raw}}", raw)

    return page


def render_approval(
    approval: dict[str, Any],
    output_path: str | Path | None = None,
    *,
    cover_assets: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Write a private local copy for development-only callers.

    Production review pages are rendered by MusicOps and returned directly from
    the approval endpoint via :func:`render_approval_html`.
    """
    target = output_path or default_review_path(str(approval.get("approval_id") or "approval"))
    return _write_private_page(render_approval_html(approval, cover_assets=cover_assets), target)
