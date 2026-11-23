#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/remote_dev_lib.sh"

REMOTE_ROOT="${MUSICOPS_PROD_ROOT:-/mnt/user/appdata/musicops}"
REMOTE_ROOT_Q="$(musicops_shell_quote "$REMOTE_ROOT")"
BUILD_COMMIT="${MUSICOPS_BUILD_COMMIT:?MUSICOPS_BUILD_COMMIT is required}"
if [[ ! "$BUILD_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid_backup_build_commit" >&2
  exit 2
fi

BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)-${BUILD_COMMIT:0:12}"
musicops_remote_exec "
  set -euo pipefail
  umask 077
  remote_root=$REMOTE_ROOT_Q
  backup_id='$BACKUP_ID'
  backup_dir=\"\$remote_root/release-backups/\$backup_id\"
  mkdir -p \"\$remote_root/release-backups\"
  test ! -e \"\$backup_dir\" || { echo release_backup_already_exists >&2; exit 2; }
  mkdir -m 700 \"\$backup_dir\"
  rollback_tag=\"musicops:rollback-\$backup_id\"
  completed=0
  backup_dir_created=1
  rollback_tag_created=0
  db_tmp=\"/tmp/musicops-release-backup-\$backup_id.sqlite3\"
  cleanup_incomplete() {
    docker exec musicops rm -f \"\$db_tmp\" >/dev/null 2>&1 || true
    if test \"\$completed\" = 0; then
      if test \"\$rollback_tag_created\" = 1; then
        docker image rm \"\$rollback_tag\" >/dev/null 2>&1 || true
      fi
      if test \"\$backup_dir_created\" = 1; then
        rm -rf \"\$backup_dir\"
      fi
    fi
  }
  trap cleanup_incomplete EXIT

  cd \"\$remote_root\"
  test -s docker-compose.v4.yml
  test -s .env.runtime
  image_id=\"\$(docker inspect musicops --format '{{.Image}}')\"
  test \"\$(docker image inspect \"\$image_id\" --format '{{index .Config.Labels \"org.senyaolab.musicops.image-kind\"}}')\" = application
  test \"\$(docker inspect musicops --format '{{.State.Status}}')\" = running
  test \"\$(docker inspect musicops --format '{{.State.Health.Status}}')\" = healthy
  if docker image inspect \"\$rollback_tag\" >/dev/null 2>&1; then
    echo rollback_image_tag_already_exists >&2
    exit 2
  fi
  previous_commit=\"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_BUILD_COMMIT=//p')\"
  previous_tree=\"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_BUILD_TREE=//p')\"
  previous_manifest=\"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_SOURCE_MANIFEST_SHA256=//p')\"
  previous_schema=\"\$(docker image inspect \"\$image_id\" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_ACTION_SCHEMA_DIGEST=//p')\"
  for value in \"\$previous_commit\" \"\$previous_tree\" \"\$previous_manifest\" \"\$previous_schema\"; do
    test -n \"\$value\" || { echo running_image_provenance_incomplete >&2; exit 2; }
  done
  docker tag \"\$image_id\" \"\$rollback_tag\"
  rollback_tag_created=1

  cp docker-compose.v4.yml \"\$backup_dir/docker-compose.v4.yml\"
  cp .env.runtime \"\$backup_dir/.env.runtime\"
  chmod 600 \"\$backup_dir/docker-compose.v4.yml\" \"\$backup_dir/.env.runtime\"
  MUSICOPS_IMAGE=\"\$image_id\" MUSICOPS_IMAGE_ID=\"\$image_id\" \\
    docker compose --env-file \"\$backup_dir/.env.runtime\" \\
      -f \"\$backup_dir/docker-compose.v4.yml\" config --quiet

  db_tmp=\"/tmp/musicops-release-backup-\$backup_id.sqlite3\"
  docker exec musicops python -c 'import sqlite3,sys; source=sqlite3.connect(\"/data/musicops-v2.db\"); target=sqlite3.connect(sys.argv[1]); source.backup(target); result=target.execute(\"PRAGMA quick_check\").fetchone()[0]; target.close(); source.close(); assert result == \"ok\", result' \"\$db_tmp\"
  docker cp \"musicops:\$db_tmp\" \"\$backup_dir/musicops-v2.db\"
  docker exec musicops rm -f \"\$db_tmp\"
  chmod 600 \"\$backup_dir/musicops-v2.db\"

  compose_sha=\"\$(sha256sum \"\$backup_dir/docker-compose.v4.yml\" | awk '{print \$1}')\"
  env_sha=\"\$(sha256sum \"\$backup_dir/.env.runtime\" | awk '{print \$1}')\"
  db_sha=\"\$(sha256sum \"\$backup_dir/musicops-v2.db\" | awk '{print \$1}')\"
  created_utc=\"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
  manifest_tmp=\"\$backup_dir/manifest.txt.tmp\"
  printf '%s\\n' \\
    'format=musicops-release-backup-v1' \\
    \"backup_id=\$backup_id\" \\
    \"image_id=\$image_id\" \\
    \"rollback_tag=\$rollback_tag\" \\
    \"build_commit=\$previous_commit\" \\
    \"build_tree=\$previous_tree\" \\
    \"source_manifest_sha256=\$previous_manifest\" \\
    \"action_schema_digest=\$previous_schema\" \\
    \"compose_sha256=\$compose_sha\" \\
    \"env_sha256=\$env_sha\" \\
    \"database_sha256=\$db_sha\" \\
    'database_quick_check=ok' \\
    \"created_utc=\$created_utc\" > \"\$manifest_tmp\"
  chmod 600 \"\$manifest_tmp\"
  mv \"\$manifest_tmp\" \"\$backup_dir/manifest.txt\"
  test \"\$(docker image inspect \"\$rollback_tag\" --format '{{.Id}}')\" = \"\$image_id\"
  completed=1
  trap - EXIT
  printf '%s\\n' \"\$backup_id\"
"
