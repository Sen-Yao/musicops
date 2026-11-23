# MusicOps

MusicOps is the Yggdrasil-side control plane for an authorized personal music
library. It owns all library reads and writes, downloads, lyrics, translation,
STT, temporary files, approvals, the operation ledger, and Navidrome checks.

Client machines do not install a Python client or CLI. Agents use the public
HTTPS endpoint with `curl`/ `jq`, and keep the single bearer token in
Bitwarden. No endpoint accepts file uploads; paths refer to the Yggdrasil
library.

## HTTP Surface

```text
GET  /health/live
GET  /health/ready
GET  /actions
POST /operations
GET  /operations/{id}
POST /operations/{id}/cancel
GET  /approvals/{id}
GET  /approvals/{id}/review
POST /approvals/{id}/execute
```

Health and review pages are public. Every action, operation, approval metadata,
and execution endpoint requires the bearer token. A review URL is a random,
read-only capability and cannot execute an approval; the execute endpoint
always authenticates the token.

Operations are durable and asynchronous. Stable request IDs prevent duplicate
side effects. In `strict`, library mutations return an immutable server preview
and require one explicit approval. In `personal_auto`, they save the same
mutation snapshot and execute directly. Execution rechecks current hashes and
reports each item in both modes.
`library.delete` is permanent.

Large lyrics, images, transcripts, and provider payloads are summarized by
default. See [docs/actions.md](docs/actions.md) and
[docs/approval.md](docs/approval.md) for the HTTP contracts. The agent-facing
manual is [.agents/skills/music-manager/SKILL.md](../.agents/skills/music-manager/SKILL.md).

## Test

```bash
PYTHONPATH=src pytest -q
```
