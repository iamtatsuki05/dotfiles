#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly HOMEBREW_FALLBACK_CONFIG="$REPO_ROOT/config/nix/homebrew-fallback.nix"
readonly MAS_APPS_CONFIG="$REPO_ROOT/config/nix/mas-apps.nix"
readonly HOMEBREW_INSTALL_URL="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"

source "$LIB_DIR/setup_profile.sh"
source "$LIB_DIR/homebrew.sh"

DRY_RUN=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/install_homebrew.sh [--profile full|cli] [options]
  zsh scripts/install_homebrew.sh --cli-only

Options:
  --dry-run                Print the Homebrew install command without executing it.
  -h, --help               Show this help.
EOF
}

log() {
  echo "===> $*"
}

list_setting_has_entries() {
  local file_path="$1"
  local setting_name="$2"

  [[ -f "$file_path" ]] || return 1
  awk -v target="$setting_name" '
    BEGIN { in_section = 0 }
    $0 ~ "^[[:space:]]*" target "[[:space:]]*=" { in_section = 1; next }
    in_section && /^[[:space:]]*[A-Za-z0-9_]+[[:space:]]*=/ { in_section = 0 }
    in_section && /^[[:space:]]*"[^"]+"/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$file_path"
}

homebrew_fallback_has_cli_entries() {
  list_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "brews"
}

mas_apps_has_entries() {
  [[ -f "$MAS_APPS_CONFIG" ]] || return 1
  grep -Eq '^[[:space:]]*("[^"]+"|[A-Za-z_][A-Za-z0-9_-]*)[[:space:]]*=' "$MAS_APPS_CONFIG"
}

homebrew_fallback_has_gui_entries() {
  list_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "casks" \
    || list_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "vscode" \
    || mas_apps_has_entries
}

profile_requires_homebrew() {
  local profile_name="$1"

  dotfiles_is_macos || return 1

  if homebrew_fallback_has_cli_entries; then
    return 0
  fi

  [[ "$profile_name" == "full" ]] && homebrew_fallback_has_gui_entries
}

parse_args() {
  local profile_args=()

  while (($#)); do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --profile|--profile=*|--cli-only|--full)
        profile_args+=("$1")
        if [[ "$1" == "--profile" ]]; then
          shift
          if ((! $#)); then
            echo "ERROR: --profile requires full or cli" >&2
            return 1
          fi
          profile_args+=("$1")
        fi
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

  dotfiles_parse_profile_args "scripts/install_homebrew.sh" "${profile_args[@]}"
}

install_homebrew() {
  if ! dotfiles_is_macos; then
    log "Skipping Homebrew install because this host is not macOS"
    return 0
  fi

  dotfiles_prepend_homebrew_to_path || true

  if dotfiles_has_homebrew; then
    log "Homebrew already installed at $(dotfiles_find_homebrew)"
    return 0
  fi

  if ! profile_requires_homebrew "$DOTFILES_PROFILE"; then
    log "Skipping Homebrew install because the selected profile does not require it"
    return 0
  fi

  if (( DRY_RUN )); then
    log "Homebrew install command: NONINTERACTIVE=1 /bin/bash -c \"\$(curl -fsSL $HOMEBREW_INSTALL_URL)\""
    return 0
  fi

  log "Installing Homebrew because the selected profile requires Homebrew fallback entries"
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL "$HOMEBREW_INSTALL_URL")"
  dotfiles_prepend_homebrew_to_path || true

  if ! dotfiles_has_homebrew; then
    echo "ERROR: Homebrew installation completed but brew is still not found." >&2
    return 1
  fi

  log "Homebrew installed at $(dotfiles_find_homebrew)"
}

main() {
  parse_args "$@"
  install_homebrew
}

main "$@"
