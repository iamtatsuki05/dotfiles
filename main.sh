#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Main setup script for dotfiles configuration
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$SCRIPT_DIR"
readonly SCRIPTS_DIR="$REPO_ROOT/scripts"
readonly DOTFILES_DIR="$REPO_ROOT/dotfiles"
readonly HOMEBREW_MISE_PATH="/opt/homebrew/bin/mise"

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

mise_command() {
  if command -v mise >/dev/null 2>&1; then
    command -v mise
    return
  fi

  if [ -x "$HOMEBREW_MISE_PATH" ]; then
    echo "$HOMEBREW_MISE_PATH"
    return
  fi

  log_error "mise is not installed or not found in PATH"
  return 1
}

install_mise_tools() {
  log_step "Installing tools managed by mise"
  "$(mise_command)" install
  log_success "mise tools installed"
}

sync_agent_files() {
  log_step "Syncing agent prompts and skills"
  zsh "$DOTFILES_DIR/.agent/sync.sh"
  log_success "Agent prompts and skills synced"
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
  sync_agent_files
  install_homebrew
  setup_defaults
  setup_configs
  install_mise_tools
  setup_neovim

  echo
  echo "Setup completed successfully!"
  echo "Please restart your terminal to apply all changes."
}

main "$@"
