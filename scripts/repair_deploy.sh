#!/usr/bin/env bash
set -euo pipefail

if [[ "${MUSICOPS_REPAIR_CONFIRMED:-}" != "DEPLOY_WITH_VERIFIED_AUTOMATIC_ROLLBACK" ]]; then
  echo "Set MUSICOPS_REPAIR_CONFIRMED=DEPLOY_WITH_VERIFIED_AUTOMATIC_ROLLBACK" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MUSICOPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(git -C "$MUSICOPS_ROOT" rev-parse --show-toplevel)"
source "$SCRIPT_DIR/remote_dev_lib.sh"
source "$SCRIPT_DIR/release_git_guard.sh"
musicops_require_main_clean "$REPOSITORY_ROOT"

REMOTE_HOST="${MUSICOPS_REMOTE_HOST:-Yggdrasil}"
REMOTE_ROOT="${MUSICOPS_PROD_ROOT:-/mnt/user/appdata/musicops}"
IMAGE="${MUSICOPS_IMAGE_TAG:?MUSICOPS_IMAGE_TAG is required}"
BUILD_COMMIT="${MUSICOPS_BUILD_COMMIT:?MUSICOPS_BUILD_COMMIT is required}"
BUILD_TREE="${MUSICOPS_BUILD_TREE:?MUSICOPS_BUILD_TREE is required}"
BUILD_TIME="${MUSICOPS_BUILD_TIME:?MUSICOPS_BUILD_TIME is required}"
SOURCE_MANIFEST_SHA256="${MUSICOPS_SOURCE_MANIFEST_SHA256:?MUSICOPS_SOURCE_MANIFEST_SHA256 is required}"
ACTION_SCHEMA_DIGEST="${MUSICOPS_ACTION_SCHEMA_DIGEST:?MUSICOPS_ACTION_SCHEMA_DIGEST is required}"
EXPECTED_ACTION_COUNT="${MUSICOPS_EXPECTED_ACTION_COUNT:-25}"
EXPECTED_UNCONSUMED_APPROVALS="${MUSICOPS_EXPECTED_UNCONSUMED_APPROVALS:-0}"

if [[ "$BUILD_COMMIT" != "$(git -C "$REPOSITORY_ROOT" rev-parse HEAD)" ]]; then
  echo "deploy_commit_must_equal_main_head" >&2
  exit 2
fi
if [[ "$BUILD_TREE" != "$(git -C "$REPOSITORY_ROOT" rev-parse "${BUILD_COMMIT}^{tree}")" ]]; then
  echo "deploy_tree_must_equal_main_tree" >&2
  exit 2
fi

IMAGE_ID="$(musicops_remote_exec "
  set -euo pipefail
  image_id=\"\$(docker image inspect '$IMAGE' --format '{{.Id}}')\"
  test \"\$(docker image inspect \"\$image_id\" --format '{{index .Config.Labels \"org.senyaolab.musicops.image-kind\"}}')\" = application
  test \"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_BUILD_COMMIT=//p')\" = '$BUILD_COMMIT'
  test \"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_BUILD_TREE=//p')\" = '$BUILD_TREE'
  test \"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_BUILD_TIME=//p')\" = '$BUILD_TIME'
  test \"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_SOURCE_MANIFEST_SHA256=//p')\" = '$SOURCE_MANIFEST_SHA256'
  test \"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_ACTION_SCHEMA_DIGEST=//p')\" = '$ACTION_SCHEMA_DIGEST'
  printf '%s\\n' \"\$image_id\"
")"

LEDGER_BEFORE="$(musicops_remote_exec "
  set -euo pipefail
  docker exec musicops python -c \"import json,sqlite3; db=sqlite3.connect('/data/musicops-v2.db'); names={row[0] for row in db.execute('select name from sqlite_master')}; forbidden=names & {'tasks','gates','plans','candidates','jobs','evidence','artifacts'}; active=list(db.execute(\\\"select id,action,status from operations where status in ('queued','running') order by created_at\\\")); counts={'operations':db.execute('select count(*) from operations').fetchone()[0],'approvals':db.execute('select count(*) from approvals').fetchone()[0],'consumed':db.execute('select count(*) from approvals where consumed_by_operation_id is not null').fetchone()[0],'unconsumed':db.execute('select count(*) from approvals where consumed_by_operation_id is null').fetchone()[0]}; assert not forbidden, sorted(forbidden); assert not active, active; assert counts['unconsumed']==$EXPECTED_UNCONSUMED_APPROVALS, counts; print(json.dumps(counts,sort_keys=True)); db.close()\"
")"

musicops_remote_exec "
  set -euo pipefail
  cd '$REMOTE_ROOT'
  test -s .env.runtime
  docker run --rm --entrypoint python '$IMAGE_ID' -m musicops.provenance verify \
    --root /app --manifest /app/.musicops-source-manifest --expected-sha256 '$SOURCE_MANIFEST_SHA256'
  test \"\$(docker run --rm --entrypoint python '$IMAGE_ID' -c 'from musicops.actions import build_registry,registry_digest; print(registry_digest(build_registry()))')\" = '$ACTION_SCHEMA_DIGEST'
"

BACKUP_ID="$(MUSICOPS_REMOTE_HOST="$REMOTE_HOST" MUSICOPS_PROD_ROOT="$REMOTE_ROOT" MUSICOPS_BUILD_COMMIT="$BUILD_COMMIT" bash "$SCRIPT_DIR/backup_release.sh")"
if [[ ! "$BACKUP_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]]; then
  echo "release_backup_returned_invalid_id" >&2
  exit 1
fi

TEMPORARY_COMPOSE="$(mktemp)"
REMOTE_COMPOSE_TMP="${REMOTE_ROOT}/.docker-compose.v4.release-${BACKUP_ID}.tmp"
RELEASE_STARTED=0
rollback_on_failure() {
  local status=$?
  trap - EXIT
  rm -f "$TEMPORARY_COMPOSE"
  if [[ "$status" -ne 0 && "$RELEASE_STARTED" == 1 ]]; then
    echo "release_failed; attempting rollback from ${BACKUP_ID}" >&2
    if MUSICOPS_REMOTE_HOST="$REMOTE_HOST" MUSICOPS_PROD_ROOT="$REMOTE_ROOT" \
      MUSICOPS_ROLLBACK_CONFIRMED=RESTORE_VERIFIED_PRODUCTION_RELEASE \
      bash "$SCRIPT_DIR/rollback_release.sh" "$BACKUP_ID"; then
      echo "automatic_rollback_verified id=${BACKUP_ID}" >&2
    else
      echo "automatic_rollback_failed id=${BACKUP_ID}; manual recovery required" >&2
    fi
  fi
  exit "$status"
}
trap rollback_on_failure EXIT
git -C "$REPOSITORY_ROOT" show "${BUILD_COMMIT}:musicops/docker-compose.prod.yml" > "$TEMPORARY_COMPOSE"
musicops_remote_copy_file "$TEMPORARY_COMPOSE" "$REMOTE_COMPOSE_TMP"
RELEASE_STARTED=1
musicops_remote_exec "set -euo pipefail; test -s '$REMOTE_COMPOSE_TMP'; mv -f '$REMOTE_COMPOSE_TMP' '$REMOTE_ROOT/docker-compose.v4.yml'"

musicops_remote_exec "
  set -euo pipefail
  cd '$REMOTE_ROOT'
  test -s .env.runtime
  MUSICOPS_IMAGE='$IMAGE_ID' MUSICOPS_IMAGE_ID='$IMAGE_ID' docker compose --env-file .env.runtime -f docker-compose.v4.yml up -d --force-recreate --no-deps musicops
  for attempt in \$(seq 1 60); do
    if curl -fsS http://127.0.0.1:8086/health/ready >/dev/null; then
      break
    fi
    if test \"\$attempt\" = 60; then
      docker logs --tail 160 musicops
      exit 1
    fi
    sleep 2
  done
  test \"\$(docker inspect musicops --format '{{.Image}}')\" = '$IMAGE_ID'
  docker exec musicops python -m musicops.provenance verify \
    --root /app --manifest /app/.musicops-source-manifest --expected-sha256 '$SOURCE_MANIFEST_SHA256'
  docker exec -i musicops python - <<'PY'
import json
import os
import sqlite3
import urllib.request

expected = {
    'git_commit': '$BUILD_COMMIT',
    'git_tree': '$BUILD_TREE',
    'built_at': '$BUILD_TIME',
    'source_manifest_sha256': '$SOURCE_MANIFEST_SHA256',
    'action_schema_digest': '$ACTION_SCHEMA_DIGEST',
    'image_id': '$IMAGE_ID',
}
ready = json.load(urllib.request.urlopen('http://127.0.0.1:8088/health/ready', timeout=10))
assert ready['ok'] is True, ready
for key, value in expected.items():
    assert ready['build'][key] == value, (key, ready['build'][key], value)
request = urllib.request.Request(
    'http://127.0.0.1:8088/actions?full=true',
    headers={
        'Authorization': 'Bearer ' + (
            os.environ.get('MUSICOPS_API_TOKEN')
            or open(os.environ.get('MUSICOPS_API_TOKEN_FILE', '/run/secrets/musicops_api_token'), encoding='utf-8').read().strip()
        ),
    },
)
actions = json.load(urllib.request.urlopen(request, timeout=10))
assert actions['schema_digest'] == '$ACTION_SCHEMA_DIGEST', actions['schema_digest']
assert len(actions['actions']) == int('$EXPECTED_ACTION_COUNT'), len(actions['actions'])
by_name = {row['name']: row for row in actions['actions']}
assert 'lyrics.translate' not in by_name, 'lyrics.translate must remain removed'
prepare = by_name['library.import.prepare']
assert prepare['operation_profile'] == 'normalized_staged', prepare
prepare_properties = prepare['input_schema']['\$defs']['StagedLyricsInput']['properties']
assert {'source_operation_id', 'source_sha256', 'translated_lines'} <= set(prepare_properties), prepare_properties
update = by_name['library.update_lyrics']
update_item = update['input_schema']['\$defs']['LyricsUpdateItem']['properties']
assert {'source_operation_id', 'source_sha256', 'translated_lines'} <= set(update_item), update_item
resolver = by_name['lyrics.resolve_masks']
assert resolver['operation_profile'] == 'masked_lyrics_resolution', resolver
required = set(resolver['input_schema']['required'])
assert required == {'canonical_source_operation_id', 'canonical_lrc_sha256', 'identity_evidence_operation_id', 'music_identity', 'resolutions'}, required
database = sqlite3.connect('/data/musicops-v2.db')
counts = {
    'operations': database.execute('select count(*) from operations').fetchone()[0],
    'approvals': database.execute('select count(*) from approvals').fetchone()[0],
    'consumed': database.execute('select count(*) from approvals where consumed_by_operation_id is not null').fetchone()[0],
    'unconsumed': database.execute('select count(*) from approvals where consumed_by_operation_id is null').fetchone()[0],
    'active': database.execute('''select count(*) from operations where status in ('queued','running')''').fetchone()[0],
}
database.close()
assert counts['active'] == 0, counts
assert counts['unconsumed'] == int('$EXPECTED_UNCONSUMED_APPROVALS'), counts
print(json.dumps({'ready': True, 'action_count': len(actions['actions']), 'counts': counts}, sort_keys=True))
PY
"

MUSICOPS_REMOTE_HOST="$REMOTE_HOST" "$SCRIPT_DIR/verify_prod_runtime_contract.sh"

LEDGER_AFTER="$(musicops_remote_exec "docker exec musicops python -c \"import json,sqlite3; db=sqlite3.connect('/data/musicops-v2.db'); print(json.dumps({'operations':db.execute('select count(*) from operations').fetchone()[0],'approvals':db.execute('select count(*) from approvals').fetchone()[0],'consumed':db.execute('select count(*) from approvals where consumed_by_operation_id is not null').fetchone()[0],'unconsumed':db.execute('select count(*) from approvals where consumed_by_operation_id is null').fetchone()[0]},sort_keys=True)); db.close()\"")"
if [[ "$LEDGER_BEFORE" != "$LEDGER_AFTER" ]]; then
  echo "repair_deploy_ledger_counts_changed: before=${LEDGER_BEFORE} after=${LEDGER_AFTER}" >&2
  exit 1
fi

echo "Repaired production with ${IMAGE_ID}; ledger counts preserved: ${LEDGER_AFTER}; rollback backup: ${BACKUP_ID}."
