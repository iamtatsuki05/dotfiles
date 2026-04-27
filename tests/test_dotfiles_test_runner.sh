#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly TEST_RUNNER="$REPO_ROOT/scripts/test_dotfiles.sh"
readonly MISE_CONFIG="$REPO_ROOT/config/mise/config.toml"
readonly CI_WORKFLOW="$REPO_ROOT/.github/workflows/dotfiles-test.yml"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

assert_output_contains() {
  local output_file="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$output_file" || fail "expected output to contain: $expected"
}

test_test_runner_exists_and_lists_checks() {
  local output
  output="$(mktemp)"

  assert_file "$TEST_RUNNER"
  assert_contains "$TEST_RUNNER" "run_syntax_checks"
  assert_contains "$TEST_RUNNER" "run_unit_tests"
  assert_contains "$TEST_RUNNER" "run_chezmoi_render_test"
  assert_contains "$TEST_RUNNER" "tests/test_agent_sync.sh"
  assert_not_contains "$TEST_RUNNER" "tests/test_setup_config.sh"

  "$TEST_ZSH_BIN" "$TEST_RUNNER" --list > "$output"
  assert_output_contains "$output" "syntax"
  assert_output_contains "$output" "unit"
  assert_output_contains "$output" "source-state"
  assert_output_contains "$output" "chezmoi-render"
  assert_output_contains "$output" "nix-static"

  rm -f "$output"
}

test_mise_task_runs_test_runner_from_repo_root() {
  assert_contains "$MISE_CONFIG" "[tasks.dotfiles-test]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/test_dotfiles.sh"'
  assert_contains "$MISE_CONFIG" 'dir = "__DOTFILES_REPO_ROOT__"'
}

test_mise_tasks_include_nix_migration_flow() {
  assert_not_contains "$MISE_CONFIG" "[tasks.homebrew-dump]"
  assert_not_contains "$MISE_CONFIG" "brew_dump.sh"
  assert_contains "$MISE_CONFIG" "[tasks.nix-migrate-brew]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/migrate_brew_to_nix.sh --apply"'
  assert_not_contains "$MISE_CONFIG" "[tasks.chezmoi-migrate]"
  assert_not_contains "$MISE_CONFIG" "migrate_to_chezmoi.sh"
  assert_contains "$MISE_CONFIG" "[tasks.nix-build]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --dry-run"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-apply]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-apply-cli]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --cli-only"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-apply-with-gui-apps]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --with-gui-apps"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-portable-install]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_portable_install.sh"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-portable-shell]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_portable_install.sh --shell"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-remove-homebrew]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-brew-cleanup]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/cleanup_package_caches.sh"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-lock-update]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock"'
  assert_contains "$MISE_CONFIG" "[tasks.nixpkgs-lock-update]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" "[tasks.home-manager-lock-update]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-darwin-lock-update]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nix-darwin"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-upgrade]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix"'
  assert_contains "$MISE_CONFIG" "[tasks.nixpkgs-upgrade]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" "[tasks.home-manager-upgrade]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-darwin-upgrade]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nix-darwin"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-pin-latest]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/manage_nix_package_version_override.sh pin-latest"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-unpin]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/manage_nix_package_version_override.sh unpin"'
  assert_contains "$MISE_CONFIG" "[tasks.mise-upgrade]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only mise"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-mise-upgrade]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh"'
}

test_github_actions_runs_dotfiles_tests_on_macos_and_ubuntu() {
  assert_contains "$CI_WORKFLOW" "ubuntu-latest"
  assert_contains "$CI_WORKFLOW" "macos-latest"
  assert_contains "$CI_WORKFLOW" "get.chezmoi.io"
  assert_contains "$CI_WORKFLOW" "zsh scripts/test_dotfiles.sh"
}

main() {
  test_test_runner_exists_and_lists_checks
  test_mise_task_runs_test_runner_from_repo_root
  test_mise_tasks_include_nix_migration_flow
  test_github_actions_runs_dotfiles_tests_on_macos_and_ubuntu
  echo "dotfiles test runner tests passed"
}

main "$@"
