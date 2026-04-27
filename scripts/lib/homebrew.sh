#!/usr/bin/env bash

dotfiles_find_homebrew() {
  local candidate
  local candidate_dir
  local path_rest="$PATH"

  if [[ -n "${HOMEBREW_PREFIX:-}" ]]; then
    candidate="${HOMEBREW_PREFIX}/bin/brew"
    if [[ -x "$candidate" ]]; then
      REPLY="$candidate"
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  while :; do
    if [[ "$path_rest" == *:* ]]; then
      candidate_dir="${path_rest%%:*}"
      path_rest="${path_rest#*:}"
    else
      candidate_dir="$path_rest"
      path_rest=""
    fi
    [[ -n "$candidate_dir" ]] || continue
    candidate="${candidate_dir%/}/brew"
    if [[ -x "$candidate" ]]; then
      REPLY="$candidate"
      printf '%s\n' "$candidate"
      return 0
    fi
    [[ -n "$path_rest" ]] || break
  done

  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$candidate" ]]; then
      REPLY="$candidate"
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

dotfiles_has_homebrew() {
  dotfiles_find_homebrew >/dev/null 2>&1
}

dotfiles_prepend_homebrew_to_path() {
  local brew_path
  local brew_dir

  dotfiles_find_homebrew >/dev/null 2>&1 || return 1
  brew_path="$REPLY"
  brew_dir="${brew_path%/*}"

  case ":$PATH:" in
    *":$brew_dir:"*)
      ;;
    *)
      PATH="$brew_dir:$PATH"
      export PATH
      ;;
  esac

  return 0
}
