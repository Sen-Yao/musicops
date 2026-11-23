import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from musicops.agent_translation import (  # noqa: E402
    CREDIT_REMOVAL_MARKER,
    assemble_bilingual_lrc,
    validate_canonical_sha256,
)
from musicops.actions import (  # noqa: E402
    ActionFailure,
    LyricsUpdateItem,
    StagedLyricsInput,
    UpdateLyricsInput,
    _update_lyrics,
    build_mutation_preview,
)
from musicops.normalized_import import _lyrics_for_staging, lyrics_for_existing_update  # noqa: E402


class ValidateCanonicalHashTests(unittest.TestCase):
    def test_accepts_exact_utf8_source_hash(self):
        source = "[00:01.00]cafe\u0301\n"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        validate_canonical_sha256(source, digest)

    def test_rejects_mismatch_and_malformed_hash(self):
        with self.assertRaisesRegex(ValueError, "canonical_sha256_mismatch"):
            validate_canonical_sha256("[00:01.00]x", "0" * 64)
        with self.assertRaisesRegex(ValueError, "canonical_sha256_invalid"):
            validate_canonical_sha256("text", "not-a-hash")


class AssembleBilingualLrcTests(unittest.TestCase):
    def test_repeated_timestamps_are_bound_by_source_order(self):
        source = "[00:01.00]first\n[00:02.00]middle\n[00:01.00]last"
        result = assemble_bilingual_lrc(source, ["甲", "乙", "丙"])
        self.assertEqual(
            result,
            "[00:01.00]first\n[00:01.00]甲\n"
            "[00:02.00]middle\n[00:02.00]乙\n"
            "[00:01.00]last\n[00:01.00]丙",
        )

    def test_multiple_timestamp_tags_remain_on_the_same_record(self):
        source = "[00:01.00][00:03.500]shared line"
        self.assertEqual(
            assemble_bilingual_lrc(source, ["共用译文"]),
            "[00:01.00][00:03.500]shared line\n"
            "[00:01.00][00:03.500]共用译文",
        )

    def test_english_korean_japanese_and_mixed_lines_keep_order_with_repeated_chorus(self):
        source = "\n".join([
            "[00:01.00]Stars over the city",
            "[00:02.00]너를 사랑해",
            "[00:03.00]夜が明ける",
            "[00:04.00]晚安, sweet dreams",
            "[00:01.00]Stars over the city",
        ])
        translations = [
            "星光洒落在城里",
            "我爱你",
            "天色渐亮",
            "晚安，愿你有个甜美的梦",
            "星光洒落在城里",
        ]

        self.assertEqual(
            assemble_bilingual_lrc(source, translations),
            "\n".join([
                "[00:01.00]Stars over the city",
                "[00:01.00]星光洒落在城里",
                "[00:02.00]너를 사랑해",
                "[00:02.00]我爱你",
                "[00:03.00]夜が明ける",
                "[00:03.00]天色渐亮",
                "[00:04.00]晚安, sweet dreams",
                "[00:04.00]晚安，愿你有个甜美的梦",
                "[00:01.00]Stars over the city",
                "[00:01.00]星光洒落在城里",
            ]),
        )

    def test_credit_marker_removes_only_its_bound_source_record(self):
        source = "[00:01.00]sung lyric\n[00:02.00]Written by A\n[00:03.00]other"
        self.assertEqual(
            assemble_bilingual_lrc(
                source,
                ["歌词", CREDIT_REMOVAL_MARKER, "另句"],
            ),
            "[00:01.00]sung lyric\n[00:01.00]歌词\n"
            "[00:03.00]other\n[00:03.00]另句",
        )

    def test_marker_like_text_is_not_a_control_value(self):
        source = "[00:01.00]sung lyric"
        self.assertEqual(
            assemble_bilingual_lrc(source, [CREDIT_REMOVAL_MARKER + " "]),
            f"{source}\n[00:01.00]{CREDIT_REMOVAL_MARKER} ",
        )

    def test_untimestamped_metadata_and_blank_lines_pass_through(self):
        source = "[ar:Artist]\n\n[00:01.00]line\n"
        self.assertEqual(
            assemble_bilingual_lrc(source, ["译文"]),
            "[ar:Artist]\n\n[00:01.00]line\n[00:01.00]译文\n",
        )

    def test_empty_timestamped_record_stays_without_an_added_duplicate(self):
        self.assertEqual(
            assemble_bilingual_lrc("[00:01.00]", [""]),
            "[00:01.00]",
        )

    def test_rejects_incomplete_or_ambiguous_payloads(self):
        with self.assertRaisesRegex(ValueError, "translated_line_count_mismatch"):
            assemble_bilingual_lrc("[00:01.00]one\n[00:02.00]two", ["one"])
        with self.assertRaisesRegex(ValueError, "translated_line_must_be_text"):
            assemble_bilingual_lrc("[00:01.00]one", [None])  # type: ignore[list-item]
        with self.assertRaisesRegex(ValueError, "translated_line_must_not_contain_newlines"):
            assemble_bilingual_lrc("[00:01.00]one", ["two\n[00:02.00]injected"])
        with self.assertRaisesRegex(ValueError, "canonical_timestamp_invalid"):
            assemble_bilingual_lrc("[00:61.00]bad", ["x"])


class StagedLyricsContractTests(unittest.TestCase):
    def test_translated_payload_requires_canonical_hash_and_source(self):
        with self.assertRaisesRegex(ValueError, "lyrics_source_operation_required"):
            StagedLyricsInput(status="translated", source_sha256="0" * 64, translated_lines=["译文"])
        with self.assertRaisesRegex(ValueError, "lyrics_source_sha256_required"):
            StagedLyricsInput(status="translated", source_operation_id="fetch-1", translated_lines=["译文"])

    def test_translated_payload_is_exclusive_to_translated_status(self):
        with self.assertRaisesRegex(ValueError, "translated_payload_requires_translated_status"):
            StagedLyricsInput(
                status="original",
                source_operation_id="fetch-1",
                source_sha256="0" * 64,
            )

    def test_translation_failure_does_not_require_translation_operation(self):
        model = StagedLyricsInput(
            status="translation_failed",
            source_operation_id="fetch-1",
            reason="Translation was not produced",
        )
        self.assertEqual(model.status, "translation_failed")


class ExistingLyricsUpdateContractTests(unittest.TestCase):
    canonical = "[00:01.00]first\n[00:01.00]second"
    expected_bilingual = (
        "[00:01.00]first\n[00:01.00]第一句\n"
        "[00:01.00]second\n[00:01.00]第二句"
    )

    def make_context(self, *, canonical=None, source_status="succeeded", target_identity=None):
        canonical = canonical if canonical is not None else self.canonical
        source = SimpleNamespace(
            id="lyrics-fetch-1",
            action="lyrics.fetch",
            status=source_status,
            input={"provider": "lrclib", "provider_id": "track-7"},
            result={
                "canonical_lrc": canonical,
                "provider": "lrclib",
                "provider_id": "track-7",
                "identity": {"title": "Track", "artist": "Artist", "album": "Album"},
            },
        )
        snapshot = {"exists": True, "sha256": "file-before"}
        written = []

        def evidence_for_file(*_args, **_kwargs):
            return {
                "ok": True,
                "exists": True,
                "metadata": target_identity or {"title": "Track", "artist": "Artist", "album": "Album"},
                "lyrics": {"text": "old lyrics"},
            }

        inspector = SimpleNamespace(
            evidence_for_file=evidence_for_file,
            file_snapshot=lambda _path: dict(snapshot),
            lyrics_write=lambda **kwargs: written.append(kwargs) or {
                "ok": True,
                "verified": True,
                "after": {"lyrics": {"sha256": kwargs["lyrics_sha256"]}},
            },
        )
        store = SimpleNamespace(
            get_operation=lambda operation_id: source if operation_id == source.id else None
        )
        navidrome = SimpleNamespace(configured=lambda: False)
        context = SimpleNamespace(
            store=store,
            library_inspector=inspector,
            navidrome_client=navidrome,
        )
        return context, source, snapshot, written

    def make_item(self, canonical=None):
        canonical = canonical if canonical is not None else self.canonical
        return LyricsUpdateItem(
            file_path="Artist/Track.flac",
            source_operation_id="lyrics-fetch-1",
            source_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            translated_lines=["第一句", "第二句"],
        )

    def test_legacy_complete_text_payload_remains_supported(self):
        item = LyricsUpdateItem(file_path="Artist/Track.flac", lyrics="[00:01.00]完整歌词")
        self.assertEqual(item.lyrics_sha256, hashlib.sha256(item.lyrics.encode("utf-8")).hexdigest())

    def test_agent_payload_requires_source_binding_and_excludes_final_text(self):
        item = LyricsUpdateItem(
            file_path="Artist/Track.flac",
            source_operation_id="lyrics-fetch-1",
            source_sha256="a" * 64,
            translated_lines=["译文"],
        )
        self.assertEqual(item.source_operation_id, "lyrics-fetch-1")
        with self.assertRaises(ValueError):
            LyricsUpdateItem(
                file_path="Artist/Track.flac",
                lyrics="[00:01.00]伪造的最终文本",
                source_operation_id="lyrics-fetch-1",
                source_sha256="a" * 64,
                translated_lines=["译文"],
            )

    def test_rejects_missing_payload_or_partial_source_binding(self):
        with self.assertRaises(ValueError):
            LyricsUpdateItem(file_path="Artist/Track.flac")
        with self.assertRaises(ValueError):
            LyricsUpdateItem(
                file_path="Artist/Track.flac",
                source_operation_id="lyrics-fetch-1",
                translated_lines=["译文"],
            )

    def test_agent_update_rebuilds_from_bound_source_and_target_identity(self):
        canonical = "[00:01.00]first\n[00:01.00]second"
        source = SimpleNamespace(
            id="lyrics-fetch-1",
            action="lyrics.fetch",
            status="succeeded",
            input={"provider": "lrclib", "provider_id": "track-7"},
            result={
                "canonical_lrc": canonical,
                "provider": "lrclib",
                "provider_id": "track-7",
                "identity": {"title": "Track", "artist": "Artist", "album": "Album"},
            },
        )
        context = SimpleNamespace(
            store=SimpleNamespace(get_operation=lambda operation_id: source if operation_id == source.id else None),
            library_inspector=SimpleNamespace(
                evidence_for_file=lambda *_args, **_kwargs: {
                    "ok": True,
                    "exists": True,
                    "metadata": {"title": "Track", "artist": "Artist", "album": "Album"},
                }
            ),
        )
        item = LyricsUpdateItem(
            file_path="Artist/Track.flac",
            source_operation_id=source.id,
            source_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            translated_lines=["第一句", "第二句"],
        )
        lyrics, digest = lyrics_for_existing_update(context, item)
        self.assertEqual(
            lyrics,
            "[00:01.00]first\n[00:01.00]第一句\n[00:01.00]second\n[00:01.00]第二句",
        )
        self.assertEqual(digest, hashlib.sha256(lyrics.encode("utf-8")).hexdigest())

    def test_preview_and_execution_write_the_same_bilingual_payload(self):
        context, _source, _snapshot, written = self.make_context()
        model = UpdateLyricsInput(items=[self.make_item()])
        preview = build_mutation_preview(context, model, action="library.update_lyrics")
        target = preview["items"][0]["target"]
        result = _update_lyrics(
            context,
            SimpleNamespace(approval_id=None, mutation_plan=preview),
            model,
        ).payload

        self.assertEqual(target["lyrics_text"], self.expected_bilingual)
        self.assertEqual(
            target["lyrics_sha256"],
            hashlib.sha256(self.expected_bilingual.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(result["overall_status"], "succeeded")
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["lyrics"], target["lyrics_text"])
        self.assertEqual(written[0]["lyrics_sha256"], target["lyrics_sha256"])
        self.assertTrue(written[0]["apply"])

    def test_execution_rejects_tampered_preview_hash_without_writing(self):
        context, _source, _snapshot, written = self.make_context()
        model = UpdateLyricsInput(items=[self.make_item()])
        preview = build_mutation_preview(context, model, action="library.update_lyrics")
        preview["items"][0]["target"]["lyrics_sha256"] = "0" * 64
        result = _update_lyrics(
            context,
            SimpleNamespace(approval_id=None, mutation_plan=preview),
            model,
        ).payload

        self.assertEqual(result["overall_status"], "failed")
        self.assertEqual(result["results"][0]["error"], "stale_lyrics_payload")
        self.assertEqual(written, [])

    def test_resolved_mask_operation_can_supply_canonical_translation_source(self):
        original = "[00:01.00]hello ****"
        resolved = "[00:01.00]hello world"
        original_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
        resolved_sha = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
        fetch = SimpleNamespace(
            id="lyrics-fetch-1",
            action="lyrics.fetch",
            status="succeeded",
            input={"provider": "lrclib", "provider_id": "track-7"},
            result={
                "canonical_lrc": original,
                "canonical_lrc_sha256": original_sha,
                "provider": "lrclib",
                "provider_id": "track-7",
                "identity": {"title": "Track", "artist": "Artist", "album": "Album"},
            },
        )
        resolution = SimpleNamespace(
            id="lyrics-resolve-1",
            action="lyrics.resolve_masks",
            status="succeeded",
            input={},
            result={
                "resolution_status": "resolved",
                "canonical_source_operation_id": fetch.id,
                "input_canonical_lrc_sha256": original_sha,
                "resolved_canonical_lrc": resolved,
                "resolved_canonical_lrc_sha256": resolved_sha,
                "resolutions": [{"canonical_timestamp": "00:01.00", "method": "agent_resolved"}],
            },
        )
        context, _source, _snapshot, _written = self.make_context()
        context.store.get_operation = lambda operation_id: {
            fetch.id: fetch,
            resolution.id: resolution,
        }.get(operation_id)
        item = LyricsUpdateItem(
            file_path="Artist/Track.flac",
            source_operation_id=resolution.id,
            source_sha256=resolved_sha,
            translated_lines=["你好，世界"],
        )
        lyrics, digest = lyrics_for_existing_update(context, item)
        self.assertEqual(lyrics, "[00:01.00]hello world\n[00:01.00]你好，世界")
        self.assertEqual(digest, hashlib.sha256(lyrics.encode("utf-8")).hexdigest())

    def test_staging_path_assembles_translation_and_records_audit_hashes(self):
        context, source, _snapshot, _written = self.make_context()
        search = SimpleNamespace(
            id="lyrics-search-1",
            action="lyrics.search",
            status="succeeded",
            input={},
            result={"candidates": [{"provider": "lrclib", "provider_id": "track-7", "title": "Track", "artist": "Artist", "album": "Album"}]},
            error=None,
        )
        context.store.get_operation = lambda operation_id: {
            source.id: source,
            search.id: search,
        }.get(operation_id)
        model = StagedLyricsInput(
            status="translated",
            source_operation_id=source.id,
            source_sha256=hashlib.sha256(self.canonical.encode("utf-8")).hexdigest(),
            translated_lines=["第一句", "第二句"],
            evidence_operation_ids=[search.id],
        )

        lyrics, audit = _lyrics_for_staging(
            context,
            model,
            track={"title": "Track", "artist": "Artist", "album": "Album"},
        )

        self.assertEqual(lyrics, self.expected_bilingual)
        self.assertEqual(audit["canonical_sha256"], model.source_sha256)
        self.assertEqual(audit["translated_sha256"], hashlib.sha256("第一句\n第二句".encode("utf-8")).hexdigest())
        self.assertEqual(audit["final_sha256"], hashlib.sha256(lyrics.encode("utf-8")).hexdigest())

    def test_mask_resolution_staging_rejects_mismatched_original_source_hash(self):
        context, _source, _snapshot, _written = self.make_context()
        original = "[00:01.00]masked ****"
        resolved = "[00:01.00]masked word"
        resolved_sha = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
        fetch = SimpleNamespace(
            id="lyrics-fetch-1", action="lyrics.fetch", status="succeeded", input={},
            result={"canonical_lrc": original, "provider": "lrclib", "provider_id": "track-7"}, error=None,
        )
        resolution = SimpleNamespace(
            id="lyrics-resolve-1", action="lyrics.resolve_masks", status="succeeded", input={}, error=None,
            result={
                "resolution_status": "resolved", "canonical_source_operation_id": fetch.id,
                "input_canonical_lrc_sha256": "0" * 64,
                "resolved_canonical_lrc": resolved, "resolved_canonical_lrc_sha256": resolved_sha,
                "resolutions": [{"canonical_timestamp": "00:01.00", "method": "agent_inferred"}],
            },
        )
        context.store.get_operation = lambda operation_id: {
            fetch.id: fetch,
            resolution.id: resolution,
        }.get(operation_id)
        model = StagedLyricsInput(
            status="translated", source_operation_id=resolution.id, source_sha256=resolved_sha,
            translated_lines=["译文"], evidence_operation_ids=[],
        )

        with self.assertRaisesRegex(ActionFailure, "masked_lyrics_source_binding_mismatch"):
            _lyrics_for_staging(context, model, track={"title": "Track", "artist": "Artist"})

    def test_unresolved_mask_operation_is_rejected(self):
        context, source, _snapshot, written = self.make_context()
        source.action = "lyrics.resolve_masks"
        source.result = {"resolution_status": "unresolved"}
        context.store.get_operation = lambda operation_id: source if operation_id == source.id else None
        item = LyricsUpdateItem(
            file_path="Artist/Track.flac",
            source_operation_id=source.id,
            source_sha256="a" * 64,
            translated_lines=["译文"],
        )
        with self.assertRaisesRegex(ActionFailure, "masked_lyrics_resolution_not_resolved"):
            lyrics_for_existing_update(context, item)
        self.assertEqual(written, [])

    def test_execution_rejects_expired_file_snapshot_without_writing(self):
        context, _source, snapshot, written = self.make_context()
        model = UpdateLyricsInput(items=[self.make_item()])
        preview = build_mutation_preview(context, model, action="library.update_lyrics")
        snapshot["sha256"] = "file-changed-after-preview"
        result = _update_lyrics(
            context,
            SimpleNamespace(approval_id=None, mutation_plan=preview),
            model,
        ).payload

        self.assertEqual(result["results"][0]["error"], "stale_approval")
        self.assertEqual(written, [])

    def test_execution_rejects_changed_canonical_source_without_writing(self):
        context, source, _snapshot, written = self.make_context()
        model = UpdateLyricsInput(items=[self.make_item()])
        preview = build_mutation_preview(context, model, action="library.update_lyrics")
        source.result["canonical_lrc"] = "[00:01.00]changed source\n[00:01.00]second"
        with self.assertRaisesRegex(ActionFailure, "canonical_sha256_mismatch"):
            _update_lyrics(
                context,
                SimpleNamespace(approval_id=None, mutation_plan=preview),
                model,
            )
        self.assertEqual(written, [])

    def test_preview_rejects_failed_canonical_source(self):
        context, _source, _snapshot, written = self.make_context(source_status="failed")
        with self.assertRaisesRegex(ActionFailure, "lyrics_source_operation_not_succeeded"):
            build_mutation_preview(
                context,
                UpdateLyricsInput(items=[self.make_item()]),
                action="library.update_lyrics",
            )
        self.assertEqual(written, [])

    def test_preview_rejects_track_identity_mismatch(self):
        context, _source, _snapshot, written = self.make_context(
            target_identity={"title": "Different Track", "artist": "Other Artist", "album": "Other"}
        )
        with self.assertRaisesRegex(ActionFailure, "lyrics_identity_unconfirmed"):
            build_mutation_preview(
                context,
                UpdateLyricsInput(items=[self.make_item()]),
                action="library.update_lyrics",
            )
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
