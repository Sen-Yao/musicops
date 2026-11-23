from __future__ import annotations

import hashlib
import re
from typing import Any


MASK_PATTERN = re.compile(r"(?<!\*)\*{2,}(?!\*)")
_TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
_LEADING_TIMESTAMPS = re.compile(r"^\s*((?:\[\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?\])+)(.*)$")
_CREDIT_PATTERN = re.compile(
    r"^\s*(?:作词|作曲|编曲|制作人|lyrics\s+by|written\s+by|composer|producer|"
    r"produced\s+by|arranged\s+by|recording\s+engineer|mix(?:ed|ing)?\s+by|"
    r"master(?:ed|ing)?\s+by)\b",
    flags=re.IGNORECASE,
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp_ms(value: str) -> int:
    match = _TIMESTAMP_PATTERN.fullmatch(f"[{str(value).strip().strip('[]')}]")
    if not match:
        raise ValueError("invalid_canonical_timestamp")
    fraction = (match.group(3) or "0").ljust(3, "0")[:3]
    return ((int(match.group(1)) * 60) + int(match.group(2))) * 1000 + int(fraction)


def _canonical_lines(canonical_lrc: str) -> tuple[list[str], dict[int, list[dict[str, Any]]]]:
    lines = canonical_lrc.splitlines()
    by_timestamp: dict[int, list[dict[str, Any]]] = {}
    for line_index, raw_line in enumerate(lines):
        match = _LEADING_TIMESTAMPS.match(raw_line)
        if not match:
            continue
        timestamps = list(_TIMESTAMP_PATTERN.finditer(match.group(1)))
        row = {
            "line_index": line_index,
            "prefix": match.group(1),
            "text": match.group(2),
            "timestamp_count": len(timestamps),
        }
        for token in timestamps:
            display = token.group(0)[1:-1]
            by_timestamp.setdefault(timestamp_ms(display), []).append({**row, "timestamp": display})
    return lines, by_timestamp


def resolve_masked_lyrics(
    *,
    canonical_lrc: str,
    canonical_lrc_sha256: str,
    resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    actual_sha256 = sha256_text(canonical_lrc)
    if actual_sha256 != canonical_lrc_sha256.lower():
        raise ValueError("canonical_lrc_sha256_mismatch")
    if not resolutions:
        raise ValueError("masked_lyrics_resolutions_required")

    lines, by_timestamp = _canonical_lines(canonical_lrc)
    replacements_by_line: dict[int, list[tuple[int, int, str]]] = {}
    audit_rows: list[dict[str, Any]] = []
    seen_targets: set[tuple[int, str]] = set()

    for resolution in resolutions:
        key = timestamp_ms(str(resolution.get("canonical_timestamp") or ""))
        matches = by_timestamp.get(key) or []
        if not matches:
            raise ValueError("canonical_timestamp_not_found")
        if len(matches) != 1:
            raise ValueError("canonical_timestamp_not_unique")
        line = matches[0]
        if int(line["timestamp_count"]) != 1:
            raise ValueError("masked_line_requires_one_timestamp")
        if _CREDIT_PATTERN.search(str(line["text"])):
            raise ValueError("credit_line_ineligible")

        original_text = str(resolution.get("original_text") or "")
        replacement_text = str(resolution.get("replacement_text") or "")
        target = (key, original_text)
        if target in seen_targets:
            raise ValueError("duplicate_mask_resolution")
        seen_targets.add(target)

        line_text = str(line["text"])
        if line_text.count(original_text) != 1:
            raise ValueError("original_text_not_unique_in_canonical_line")
        start = line_text.find(original_text)
        end = start + len(original_text)
        existing = replacements_by_line.setdefault(int(line["line_index"]), [])
        if any(start < other_end and other_start < end for other_start, other_end, _value in existing):
            raise ValueError("overlapping_mask_resolutions")
        existing.append((start, end, replacement_text))
        audit_rows.append({
            "canonical_timestamp": str(line["timestamp"]),
            "original_text": original_text,
            "replacement_text": replacement_text,
            "method": str(resolution.get("method") or ""),
            "stt_operation_id": str(resolution.get("stt_operation_id") or ""),
        })

    resolved_lines = list(lines)
    for line_index, replacements in replacements_by_line.items():
        line = next(rows[0] for rows in by_timestamp.values() if rows[0]["line_index"] == line_index)
        text = str(line["text"])
        for start, end, replacement_text in sorted(replacements, reverse=True):
            text = f"{text[:start]}{replacement_text}{text[end:]}"
        resolved_lines[line_index] = f"{line['prefix']}{text}"

    candidate_lrc = "\n".join(resolved_lines)
    remaining_mask_count = len(MASK_PATTERN.findall(candidate_lrc))
    resolved = remaining_mask_count == 0
    return {
        "ok": True,
        "purpose": "masked_lyrics_resolution",
        "resolution_status": "resolved" if resolved else "unresolved",
        "input_canonical_lrc_sha256": actual_sha256,
        "resolved_canonical_lrc": candidate_lrc if resolved else "",
        "resolved_canonical_lrc_sha256": sha256_text(candidate_lrc) if resolved else "",
        "remaining_mask_count": remaining_mask_count,
        "resolutions": audit_rows,
    }
