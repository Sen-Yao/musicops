from __future__ import annotations

import re
import unicodedata
from typing import Any

from opencc import OpenCC


IDENTITY_POLICY_VERSION = "track-identity-v2"

_T2S = OpenCC("t2s")
_KANA_OR_HANGUL = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_VERSION_SUFFIX = re.compile(r"\s*[\(\[]\s*(?:explicit|clean)\s*[\)\]]\s*$", flags=re.IGNORECASE)
_ARTIST_SEPARATOR = re.compile(r"\s*(?:[;&]|\bwith\b)\s*", flags=re.IGNORECASE)
_TRUNCATED_SUFFIX = re.compile(r"(?:\.{2,}|…)$")


def simplify_chinese(value: Any) -> str:
    """Normalize Unicode and convert Chinese text without changing Japanese or Korean."""
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    if not text or not _HAN.search(text) or _KANA_OR_HANGUL.search(text):
        return text
    return _T2S.convert(text)


def normalize_identity_text(value: Any) -> str:
    text = " ".join(simplify_chinese(value).casefold().split())
    return " ".join(_VERSION_SUFFIX.sub("", text).split())


def normalize_artist_identity(value: Any) -> tuple[str, bool]:
    text = normalize_identity_text(value)
    truncated = bool(_TRUNCATED_SUFFIX.search(text))
    if truncated:
        text = _TRUNCATED_SUFFIX.sub("", text).rstrip()
    text = _ARTIST_SEPARATOR.sub(" & ", text)
    return " ".join(text.split()), truncated


def identity_key(value: Any) -> str:
    return " ".join(simplify_chinese(value).casefold().split())


def artist_identity_compatible(expected: Any, actual: Any) -> bool:
    expected_text, expected_truncated = normalize_artist_identity(expected)
    actual_text, actual_truncated = normalize_artist_identity(actual)
    if not expected_text or not actual_text:
        return True
    if expected_text == actual_text:
        return True
    if actual_truncated and len(actual_text) >= 12 and expected_text.startswith(actual_text):
        return True
    if expected_truncated and len(expected_text) >= 12 and actual_text.startswith(expected_text):
        return True
    return False


def compare_track_identities(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    compatible = True
    saw_variant = False
    saw_ambiguous = False

    for field in ("title", "artist", "album"):
        expected_raw = str(expected.get(field) or "").strip()
        actual_raw = str(actual.get(field) or "").strip()
        if field == "artist":
            expected_normalized, expected_truncated = normalize_artist_identity(expected_raw)
            actual_normalized, actual_truncated = normalize_artist_identity(actual_raw)
        else:
            expected_normalized = normalize_identity_text(expected_raw)
            actual_normalized = normalize_identity_text(actual_raw)
            expected_truncated = actual_truncated = False

        if not expected_normalized or not actual_normalized:
            status = "not_compared"
        elif actual_truncated and len(actual_normalized) >= 12 and expected_normalized.startswith(actual_normalized):
            status = "truncated_provider_field"
            saw_variant = True
        elif expected_truncated and len(expected_normalized) >= 12 and actual_normalized.startswith(expected_normalized):
            status = "truncated_provider_field"
            saw_variant = True
        elif expected_truncated or actual_truncated:
            status = "ambiguous_truncation"
            compatible = False
            saw_ambiguous = True
        elif expected_normalized != actual_normalized:
            status = "mismatch"
            compatible = False
        elif expected_raw == actual_raw:
            status = "exact"
        else:
            status = "equivalent_display_variant"
            saw_variant = True

        fields[field] = {
            "status": status,
            "expected": expected_raw,
            "actual": actual_raw,
            "expected_normalized": expected_normalized,
            "actual_normalized": actual_normalized,
        }

    if compatible:
        classification = "equivalent_display_variant" if saw_variant else "exact"
    elif saw_ambiguous and all(row["status"] != "mismatch" for row in fields.values()):
        classification = "ambiguous"
    else:
        classification = "mismatch"
    return {
        "policy_version": IDENTITY_POLICY_VERSION,
        "compatible": compatible,
        "classification": classification,
        "fields": fields,
    }


def identity_compatible(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return bool(compare_track_identities(expected, actual)["compatible"])
