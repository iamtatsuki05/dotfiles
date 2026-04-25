#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly MISE_CONFIG_FILE="$REPO_ROOT/config/mise/config.toml"
readonly MISE_TEMPLATE_FILE="$REPO_ROOT/home/.chezmoitemplates/mise-config.toml"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

source "$LIB_DIR/setup_profile.sh"

SELECTED_SHELL="zsh"
INSTALL_GUI_APPS=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/update_managed_versions.sh [--profile full|cli] [options]
  zsh scripts/update_managed_versions.sh --cli-only

Options:
  --shell zsh|bash        Shell to use for repository helper scripts. Default: zsh.
  --profile full|cli      Select setup profile. macOS defaults to full; Linux defaults to cli.
  --cli-only              Alias for --profile cli.
  --with-gui-apps         Include GUI apps when applying the updated Nix profile.
  -h, --help              Show this help.

This command:
  1. bumps every tool in config/mise/config.toml to the latest version via mise upgrade --bump
  2. syncs home/.chezmoitemplates/mise-config.toml and ~/.config/mise/config.toml
  3. updates flake.lock with nix flake update
  4. applies the updated Nix configuration
EOF
}

log() {
  echo "===> $*"
}

mise_command() {
  if command -v mise >/dev/null 2>&1; then
    command -v mise
    return 0
  fi

  echo "ERROR: mise is not installed or not found in PATH" >&2
  return 1
}

nix_command() {
  if command -v nix >/dev/null 2>&1; then
    command -v nix
    return 0
  fi

  echo "ERROR: nix is not installed or not found in PATH" >&2
  return 1
}

run_repo_script() {
  local script_name="$1"
  shift

  "$SELECTED_SHELL" "$SCRIPT_DIR/$script_name" "$@"
}

parse_args() {
  local profile_args=()

  while (($#)); do
    case "$1" in
      --shell)
        shift
        if ((! $#)); then
          echo "ERROR: --shell requires zsh or bash" >&2
          return 1
        fi
        SELECTED_SHELL="$1"
        ;;
      --shell=*)
        SELECTED_SHELL="${1#--shell=}"
        ;;
      --profile)
        profile_args+=("$1")
        shift
        if ((! $#)); then
          echo "ERROR: --profile requires full or cli" >&2
          return 1
        fi
        profile_args+=("$1")
        ;;
      --profile=*|--cli-only|--full)
        profile_args+=("$1")
        ;;
      --with-gui-apps)
        INSTALL_GUI_APPS=1
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

  case "$SELECTED_SHELL" in
    zsh|bash)
      ;;
    *)
      echo "ERROR: unsupported shell: $SELECTED_SHELL" >&2
      echo "Choose one of: zsh, bash" >&2
      return 1
      ;;
  esac

  if ! command -v "$SELECTED_SHELL" >/dev/null 2>&1; then
    echo "ERROR: shell not found in PATH: $SELECTED_SHELL" >&2
    return 1
  fi

  dotfiles_parse_profile_args "scripts/update_managed_versions.sh" "${profile_args[@]}"
}

update_mise_versions() {
  log "Bumping managed mise tools to the latest versions"
  MISE_GLOBAL_CONFIG_FILE="$MISE_CONFIG_FILE" "$(mise_command)" upgrade --bump
}

sync_mise_templates() {
  log "Syncing tracked mise templates"
  cp "$MISE_CONFIG_FILE" "$MISE_TEMPLATE_FILE"
}

sync_home_mise_config() {
  local target_file="$XDG_CONFIG_HOME/mise/config.toml"
  local target_dir="${target_file%/*}"
  local tmp
  local line

  log "Syncing ~/.config/mise/config.toml"
  mkdir -p "$target_dir"
  tmp="$(mktemp)"

  while IFS= read -r line || [[ -n "$line" ]]; do
    printf '%s\n' "${line//__DOTFILES_REPO_ROOT__/$REPO_ROOT}"
  done < "$MISE_CONFIG_FILE" > "$tmp"

  mv "$tmp" "$target_file"
}

update_nix_lockfile() {
  log "Updating flake.lock"
  (
    cd "$REPO_ROOT"
    "$(nix_command)" flake update
  )
}

apply_nix_configuration() {
  local args=("--profile" "$DOTFILES_PROFILE")

  if [[ "$INSTALL_GUI_APPS" == "1" ]]; then
    args+=("--with-gui-apps")
  fi

  log "Applying updated Nix configuration"
  run_repo_script "nix_install.sh" "${args[@]}"
}

main() {
  parse_args "$@"

  log "Updating versions managed by mise and Nix (profile: $DOTFILES_PROFILE, shell: $SELECTED_SHELL)"
  update_mise_versions
  sync_mise_templates
  sync_home_mise_config
  update_nix_lockfile
  apply_nix_configuration
  log "Managed version update complete"
}

main "$@"
