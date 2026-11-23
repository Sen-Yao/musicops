from __future__ import annotations

from musicops.navidrome import NavidromeClient


TARGET = "_MusicOpsSmoke/2026-07-17/The Real Her - Drake - gateway-smoke.mp3"
EXPECTED_METADATA = {"title": "The Real Her", "album": "Take Care (Deluxe)"}


def _client_with_results(results):
    client = NavidromeClient()
    client.db_search_evidence = lambda query, limit: {
        "ok": True,
        "query": query,
        "limit": limit,
        "result_count": len(results),
        "results": results,
    }
    return client


def test_path_evidence_rejects_metadata_match_at_a_different_path():
    client = _client_with_results([
        {"path": "Artist/Album/The Real Her.mp3", **EXPECTED_METADATA},
    ])

    evidence = client.path_evidence(TARGET, query="The Real Her", expected_metadata=EXPECTED_METADATA)

    assert evidence["matched"] is False
    assert evidence["path_matched"] is False
    assert evidence["metadata_matched"] is False
    assert evidence["matched_rows"] == []


def test_path_evidence_requires_expected_metadata_on_the_exact_path():
    client = _client_with_results([
        {"path": TARGET, "title": "The Real Her", "album": "Wrong Album"},
    ])

    evidence = client.path_evidence(TARGET, query="The Real Her", expected_metadata=EXPECTED_METADATA)

    assert evidence["matched"] is False
    assert evidence["path_matched"] is True
    assert evidence["metadata_matched"] is False


def test_path_evidence_accepts_exact_path_and_metadata():
    row = {"path": TARGET, **EXPECTED_METADATA}
    client = _client_with_results([row])

    evidence = client.path_evidence(TARGET, query="The Real Her", expected_metadata=EXPECTED_METADATA)

    assert evidence["matched"] is True
    assert evidence["path_matched"] is True
    assert evidence["metadata_matched"] is True
    assert evidence["match_type"] == "path_and_metadata"
    assert evidence["matched_rows"] == [row]


def test_unique_metadata_accepts_one_logical_path_match():
    row = {
        "path": "MusicOps/Production Gateway Smoke/MusicOps Gateway Smoke 2026-07-17.mp3",
        "title": "MusicOps Gateway Smoke 2026-07-17",
        "artist": "MusicOps",
        "album": "Production Gateway Smoke",
    }
    client = _client_with_results([row])

    evidence = client.path_evidence(
        TARGET,
        expected_metadata={key: row[key] for key in ("title", "artist", "album")},
        match_strategy="unique_metadata",
    )

    assert evidence["matched"] is True
    assert evidence["path_matched"] is False
    assert evidence["metadata_candidate_count"] == 1
    assert evidence["metadata_ambiguous"] is False
    assert evidence["match_type"] == "unique_metadata"
    assert evidence["matched_rows"] == [row]


def test_unique_metadata_rejects_ambiguous_matches():
    rows = [
        {"path": "One.mp3", **EXPECTED_METADATA},
        {"path": "Two.mp3", **EXPECTED_METADATA},
    ]
    client = _client_with_results(rows)

    evidence = client.path_evidence(
        TARGET,
        query="The Real Her",
        expected_metadata=EXPECTED_METADATA,
        match_strategy="unique_metadata",
    )

    assert evidence["matched"] is False
    assert evidence["metadata_candidate_count"] == 2
    assert evidence["metadata_ambiguous"] is True
    assert evidence["matched_rows"] == []
