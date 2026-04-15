#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Install and configure Homebrew with packages from Brewfile
# =============================================================================

readonly ZPROFILE="$HOME/.zprofile"
readonly ZSHRC="$HOME/.zshrc"
readonly SHELLENV_LINE='if command -v brew >/dev/null 2>&1; then eval "$(brew shellenv)"; fi'
readonly HOMEBREW_PREFIX_LINE='if command -v brew >/dev/null 2>&1; then export HOMEBREW_PREFIX="${HOMEBREW_PREFIX:-$(brew --prefix)}"; fi'
readonly POSTGRES_ICU_PATH_LINE='if [ -n "${HOMEBREW_PREFIX:-}" ]; then export PATH="${HOMEBREW_PREFIX}/opt/icu4c/bin:$PATH"; fi'
readonly POSTGRES_PKG_CONFIG_PATH_LINE='if [ -n "${HOMEBREW_PREFIX:-}" ]; then export PKG_CONFIG_PATH="${HOMEBREW_PREFIX}/opt/icu4c/lib/pkgconfig:${HOMEBREW_PREFIX}/opt/curl/lib/pkgconfig:${HOMEBREW_PREFIX}/opt/zlib/lib/pkgconfig:${PKG_CONFIG_PATH:-}"; fi'
readonly MISE_LINE='if command -v mise >/dev/null 2>&1; then eval "$(mise activate zsh)"; fi'

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
command_exists() {
  command -v "$1" >/dev/null 2>&1
}

brew_command() {
  if command_exists brew; then
    command -v brew
    return
  fi

  if [ -n "${HOMEBREW_PREFIX:-}" ] && [ -x "${HOMEBREW_PREFIX}/bin/brew" ]; then
    echo "${HOMEBREW_PREFIX}/bin/brew"
    return
  fi

  local brew_path
  brew_path="$(zsh -lic 'command -v brew' 2>/dev/null)" || true
  if [ -n "$brew_path" ] && [ -x "$brew_path" ]; then
    echo "$brew_path"
    return
  fi

  return 1
}

append_if_missing() {
  local file="$1"
  local line="$2"

  if ! grep -Fxq "$line" "$file" 2>/dev/null; then
    echo "$line" >> "$file"
    echo "Added to $file: $line"
  else
    echo "Already exists in $file: $line"
  fi
}

# -----------------------------------------------------------------------------
# Installation steps
# -----------------------------------------------------------------------------
install_homebrew() {
  if command_exists brew; then
    echo "Homebrew is already installed"
    return
  fi

  echo "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
}

configure_shell_environment() {
  echo "Configuring shell environment..."
  append_if_missing "$ZPROFILE" "$SHELLENV_LINE"

  # Activate brew for current session
  local brew_path
  brew_path="$(brew_command)" || return 1
  eval "$("$brew_path" shellenv)"
}

install_packages() {
  echo "Installing packages from global Brewfile..."
  brew bundle --global
}

configure_mise() {
  echo "Configuring mise..."
  append_if_missing "$ZSHRC" "$MISE_LINE"
}

configure_postgres_build_environment() {
  echo "Configuring PostgreSQL build environment..."
  append_if_missing "$ZSHRC" "$HOMEBREW_PREFIX_LINE"
  append_if_missing "$ZSHRC" "$POSTGRES_ICU_PATH_LINE"
  append_if_missing "$ZSHRC" "$POSTGRES_PKG_CONFIG_PATH_LINE"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  install_homebrew
  configure_shell_environment
  install_packages
  configure_postgres_build_environment
  configure_mise

  echo "Homebrew setup complete"
}

main "$@"
