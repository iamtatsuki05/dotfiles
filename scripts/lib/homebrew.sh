#!/usr/bin/env bash

dotfiles_find_homebrew() {
  local candidate

  if command -v brew >/dev/null 2>&1; then
    command -v brew
    return 0
  fi

  if [[ -n "${HOMEBREW_PREFIX:-}" ]]; then
    candidate="${HOMEBREW_PREFIX}/bin/brew"
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$candidate" ]]; then
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

  brew_path="$(dotfiles_find_homebrew 2>/dev/null)" || return 1
  brew_dir="$(dirname "$brew_path")"

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
