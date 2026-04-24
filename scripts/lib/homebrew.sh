#!/usr/bin/zsh

dotfiles_brew_command() {
  local brew_path

  if command -v brew >/dev/null 2>&1; then
    command -v brew
    return
  fi

  for brew_path in /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
    if [[ -x "$brew_path" ]]; then
      echo "$brew_path"
      return
    fi
  done

  if [[ -n "${HOMEBREW_PREFIX:-}" && -x "${HOMEBREW_PREFIX}/bin/brew" ]]; then
    echo "${HOMEBREW_PREFIX}/bin/brew"
    return
  fi

  brew_path="$(zsh -lic 'command -v brew' 2>/dev/null)" || true
  if [[ -n "$brew_path" && -x "$brew_path" ]]; then
    echo "$brew_path"
    return
  fi

  return 1
}
