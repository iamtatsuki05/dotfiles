#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

LIST_ONLY=0
SYNTAX_ONLY=0
SKIP_CHEZMOI=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/test_dotfiles.sh [options]

Options:
  --list          List checks without running them.
  --syntax-only   Run only zsh syntax checks.
  --skip-chezmoi  Skip chezmoi rendered-home integration checks.
  -h, --help      Show this help.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --list)
        LIST_ONLY=1
        ;;
      --syntax-only)
        SYNTAX_ONLY=1
        ;;
      --skip-chezmoi)
        SKIP_CHEZMOI=1
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

log_step() {
  echo "===> $*"
}

list_checks() {
  cat <<'EOF'
syntax
unit
source-state
chezmoi-render
nix-static
EOF
}

run_zsh_syntax_for() {
  local file_path="$1"

  "$TEST_ZSH_BIN" -n "$file_path"
}

run_syntax_checks() {
  log_step "Running zsh syntax checks"

  run_zsh_syntax_for "$REPO_ROOT/main.sh"

  local file_path
  while IFS= read -r file_path; do
    run_zsh_syntax_for "$file_path"
  done < <(find "$REPO_ROOT/scripts" "$REPO_ROOT/tests" -type f -name "*.sh" | sort)
}

run_unit_tests() {
  log_step "Running shell unit tests"
  "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_agent_sync.sh"
  "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_chezmoi_migration.sh"
  "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_dotfiles_test_runner.sh"
  "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_nix_migration.sh"
}

run_source_state_tests() {
  log_step "Checking chezmoi source state"
  "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_chezmoi_source_state.sh"
}

run_chezmoi_render_test() {
  if (( SKIP_CHEZMOI )); then
    echo "SKIP: chezmoi rendered-home checks disabled by --skip-chezmoi"
    return 0
  fi

  log_step "Rendering chezmoi source state into a temporary home"
  "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_chezmoi_rendered_home.sh"
}

run_nix_static_tests() {
  log_step "Checking Nix files"

  if command -v nix-instantiate >/dev/null 2>&1; then
    nix-instantiate --parse "$REPO_ROOT/flake.nix" >/dev/null
  else
    echo "SKIP: nix-instantiate is not installed"
  fi
}

main() {
  parse_args "$@"

  if (( LIST_ONLY )); then
    list_checks
    return 0
  fi

  run_syntax_checks
  if (( SYNTAX_ONLY )); then
    return 0
  fi

  run_unit_tests
  run_source_state_tests
  run_chezmoi_render_test
  run_nix_static_tests

  echo "dotfiles tests passed"
}

main "$@"
