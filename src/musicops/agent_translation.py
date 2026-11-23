"""Isolated pure-function prototype for ordered Agent translation assembly."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

CREDIT_REMOVAL_MARKER = "__MUSICOPS_REMOVE_CREDIT__"
_TIMESTAMP_RECORD = re.compile(
    r"^(?P<tags>(?:\[[0-9]{1,3}:[0-5][0-9](?:[.:][0-9]{1,3})?\])+)(?P<text>.*)$"
)
_TIMESTAMP_LIKE = re.compile(r"^\[[0-9]+:[0-9]+")


def validate_canonical_sha256(canonical_lrc: str, expected_sha256: str) -> None:
    """Fail closed unless the exact UTF-8 source text matches its binding."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
        raise ValueError("canonical_sha256_invalid")
    actual = hashlib.sha256(canonical_lrc.encode("utf-8")).hexdigest()
    if actual.casefold() != expected_sha256.casefold():
        raise ValueError("canonical_sha256_mismatch")


def assemble_bilingual_lrc(
    canonical_lrc: str,
    translated_lines: Sequence[str],
) -> str:
    """Interleave ordered translations without using timestamps as row keys.

    Every timestamped physical source line consumes exactly one translation
    entry. Untimestamped metadata and blank lines pass through unchanged.
    Source line content is retained; output line endings are normalized to LF.
    """
    if isinstance(translated_lines, (str, bytes)):
        raise ValueError("translated_lines_must_be_a_sequence")

    source_lines = canonical_lrc.splitlines()
    has_trailing_newline = canonical_lrc.endswith(("\n", "\r"))
    records: list[tuple[str, re.Match[str] | None]] = []
    for line in source_lines:
        match = _TIMESTAMP_RECORD.match(line)
        if match is None and _TIMESTAMP_LIKE.match(line):
            raise ValueError("canonical_timestamp_invalid")
        records.append((line, match))

    record_count = sum(match is not None for _, match in records)
    if len(translated_lines) != record_count:
        raise ValueError("translated_line_count_mismatch")

    output: list[str] = []
    translation_index = 0
    for source_line, match in records:
        if match is None:
            output.append(source_line)
            continue

        translation = translated_lines[translation_index]
        translation_index += 1
        if not isinstance(translation, str):
            raise ValueError("translated_line_must_be_text")
        if "\n" in translation or "\r" in translation:
            raise ValueError("translated_line_must_not_contain_newlines")
        if translation == CREDIT_REMOVAL_MARKER:
            continue

        output.append(source_line)
        if not match.group("text").strip() and not translation.strip():
            continue
        output.append(f"{match.group('tags')}{translation}")

    assembled = "\n".join(output)
    if has_trailing_newline and output:
        assembled += "\n"
    return assembled
