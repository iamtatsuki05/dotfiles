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
SUDO_KEEPALIVE_PID=""
SKIP_MAS_APPS=0

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

usage() {
  dotfiles_print_profile_usage "main.sh"
  cat <<'EOF'

Options:
  --skip-mas-apps  Skip Mac App Store apps while keeping the selected profile.
EOF
}

parse_main_args() {
  local profile_args=()

  SKIP_MAS_APPS=0
  while (($#)); do
    case "$1" in
      --skip-mas-apps)
        SKIP_MAS_APPS=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --profile)
        profile_args+=("$1")
        shift
        if ((! $#)); then
          echo "ERROR: --profile requires a value" >&2
          return 1
        fi
        profile_args+=("$1")
        ;;
      *)
        profile_args+=("$1")
        ;;
    esac
    shift
  done

  dotfiles_parse_profile_args "main.sh" "${profile_args[@]}"
}

# -----------------------------------------------------------------------------
# Privilege helpers
# -----------------------------------------------------------------------------
cleanup_sudo_keepalive() {
  if [[ -n "$SUDO_KEEPALIVE_PID" ]]; then
    kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    wait "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    SUDO_KEEPALIVE_PID=""
  fi
}

prepare_sudo_authentication() {
  if ! dotfiles_is_macos; then
    return 0
  fi

  if [[ "${DOTFILES_SKIP_SUDO_KEEPALIVE:-0}" == "1" ]]; then
    log_skip "Sudo authentication keepalive disabled"
    return 0
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    log_skip "sudo is not available"
    return 0
  fi

  log_step "Preparing sudo authentication for this setup run"
  sudo -v
  sudo -n true
  (
    while true; do
      sleep 60
      sudo -n true 2>/dev/null || exit
    done
  ) &
  SUDO_KEEPALIVE_PID="$!"
  trap cleanup_sudo_keepalive EXIT
  log_success "Sudo authentication cached"
}

install_rosetta_if_needed() {
  local profile="$1"

  if ! dotfiles_is_macos || [[ "$profile" != "full" ]]; then
    return 0
  fi

  if [[ "${DOTFILES_SKIP_ROSETTA_INSTALL:-0}" == "1" ]]; then
    log_skip "Rosetta 2 install disabled"
    return 0
  fi

  if [[ "$(uname -m)" != "arm64" ]]; then
    log_skip "Rosetta 2 is not required on this Mac"
    return 0
  fi

  if command -v pkgutil >/dev/null 2>&1 && pkgutil --pkg-info com.apple.pkg.RosettaUpdateAuto >/dev/null 2>&1; then
    log_skip "Rosetta 2 is already installed"
    return 0
  fi

  if ! command -v softwareupdate >/dev/null 2>&1; then
    log_error "softwareupdate is required to install Rosetta 2"
    return 1
  fi

  log_step "Installing Rosetta 2 for Intel-only macOS installers"
  sudo softwareupdate --install-rosetta --agree-to-license
  log_success "Rosetta 2 installed"
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

prepend_mise_tool_dir() {
  local mise_bin="$1"
  local executable_name="$2"
  local resolved_path

  resolved_path="$("$mise_bin" which "$executable_name" 2>/dev/null || true)"
  if [[ -n "$resolved_path" && -x "$resolved_path" ]]; then
    export PATH="${resolved_path:h}:$PATH"
    REPLY="$resolved_path"
    return 0
  fi

  REPLY=""
  return 1
}

install_mise_bootstrap_tools() {
  local mise_bin="$1"
  local python_path

  log_step "Installing mise bootstrap tools"
  "$mise_bin" install python uv

  prepend_mise_tool_dir "$mise_bin" python3 || true
  python_path="$REPLY"
  prepend_mise_tool_dir "$mise_bin" uv || true
  if [[ -n "$python_path" ]]; then
    export CLOUDSDK_PYTHON="$python_path"
  fi

  log_success "mise bootstrap tools installed"
}

install_mise_tools() {
  local mise_bin

  log_step "Installing tools managed by mise"
  mise_bin="$(mise_command)"
  install_mise_bootstrap_tools "$mise_bin"
  CPPFLAGS="${CPPFLAGS:-}" \
    LDFLAGS="${LDFLAGS:-}" \
    LIBS="${LIBS:-}" \
    PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}" \
    "$mise_bin" install
  log_success "mise tools installed"
}

sync_agent_files() {
  log_step "Syncing agent prompts and skills"
  zsh "$SCRIPTS_DIR/setup_agent_files.sh"
  log_success "Agent prompts and skills synced"
}

install_mas_apps_best_effort() {
  local profile="$1"

  log_step "Installing Mac App Store apps best-effort"
  if (( SKIP_MAS_APPS )); then
    DOTFILES_SKIP_MAS_APPS=1 zsh "$SCRIPTS_DIR/install_mas_apps.sh" --profile "$profile"
  else
    zsh "$SCRIPTS_DIR/install_mas_apps.sh" --profile "$profile"
  fi
  log_success "Mac App Store app step complete"
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
  parse_main_args "$@"
  local profile="$DOTFILES_PROFILE"

  echo "Starting dotfiles setup..."
  echo "OS: $DOTFILES_OS_NAME"
  echo "Profile: $profile"
  echo

  prepare_sudo_authentication
  install_rosetta_if_needed "$profile"
  install_homebrew_if_needed "$profile"
  install_nix_daemon_if_needed
  install_nix "$profile"
  activate_nix_environment
  install_mas_apps_best_effort "$profile"
  apply_chezmoi "$profile"
  sync_agent_files
  setup_git_hooks "$profile"
  install_mise_tools

  echo
  echo "Setup completed successfully!"
  echo "Please restart your terminal to apply all changes."
}

main "$@"
