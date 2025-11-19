#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Main setup script for dotfiles configuration
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$SCRIPT_DIR"
readonly SCRIPTS_DIR="$REPO_ROOT/scripts"
readonly DOTFILES_DIR="$REPO_ROOT/dotfiles"

# -----------------------------------------------------------------------------
# Logging helpers
# -----------------------------------------------------------------------------
log_step() {
  echo "===> $*"
}

log_success() {
  echo "✓ $*"
}

log_error() {
  echo "✗ ERROR: $*" >&2
}

# -----------------------------------------------------------------------------
# Setup steps
# -----------------------------------------------------------------------------
copy_dotfiles() {
  log_step "Copying dotfiles to home directory"
  cp -r "$DOTFILES_DIR"/. ~/
  log_success "Dotfiles copied"
}

install_homebrew() {
  log_step "Installing Homebrew and packages"
  sh "$SCRIPTS_DIR/brew_install.sh"
  log_success "Homebrew setup complete"
}

setup_defaults() {
  log_step "Configuring macOS default settings"
  sh "$SCRIPTS_DIR/default_setup.sh"
  log_success "Default settings configured"
}

setup_configs() {
  log_step "Setting up application configs"
  sh "$SCRIPTS_DIR/setup_config.sh"
  log_success "Application configs set up"
}

setup_neovim() {
  log_step "Setting up Neovim"
  sh "$SCRIPTS_DIR/setup_nvim.sh"
  log_success "Neovim setup complete"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  echo "Starting dotfiles setup..."
  echo

  copy_dotfiles
  install_homebrew
  setup_defaults
  setup_configs
  setup_neovim

  echo
  echo "Setup completed successfully!"
  echo "Please restart your terminal to apply all changes."
}

main "$@"
