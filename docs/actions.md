# Action Contract

## Envelope

```json
{
  "action": "audio.transcribe",
  "input": {
    "file_path": "Artist/Album/Song.flac",
    "start_seconds": 31.5,
    "end_seconds": 38.0,
    "quality": "balanced"
  },
  "request_id": "stt-song-window-001"
}
```

Submit it with `POST /operations` and poll
`GET /operations/{operation_id}` until `succeeded`, `failed`,
`uncertain`, or `cancelled`. Use `?full=true` only when a large result is
needed. The authoritative catalog is `GET /actions`; use
`GET /actions?name=library.search&full=true` for one input schema.

Every authenticated request uses:

```http
Authorization: Bearer <token>
```

No request accepts an uploaded file. `file_path` and temporary-file IDs are
resolved inside MusicOps on Yggdrasil.

## Catalog

| Area | Actions |
|---|---|
| Library read | `library.search`, `library.read`, `library.album_diagnose`, `library.album_metadata_audit` |
| Library mutation | `library.import`, `library.add`, `library.update_metadata`, `library.update_lyrics`, `library.update_cover`, `library.move`, `library.delete` |
| Download | `download.search`, `download.fetch` |
| Canonical metadata | `metadata.search`, `metadata.fetch` |
| Lyrics/language | `lyrics.search`, `lyrics.fetch`, `lyrics.resolve_masks` |
| Audio | `audio.transcribe` |
| Navidrome | `navidrome.search`, `navidrome.verify`, `navidrome.refresh` |
| Temporary files | `temporary.inspect`, `temporary.delete`, `library.import.prepare` |

Mutation actions follow the live execution policy: `personal_auto` persists a
server mutation snapshot and returns an operation; `strict` returns an approval
preview. The approval workflow is documented in [approval.md](approval.md).

Each descriptor includes an input `schema_digest`, a static
`contract_digest`, availability, risk, and a confirmation policy. The policy
is `none`, `explicit_intent_sufficient`, or
`explicit_preview_approval`. Configured processor actions identify their
data-handling boundary.

## Normalized Import

The normal import path is:

1. Select one canonical release with `metadata.search` and
   `metadata.fetch`.
2. Run `download.search`, then call `download.fetch` with its operation ID,
   candidate index, and the selected canonical metadata operation/manifest/track.
3. Resolve content rating and best-effort lyrics/translation/cover evidence.
4. Call `library.import.prepare` to create one normalized staged artifact.
5. Submit `library.import`; when the response requires approval, review and
   approve exactly that server-generated artifact.
6. Inspect the publication read-back. Run `navidrome.verify` with
   `expected_present: true` when playback visibility is requested or when
   troubleshooting indexing.

The library write is complete after its file and managed metadata read-back
passes. Navidrome visibility may lag; do not repeat the import just because
indexing is delayed.

The target filename and metadata are server-derived. Existing different bytes
produce `replacement_decision_required`; MusicOps never overwrites or adds a
numeric suffix. Staged artifacts expire after 30 days and downloaded temporary
audio after seven days. `library.add` is an explicit audio-only legacy path.

Candidate title/artist/album comparisons are advisory. `download.fetch` and
`library.import.prepare` return `identity_match` and `warnings` when the text
differs or is ambiguous; they do not reject a selected candidate on that basis.
The agent evaluates those differences using the user's selection and available
evidence. The comparison and warning persist through download storage, staged
audit metadata, and the import review page. No override flag or separate warning
approval is required. Candidate snapshot binding, manifest/file hashes, target
collision checks, and the configured execution policy remain hard checks.

## Lyrics And Metadata

Provider results are candidates, not identity proof. Agent-generated lyrics
translations must bind the canonical source operation and its SHA-256, plus
identity evidence, to the exact target. MusicOps assembles bilingual LRC
deterministically from ordered translation entries; it does not call a
translation model. Submit through the live-supported `library.import.prepare`
or `library.update_lyrics` contract. The write follows the live execution policy.

Canonical metadata comes from a succeeded metadata operation, not filenames,
model knowledge, or a mixture of releases. Update only fields in the approved
preview. For masked lyrics, resolve every remaining mask through the documented
`lyrics.resolve_masks` contract before translation or write-back.

`library.album_metadata_audit` scans audio files directly and reports
`scan_status`, counts, relative paths, readable metadata, read failures, and
files skipped by a scan budget or interruption. An absent, empty, or
whitespace-only `album` value is missing. Provider lookups are evidence-bound
candidate suggestions; ambiguous or insufficient matches remain
`needs_manual_review`. The audit never writes library files. Any write must go
through `library.update_metadata` and its existing approval preview.

## Projection And Failure Semantics

Default operation responses summarize large lyrics, images, transcripts, and raw
provider payloads. Full values are available only through an authenticated
`?full=true` request and follow ledger retention rules. Secrets, credentials,
and authenticated URLs never appear in input or output.

- `failed`: no uncertain side effect remains.
- `uncertain`: a non-repeatable action may have side effects; inspect the
  original operation and do not blindly resubmit.
- `partial_success`: item results contain both successes and failures.
- `stale_approval`: create and review a new preview.
- `published_pending_visibility`: the library write passed readback but
  Navidrome visibility is not yet proven.

After a successful library mutation, inspect read-back facts and verify
Navidrome visibility. Deletion uses `expected_present: false` and is
permanent.


### Best-effort preparation and compatibility

Text identity disagreements, unknown content rating without STT, untranslated
original lyrics, skipped lyrics candidates, unresolved masks, missing optional
metadata, skipped supplemental sources, and rejected optional cover data produce
structured warnings. Preparation and import review retain those warnings. Invalid
cover bytes are omitted; incompatible supplemental releases are never merged.
Essential title/artist and provider binding, hashes, quality evidence, file
confinement, collision checks and exact readback remain enforced.

`MUSICOPS_MUTATION_MODE=personal_auto` automatically executes every requested
library mutation, including add, normalized import, metadata, lyrics, cover,
move and delete. Each operation persists its server-generated mutation snapshot
and retains hash/staleness checks and readback. `strict` requires exact preview
approval for those same actions. The live catalog reports the effective policy;
read-only, download and preparation actions remain asynchronous in both modes.
Stable request retries return the original operation and snapshot, including
after a source has been consumed or a file moved/deleted. Lyric requests may omit the payload hash; the server derives it and still
verifies exact readback. Explicitly supplied incorrect hashes remain rejected.
Translation prompt `gateway_v15` preserves unresolved masks without guessing.
