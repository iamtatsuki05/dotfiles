#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly DEFAULT_OLDER_THAN="30d"

source "$LIB_DIR/homebrew.sh"

APPLY=0
OLDER_THAN="$DEFAULT_OLDER_THAN"
SKIP_NIX=0
SKIP_HOMEBREW=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/cleanup_package_caches.sh [options]

Options:
  --dry-run            Print the cleanup commands without running them. This is the default.
  --apply              Run the cleanup commands.
  --older-than Nd      Delete Nix profile generations older than Nd. Default: $DEFAULT_OLDER_THAN.
  --skip-nix           Skip Nix cleanup.
  --skip-homebrew      Skip Homebrew cleanup.
  -h, --help           Show this help.

Default cleanup:
  1. nix profile wipe-history --older-than Nd
  2. nix-collect-garbage --delete-older-than Nd
  3. nix store optimise
  4. brew cleanup --prune=all --scrub
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run)
        APPLY=0
        ;;
      --apply)
        APPLY=1
        ;;
      --older-than)
        shift
        if ((! $#)); then
          echo "ERROR: --older-than requires a value such as 30d" >&2
          return 1
        fi
        OLDER_THAN="$1"
        ;;
      --older-than=*)
        OLDER_THAN="${1#--older-than=}"
        ;;
      --skip-nix)
        SKIP_NIX=1
        ;;
      --skip-homebrew)
        SKIP_HOMEBREW=1
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

  if [[ ! "$OLDER_THAN" =~ '^[0-9]+d$' ]]; then
    echo "ERROR: --older-than must look like 30d" >&2
    return 1
  fi
}

log() {
  echo "===> $*"
}

warn() {
  echo "===> $*" >&2
}

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

run_or_print() {
  if (( APPLY )); then
    "$@"
    return 0
  fi

  print_command "$@"
}

run_nix_cleanup() {
  if (( SKIP_NIX )); then
    log "Skipping Nix cleanup"
    return 0
  fi

  if ! command -v nix >/dev/null 2>&1; then
    warn "Skipping Nix cleanup because nix is not installed"
    return 0
  fi

  log "Nix cleanup"
  run_or_print nix profile wipe-history --older-than "$OLDER_THAN"
  run_or_print nix-collect-garbage --delete-older-than "$OLDER_THAN"
  run_or_print nix store optimise
}

run_homebrew_cleanup() {
  local brew_path

  if (( SKIP_HOMEBREW )); then
    log "Skipping Homebrew cleanup"
    return 0
  fi

  if ! dotfiles_has_homebrew; then
    warn "Skipping Homebrew cleanup because brew is not installed"
    return 0
  fi

  brew_path="$(dotfiles_find_homebrew)"

  log "Homebrew cleanup"
  run_or_print "$brew_path" cleanup --prune=all --scrub
}

main() {
  parse_args "$@"

  run_nix_cleanup
  run_homebrew_cleanup

  if (( APPLY )); then
    echo "Package cache cleanup complete"
  else
    echo "DRY-RUN: package caches were not removed"
  fi
}

main "$@"
