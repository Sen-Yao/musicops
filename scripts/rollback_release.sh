#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/remote_dev_lib.sh"

MODE=apply
if [[ "${1:-}" == "--check" ]]; then
  MODE=check
  shift
fi
BACKUP_ID="${1:?Usage: rollback_release.sh [--check] BACKUP_ID}"
if [[ ! "$BACKUP_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]]; then
  echo "invalid_release_backup_id" >&2
  exit 2
fi
if [[ "$MODE" == apply && "${MUSICOPS_ROLLBACK_CONFIRMED:-}" != "RESTORE_VERIFIED_PRODUCTION_RELEASE" ]]; then
  echo "Set MUSICOPS_ROLLBACK_CONFIRMED=RESTORE_VERIFIED_PRODUCTION_RELEASE" >&2
  exit 2
fi

REMOTE_ROOT="${MUSICOPS_PROD_ROOT:-/mnt/user/appdata/musicops}"
REMOTE_ROOT_Q="$(musicops_shell_quote "$REMOTE_ROOT")"
musicops_remote_exec "
  set -euo pipefail
  umask 077
  remote_root=$REMOTE_ROOT_Q
  backup_id='$BACKUP_ID'
  backup_dir=\"\$remote_root/release-backups/\$backup_id\"
  manifest=\"\$backup_dir/manifest.txt\"
  test -d \"\$backup_dir\" && test -s \"\$manifest\"
  test \"\$(sed -n '1p' \"\$manifest\")\" = 'format=musicops-release-backup-v1'
  read_value() {
    awk -F= -v key=\"\$1\" '\$1 == key {n++; print substr(\$0,index(\$0,\"=\")+1)} END {if(n!=1) exit 1}' \"\$manifest\"
  }
  test \"\$(read_value backup_id)\" = \"\$backup_id\"
  image_id=\"\$(read_value image_id)\"
  rollback_tag=\"\$(read_value rollback_tag)\"
  build_commit=\"\$(read_value build_commit)\"
  compose_sha=\"\$(read_value compose_sha256)\"
  env_sha=\"\$(read_value env_sha256)\"
  db_sha=\"\$(read_value database_sha256)\"
  [[ \"\$image_id\" =~ ^sha256:[0-9a-f]{64}$ ]]
  [[ \"\$build_commit\" =~ ^[0-9a-f]{40}$ ]]
  test \"\$rollback_tag\" = \"musicops:rollback-\$backup_id\"
  test \"\$(sha256sum \"\$backup_dir/docker-compose.v4.yml\" | awk '{print \$1}')\" = \"\$compose_sha\"
  test \"\$(sha256sum \"\$backup_dir/.env.runtime\" | awk '{print \$1}')\" = \"\$env_sha\"
  test \"\$(sha256sum \"\$backup_dir/musicops-v2.db\" | awk '{print \$1}')\" = \"\$db_sha\"
  test \"\$(read_value database_quick_check)\" = ok
  docker image inspect \"\$image_id\" >/dev/null
  test \"\$(docker image inspect \"\$rollback_tag\" --format '{{.Id}}')\" = \"\$image_id\"
  MUSICOPS_IMAGE=\"\$image_id\" MUSICOPS_IMAGE_ID=\"\$image_id\" \\
    docker compose --env-file \"\$backup_dir/.env.runtime\" \\
      -f \"\$backup_dir/docker-compose.v4.yml\" config --quiet

  if test '$MODE' = check; then
    printf 'rollback_backup_valid id=%s image=%s commit=%s database=verified\\n' \"\$backup_id\" \"\$image_id\" \"\$build_commit\"
    exit 0
  fi

  compose_tmp=\"\$(mktemp \"\$remote_root/.docker-compose.rollback.XXXXXX\")\"
  env_tmp=\"\$(mktemp \"\$remote_root/.env.runtime.rollback.XXXXXX\")\"
  cleanup_tmp() { rm -f \"\$compose_tmp\" \"\$env_tmp\"; }
  trap cleanup_tmp EXIT
  cp \"\$backup_dir/docker-compose.v4.yml\" \"\$compose_tmp\"
  cp \"\$backup_dir/.env.runtime\" \"\$env_tmp\"
  chmod 600 \"\$compose_tmp\" \"\$env_tmp\"
  mv -f \"\$compose_tmp\" \"\$remote_root/docker-compose.v4.yml\"
  mv -f \"\$env_tmp\" \"\$remote_root/.env.runtime\"
  cd \"\$remote_root\"
  MUSICOPS_IMAGE=\"\$image_id\" MUSICOPS_IMAGE_ID=\"\$image_id\" \\
    docker compose --env-file .env.runtime -f docker-compose.v4.yml \\
      up -d --force-recreate --no-deps musicops
  for attempt in \$(seq 1 60); do
    if curl -fsS http://127.0.0.1:8086/health/ready >/dev/null; then break; fi
    if test \"\$attempt\" = 60; then
      docker logs --tail 160 musicops
      echo rollback_readiness_failed >&2
      exit 1
    fi
    sleep 2
  done
  test \"\$(docker inspect musicops --format '{{.Image}}')\" = \"\$image_id\"
  test \"\$(docker exec musicops python -c 'import json,urllib.request; print(json.load(urllib.request.urlopen(\"http://127.0.0.1:8088/health/ready\", timeout=10))[\"build\"][\"git_commit\"])')\" = \"\$build_commit\"
  printf 'rollback_verified id=%s image=%s commit=%s\\n' \"\$backup_id\" \"\$image_id\" \"\$build_commit\"
"
