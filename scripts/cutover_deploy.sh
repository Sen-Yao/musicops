#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MUSICOPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(git -C "$MUSICOPS_ROOT" rev-parse --show-toplevel)"
source "$SCRIPT_DIR/release_git_guard.sh"
musicops_require_main_clean "$REPOSITORY_ROOT"

echo "cutover_deploy_is_disabled_in_v4_use_repair_deploy" >&2
exit 2
