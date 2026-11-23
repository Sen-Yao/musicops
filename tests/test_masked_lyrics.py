from __future__ import annotations

import hashlib

import pytest

from musicops.masked_lyrics import resolve_masked_lyrics


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _resolution(
    *,
    timestamp: str = "00:42.000",
    original: str = "****",
    replacement: str = "word",
    method: str = "agent_inferred",
    stt_operation_id: str = "",
) -> dict:
    return {
        "canonical_timestamp": timestamp,
        "original_text": original,
        "replacement_text": replacement,
        "method": method,
        "stt_operation_id": stt_operation_id,
    }


def _resolve(canonical: str, resolutions: list[dict]) -> dict:
    return resolve_masked_lyrics(
        canonical_lrc=canonical,
        canonical_lrc_sha256=_sha256(canonical),
        resolutions=resolutions,
    )


def test_agent_inferred_replacement_resolves_literal_mask():
    canonical = "[00:42.000]say **** now"

    result = _resolve(canonical, [_resolution(replacement="heard")])

    assert result["resolution_status"] == "resolved"
    assert result["resolved_canonical_lrc"] == "[00:42.000]say heard now"
    assert result["resolved_canonical_lrc_sha256"] == _sha256(result["resolved_canonical_lrc"])
    assert result["remaining_mask_count"] == 0
    assert result["resolutions"] == [{
        "canonical_timestamp": "00:42.000",
        "original_text": "****",
        "replacement_text": "heard",
        "method": "agent_inferred",
        "stt_operation_id": "",
    }]


def test_agent_can_identify_x_obfuscation_without_musicops_pattern_rules():
    canonical = "[00:42.000]say fxxk now"

    result = _resolve(canonical, [_resolution(original="fxxk", replacement="fuck")])

    assert result["resolution_status"] == "resolved"
    assert result["resolved_canonical_lrc"] == "[00:42.000]say fuck now"


def test_stt_hint_is_recorded_without_transcript_matching():
    canonical = "[00:42.000]say **** now"

    result = _resolve(canonical, [_resolution(
        replacement="heard",
        method="agent_resolved_with_stt_hint",
        stt_operation_id="op-stt",
    )])

    assert result["resolution_status"] == "resolved"
    assert result["resolutions"][0]["stt_operation_id"] == "op-stt"


def test_unsubmitted_literal_mask_keeps_resolution_unresolved():
    canonical = "[00:42.000]first ****\n[00:43.000]second ****"

    result = _resolve(canonical, [_resolution(timestamp="00:42.000", replacement="one")])

    assert result["resolution_status"] == "unresolved"
    assert result["resolved_canonical_lrc"] == ""
    assert result["resolved_canonical_lrc_sha256"] == ""
    assert result["remaining_mask_count"] == 1


def test_original_text_must_be_unique_in_target_line():
    canonical = "[00:42.000]**** then ****"

    with pytest.raises(ValueError, match="original_text_not_unique_in_canonical_line"):
        _resolve(canonical, [_resolution()])


def test_multiple_distinct_tokens_on_one_line_can_be_resolved():
    canonical = "[00:42.000]fxxk then shxt"

    result = _resolve(canonical, [
        _resolution(original="fxxk", replacement="fuck"),
        _resolution(original="shxt", replacement="shit"),
    ])

    assert result["resolution_status"] == "resolved"
    assert result["resolved_canonical_lrc"] == "[00:42.000]fuck then shit"


def test_replacement_preserves_all_other_line_characters():
    canonical = "[00:42.000]  say **** now  "

    result = _resolve(canonical, [_resolution(replacement="heard")])

    assert result["resolved_canonical_lrc"] == "[00:42.000]  say heard now  "


def test_credit_lines_are_not_eligible_for_resolution():
    canonical = "[00:42.000]Produced by ****"

    with pytest.raises(ValueError, match="credit_line_ineligible"):
        _resolve(canonical, [_resolution(replacement="someone")])


def test_duplicate_timestamp_is_rejected():
    canonical = "[00:42.000]say **** now\n[00:42.000]repeat **** now"

    with pytest.raises(ValueError, match="canonical_timestamp_not_unique"):
        _resolve(canonical, [_resolution()])


def test_multi_timestamp_line_is_rejected():
    canonical = "[00:42.000][00:44.000]say **** now"

    with pytest.raises(ValueError, match="masked_line_requires_one_timestamp"):
        _resolve(canonical, [_resolution()])


def test_canonical_hash_must_match():
    canonical = "[00:42.000]say **** now"

    with pytest.raises(ValueError, match="canonical_lrc_sha256_mismatch"):
        resolve_masked_lyrics(
            canonical_lrc=canonical,
            canonical_lrc_sha256="0" * 64,
            resolutions=[_resolution()],
        )
