#!/usr/bin/zsh

set -euo pipefail
zmodload zsh/datetime

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h}"
readonly REMOVE_HOMEBREW_SCRIPT="$SCRIPT_DIR/remove_homebrew.sh"
readonly HOMEBREW_FALLBACK_CONFIG="$REPO_ROOT/config/nix/homebrew-fallback.nix"
readonly MAS_APPS_CONFIG="$REPO_ROOT/config/nix/mas-apps.nix"
readonly -a NIX_EXPERIMENTAL_ARGS=(--extra-experimental-features "nix-command flakes")
readonly HOME_MANAGER_BACKUP_EXTENSION="before-nix-darwin"
readonly DARWIN_SUDO_LOCAL_PATH="${DOTFILES_DARWIN_SUDO_LOCAL_PATH:-/etc/pam.d/sudo_local}"
readonly DARWIN_SUDO_LOCAL_BACKUP_PATH="${DARWIN_SUDO_LOCAL_PATH}.${HOME_MANAGER_BACKUP_EXTENSION}"
readonly DARWIN_ETC_SHELL_RC_PATHS="${DOTFILES_DARWIN_ETC_SHELL_RC_PATHS:-/etc/bashrc:/etc/zshrc}"
readonly HOME_MANAGER_BACKUP_ARCHIVE_EPOCH="${DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH:-$EPOCHSECONDS}"

source "$SCRIPT_DIR/lib/setup_profile.sh"
source "$SCRIPT_DIR/lib/homebrew.sh"
source "$SCRIPT_DIR/lib/homebrew_fallback.sh"
source "$SCRIPT_DIR/lib/runtime.sh"

DRY_RUN=0
PROFILE=""
INSTALL_GUI_APPS=0
UNINSTALL_HOMEBREW=0
HOST_ATTR=""
NIX_FLAKE_WORKTREE=""

usage() {
  cat <<EOF
Usage:
  zsh scripts/nix_install.sh [--profile full|cli] [options]
  zsh scripts/nix_install.sh --cli-only

Options:
  --profile full|cli       Select setup profile. Defaults to cli.
  --cli-only               Alias for --profile cli.
  --with-gui-apps          Include GUI apps. On Linux, DISPLAY or WAYLAND_DISPLAY is required.
  --dry-run                Build the selected configuration without switching.
  --host ATTR              Use an explicit flake output attribute.
  --uninstall-homebrew     Run scripts/remove_homebrew.sh --apply after a successful Nix switch.
  -h, --help               Show this help.

Default flake outputs include aarch64-darwin-full, aarch64-darwin-cli,
x86_64-darwin-full, x86_64-darwin-cli, aarch64-linux-full,
aarch64-linux-cli, x86_64-linux-full, and x86_64-linux-cli.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --profile)
        shift
        if ((! $#)); then
          echo "ERROR: --profile requires full or cli" >&2
          return 1
        fi
        PROFILE="$1"
        ;;
      --profile=*)
        PROFILE="${1#--profile=}"
        ;;
      --cli-only)
        PROFILE="cli"
        ;;
      --with-gui-apps)
        INSTALL_GUI_APPS=1
        ;;
      --host)
        shift
        if ((! $#)); then
          echo "ERROR: --host requires a flake output attribute" >&2
          return 1
        fi
        HOST_ATTR="$1"
        ;;
      --host=*)
        HOST_ATTR="${1#--host=}"
        ;;
      --uninstall-homebrew)
        UNINSTALL_HOMEBREW=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        usage >&2
        return 1
        ;;
    esac
    shift
  done

  if [[ -z "$PROFILE" ]]; then
    default_profile
    PROFILE="$REPLY"
  fi
  case "$PROFILE" in
    full|cli)
      ;;
    *)
      echo "ERROR: unknown profile: $PROFILE" >&2
      return 1
      ;;
  esac
}

default_profile() {
  REPLY="cli"
}

system_attr() {
  local os_name machine

  if dotfiles_is_macos; then
    os_name="Darwin"
  else
    os_name="Linux"
  fi
  machine="${CPUTYPE:-${MACHTYPE%%-*}}"

  case "$os_name:$machine" in
    Darwin:arm64|Darwin:aarch64)
      REPLY="aarch64-darwin"
      ;;
    Darwin:x86_64)
      REPLY="x86_64-darwin"
      ;;
    Linux:aarch64|Linux:arm64)
      REPLY="aarch64-linux"
      ;;
    Linux:x86_64|Linux:amd64)
      REPLY="x86_64-linux"
      ;;
    *)
      echo "ERROR: unsupported system: $os_name $machine" >&2
      return 1
      ;;
  esac
}

is_gui_environment() {
  if dotfiles_is_macos; then
    return 0
  fi

  [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]
}

selected_profile() {
  if [[ "$PROFILE" == "full" || "$INSTALL_GUI_APPS" == "1" ]]; then
    if ! is_gui_environment; then
      echo "ERROR: GUI apps require macOS, DISPLAY, or WAYLAND_DISPLAY. Use --cli-only for CLI setup." >&2
      return 1
    fi
    REPLY="full"
    return
  fi

  REPLY="cli"
}

nix_command() {
  if dotfiles_resolve_command_from_path "nix"; then
    return
  fi

  if dotfiles_resolve_command_from_path "nix-rootless"; then
    return
  fi

  echo "ERROR: nix is not installed or not found in PATH" >&2
  echo "Install Nix first, then rerun this script." >&2
  return 1
}

homebrew_command_exists() {
  dotfiles_has_homebrew
}

ensure_homebrew_available_for_profile() {
  local profile_name="$1"

  if ! dotfiles_profile_requires_homebrew "$profile_name" || homebrew_command_exists; then
    return 0
  fi

  echo "ERROR: Homebrew is required for this Nix profile but brew is not installed." >&2
  echo "Run zsh scripts/install_homebrew.sh --profile $profile_name, then rerun this script." >&2

  if dotfiles_homebrew_fallback_has_cli_entries; then
    echo "config/nix/homebrew-fallback.nix still has brew entries, so even the CLI profile depends on Homebrew." >&2
  else
    echo "Only Homebrew-managed GUI fallback apps remain. Use --cli-only to continue without those GUI updates." >&2
  fi

  return 1
}

cleanup_flake_worktree() {
  if [[ -n "$NIX_FLAKE_WORKTREE" && -d "$NIX_FLAKE_WORKTREE" ]]; then
    rm -rf "$NIX_FLAKE_WORKTREE"
  fi
}

next_home_manager_backup_archive_path() {
  local backup_path="$1"
  local archive_path="${backup_path}.stale-${HOME_MANAGER_BACKUP_ARCHIVE_EPOCH}"
  local suffix_index=0

  while [[ -e "$archive_path" || -L "$archive_path" ]]; do
    suffix_index=$((suffix_index + 1))
    archive_path="${backup_path}.stale-${HOME_MANAGER_BACKUP_ARCHIVE_EPOCH}-${suffix_index}"
  done

  REPLY="$archive_path"
}

archive_existing_home_manager_backups() {
  local config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
  local -a stale_backups=()
  local backup_path
  local archive_path

  (( DRY_RUN )) && return 0

  setopt local_options null_glob glob_dots
  stale_backups+=("$HOME"/.*."$HOME_MANAGER_BACKUP_EXTENSION"(N))
  if [[ -d "$config_dir" ]]; then
    stale_backups+=("$config_dir"/**/*."$HOME_MANAGER_BACKUP_EXTENSION"(N))
  fi

  for backup_path in "${stale_backups[@]}"; do
    [[ -e "$backup_path" || -L "$backup_path" ]] || continue

    next_home_manager_backup_archive_path "$backup_path"
    archive_path="$REPLY"
    echo "Archiving existing Home Manager backup $backup_path to $archive_path before activation."
    mv "$backup_path" "$archive_path"
  done
}

backup_existing_darwin_sudo_local() {
  if ! dotfiles_is_macos || (( DRY_RUN )); then
    return 0
  fi

  [[ -e "$DARWIN_SUDO_LOCAL_PATH" ]] || return 0
  [[ -L "$DARWIN_SUDO_LOCAL_PATH" ]] && return 0

  if [[ -e "$DARWIN_SUDO_LOCAL_BACKUP_PATH" ]]; then
    echo "ERROR: $DARWIN_SUDO_LOCAL_PATH is blocking nix-darwin activation, but $DARWIN_SUDO_LOCAL_BACKUP_PATH already exists." >&2
    echo "Please review both files, then rerun this script." >&2
    return 1
  fi

  echo "Backing up existing $DARWIN_SUDO_LOCAL_PATH to $DARWIN_SUDO_LOCAL_BACKUP_PATH before nix-darwin manages sudo Touch ID."
  sudo mv "$DARWIN_SUDO_LOCAL_PATH" "$DARWIN_SUDO_LOCAL_BACKUP_PATH"
}

backup_existing_darwin_etc_shell_rc_files() {
  if ! dotfiles_is_macos || (( DRY_RUN )); then
    return 0
  fi

  local -a rc_paths
  local rc_path
  local backup_path

  rc_paths=("${(@ps/:/)DARWIN_ETC_SHELL_RC_PATHS}")
  for rc_path in "${rc_paths[@]}"; do
    [[ -n "$rc_path" ]] || continue
    [[ -e "$rc_path" ]] || continue
    [[ -L "$rc_path" ]] && continue

    backup_path="${rc_path}.${HOME_MANAGER_BACKUP_EXTENSION}"
    if [[ -e "$backup_path" ]]; then
      echo "ERROR: $rc_path is blocking nix-darwin activation, but $backup_path already exists." >&2
      echo "Please review both files, then rerun this script." >&2
      return 1
    fi

    echo "Backing up existing $rc_path to $backup_path before nix-darwin manages shell startup files."
    sudo mv "$rc_path" "$backup_path"
  done
}

has_untracked_nix_sources() {
  local temp_root
  local temp_dir
  local untracked_file
  local untracked_entry

  git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
  dotfiles_temporary_directory_root
  temp_root="$REPLY"
  dotfiles_create_unique_temp_directory "$temp_root" "dotfiles-untracked" || return 1
  temp_dir="$REPLY"
  untracked_file="$temp_dir/list"

  git -C "$REPO_ROOT" ls-files --others --exclude-standard -- \
    flake.nix flake.lock config/nix scripts/nix_install.sh scripts/remove_homebrew.sh > "$untracked_file"
  IFS= read -r untracked_entry < "$untracked_file" || true
  rm -rf "$temp_dir"

  [[ -n "$untracked_entry" ]]
}

prepare_flake_path() {
  local temp_root

  if ! has_untracked_nix_sources; then
    REPLY="$REPO_ROOT"
    return
  fi

  dotfiles_temporary_directory_root
  temp_root="$REPLY"
  dotfiles_create_unique_temp_directory "$temp_root" "dotfiles-flake" || return 1
  NIX_FLAKE_WORKTREE="$REPLY"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude .git \
      --exclude .tmp \
      --exclude result \
      --exclude 'result-*' \
      --exclude .agent \
      "$REPO_ROOT"/ "$NIX_FLAKE_WORKTREE"/
  else
    cp -R "$REPO_ROOT"/. "$NIX_FLAKE_WORKTREE"/
    rm -rf \
      "$NIX_FLAKE_WORKTREE/.git" \
      "$NIX_FLAKE_WORKTREE/.tmp" \
      "$NIX_FLAKE_WORKTREE/.agent" \
      "$NIX_FLAKE_WORKTREE/dotfiles/.agent"
    find "$NIX_FLAKE_WORKTREE" -maxdepth 1 \( -name result -o -name 'result-*' \) -exec rm -rf {} +
  fi
  REPLY="$NIX_FLAKE_WORKTREE"
}

flake_url() {
  if [[ -e "$1/.git" ]] && git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    REPLY="$1"
    return
  fi

  REPLY="path:$1"
}

flake_attr() {
  local profile_name="$1"

  if [[ -n "$HOST_ATTR" ]]; then
    REPLY="$HOST_ATTR"
    return
  fi

  system_attr
  REPLY="${REPLY}-${profile_name}"
}

run_darwin_rebuild() {
  local attr="$1"
  local nix_bin="$2"
  local flake_path="$3"
  local flake_ref
  local flake_username
  flake_url "$flake_path"
  flake_ref="$REPLY"
  flake_username="${DOTFILES_USERNAME:-${USER:-}}"

  if (( DRY_RUN )); then
    if command -v darwin-rebuild >/dev/null 2>&1; then
      env DOTFILES_USERNAME="$flake_username" darwin-rebuild build --impure --flake "$flake_ref#$attr"
    else
      env DOTFILES_USERNAME="$flake_username" "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" --impure run "$flake_ref#darwin-rebuild" -- build --impure --flake "$flake_ref#$attr"
    fi
    return
  fi

  if command -v darwin-rebuild >/dev/null 2>&1; then
    sudo env HOME=/var/root DOTFILES_USERNAME="$flake_username" darwin-rebuild switch --impure --flake "$flake_ref#$attr"
  else
    sudo env HOME=/var/root DOTFILES_USERNAME="$flake_username" "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" --impure run "$flake_ref#darwin-rebuild" -- switch --impure --flake "$flake_ref#$attr"
  fi
}

run_home_manager() {
  local attr="$1"
  local nix_bin="$2"
  local flake_path="$3"
  local flake_ref
  local flake_username
  flake_url "$flake_path"
  flake_ref="$REPLY"
  flake_username="${DOTFILES_USERNAME:-${USER:-}}"

  if (( DRY_RUN )); then
    if command -v home-manager >/dev/null 2>&1; then
      env DOTFILES_USERNAME="$flake_username" home-manager build --impure --flake "$flake_ref#$attr"
    else
      env DOTFILES_USERNAME="$flake_username" "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" --impure run "$flake_ref#home-manager" -- build --impure --flake "$flake_ref#$attr"
    fi
    return
  fi

  if command -v home-manager >/dev/null 2>&1; then
    env DOTFILES_USERNAME="$flake_username" home-manager switch -b "$HOME_MANAGER_BACKUP_EXTENSION" --impure --flake "$flake_ref#$attr"
  else
    env DOTFILES_USERNAME="$flake_username" "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" --impure run "$flake_ref#home-manager" -- switch -b "$HOME_MANAGER_BACKUP_EXTENSION" --impure --flake "$flake_ref#$attr"
  fi
}

remove_homebrew_after_switch() {
  if (( ! UNINSTALL_HOMEBREW || DRY_RUN )); then
    return 0
  fi

  zsh "$REMOVE_HOMEBREW_SCRIPT" --apply --confirm-nix-ready
}

main() {
  local nix_bin
  local profile_name
  local attr
  local flake_path
  parse_args "$@"

  nix_command
  nix_bin="$REPLY"
  selected_profile
  profile_name="$REPLY"
  ensure_homebrew_available_for_profile "$profile_name"
  flake_attr "$profile_name"
  attr="$REPLY"
  prepare_flake_path
  flake_path="$REPLY"

  echo "Nix profile: $profile_name"
  echo "Flake output: $attr"
  echo "Flake path: $flake_path"

  if dotfiles_is_macos; then
    archive_existing_home_manager_backups
    backup_existing_darwin_sudo_local
    backup_existing_darwin_etc_shell_rc_files
    run_darwin_rebuild "$attr" "$nix_bin" "$flake_path"
  else
    archive_existing_home_manager_backups
    run_home_manager "$attr" "$nix_bin" "$flake_path"
  fi

  remove_homebrew_after_switch
}

if [[ "${DOTFILES_NIX_INSTALL_LIBRARY_MODE:-0}" != "1" ]]; then
  trap cleanup_flake_worktree EXIT
  main "$@"
fi
