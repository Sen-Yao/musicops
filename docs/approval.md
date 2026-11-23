# Approval Contract

Library mutations follow `MUSICOPS_MUTATION_MODE` and the authenticated action
catalog. The default `strict` mode reads current Yggdrasil state and returns an
immutable preview for explicit user approval. In `personal_auto`, the same
server-generated snapshot is saved as the operation's `mutation_plan` and the
mutation is queued directly, without a manual approval or approval record.
Both modes recheck current hashes and verify writes; automatic mode changes
human confirmation, not file integrity or collision handling. Only act within
the user's requested changes; deletion remains permanent.

Existing explicit approval records remain stored and executable through their
authenticated endpoint with the same provenance and staleness checks. Changing
modes does not execute or consume them automatically.

The remaining approval workflow describes `strict` mode:


```json
{
  "approval_required": true,
  "approval": {
    "approval_id": "approval_...",
    "action": "library.delete",
    "intent_sha256": "...",
    "review_path": "/approvals/approval_.../review",
    "review_url": "https://musicops.example.com/approvals/approval_.../review",
    "preview": {
      "authoritative_source": "musicops_server_current_state",
      "fixed_batch_sha256": "...",
      "permanent_delete": true,
      "items": []
    }
  }
}
```

`review_url` is present only when `MUSICOPS_PUBLIC_BASE_URL` is configured.
The path is backed by the random `approval_id`. It is a public, read-only
capability so a reviewer can open it without a token. It never accepts an
approval command and cannot mutate the library. Treat a leaked URL as
preview-data disclosure. All JSON approval metadata and all execution requests
still require the bearer token.

The review endpoint renders HTML on Yggdrasil. For lyrics updates it reads
cover data through the server library inspector and embeds it only when the
bytes match the preview's SHA-256. It does not call another API or require a
client-side renderer. The page sets `no-store`, a restrictive CSP, and
referrer/frame protections.

The item list and hashes are immutable. Approval authorization is single-use
and has no wall-clock expiry; payload retention is a separate ledger policy.
The action schema and contract digests are checked again before consumption.
Missing provenance, a changed contract/schema, invalid stored input, or a stale
file leaves the approval unconsumed.

After the user approves the exact displayed page:

```http
POST /approvals/approval_.../execute
Authorization: Bearer <token>
Content-Type: application/json

{
  "request_id": "execute-delete-001",
  "actor": "user",
  "reason": "User approved the exact displayed preview",
  "approved_at": 1784200000.0
}
```

MusicOps compares each current path and hash with the preview immediately before
writing. Changed items fail as `stale_approval`; other fixed-batch items may
succeed, and every item result is reported. Successful writes perform readback
and request one Navidrome refresh when configured. `library.delete` is
permanent and has no restore endpoint.

Administrative cleanup of abandoned approvals is separate from execution and
does not authorize a mutation.
