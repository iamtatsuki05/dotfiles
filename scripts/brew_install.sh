#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Install and configure Homebrew with packages from Brewfile
# =============================================================================

readonly ZPROFILE="$HOME/.zprofile"
readonly ZSHRC="$HOME/.zshrc"
readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly DOTFILES_DIR="$REPO_ROOT/dotfiles"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly SHELLENV_LINE='if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; elif [ -x /home/linuxbrew/.linuxbrew/bin/brew ]; then eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"; elif command -v brew >/dev/null 2>&1; then eval "$(brew shellenv)"; fi'
readonly HOMEBREW_PREFIX_LINE='if command -v brew >/dev/null 2>&1; then export HOMEBREW_PREFIX="${HOMEBREW_PREFIX:-$(brew --prefix)}"; fi'
readonly POSTGRES_ICU_PATH_LINE='if [ -n "${HOMEBREW_PREFIX:-}" ]; then export PATH="${HOMEBREW_PREFIX}/opt/icu4c/bin:$PATH"; fi'
readonly POSTGRES_PKG_CONFIG_PATH_LINE='if [ -n "${HOMEBREW_PREFIX:-}" ]; then export PKG_CONFIG_PATH="${HOMEBREW_PREFIX}/opt/icu4c/lib/pkgconfig:${HOMEBREW_PREFIX}/opt/curl/lib/pkgconfig:${HOMEBREW_PREFIX}/opt/zlib/lib/pkgconfig:${PKG_CONFIG_PATH:-}"; fi'
readonly MISE_LINE='if command -v mise >/dev/null 2>&1; then eval "$(mise activate zsh)"; fi'

source "$LIB_DIR/setup_profile.sh"
source "$LIB_DIR/homebrew.sh"

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
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
  if dotfiles_brew_command >/dev/null; then
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
  brew_path="$(dotfiles_brew_command)" || return 1
  eval "$("$brew_path" shellenv)"
}

install_packages() {
  local profile="$1"
  local brewfile

  case "$profile" in
    full)
      brewfile="$DOTFILES_DIR/.Brewfile"
      ;;
    cli)
      brewfile="$DOTFILES_DIR/.Brewfile.cli"
      ;;
  esac

  if [ ! -f "$brewfile" ]; then
    echo "ERROR: Brewfile not found: $brewfile" >&2
    return 1
  fi

  echo "Installing packages from $brewfile..."
  brew bundle --file="$brewfile"
}

configure_mise() {
  echo "Configuring mise..."
  append_if_missing "$ZPROFILE" "$MISE_LINE"
}

configure_postgres_build_environment() {
  if ! dotfiles_is_macos; then
    echo "Skipping PostgreSQL Homebrew build environment on $DOTFILES_OS_NAME"
    return 0
  fi

  echo "Configuring PostgreSQL build environment..."
  append_if_missing "$ZSHRC" "$HOMEBREW_PREFIX_LINE"
  append_if_missing "$ZSHRC" "$POSTGRES_ICU_PATH_LINE"
  append_if_missing "$ZSHRC" "$POSTGRES_PKG_CONFIG_PATH_LINE"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  dotfiles_parse_profile_args "scripts/brew_install.sh" "$@"
  local profile="$DOTFILES_PROFILE"

  echo "Homebrew profile: $profile"
  install_homebrew
  configure_shell_environment
  install_packages "$profile"
  configure_postgres_build_environment
  configure_mise

  echo "Homebrew setup complete"
}

main "$@"
