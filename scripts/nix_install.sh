#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly REMOVE_HOMEBREW_SCRIPT="$SCRIPT_DIR/remove_homebrew.sh"
readonly -a NIX_EXPERIMENTAL_ARGS=(--extra-experimental-features "nix-command flakes")
readonly HOME_MANAGER_BACKUP_EXTENSION="before-nix-darwin"

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
  --profile full|cli       Select setup profile. macOS defaults to full; Linux defaults to cli.
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

  PROFILE="${PROFILE:-$(default_profile)}"
  case "$PROFILE" in
    full|cli)
      ;;
    *)
      echo "ERROR: unknown profile: $PROFILE" >&2
      return 1
      ;;
  esac
}

os_name() {
  uname -s
}

is_macos() {
  [[ "$(os_name)" == "Darwin" ]]
}

default_profile() {
  if is_macos; then
    echo "full"
  else
    echo "cli"
  fi
}

system_attr() {
  local machine
  machine="$(uname -m)"

  case "$(os_name):$machine" in
    Darwin:arm64|Darwin:aarch64)
      echo "aarch64-darwin"
      ;;
    Darwin:x86_64)
      echo "x86_64-darwin"
      ;;
    Linux:aarch64|Linux:arm64)
      echo "aarch64-linux"
      ;;
    Linux:x86_64|Linux:amd64)
      echo "x86_64-linux"
      ;;
    *)
      echo "ERROR: unsupported system: $(os_name) $machine" >&2
      return 1
      ;;
  esac
}

is_gui_environment() {
  if is_macos; then
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
    echo "full"
    return
  fi

  echo "cli"
}

nix_command() {
  if command -v nix >/dev/null 2>&1; then
    command -v nix
    return
  fi

  if command -v nix-rootless >/dev/null 2>&1; then
    command -v nix-rootless
    return
  fi

  echo "ERROR: nix is not installed or not found in PATH" >&2
  echo "Install Nix first, then rerun this script." >&2
  return 1
}

cleanup_flake_worktree() {
  if [[ -n "$NIX_FLAKE_WORKTREE" && -d "$NIX_FLAKE_WORKTREE" ]]; then
    rm -rf "$NIX_FLAKE_WORKTREE"
  fi
}

has_untracked_nix_sources() {
  git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
  [[ -n "$(git -C "$REPO_ROOT" ls-files --others --exclude-standard -- flake.nix flake.lock config/nix scripts/nix_install.sh scripts/remove_homebrew.sh)" ]]
}

prepare_flake_path() {
  local temp_root

  if ! has_untracked_nix_sources; then
    echo "$REPO_ROOT"
    return
  fi

  if is_macos; then
    temp_root="/private/tmp"
  else
    temp_root="${TMPDIR:-/tmp}"
  fi

  NIX_FLAKE_WORKTREE="$(mktemp -d "$temp_root/dotfiles-flake.XXXXXX")"
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
  echo "$NIX_FLAKE_WORKTREE"
}

flake_url() {
  local flake_path="$1"
  echo "path:$flake_path"
}

flake_attr() {
  local profile_name="$1"
  local system_name

  if [[ -n "$HOST_ATTR" ]]; then
    echo "$HOST_ATTR"
    return
  fi

  system_name="$(system_attr)"
  echo "${system_name}-${profile_name}"
}

run_darwin_rebuild() {
  local attr="$1"
  local nix_bin="$2"
  local flake_path="$3"
  local flake_ref
  flake_ref="$(flake_url "$flake_path")"

  if (( DRY_RUN )); then
    if command -v darwin-rebuild >/dev/null 2>&1; then
      darwin-rebuild build --flake "$flake_ref#$attr"
    else
      "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" run "$flake_ref#darwin-rebuild" -- build --flake "$flake_ref#$attr"
    fi
    return
  fi

  if command -v darwin-rebuild >/dev/null 2>&1; then
    sudo env HOME=/var/root darwin-rebuild switch --flake "$flake_ref#$attr"
  else
    sudo env HOME=/var/root "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" run "$flake_ref#darwin-rebuild" -- switch --flake "$flake_ref#$attr"
  fi
}

run_home_manager() {
  local attr="$1"
  local nix_bin="$2"
  local flake_path="$3"
  local flake_ref
  flake_ref="$(flake_url "$flake_path")"

  if (( DRY_RUN )); then
    if command -v home-manager >/dev/null 2>&1; then
      home-manager build --flake "$flake_ref#$attr"
    else
      "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" run "$flake_ref#home-manager" -- build --flake "$flake_ref#$attr"
    fi
    return
  fi

  if command -v home-manager >/dev/null 2>&1; then
    home-manager switch -b "$HOME_MANAGER_BACKUP_EXTENSION" --flake "$flake_ref#$attr"
  else
    "$nix_bin" "${NIX_EXPERIMENTAL_ARGS[@]}" run "$flake_ref#home-manager" -- switch -b "$HOME_MANAGER_BACKUP_EXTENSION" --flake "$flake_ref#$attr"
  fi
}

remove_homebrew_after_switch() {
  if (( ! UNINSTALL_HOMEBREW || DRY_RUN )); then
    return 0
  fi

  zsh "$REMOVE_HOMEBREW_SCRIPT" --apply --confirm-nix-ready
}

main() {
  parse_args "$@"

  local nix_bin
  local profile_name
  local attr
  local flake_path
  nix_bin="$(nix_command)"
  profile_name="$(selected_profile)"
  attr="$(flake_attr "$profile_name")"
  flake_path="$(prepare_flake_path)"

  echo "Nix profile: $profile_name"
  echo "Flake output: $attr"
  echo "Flake path: $flake_path"

  if is_macos; then
    run_darwin_rebuild "$attr" "$nix_bin" "$flake_path"
  else
    run_home_manager "$attr" "$nix_bin" "$flake_path"
  fi

  remove_homebrew_after_switch
}

if [[ "${DOTFILES_NIX_INSTALL_LIBRARY_MODE:-0}" != "1" ]]; then
  trap cleanup_flake_worktree EXIT
  main "$@"
fi
