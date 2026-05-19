#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Main setup script for dotfiles configuration
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$SCRIPT_DIR"
readonly SCRIPTS_DIR="$REPO_ROOT/scripts"
readonly LIB_DIR="$SCRIPTS_DIR/lib"
readonly NIX_INSTALL_URL="https://nixos.org/nix/install"
readonly NIX_INSTALL_SHELL="${DOTFILES_NIX_INSTALL_SHELL:-/bin/sh}"

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
install_nix() {
  local profile="$1"

  log_step "Applying Nix configuration"
  zsh "$SCRIPTS_DIR/nix_install.sh" --profile "$profile"
  log_success "Nix setup complete"
}

nix_command_exists() {
  command -v nix >/dev/null 2>&1
}

install_nix_daemon_if_needed() {
  local installer

  activate_nix_environment
  if nix_command_exists; then
    log_skip "Nix is already installed"
    return 0
  fi

  if ! dotfiles_is_macos; then
    log_error "nix is not installed or not found in PATH"
    echo "On Linux, install Nix first or use: zsh scripts/nix_portable_install.sh" >&2
    return 1
  fi

  if ! command -v curl >/dev/null 2>&1; then
    log_error "curl is required to install Nix"
    return 1
  fi

  log_step "Installing Nix daemon"
  installer="$(mktemp "${TMPDIR:-/tmp}/dotfiles-nix-install.XXXXXX")"
  (
    trap 'rm -f "$installer"' EXIT
    curl --fail --proto '=https' --tlsv1.2 -L "$NIX_INSTALL_URL" -o "$installer"
    "$NIX_INSTALL_SHELL" "$installer" --daemon --yes
  )

  activate_nix_environment
  if ! nix_command_exists; then
    log_error "Nix installation completed but nix is still not found in PATH"
    echo "Restart the terminal or source /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh, then rerun zsh main.sh." >&2
    return 1
  fi

  log_success "Nix installed"
}

install_homebrew_if_needed() {
  local profile="$1"

  log_step "Ensuring Homebrew is available when required"
  zsh "$SCRIPTS_DIR/install_homebrew.sh" --profile "$profile"
  dotfiles_prepend_homebrew_to_path || true
  log_success "Homebrew prerequisites checked"
}

activate_nix_environment() {
  log_step "Activating Nix environment for this setup run"

  local nix_profile_paths="${DOTFILES_NIX_PROFILE_PATHS-/run/current-system/sw/bin:/etc/profiles/per-user/$USER/bin:$HOME/.nix-profile/bin:/nix/var/nix/profiles/default/bin}"
  if [[ -n "$nix_profile_paths" ]]; then
    export PATH="$nix_profile_paths:$PATH"
  fi

  local hm_vars
  for hm_vars in \
    "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh" \
    "/etc/profiles/per-user/$USER/etc/profile.d/hm-session-vars.sh"
  do
    if [[ -r "$hm_vars" ]]; then
      source "$hm_vars"
    fi
  done

  log_success "Nix environment activated"
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
  CPPFLAGS="${CPPFLAGS:-}" \
    LDFLAGS="${LDFLAGS:-}" \
    LIBS="${LIBS:-}" \
    PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}" \
    "$(mise_command)" install
  log_success "mise tools installed"
}

sync_agent_files() {
  log_step "Syncing agent prompts and skills"
  zsh "$SCRIPTS_DIR/setup_agent_files.sh"
  log_success "Agent prompts and skills synced"
}

apply_chezmoi() {
  local profile="$1"

  log_step "Applying chezmoi home files"
  zsh "$SCRIPTS_DIR/chezmoi_apply.sh" --profile "$profile" --mark-default
  log_success "chezmoi home files applied"
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

  install_homebrew_if_needed "$profile"
  install_nix_daemon_if_needed
  install_nix "$profile"
  activate_nix_environment
  apply_chezmoi "$profile"
  sync_agent_files
  setup_git_hooks "$profile"
  install_mise_tools

  echo
  echo "Setup completed successfully!"
  echo "Please restart your terminal to apply all changes."
}

main "$@"
