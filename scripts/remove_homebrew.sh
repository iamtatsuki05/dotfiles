#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly HOMEBREW_FALLBACK_CONFIG="$REPO_ROOT/config/nix/homebrew-fallback.nix"

APPLY=0
CONFIRM_NIX_READY=0
FORCE=0

usage() {
  cat <<'EOF'
Usage:
  zsh scripts/remove_homebrew.sh --dry-run
  zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready

Options:
  --dry-run            Print the Homebrew uninstall command without running it. This is the default.
  --apply              Run the official Homebrew uninstall script.
  --confirm-nix-ready  Required with --apply. Confirms Nix setup already works.
  --force              Allow removal even when a Homebrew fallback config exists.
  -h, --help           Show this help.
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
      --confirm-nix-ready)
        CONFIRM_NIX_READY=1
        ;;
      --force)
        FORCE=1
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
}

homebrew_fallback_has_entries() {
  [[ -f "$HOMEBREW_FALLBACK_CONFIG" ]] || return 1
  awk '
    /^[[:space:]]*(taps|brews|casks|vscode)[[:space:]]*=/ { in_homebrew_section = 1; next }
    /^[[:space:]]*[A-Za-z0-9_]+[[:space:]]*=/ { in_homebrew_section = 0 }
    in_homebrew_section && /^[[:space:]]*"[^"]+"/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$HOMEBREW_FALLBACK_CONFIG"
}

print_command() {
  cat <<'EOF'
Homebrew uninstall command:
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)"
EOF
}

remove_homebrew() {
  print_command

  if (( ! APPLY )); then
    echo "DRY-RUN: Homebrew was not removed"
    return 0
  fi

  if (( ! CONFIRM_NIX_READY )); then
    echo "ERROR: Refusing to remove Homebrew until Nix setup has been confirmed." >&2
    echo "Run zsh scripts/nix_install.sh first, then use --apply --confirm-nix-ready." >&2
    return 1
  fi

  if (( ! FORCE )) && homebrew_fallback_has_entries; then
    echo "ERROR: Refusing to remove Homebrew because config/nix/homebrew-fallback.nix still contains fallback packages." >&2
    echo "Remove those fallback entries first, or rerun with --force if you intentionally want to break Homebrew-managed fallbacks." >&2
    return 1
  fi

  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)"
}

main() {
  parse_args "$@"
  remove_homebrew
}

main "$@"
