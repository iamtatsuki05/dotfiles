#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly TEST_RUNNER="$REPO_ROOT/tests/run.sh"
readonly MISE_CONFIG="$REPO_ROOT/config/mise/config.toml"
readonly KIMI_WEBBRIDGE_SETUP_SCRIPT="$REPO_ROOT/scripts/setup_kimi_webbridge.sh"
readonly CI_WORKFLOW="$REPO_ROOT/.github/workflows/dotfiles-test.yml"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

source "$TEST_DIR/lib/assertions.sh"

write_fixture_zsh_script() {
  local file_path="$1"
  local message="$2"

  mkdir -p "${file_path:h}"
  {
    print -r -- "#!$TEST_ZSH_BIN"
    print -r -- "set -euo pipefail"
    print -r -- "print -r -- ${(qqq)message}"
  } > "$file_path"
  chmod +x "$file_path"
}

create_runner_fixture() {
  local repo="$1"

  mkdir -p "$repo/scripts" "$repo/tests"
  cp "$TEST_RUNNER" "$repo/tests/run.sh"
  chmod +x "$repo/tests/run.sh"

  write_fixture_zsh_script "$repo/main.sh" "main"
  write_fixture_zsh_script "$repo/scripts/helper.sh" "helper"
  write_fixture_zsh_script "$repo/tests/test_agent_delegation_analysis.sh" "unit:agent-delegation"
  write_fixture_zsh_script "$repo/tests/test_agent_html_preview_review.sh" "unit:html-preview-review"
  print -r -- 'print("unit:agent-run-compact")' > "$repo/tests/test_agent_run_compact.py"
  write_fixture_zsh_script "$repo/tests/test_agent_sync.sh" "unit:agent"
  write_fixture_zsh_script "$repo/tests/test_agent_support_matrix.sh" "unit:agent-support"
  write_fixture_zsh_script "$repo/tests/test_agent_skill_upstreams.sh" "unit:skill-upstreams"
  write_fixture_zsh_script "$repo/tests/test_claude_account.sh" "unit:claude-account"
  write_fixture_zsh_script "$repo/tests/test_chezmoi_migration.sh" "unit:chezmoi"
  {
    print -r -- "#!$TEST_ZSH_BIN"
    print -r -- 'set -euo pipefail'
    print -r -- 'case "$*" in'
    print -r -- '  *"--selector source"*) print -r -- multi-shell-source ;;'
    print -r -- '  *"--selector render"*) print -r -- multi-shell-render ;;'
    print -r -- '  *) print -u2 -r -- "unexpected multi-shell selector: $*"; exit 1 ;;'
    print -r -- 'esac'
  } > "$repo/tests/test_multi_shell_config.sh"
  chmod +x "$repo/tests/test_multi_shell_config.sh"
  write_fixture_zsh_script "$repo/tests/test_dotfiles_test_runner.sh" "unit:runner"
  write_fixture_zsh_script "$repo/tests/test_hermes_agent_setup.sh" "unit:hermes"
  write_fixture_zsh_script "$repo/tests/test_japanese_prose_lint.sh" "unit:japanese-prose-lint"
  write_fixture_zsh_script "$repo/tests/test_nix_migration.sh" "unit:nix"
  write_fixture_zsh_script "$repo/tests/test_chezmoi_source_state.sh" "source-state"
  write_fixture_zsh_script "$repo/tests/test_chezmoi_rendered_home.sh" "chezmoi-render-test-ran"
  print -r -- "{}" > "$repo/flake.nix"
}

test_test_runner_exists_and_lists_checks() {
  local output
  output="$(mktemp)"

  assert_file "$TEST_RUNNER"
  assert_contains "$TEST_RUNNER" "run_syntax_checks"
  assert_contains "$TEST_RUNNER" "run_unit_tests"
  assert_contains "$TEST_RUNNER" "run_chezmoi_render_test"
  assert_contains "$TEST_RUNNER" "tests/test_agent_delegation_analysis.sh"
  assert_contains "$TEST_RUNNER" "tests/test_agent_html_preview_review.sh"
  assert_contains "$TEST_RUNNER" "tests/test_agent_run_compact.py"
  assert_contains "$TEST_RUNNER" "tests/test_agent_sync.sh"
  assert_contains "$TEST_RUNNER" "tests/test_agent_support_matrix.sh"
  assert_contains "$TEST_RUNNER" "tests/test_agent_skill_upstreams.sh"
  assert_contains "$TEST_RUNNER" "tests/test_claude_account.sh"
  assert_contains "$TEST_RUNNER" "tests/test_hermes_agent_setup.sh"
  assert_contains "$TEST_RUNNER" "tests/test_multi_shell_config.sh"
  assert_contains "$TEST_RUNNER" "--selector source"
  assert_contains "$TEST_RUNNER" "--selector render"
  assert_contains "$TEST_RUNNER" "tests/test_japanese_prose_lint.sh"
  assert_not_contains "$TEST_RUNNER" "tests/test_setup_config.sh"

  "$TEST_ZSH_BIN" "$TEST_RUNNER" --list > "$output"
  assert_output_contains "$output" "syntax"
  assert_output_contains "$output" "unit"
  assert_output_contains "$output" "source-state"
  assert_output_contains "$output" "chezmoi-render"
  assert_output_contains "$output" "nix-static"

  rm -f "$output"
}

test_test_runner_references_existing_test_files() {
  local relative_path
  local -a referenced_tests

  referenced_tests=("${(@f)$(sed -n 's|.*\$REPO_ROOT/\(tests/[^\"]*\)".*|\1|p' "$TEST_RUNNER")}")
  (( ${#referenced_tests[@]} > 0 )) || fail "expected test runner to reference test files"

  for relative_path in "${referenced_tests[@]}"; do
    assert_file "$REPO_ROOT/$relative_path"
  done
}

test_test_runner_syntax_only_stops_before_unit_tests() {
  local repo
  local output

  make_temp_dir "dotfiles-runner-test"
  repo="${REPLY:A}"
  output="$repo/output.log"
  create_runner_fixture "$repo"

  "$TEST_ZSH_BIN" "$repo/tests/run.sh" --syntax-only > "$output"

  assert_output_contains "$output" "===> Running zsh syntax checks"
  assert_not_contains "$output" "unit:agent"
  assert_not_contains "$output" "source-state"
  assert_not_contains "$output" "chezmoi-render-test-ran"

  rm -rf "$repo"
}

test_test_runner_skip_chezmoi_keeps_fast_checks() {
  local repo
  local bin_dir
  local nix_log
  local output

  make_temp_dir "dotfiles-runner-test"
  repo="${REPLY:A}"
  bin_dir="$repo/bin"
  nix_log="$repo/nix.log"
  output="$repo/output.log"
  create_runner_fixture "$repo"
  mkdir -p "$bin_dir"
  {
    print -r -- "#!$TEST_ZSH_BIN"
    print -r -- "set -euo pipefail"
    print -r -- "print -r -- \"nix-static:\$*\" >> ${(qqq)nix_log}"
  } > "$bin_dir/nix-instantiate"
  chmod +x "$bin_dir/nix-instantiate"

  PATH="$bin_dir:$PATH" "$TEST_ZSH_BIN" "$repo/tests/run.sh" --skip-chezmoi > "$output"

  assert_output_contains "$output" "unit:agent"
  assert_output_contains "$output" "unit:agent-delegation"
  assert_output_contains "$output" "unit:html-preview-review"
  assert_output_contains "$output" "unit:agent-run-compact"
  assert_output_contains "$output" "unit:agent-support"
  assert_output_contains "$output" "unit:skill-upstreams"
  assert_output_contains "$output" "unit:claude-account"
  assert_output_contains "$output" "unit:japanese-prose-lint"
  assert_output_contains "$output" "unit:nix"
  assert_output_contains "$output" "source-state"
  assert_output_contains "$output" "multi-shell-source"
  assert_output_contains "$output" "SKIP: chezmoi rendered-home checks disabled by --skip-chezmoi"
  assert_output_contains "$output" "SKIP: multi-shell render/runtime checks disabled by --skip-chezmoi"
  assert_output_contains "$output" "dotfiles tests passed"
  assert_not_contains "$output" "chezmoi-render-test-ran"
  assert_not_contains "$output" "multi-shell-render"
  assert_contains "$nix_log" "nix-static:--parse $repo/flake.nix"

  rm -rf "$repo"
}

test_test_runner_runs_render_checks_by_default() {
  local repo output

  make_temp_dir "dotfiles-runner-test"
  repo="${REPLY:A}"
  output="$repo/output.log"
  create_runner_fixture "$repo"

  "$TEST_ZSH_BIN" "$repo/tests/run.sh" > "$output"

  assert_output_contains "$output" "chezmoi-render-test-ran"
  assert_output_contains "$output" "multi-shell-render"
  assert_not_contains "$output" "SKIP: chezmoi rendered-home checks disabled by --skip-chezmoi"
  assert_not_contains "$output" "SKIP: multi-shell render/runtime checks disabled by --skip-chezmoi"
  assert_output_contains "$output" "dotfiles tests passed"

  rm -rf "$repo"
}

test_multi_shell_required_bash5_skip_has_failure_summary() {
  local repo fake_bash fake_fish output exit_status

  make_temp_dir "dotfiles-required-skip-test"
  repo="${REPLY:A}"
  fake_bash="$repo/fake-bash"
  output="$repo/output.log"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'for arg in "$@"; do'
    print -r -- '  if [ "$arg" = -c ]; then printf "%s\\n" 4; exit 0; fi'
    print -r -- 'done'
    print -r -- 'exit 4'
  } > "$fake_bash"
  chmod +x "$fake_bash"
  [[ "$("$fake_bash" --noprofile --norc -c true)" == 4 ]] || fail 'fake Bash unavailable fixture must report deterministic unsupported major 4'
  fake_fish="$repo/fish"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- 'mise_path="$(command -v mise)"'
    print -r -- 'fixture_dir="$(dirname "$mise_path")/.."'
    print -r -- 'gcloud_log="$fixture_dir/gcloud.log"'
    print -r -- 'mise_log="$fixture_dir/mise.log"'
    print -r -- 'interactive=0'
    print -r -- 'for arg in "$@"; do [ "$arg" = -i ] && interactive=1; done'
    print -r -- 'if [ "$interactive" -eq 1 ] && [ -n "${FAKE_MISE_MODE:-}" ]; then'
    print -r -- '  printf "%s\\n" "dotfiles: mise activate fish failed" >&2'
    print -r -- '  printf "%s\\n" "source_status=1" "activation_failed=1" "after_status=0"'
    print -r -- '  printf "%s\\n" "activate fish" >> "$mise_log"'
    print -r -- '  exit 0'
    print -r -- 'fi'
    print -r -- 'if [ "$interactive" -eq 1 ]; then'
    print -r -- '  printf "%s\\n" "mise=$mise_path" "activated=yes" "activation_failed=0" "interactive=yes"'
    print -r -- '  printf "%s\\n" "init" "auth login" "compute instances list" > "$gcloud_log"'
    print -r -- '  printf "%s\\n" "activate fish" >> "$mise_log"'
    print -r -- 'else'
    print -r -- '  printf "%s\\n" "editor=fixture-editor" "config=$HOME/.config" "cache=$HOME/.fixture-cache" "data=$HOME/.fixture-data" "state=$HOME/.fixture-state" "mise=$mise_path" "foreign=foreign" "ginit=absent" "activation_failed=0" "path=$HOME/.fixture-bin"'
    print -r -- 'fi'
  } > "$fake_fish"
  chmod +x "$fake_fish"

  set +e
  PATH="$repo:$PATH" BASH5_BIN="$fake_bash" "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_multi_shell_config.sh" --selector render > "$output" 2>&1
  exit_status=$?
  set -e

  (( exit_status != 0 )) || fail 'required Bash 5 skip must keep a non-zero status'
  assert_output_contains "$output" 'shell=bash5|target=rendered-home|status=SKIP|requirement=required|reason=bash5-unavailable'
  assert_output_contains "$output" 'multi-shell render/runtime checks failed'
  assert_not_contains "$output" 'multi-shell render/runtime checks passed'

  rm -rf "$repo"
}

test_mise_task_runs_test_runner_from_repo_root() {
  assert_contains "$MISE_CONFIG" "[tasks.dotfiles-test]"
  assert_contains "$MISE_CONFIG" 'run = "zsh tests/run.sh"'
  assert_contains "$MISE_CONFIG" 'dir = "__DOTFILES_REPO_ROOT__"'
  assert_contains "$MISE_CONFIG" "[tasks.agent-sync]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/setup_agent_files.sh"'
  assert_contains "$MISE_CONFIG" "[tasks.kimi-webbridge-setup]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/setup_kimi_webbridge.sh"'
  assert_contains "$MISE_CONFIG" 'description = "Kimi WebBridgeのlocal serviceとagent skillを設定"'
  assert_file "$KIMI_WEBBRIDGE_SETUP_SCRIPT"
  assert_contains "$KIMI_WEBBRIDGE_SETUP_SCRIPT" "https://cdn.kimi.com/webbridge"
  assert_contains "$KIMI_WEBBRIDGE_SETUP_SCRIPT" "install-skill -y"
}

test_mise_tasks_include_nix_migration_flow() {
  assert_not_contains "$MISE_CONFIG" "[tasks.homebrew-dump]"
  assert_not_contains "$MISE_CONFIG" "brew_dump.sh"
  assert_contains "$MISE_CONFIG" "[tasks.nix-migrate-brew]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/migrate_brew_to_nix.sh --apply"'
  assert_not_contains "$MISE_CONFIG" "[tasks.chezmoi-migrate]"
  assert_not_contains "$MISE_CONFIG" "migrate_to_chezmoi.sh"
  assert_contains "$MISE_CONFIG" "[tasks.nix-build]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --cli-only --dry-run"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-apply]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --cli-only"'
  assert_not_contains "$MISE_CONFIG" "[tasks.nix-apply-cli]"
  assert_contains "$MISE_CONFIG" "[tasks.nix-apply-with-gui-apps]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_install.sh --with-gui-apps"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-portable-install]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_portable_install.sh"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-portable-shell]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/nix_portable_install.sh --shell"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-remove-homebrew]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready"'
  assert_contains "$MISE_CONFIG" "[tasks.package-cleanup]"
  assert_contains "$MISE_CONFIG" 'alias = "nix-brew-cleanup"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/cleanup_package_caches.sh"'
  assert_contains "$MISE_CONFIG" "[tasks.lock-update]"
  assert_contains "$MISE_CONFIG" 'alias = "nix-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock"'
  assert_contains "$MISE_CONFIG" "[tasks.lock-update-nixpkgs]"
  assert_contains "$MISE_CONFIG" 'alias = "nixpkgs-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" "[tasks.lock-update-home-manager]"
  assert_contains "$MISE_CONFIG" 'alias = "home-manager-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" "[tasks.lock-update-nix-darwin]"
  assert_contains "$MISE_CONFIG" 'alias = "nix-darwin-lock-update"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only lock --nix-input nix-darwin"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-update]"
  assert_contains "$MISE_CONFIG" 'alias = "nix-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix"'
  assert_contains "$MISE_CONFIG" "[tasks.nixpkgs-update]"
  assert_contains "$MISE_CONFIG" 'alias = "nixpkgs-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nixpkgs"'
  assert_contains "$MISE_CONFIG" "[tasks.home-manager-update]"
  assert_contains "$MISE_CONFIG" 'alias = "home-manager-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input home-manager"'
  assert_contains "$MISE_CONFIG" "[tasks.nix-darwin-update]"
  assert_contains "$MISE_CONFIG" 'alias = "nix-darwin-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only nix --nix-input nix-darwin"'
  assert_not_contains "$MISE_CONFIG" "[tasks.nix-pin-latest]"
  assert_not_contains "$MISE_CONFIG" "scripts/manage_nix_package_version_override.sh"
  assert_not_contains "$MISE_CONFIG" "install_before"
  assert_contains "$MISE_CONFIG" "[tasks.mise-update]"
  assert_contains "$MISE_CONFIG" 'alias = "mise-upgrade"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only mise"'
  assert_contains "$MISE_CONFIG" "[tasks.package-update]"
  assert_contains "$MISE_CONFIG" 'alias = "nix-mise-upgrade"'
  assert_not_contains "$MISE_CONFIG" 'git-head-commit-rest'
  assert_contains "$MISE_CONFIG" '[tools."http:devin"]'
  assert_contains "$MISE_CONFIG" 'version_list_url = "https://static.devin.ai/cli/current/manifest.json"'
  assert_contains "$MISE_CONFIG" '[tools."http:cursor-agent"]'
  assert_contains "$MISE_CONFIG" 'https://downloads.cursor.com/lab/{{ version }}/{{ os(macos="darwin", linux="linux") }}/{{ arch(x64="x64", arm64="arm64") }}/agent-cli-package.tar.gz'
  assert_contains "$MISE_CONFIG" 'opencode = "latest"'
  assert_contains "$MISE_CONFIG" '"github:ogulcancelik/herdr" = "latest"'
  assert_contains "$MISE_CONFIG" '"pipx:markitdown" = "latest"'
  assert_contains "$MISE_CONFIG" '"pipx:google-colab-cli" = "latest"'
  assert_not_contains "$MISE_CONFIG" 'pipx:git+https://github.com/NousResearch/hermes-agent.git'
  assert_contains "$MISE_CONFIG" "[tasks.hermes-setup]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/setup_hermes_agent.sh"'
  assert_contains "$MISE_CONFIG" "[tasks.hermes-update]"
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh --only hermes"'
  assert_contains "$MISE_CONFIG" '"npm:@github/copilot" = "latest"'
  assert_contains "$MISE_CONFIG" '"npm:openclaw" = "latest"'
  assert_contains "$MISE_CONFIG" 'run = "zsh scripts/update_managed_versions.sh"'
}

test_github_actions_runs_dotfiles_tests_on_macos_and_ubuntu() {
  assert_contains "$CI_WORKFLOW" "ubuntu-latest"
  assert_contains "$CI_WORKFLOW" "macos-latest"
  assert_contains "$CI_WORKFLOW" "get.chezmoi.io"
  assert_contains "$CI_WORKFLOW" "/bin/zsh tests/run.sh"
}

main() {
  test_test_runner_exists_and_lists_checks
  test_test_runner_references_existing_test_files
  test_test_runner_syntax_only_stops_before_unit_tests
  test_test_runner_skip_chezmoi_keeps_fast_checks
  test_test_runner_runs_render_checks_by_default
  test_multi_shell_required_bash5_skip_has_failure_summary
  test_mise_task_runs_test_runner_from_repo_root
  test_mise_tasks_include_nix_migration_flow
  test_github_actions_runs_dotfiles_tests_on_macos_and_ubuntu
  echo "dotfiles test runner tests passed"
}

main "$@"
