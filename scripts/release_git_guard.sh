#!/usr/bin/env bash

musicops_require_main_clean() {
  local repository_root="$1"
  local branch
  branch="$(git -C "$repository_root" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ -z "$branch" ]]; then
    echo "release_refuses_detached_head" >&2
    return 2
  fi
  if [[ "$branch" != "main" ]]; then
    echo "release_requires_main_branch: ${branch}" >&2
    return 2
  fi
  if ! git -C "$repository_root" diff --quiet -- || ! git -C "$repository_root" diff --cached --quiet --; then
    echo "release_refuses_dirty_tracked_files" >&2
    return 2
  fi
  if [[ -n "$(git -C "$repository_root" ls-files --others --exclude-standard)" ]]; then
    echo "release_refuses_untracked_files" >&2
    return 2
  fi
}
