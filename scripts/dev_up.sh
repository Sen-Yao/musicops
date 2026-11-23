#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${MUSICOPS_REMOTE_HOST:-Yggdrasil}"
REMOTE_ROOT="${MUSICOPS_DEV_ROOT:-/mnt/user/appdata/musicops-dev}"
REMOTE_SOURCE="${REMOTE_ROOT}/source"
DEV_PORT="${MUSICOPS_DEV_HOST_PORT:-18086}"
DEV_BIND="${MUSICOPS_DEV_BIND:-127.0.0.1}"
DEV_TOKEN="${MUSICOPS_DEV_TOKEN:-dev-token}"
LIBRARY_PATH="${MUSICOPS_LIBRARY_PATH:-/mnt/user/media/music}"
SESSION_PATH="${TELEGRAM_SESSION_PATH:-/tmp}"

cd "$(dirname "$0")/.."
source ./scripts/remote_dev_lib.sh

"$(dirname "$0")/dev_sync.sh"

musicops_remote_exec "
  set -e
  mkdir -p '$REMOTE_ROOT/data'
  chown 1000:1000 '$REMOTE_ROOT/data'
"

musicops_remote_exec "
  set -e
  cd '$REMOTE_SOURCE'
  MUSICOPS_API_TOKEN='$DEV_TOKEN' \
  MUSICOPS_DEV_SOURCE='$REMOTE_SOURCE' \
  MUSICOPS_DEV_DATA='$REMOTE_ROOT/data' \
  MUSICOPS_DEV_BIND='$DEV_BIND' \
  MUSICOPS_DEV_HOST_PORT='$DEV_PORT' \
  MUSICOPS_LIBRARY_PATH='$LIBRARY_PATH' \
  TELEGRAM_SESSION_PATH='$SESSION_PATH' \
  docker compose -f docker-compose.dev.yml up -d --build musicops-dev
"

echo "musicops-dev requested on ${REMOTE_HOST} at ${DEV_BIND}:${DEV_PORT}"
