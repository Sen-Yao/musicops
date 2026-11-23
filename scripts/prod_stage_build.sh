#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MUSICOPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(git -C "$MUSICOPS_ROOT" rev-parse --show-toplevel)"
source "$SCRIPT_DIR/remote_dev_lib.sh"
source "$SCRIPT_DIR/release_git_guard.sh"
musicops_require_main_clean "$REPOSITORY_ROOT"

REMOTE_HOST="${MUSICOPS_REMOTE_HOST:-Yggdrasil}"
REMOTE_ROOT="${MUSICOPS_PROD_ROOT:-/mnt/user/appdata/musicops}"
BUILD_KIND="${MUSICOPS_BUILD_KIND:-app}"
TAG="${MUSICOPS_IMAGE_TAG:-musicops:v4-$(date -u +%Y%m%d%H%M%S)}"
BUILD_COMMIT="${MUSICOPS_BUILD_COMMIT:-$(git -C "$REPOSITORY_ROOT" rev-parse HEAD)}"
BUILD_TREE="$(git -C "$REPOSITORY_ROOT" rev-parse "${BUILD_COMMIT}^{tree}")"
BUILD_TIME="${MUSICOPS_BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

if [[ "$BUILD_COMMIT" != "$(git -C "$REPOSITORY_ROOT" rev-parse HEAD)" ]]; then
  echo "build_commit_must_equal_main_head" >&2
  exit 2
fi
if [[ ! "$TAG" =~ ^musicops:[A-Za-z0-9_.-]+$ ]]; then
  echo "invalid_musicops_image_tag" >&2
  exit 2
fi

TEMPORARY_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMPORARY_ROOT"' EXIT
CONTEXT_ROOT="$TEMPORARY_ROOT/context"
mkdir -p "$CONTEXT_ROOT"
git -C "$REPOSITORY_ROOT" archive --format=tar "${BUILD_COMMIT}:musicops" | tar -xf - -C "$CONTEXT_ROOT"
if find "$CONTEXT_ROOT" -name '._*' -print -quit | grep -q .; then
  echo "archive_contains_appledouble_artifact" >&2
  exit 2
fi

manifest_json="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$CONTEXT_ROOT/src" python3 -m musicops.provenance write \
  --root "$CONTEXT_ROOT" --output "$CONTEXT_ROOT/.musicops-source-manifest")"
SOURCE_MANIFEST_SHA256="$(printf '%s' "$manifest_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["manifest_sha256"])')"
BUILD_PYTHON="${MUSICOPS_BUILD_PYTHON:-$REPOSITORY_ROOT/.venv/bin/python}"
if [[ ! -x "$BUILD_PYTHON" ]]; then
  BUILD_PYTHON="$(command -v python3)"
fi
ACTION_SCHEMA_DIGEST="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$CONTEXT_ROOT/src" "$BUILD_PYTHON" -c \
  'from musicops.actions import build_registry, registry_digest; print(registry_digest(build_registry()))')"
if find "$CONTEXT_ROOT" \( -name '._*' -o -name '__pycache__' -o -name '*.pyc' \) -print -quit | grep -q .; then
  echo "generated_build_context_contains_stale_artifact" >&2
  exit 2
fi

case "$BUILD_KIND" in
  app)
    DOCKERFILE="Dockerfile.hotfix"
    BASE_REFERENCE="${MUSICOPS_RUNTIME_BASE_IMAGE:?MUSICOPS_RUNTIME_BASE_IMAGE is required for app builds}"
    BASE_IMAGE_ID="$(musicops_remote_exec "
      set -euo pipefail
      image_id=\"\$(docker image inspect '$BASE_REFERENCE' --format '{{.Id}}')\"
      test \"\$(docker image inspect \"\$image_id\" --format '{{index .Config.Labels \"org.senyaolab.musicops.image-kind\"}}')\" = runtime-base
      printf '%s\\n' \"\$image_id\"
    ")"
    ;;
  runtime-base)
    if [[ "${MUSICOPS_RUNTIME_BASE_CONFIRMED:-}" != "CREATE_CLEAN_RUNTIME_BASE_WITHOUT_SYSTEM_INSTALL" ]]; then
      echo "Set MUSICOPS_RUNTIME_BASE_CONFIRMED=CREATE_CLEAN_RUNTIME_BASE_WITHOUT_SYSTEM_INSTALL" >&2
      exit 2
    fi
    DOCKERFILE="Dockerfile.runtime-base"
    BASE_REFERENCE="${MUSICOPS_RUNTIME_SOURCE_IMAGE:?MUSICOPS_RUNTIME_SOURCE_IMAGE is required}"
    BASE_IMAGE_ID="$(musicops_remote_exec "
      set -euo pipefail
      image_id=\"\$(docker image inspect '$BASE_REFERENCE' --format '{{.Id}}')\"
      docker run --rm --entrypoint sh \"\$image_id\" -c 'command -v ffmpeg >/dev/null && python --version >/dev/null'
      printf '%s\\n' \"\$image_id\"
    ")"
    ;;
  *)
    echo "MUSICOPS_BUILD_KIND must be app or runtime-base" >&2
    exit 2
    ;;
esac

request_text="$(printf '%s\n' \
  "tag=$TAG" \
  "build_kind=$BUILD_KIND" \
  "dockerfile=$DOCKERFILE" \
  "base_image_id=$BASE_IMAGE_ID" \
  "build_commit=$BUILD_COMMIT" \
  "build_tree=$BUILD_TREE" \
  "build_time=$BUILD_TIME" \
  "source_manifest_sha256=$SOURCE_MANIFEST_SHA256" \
  "action_schema_digest=$ACTION_SCHEMA_DIGEST")"
REQUEST_SHA256="$(printf '%s' "$request_text" | shasum -a 256 | awk '{print $1}')"
BUILD_ROOT="${REMOTE_ROOT}/build-requests/${REQUEST_SHA256}"
TAG_KEY="$(printf '%s' "$TAG" | tr -c 'A-Za-z0-9_.-' '_')"
TAG_LOCK_ROOT="${REMOTE_ROOT}/build-requests/tags/${TAG_KEY}"
BUILD_STATUS="${BUILD_ROOT}/status"
BUILD_LOG="${BUILD_ROOT}/build.log"
BUILD_PID="${BUILD_ROOT}/runner.pid"
BUILD_RUNNER="${BUILD_ROOT}/runner.sh"
BUILD_RESULT="${BUILD_ROOT}/result"
REQUEST_ENCODED="$(printf '%s' "$request_text" | base64 | tr -d '\n')"

echo "Build kind: ${BUILD_KIND}"
echo "Image tag: ${TAG}"
echo "Base image ID: ${BASE_IMAGE_ID}"
echo "Git commit: ${BUILD_COMMIT}"
echo "Git tree: ${BUILD_TREE}"
echo "Source manifest SHA-256: ${SOURCE_MANIFEST_SHA256}"
echo "Action schema digest: ${ACTION_SCHEMA_DIGEST}"
echo "Build time: ${BUILD_TIME}"
echo "Build request SHA-256: ${REQUEST_SHA256}"
echo "Remote status: ${REMOTE_HOST}:${BUILD_STATUS}"
echo "Remote log: ${REMOTE_HOST}:${BUILD_LOG}"
echo "System dependency installation: no"

remote_state="$(musicops_remote_exec "
  set -euo pipefail
  mkdir -p '${REMOTE_ROOT}/build-requests/tags'
  if mkdir '$TAG_LOCK_ROOT' 2>/dev/null; then
    printf '%s\\n' '$REQUEST_SHA256' > '$TAG_LOCK_ROOT/request_sha256'
  else
    test \"\$(cat '$TAG_LOCK_ROOT/request_sha256')\" = '$REQUEST_SHA256' || { printf 'tag-request-mismatch\\n'; exit 0; }
  fi
  if test -s '$BUILD_STATUS'; then cat '$BUILD_STATUS'; else printf 'missing\\n'; fi
")"
if [[ "$remote_state" == "tag-request-mismatch" ]]; then
  echo "remote_build_tag_request_mismatch" >&2
  exit 2
fi

if [[ "$remote_state" == "missing" || "$remote_state" == "prepared" || "$remote_state" == failed:* ]]; then
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$CONTEXT_ROOT" -cf "$TEMPORARY_ROOT/context.tar" .
  if tar -tf "$TEMPORARY_ROOT/context.tar" | grep -E '(^|/)\._' >/dev/null; then
    echo "generated_build_archive_contains_appledouble_artifact" >&2
    exit 2
  fi
  musicops_remote_exec "rm -rf '$BUILD_ROOT'; mkdir -p '$BUILD_ROOT/context'"
  musicops_remote_copy_archive "$TEMPORARY_ROOT/context.tar" "${BUILD_ROOT}/context.tar"
  musicops_remote_exec "
    set -euo pipefail
    tar -xf '$BUILD_ROOT/context.tar' -C '$BUILD_ROOT/context'
    rm -f '$BUILD_ROOT/context.tar'
    test -z \"\$(find '$BUILD_ROOT/context' -name '._*' -print -quit)\"
    printf '%s' '$REQUEST_ENCODED' | base64 -d > '$BUILD_ROOT/request'
    printf 'prepared\\n' > '$BUILD_STATUS'
  "
  BASE_BUILD_REFERENCE="musicops:build-base-${REQUEST_SHA256:0:16}"
  if [[ "$BUILD_KIND" == "app" ]]; then
    docker_command="docker tag '$BASE_IMAGE_ID' '$BASE_BUILD_REFERENCE' && test \"\$(docker image inspect '$BASE_BUILD_REFERENCE' --format '{{.Id}}')\" = '$BASE_IMAGE_ID' && docker build --network=host --progress=plain -f '$DOCKERFILE' --build-arg MUSICOPS_BASE_IMAGE='$BASE_BUILD_REFERENCE' --build-arg MUSICOPS_BUILD_COMMIT='$BUILD_COMMIT' --build-arg MUSICOPS_BUILD_TREE='$BUILD_TREE' --build-arg MUSICOPS_BUILD_TIME='$BUILD_TIME' --build-arg MUSICOPS_SOURCE_MANIFEST_SHA256='$SOURCE_MANIFEST_SHA256' --build-arg MUSICOPS_ACTION_SCHEMA_DIGEST='$ACTION_SCHEMA_DIGEST' -t '$TAG' ."
    verify_command="test \"\$(docker image inspect '$TAG' --format '{{index .Config.Labels \"org.senyaolab.musicops.image-kind\"}}')\" = application && docker run --rm --entrypoint python '$TAG' -m musicops.provenance verify --root /app --manifest /app/.musicops-source-manifest --expected-sha256 '$SOURCE_MANIFEST_SHA256' && test \"\$(docker run --rm --entrypoint python '$TAG' -c 'from musicops.actions import build_registry,registry_digest; print(registry_digest(build_registry()))')\" = '$ACTION_SCHEMA_DIGEST'"
  else
    docker_command="docker tag '$BASE_IMAGE_ID' '$BASE_BUILD_REFERENCE' && test \"\$(docker image inspect '$BASE_BUILD_REFERENCE' --format '{{.Id}}')\" = '$BASE_IMAGE_ID' && docker build --network=host --progress=plain -f '$DOCKERFILE' --build-arg MUSICOPS_RUNTIME_SOURCE_IMAGE='$BASE_BUILD_REFERENCE' --build-arg MUSICOPS_BUILD_COMMIT='$BUILD_COMMIT' --build-arg MUSICOPS_BUILD_TREE='$BUILD_TREE' --build-arg MUSICOPS_BUILD_TIME='$BUILD_TIME' -t '$TAG' ."
    verify_command="test \"\$(docker image inspect '$TAG' --format '{{index .Config.Labels \"org.senyaolab.musicops.image-kind\"}}')\" = runtime-base && docker run --rm --entrypoint sh '$TAG' -c 'test ! -e /app && command -v ffmpeg >/dev/null && python -c \"import importlib.util; assert importlib.util.find_spec(\\\"musicops\\\") is None\"'"
  fi
  runner_script="$(printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    "status_file='$BUILD_STATUS'" \
    "log_file='$BUILD_LOG'" \
    "context_root='$BUILD_ROOT/context'" \
    "result_file='$BUILD_RESULT'" \
    "base_build_reference='$BASE_BUILD_REFERENCE'" \
    'finish() {' \
    '  code=$?' \
    '  set +e' \
    '  if [[ "$code" -eq 0 ]]; then printf "succeeded\\n" > "${status_file}.tmp"; else printf "failed:%s\\n" "$code" > "${status_file}.tmp"; fi' \
    '  mv "${status_file}.tmp" "$status_file"' \
    '  docker image rm "$base_build_reference" >/dev/null 2>&1 || true' \
    '  rm -rf "$context_root"' \
    '}' \
    'trap finish EXIT' \
    'printf "running\\n" > "${status_file}.tmp" && mv "${status_file}.tmp" "$status_file"' \
    'cd "$context_root" || exit 90' \
    "$docker_command > \"\$log_file\" 2>&1" \
    "$verify_command >> \"\$log_file\" 2>&1" \
    "docker image inspect '$TAG' --format '{{.Id}}' > \"\$result_file\"")"
  RUNNER_ENCODED="$(printf '%s' "$runner_script" | base64 | tr -d '\n')"
  launch_result="$(musicops_remote_exec "
    set -euo pipefail
    current=\"\$(cat '$BUILD_STATUS')\"
    case \"\$current\" in running|succeeded) printf 'attach:%s\\n' \"\$current\"; exit 0;; esac
    printf '%s' '$RUNNER_ENCODED' | base64 -d > '$BUILD_RUNNER'
    chmod 0700 '$BUILD_RUNNER'
    nohup '$BUILD_RUNNER' </dev/null >/dev/null 2>&1 &
    runner_pid=\"\$!\"
    printf '%s\\n' \"\$runner_pid\" > '$BUILD_PID'
    kill -0 \"\$runner_pid\"
    printf 'launched\\n'
  ")"
  case "$launch_result" in launched|attach:running|attach:succeeded) ;; *) echo "remote_build_launch_failed:${launch_result}" >&2; exit 1;; esac
elif [[ "$remote_state" != "running" && "$remote_state" != "succeeded" && "$remote_state" != "prepared" ]]; then
  echo "unexpected_remote_build_state:${remote_state}" >&2
  exit 1
fi

while true; do
  state="$(musicops_remote_exec "cat '$BUILD_STATUS'")"
  case "$state" in
    succeeded) break ;;
    failed:*)
      musicops_remote_exec "tail -n 100 '$BUILD_LOG'" >&2 || true
      echo "remote_build_failed:${state}" >&2
      exit 1
      ;;
    prepared|running)
      if [[ "$state" == "running" ]]; then
        musicops_remote_exec "runner_pid=\$(cat '$BUILD_PID'); kill -0 \"\$runner_pid\"; ps -p \"\$runner_pid\" -o args= | grep -F '$BUILD_RUNNER' >/dev/null" || {
          echo "remote_build_runner_stale" >&2
          exit 1
        }
      fi
      ;;
    *) echo "unexpected_remote_build_state:${state}" >&2; exit 1 ;;
  esac
  sleep 10
done

IMAGE_ID="$(musicops_remote_exec "cat '$BUILD_RESULT'")"
musicops_remote_exec "
  set -euo pipefail
  test \"\$(docker image inspect '$TAG' --format '{{.Id}}')\" = '$IMAGE_ID'
  if test '$BUILD_KIND' = app; then
    test \"\$(docker image inspect '$IMAGE_ID' --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_BUILD_COMMIT=//p')\" = '$BUILD_COMMIT'
    test \"\$(docker image inspect '$IMAGE_ID' --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_BUILD_TREE=//p')\" = '$BUILD_TREE'
    test \"\$(docker image inspect '$IMAGE_ID' --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_SOURCE_MANIFEST_SHA256=//p')\" = '$SOURCE_MANIFEST_SHA256'
    test \"\$(docker image inspect '$IMAGE_ID' --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MUSICOPS_ACTION_SCHEMA_DIGEST=//p')\" = '$ACTION_SCHEMA_DIGEST'
  fi
"

printf 'Built %s as %s\n' "$TAG" "$IMAGE_ID"
printf 'Provenance commit=%s tree=%s source_manifest=%s action_schema=%s build_time=%s request=%s\n' \
  "$BUILD_COMMIT" "$BUILD_TREE" "$SOURCE_MANIFEST_SHA256" "$ACTION_SCHEMA_DIGEST" "$BUILD_TIME" "$REQUEST_SHA256"
