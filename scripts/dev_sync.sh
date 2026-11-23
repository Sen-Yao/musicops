#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${MUSICOPS_REMOTE_HOST:-Yggdrasil}"
REMOTE_ROOT="${MUSICOPS_DEV_ROOT:-/mnt/user/appdata/musicops-dev}"
REMOTE_SOURCE="${REMOTE_ROOT}/source"

cd "$(dirname "$0")/.."

source ./scripts/remote_dev_lib.sh

MUSICOPS_SYNC_EXCLUDES=(
  '.git'
  '.venv'
  '__pycache__'
  '.pytest_cache'
  '.ruff_cache'
  '.mypy_cache'
  'data'
  'tmp'
  'temp'
  '.env'
  '.env.*'
  '*.session'
  '*.pyc'
)

musicops_remote_exec "mkdir -p '$REMOTE_SOURCE' '$REMOTE_ROOT/data'"
musicops_remote_sync "." "$REMOTE_SOURCE"

echo "Synced MusicOps source to ${REMOTE_HOST}:${REMOTE_SOURCE}"
