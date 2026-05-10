#!/usr/bin/env zsh

dotfiles_resolve_command_from_path() {
  local command_name="$1"
  local candidate_dir
  local candidate
  local path_rest="$PATH"

  while :; do
    if [[ "$path_rest" == *:* ]]; then
      candidate_dir="${path_rest%%:*}"
      path_rest="${path_rest#*:}"
    else
      candidate_dir="$path_rest"
      path_rest=""
    fi
    [[ -n "$candidate_dir" ]] || continue
    candidate="${candidate_dir%/}/$command_name"
    if [[ -x "$candidate" && ! -d "$candidate" ]]; then
      REPLY="$candidate"
      return 0
    fi
    [[ -n "$path_rest" ]] || break
  done

  return 1
}

dotfiles_temporary_directory_root() {
  if dotfiles_is_macos 2>/dev/null; then
    REPLY="/private/tmp"
  else
    REPLY="${TMPDIR:-/tmp}"
  fi
}

dotfiles_create_unique_temp_directory() {
  local temp_root="$1"
  local prefix="$2"
  local suffix_index=0
  local candidate

  while ((suffix_index < 1024)); do
    candidate="$temp_root/${prefix}.$$.$suffix_index"
    if mkdir "$candidate" 2>/dev/null; then
      REPLY="$candidate"
      return 0
    fi
    suffix_index=$((suffix_index + 1))
  done

  echo "ERROR: failed to create a temporary directory in $temp_root for $prefix" >&2
  return 1
}

dotfiles_create_unique_temp_file() {
  local temp_root="$1"
  local prefix="$2"
  local suffix_index=0
  local candidate

  while ((suffix_index < 1024)); do
    candidate="$temp_root/${prefix}.$$.$suffix_index"
    if [[ ! -e "$candidate" ]]; then
      : > "$candidate"
      REPLY="$candidate"
      return 0
    fi
    suffix_index=$((suffix_index + 1))
  done

  echo "ERROR: failed to create a temporary file in $temp_root for $prefix" >&2
  return 1
}
