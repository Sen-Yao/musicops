#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${MUSICOPS_REMOTE_HOST:-Yggdrasil}"
DEV_PORT="${MUSICOPS_DEV_HOST_PORT:-18086}"
DEV_TOKEN="${MUSICOPS_DEV_TOKEN:-dev-token}"
BASE_URL="http://127.0.0.1:${DEV_PORT}"

cd "$(dirname "$0")/.."
source ./scripts/remote_dev_lib.sh

musicops_remote_exec "
  set -e
  printf 'live: '
  curl -fsS '${BASE_URL}/health/live'
  printf '\nready: '
  curl -fsS '${BASE_URL}/health/ready'
  printf '\nactions: '
  curl -fsS -H 'Authorization: Bearer ${DEV_TOKEN}' '${BASE_URL}/actions'
  printf '\ncontainer: '
  docker inspect musicops-dev --format 'status={{.State.Status}} running={{.State.Running}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} image={{.Config.Image}}'
"
