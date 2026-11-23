#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MUSICOPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(git -C "$MUSICOPS_ROOT" rev-parse --show-toplevel)"
source "$SCRIPT_DIR/remote_dev_lib.sh"
source "$SCRIPT_DIR/release_git_guard.sh"
musicops_require_main_clean "$REPOSITORY_ROOT"

V4_IMAGE="${MUSICOPS_V4_IMAGE:?MUSICOPS_V4_IMAGE is required}"
RUNTIME_BASE_IMAGE="${MUSICOPS_RUNTIME_BASE_IMAGE:?MUSICOPS_RUNTIME_BASE_IMAGE is required}"
APPLY=0
if [[ "${MUSICOPS_IMAGE_CLEANUP_CONFIRMED:-}" == "DELETE_ALL_OLD_MUSICOPS_APPLICATION_IMAGES" ]]; then
  APPLY=1
fi

musicops_remote_exec "
  set -euo pipefail
  v4_id=\"\$(docker image inspect '$V4_IMAGE' --format '{{.Id}}')\"
  runtime_id=\"\$(docker image inspect '$RUNTIME_BASE_IMAGE' --format '{{.Id}}')\"
  current_id=\"\$(docker inspect musicops --format '{{.Image}}')\"
  test \"\$current_id\" = \"\$v4_id\"
  test \"\$(docker image inspect \"\$v4_id\" --format '{{index .Config.Labels \"org.senyaolab.musicops.image-kind\"}}')\" = application
  test \"\$(docker image inspect \"\$runtime_id\" --format '{{index .Config.Labels \"org.senyaolab.musicops.image-kind\"}}')\" = runtime-base
  test \"\$v4_id\" != \"\$runtime_id\"

  candidate_file=\"\$(mktemp)\"
  protected_file=\"\$(mktemp)\"
  trap 'rm -f \"\$candidate_file\" \"\$protected_file\"' EXIT
  docker image ls --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}' \
    | awk -F'|' '\$1 == \"musicops\" && \$2 ~ /^rollback-/ {print \$3}' \
    | sort -u > \"\$protected_file\"
  docker image ls --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}' \
    | awk -F'|' '\$1 == \"musicops\" {print}' \
    | while IFS='|' read -r repository tag image_id; do
        if test \"\$image_id\" != \"\$v4_id\" && test \"\$image_id\" != \"\$runtime_id\" \
          && ! grep -Fxq \"\$image_id\" \"\$protected_file\"; then
          printf '%s|%s|%s\\n' \"\$repository\" \"\$tag\" \"\$image_id\" >> \"\$candidate_file\"
        fi
      done

  while IFS='|' read -r repository tag image_id; do
    test -n \"\$image_id\" || continue
    references=\"\$(docker ps -aq | xargs -r docker inspect --format '{{.Name}}|{{.Image}}' | awk -F'|' -v target=\"\$image_id\" '\$2 == target {print \$1}')\"
    if test -n \"\$references\"; then
      printf 'old_musicops_image_still_referenced:%s:%s\\n' \"\$image_id\" \"\$references\" >&2
      exit 3
    fi
    printf 'candidate %s:%s %s\\n' \"\$repository\" \"\$tag\" \"\$image_id\"
  done < \"\$candidate_file\"

  if test '$APPLY' = 1; then
    while IFS='|' read -r repository tag image_id; do
      test -n \"\$image_id\" || continue
      docker image rm \"\$repository:\$tag\"
      printf 'deleted %s:%s %s\\n' \"\$repository\" \"\$tag\" \"\$image_id\"
    done < \"\$candidate_file\"
    cut -d'|' -f3 \"\$candidate_file\" | sort -u | while read -r image_id; do
      if docker image inspect \"\$image_id\" >/dev/null 2>&1; then
        docker image rm \"\$image_id\"
      fi
    done
  fi

  remaining=\"\$(docker image ls --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}' | awk -F'|' '\$1 == \"musicops\" {print}')\"
  if test '$APPLY' = 1; then
    unexpected=\"\$(printf '%s\\n' \"\$remaining\" | awk -F'|' -v v4=\"\$v4_id\" -v runtime=\"\$runtime_id\" 'NF == 3 && \$2 !~ /^rollback-/ && \$3 != v4 && \$3 != runtime {print}')\"
    test -z \"\$unexpected\" || { printf '%s\\n' \"\$unexpected\" >&2; exit 4; }
  fi
  printf 'keep application %s\\n' \"\$v4_id\"
  printf 'keep runtime-base %s\\n' \"\$runtime_id\"
  printf 'mode %s\\n' \"$([[ "$APPLY" == 1 ]] && printf apply || printf dry-run)\"
"
