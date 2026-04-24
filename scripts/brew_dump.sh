#!/usr/bin/zsh

set -euo pipefail

# =============================================================================
# Dump the macOS Homebrew bundle and derive the portable CLI bundle.
#
# The heavy lifting is intentionally delegated to Homebrew Bundle:
# - full bundle: brew bundle dump --file
# - CLI bundle:  brew bundle dump --file --tap --formula --uv
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly DOTFILES_DIR="$REPO_ROOT/dotfiles"
readonly FULL_BREWFILE="$DOTFILES_DIR/.Brewfile"
readonly CLI_BREWFILE="$DOTFILES_DIR/.Brewfile.cli"
readonly OS_NAME="$(uname -s)"

DUMP_FULL=1
RUN_CHECK=1

print_usage() {
  cat <<'EOF'
Usage:
  zsh scripts/brew_dump.sh [--generate-cli-only] [--no-check]

Workflow:
  1. On macOS, dump the current Homebrew state to dotfiles/.Brewfile.
  2. Dump tap/formula/uv entries to dotfiles/.Brewfile.cli.
  3. Optionally run brew bundle check for both files.

Options:
  --generate-cli-only  Regenerate only dotfiles/.Brewfile.cli from the current Homebrew state.
  --no-check           Skip brew bundle check.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --generate-cli-only)
        DUMP_FULL=0
        ;;
      --no-check)
        RUN_CHECK=0
        ;;
      -h|--help)
        print_usage
        exit 0
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        print_usage >&2
        return 1
        ;;
    esac
    shift
  done
}

require_command() {
  local command_name="$1"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "ERROR: command not found: $command_name" >&2
    return 1
  fi
}

require_macos_for_dump() {
  if [[ "$DUMP_FULL" == "1" && "$OS_NAME" != "Darwin" ]]; then
    echo "ERROR: dumping the full Brewfile is macOS-only. Use --generate-cli-only on $OS_NAME." >&2
    return 1
  fi
}

dump_full_brewfile() {
  echo "Dumping Homebrew state to $FULL_BREWFILE..."

  HOMEBREW_BUNDLE_DUMP_NO_GO="${HOMEBREW_BUNDLE_DUMP_NO_GO:-1}" \
    HOMEBREW_BUNDLE_DUMP_NO_CARGO="${HOMEBREW_BUNDLE_DUMP_NO_CARGO:-1}" \
    HOMEBREW_BUNDLE_DUMP_NO_KREW="${HOMEBREW_BUNDLE_DUMP_NO_KREW:-1}" \
    HOMEBREW_BUNDLE_DUMP_NO_NPM="${HOMEBREW_BUNDLE_DUMP_NO_NPM:-1}" \
    brew bundle dump --file="$FULL_BREWFILE" --force
}

generate_cli_brewfile() {
  echo "Dumping CLI Homebrew state to $CLI_BREWFILE..."
  brew bundle dump --file="$CLI_BREWFILE" --force --tap --formula --uv
}

check_brewfile() {
  local brewfile="$1"

  echo "Checking $brewfile..."
  brew bundle check --file="$brewfile"
}

main() {
  parse_args "$@"
  require_command brew
  require_macos_for_dump

  if [[ "$DUMP_FULL" == "1" ]]; then
    dump_full_brewfile
  fi

  generate_cli_brewfile

  if [[ "$RUN_CHECK" == "1" ]]; then
    if [[ "$DUMP_FULL" == "1" ]]; then
      check_brewfile "$FULL_BREWFILE"
    fi
    check_brewfile "$CLI_BREWFILE"
  fi
}

main "$@"
