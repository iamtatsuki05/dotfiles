#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Main setup script for dotfiles configuration
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$SCRIPT_DIR"
readonly SCRIPTS_DIR="$REPO_ROOT/scripts"
readonly DOTFILES_DIR="$REPO_ROOT/dotfiles"
readonly LIB_DIR="$SCRIPTS_DIR/lib"

source "$LIB_DIR/setup_profile.sh"
source "$LIB_DIR/homebrew.sh"

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

log_skip() {
  echo "- Skipped: $*"
}

# -----------------------------------------------------------------------------
# Setup steps
# -----------------------------------------------------------------------------
copy_dotfiles() {
  local profile="$1"

  log_step "Copying dotfiles to home directory"
  cp -r "$DOTFILES_DIR"/. ~/

  if [[ "$profile" == "cli" ]]; then
    cp "$DOTFILES_DIR/.Brewfile.cli" "$HOME/.Brewfile"
    log_success "CLI Brewfile activated at ~/.Brewfile"
  fi

  log_success "Dotfiles copied"
}

install_homebrew() {
  local profile="$1"

  log_step "Installing Homebrew and packages"
  zsh "$SCRIPTS_DIR/brew_install.sh" --profile "$profile"
  log_success "Homebrew setup complete"
}

activate_homebrew_environment() {
  log_step "Activating Homebrew environment for this setup run"

  local brew_path
  brew_path="$(dotfiles_brew_command)" || {
    log_error "brew is not installed or not found in PATH"
    return 1
  }

  eval "$("$brew_path" shellenv)"

  log_success "Homebrew environment activated"
}

configure_postgres_build_environment() {
  if ! dotfiles_is_macos; then
    log_skip "PostgreSQL Homebrew build environment is macOS-specific"
    return 0
  fi

  log_step "Configuring PostgreSQL build environment for mise"

  export HOMEBREW_PREFIX="${HOMEBREW_PREFIX:-$(brew --prefix)}"
  local icu_prefix
  local openssl_prefix
  local util_linux_prefix
  icu_prefix="$(brew --prefix icu4c)"
  openssl_prefix="$(brew --prefix openssl@3)"
  util_linux_prefix="$(brew --prefix util-linux)"

  # vfox-postgres defaults to --with-uuid=ossp on macOS, but that fails with the SDK uuid_t headers.
  export PATH="${icu_prefix}/bin:$PATH"
  export PKG_CONFIG_PATH="${icu_prefix}/lib/pkgconfig:$(brew --prefix curl)/lib/pkgconfig:$(brew --prefix zlib)/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
  export CPPFLAGS="-I${util_linux_prefix}/include ${CPPFLAGS:-}"
  export LDFLAGS="-L${util_linux_prefix}/lib ${LDFLAGS:-}"
  export LIBS="-luuid ${LIBS:-}"
  export POSTGRES_CONFIGURE_OPTIONS="--with-openssl --with-zlib --with-uuid=e2fs --with-libraries=${openssl_prefix}/lib:${icu_prefix}/lib:${util_linux_prefix}/lib --with-includes=${openssl_prefix}/include:${icu_prefix}/include:${util_linux_prefix}/include"

  log_success "PostgreSQL build environment configured"
}

setup_defaults() {
  if ! dotfiles_is_macos; then
    log_skip "macOS default settings are not available on $DOTFILES_OS_NAME"
    return 0
  fi

  log_step "Configuring macOS default settings"
  zsh "$SCRIPTS_DIR/default_setup.sh"
  log_success "Default settings configured"
}

setup_configs() {
  log_step "Setting up application configs"
  zsh "$SCRIPTS_DIR/setup_config.sh"
  log_success "Application configs set up"
}

setup_cron() {
  log_step "Syncing cron jobs"
  zsh "$SCRIPTS_DIR/setup_cron.sh"
  log_success "Cron jobs synced"
}

setup_git_hooks() {
  local profile="$1"

  log_step "Installing git hooks"
  zsh "$SCRIPTS_DIR/setup_git_hooks.sh" --profile "$profile"
  log_success "Git hooks installed"
}

mise_command() {
  if command -v mise >/dev/null 2>&1; then
    command -v mise
    return
  fi

  log_error "mise is not installed or not found in PATH"
  return 1
}

install_mise_tools() {
  log_step "Installing tools managed by mise"
  # Passing these via the shell environment works reliably, while mise config [env] did not affect vfox-postgres configure options.
  CPPFLAGS="${CPPFLAGS:-}" \
    LDFLAGS="${LDFLAGS:-}" \
    LIBS="${LIBS:-}" \
    PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}" \
    POSTGRES_CONFIGURE_OPTIONS="${POSTGRES_CONFIGURE_OPTIONS:-}" \
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
  zsh "$SCRIPTS_DIR/setup_nvim.sh"
  log_success "Neovim setup complete"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  dotfiles_parse_profile_args "main.sh" "$@"
  local profile="$DOTFILES_PROFILE"

  echo "Starting dotfiles setup..."
  echo "OS: $DOTFILES_OS_NAME"
  echo "Profile: $profile"
  echo

  copy_dotfiles "$profile"
  sync_agent_files
  install_homebrew "$profile"
  activate_homebrew_environment
  configure_postgres_build_environment
  if [[ "$profile" == "full" ]]; then
    setup_defaults
  else
    log_skip "macOS default settings are not part of the cli profile"
  fi
  setup_configs
  if [[ "$profile" == "full" ]]; then
    setup_cron
  else
    log_skip "cron sync is not part of the cli profile"
  fi
  setup_git_hooks "$profile"
  install_mise_tools
  setup_neovim

  echo
  echo "Setup completed successfully!"
  echo "Please restart your terminal to apply all changes."
}

main "$@"
