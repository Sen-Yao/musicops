#!/usr/bin/env bash

MUSICOPS_REMOTE_HOST="${MUSICOPS_REMOTE_HOST:-Yggdrasil}"
MUSICOPS_JUMP_HOST="${MUSICOPS_JUMP_HOST:-}"

musicops_shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

musicops_remote_exec() {
  local script="$1"
  local encoded
  local inner_command
  encoded="$(printf '%s' "$script" | base64 | tr -d '\n')"
  inner_command="printf %s $encoded | base64 -d | bash"
  if [[ -n "$MUSICOPS_JUMP_HOST" ]]; then
    ssh -J "$MUSICOPS_JUMP_HOST" "$MUSICOPS_REMOTE_HOST" "$inner_command"
  else
    ssh "$MUSICOPS_REMOTE_HOST" "$inner_command"
  fi
}

musicops_remote_copy_file() {
  local source_file="$1"
  local remote_path="$2"
  if [[ -n "$MUSICOPS_JUMP_HOST" ]]; then
    rsync -az -e "ssh -J ${MUSICOPS_JUMP_HOST}" \
      "$source_file" "${MUSICOPS_REMOTE_HOST}:${remote_path}"
  else
    rsync -az -e ssh \
      "$source_file" "${MUSICOPS_REMOTE_HOST}:${remote_path}"
  fi
}

musicops_remote_copy_archive() {
  local archive_file="$1"
  local remote_path="$2"
  musicops_remote_copy_file "$archive_file" "$remote_path"
}

musicops_remote_sync() {
  local source_dir="$1"
  local remote_dir="$2"
  local tar_excludes=()
  local exclude
  for exclude in "${MUSICOPS_SYNC_EXCLUDES[@]}"; do
    tar_excludes+=(--exclude="$exclude")
  done
  if [[ -n "$MUSICOPS_JUMP_HOST" ]]; then
    musicops_remote_exec "rm -rf '$remote_dir/.incoming' && mkdir -p '$remote_dir/.incoming'"
    tar -C "$source_dir" -czf - "${tar_excludes[@]}" . \
      | ssh -J "$MUSICOPS_JUMP_HOST" "$MUSICOPS_REMOTE_HOST" "tar -C '$remote_dir/.incoming' -xzf -"
    musicops_remote_exec "
      set -e
      find '$remote_dir' -mindepth 1 -maxdepth 1 ! -name .incoming -exec rm -rf {} +
      find '$remote_dir/.incoming' -mindepth 1 -maxdepth 1 -exec mv {} '$remote_dir/' \\;
      rmdir '$remote_dir/.incoming'
    "
  else
    musicops_remote_exec "rm -rf '$remote_dir/.incoming' && mkdir -p '$remote_dir/.incoming'"
    tar -C "$source_dir" -czf - "${tar_excludes[@]}" . \
      | ssh "$MUSICOPS_REMOTE_HOST" "tar -C '$remote_dir/.incoming' -xzf -"
    musicops_remote_exec "
      set -e
      find '$remote_dir' -mindepth 1 -maxdepth 1 ! -name .incoming -exec rm -rf {} +
      find '$remote_dir/.incoming' -mindepth 1 -maxdepth 1 -exec mv {} '$remote_dir/' \\;
      rmdir '$remote_dir/.incoming'
    "
  fi
}
