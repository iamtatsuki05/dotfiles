#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Install and configure Homebrew with packages from Brewfile
# =============================================================================

readonly BREW_PATH="/opt/homebrew/bin/brew"
readonly ZPROFILE="$HOME/.zprofile"
readonly ZSHRC="$HOME/.zshrc"
readonly SHELLENV_LINE='eval "$(/opt/homebrew/bin/brew shellenv)"'
readonly MISE_LINE='eval "$(/opt/homebrew/bin/mise activate zsh)"'

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
command_exists() {
  command -v "$1" >/dev/null 2>&1
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
  if [[ -x "$BREW_PATH" ]]; then
    eval "$("$BREW_PATH" shellenv)"
  fi
}

install_packages() {
  echo "Installing packages from global Brewfile..."
  brew bundle --global
}

configure_mise() {
  echo "Configuring mise..."
  append_if_missing "$ZSHRC" "$MISE_LINE"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  install_homebrew
  configure_shell_environment
  install_packages
  configure_mise

  echo "Homebrew setup complete"
}

main "$@"
