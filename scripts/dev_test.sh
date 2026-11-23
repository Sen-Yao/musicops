#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${MUSICOPS_REMOTE_HOST:-Yggdrasil}"

cd "$(dirname "$0")/.."
source ./scripts/remote_dev_lib.sh

musicops_remote_exec "
  set -e
  docker exec -e PYTHONPYCACHEPREFIX=/tmp/pycache musicops-dev python -m py_compile \
    /app/src/musicops/api.py \
    /app/src/musicops/actions.py \
    /app/src/musicops/library.py \
    /app/src/musicops/main.py
  docker exec -e PYTHONPYCACHEPREFIX=/tmp/pycache musicops-dev python -m pytest -q
"
