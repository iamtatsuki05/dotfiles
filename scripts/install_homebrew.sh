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
source "$LIB_DIR/homebrew_fallback.sh"

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

  if ! dotfiles_profile_requires_homebrew "$DOTFILES_PROFILE"; then
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
