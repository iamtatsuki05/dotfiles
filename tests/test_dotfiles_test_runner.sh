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
    print -r -- '  *"--selector source"*) if [[ "$*" == *"--skip-chezmoi"* ]]; then print -r -- source-resolver-skipped; print -r -- "MATRIX_RESULT|os=linux|shell=chezmoi|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=chezmoi-skipped"; else print -r -- source-resolver-called; print -r -- "MATRIX_RESULT|os=linux|shell=chezmoi|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=chezmoi-unavailable"; fi; print -r -- multi-shell-source ;;'
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

test_shared_chezmoi_resolver_finds_home_local_bin_outside_path() {
  local fixture home_dir chezmoi_bin output rc=0

  make_temp_dir "chezmoi-resolver-test"
  fixture="${REPLY:A}"
  home_dir="$fixture/home"
  chezmoi_bin="$home_dir/.local/bin/chezmoi"
  mkdir -p "$chezmoi_bin:h"
  print -r -- '#!/bin/sh' > "$chezmoi_bin"
  chmod +x "$chezmoi_bin"

  output="$(
    env -i HOME="$home_dir" PATH="/bin:/usr/bin:/usr/sbin:/sbin" \
      "$TEST_ZSH_BIN" -f -c '
        source "$1"
        resolve_chezmoi
        print -r -- "$REPLY"
      ' _ "$TEST_DIR/lib/assertions.sh"
  )" || rc=$?
  (( rc == 0 )) || fail 'shared chezmoi resolver failed in a temp HOME'
  [[ "$output" == "$chezmoi_bin" ]] || fail "shared chezmoi resolver selected an unexpected path: $output"

  rm -rf "$fixture"
}

write_resolver_executable() {
  local file_path="$1"

  mkdir -p "$file_path:h"
  print -r -- '#!/bin/sh' > "$file_path"
  chmod +x "$file_path"
}

write_resolver_mise() {
  local file_path="$1" mise_status="${2:-0}"

  mkdir -p "$file_path:h"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- 'if [ "${1:-}" = where ] && [ "${2:-}" = chezmoi@latest ]; then'
    print -r -- '  printf "%s\n" "resolver warning" >&2'
    print -r -- '  printf "%s\n" "$MISE_CHEZMOI_INSTALL_DIR"'
    print -r -- "  exit $mise_status"
    print -r -- 'fi'
    print -r -- 'exit 1'
  } > "$file_path"
  chmod +x "$file_path"
}

resolver_probe() {
  local home_dir="$1" path_result="$2" mise_bin="$3" mise_install_dir="$4"

  env -i HOME="$home_dir" PATH="/bin:/usr/bin" \
    RESOLVER_PATH_RESULT="$path_result" MISE_BIN="$mise_bin" \
    MISE_CHEZMOI_INSTALL_DIR="$mise_install_dir" \
    "$TEST_ZSH_BIN" -f -c '
      command() {
        if [[ "$1" == -v && "$2" == chezmoi ]]; then
          if [[ -n "${RESOLVER_PATH_RESULT:-}" ]]; then
            print -r -- "$RESOLVER_PATH_RESULT"
            return 0
          fi
          return 1
        fi
        if [[ "$1" == -v && "$2" == mise ]]; then
          print -r -- "$MISE_BIN"
          return 0
        fi
        builtin command "$@"
      }
      source "$1"
      resolve_chezmoi || exit $?
      print -r -- "$REPLY"
    ' _ "$TEST_DIR/lib/assertions.sh"
}

test_shared_chezmoi_resolver_rejects_path_directory_and_falls_back_to_mise() {
  local fixture path_candidate mise_bin mise_install_dir mise_candidate output rc=0

  make_temp_dir "chezmoi-path-directory-test"
  fixture="${REPLY:A}"
  path_candidate="$fixture/path/chezmoi"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"
  mise_candidate="$mise_install_dir/chezmoi"
  mkdir -p "$path_candidate"
  chmod +x "$path_candidate"
  write_resolver_mise "$mise_bin"
  write_resolver_executable "$mise_candidate"

  output="$(resolver_probe "$fixture/home" "$path_candidate" "$mise_bin" "$mise_install_dir")" || rc=$?
  (( rc == 0 )) || fail 'resolver must continue after rejecting a PATH directory'
  [[ "$output" == "$mise_candidate" ]] || fail "PATH directory must fall back to mise: $output"

  rm -rf "$fixture"
}

test_shared_chezmoi_resolver_rejects_home_directory_and_falls_back_to_mise() {
  local fixture home_dir home_candidate mise_bin mise_install_dir mise_candidate output rc=0

  make_temp_dir "chezmoi-home-directory-test"
  fixture="${REPLY:A}"
  home_dir="$fixture/home"
  home_candidate="$home_dir/.local/bin/chezmoi"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"
  mise_candidate="$mise_install_dir/chezmoi"
  mkdir -p "$home_candidate"
  chmod +x "$home_candidate"
  write_resolver_mise "$mise_bin"
  write_resolver_executable "$mise_candidate"

  output="$(resolver_probe "$home_dir" "" "$mise_bin" "$mise_install_dir")" || rc=$?
  (( rc == 0 )) || fail 'resolver must continue after rejecting a HOME directory'
  [[ "$output" == "$mise_candidate" ]] || fail "HOME directory must fall back to mise: $output"

  rm -rf "$fixture"
}

test_shared_chezmoi_resolver_rejects_path_control_byte_and_falls_back_to_mise() {
  local fixture control_dir path_candidate mise_bin mise_install_dir mise_candidate output rc=0

  make_temp_dir "chezmoi-path-control-test"
  fixture="${REPLY:A}"
  control_dir="$fixture/path"$'\n''segment'
  path_candidate="$control_dir/chezmoi"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"
  mise_candidate="$mise_install_dir/chezmoi"
  write_resolver_executable "$path_candidate"
  write_resolver_mise "$mise_bin"
  write_resolver_executable "$mise_candidate"

  output="$(resolver_probe "$fixture/home" "$path_candidate" "$mise_bin" "$mise_install_dir")" || rc=$?
  (( rc == 0 )) || fail 'resolver must continue after rejecting a PATH control-byte path'
  [[ "$output" == "$mise_candidate" ]] || fail "PATH control-byte path must fall back to mise: $output"

  rm -rf "$fixture"
}

test_shared_chezmoi_resolver_rejects_home_control_byte_and_falls_back_to_mise() {
  local fixture home_dir home_candidate mise_bin mise_install_dir mise_candidate output rc=0

  make_temp_dir "chezmoi-home-control-test"
  fixture="${REPLY:A}"
  home_dir="$fixture/home"$'\t''segment'
  home_candidate="$home_dir/.local/bin/chezmoi"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"
  mise_candidate="$mise_install_dir/chezmoi"
  write_resolver_executable "$home_candidate"
  write_resolver_mise "$mise_bin"
  write_resolver_executable "$mise_candidate"

  output="$(resolver_probe "$home_dir" "" "$mise_bin" "$mise_install_dir")" || rc=$?
  (( rc == 0 )) || fail 'resolver must continue after rejecting a HOME control-byte path'
  [[ "$output" == "$mise_candidate" ]] || fail "HOME control-byte path must fall back to mise: $output"

  rm -rf "$fixture"
}

test_shared_chezmoi_resolver_rejects_mise_directory_candidate() {
  local fixture mise_bin mise_install_dir output rc=0

  make_temp_dir "chezmoi-mise-directory-test"
  fixture="${REPLY:A}"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"
  mkdir -p "$mise_install_dir/chezmoi"
  chmod +x "$mise_install_dir/chezmoi"
  write_resolver_mise "$mise_bin"

  output="$(resolver_probe "$fixture/home" "" "$mise_bin" "$mise_install_dir")" || rc=$?
  (( rc == 127 )) || fail "resolver must reject a mise ChezMoi directory with rc=127 (got $rc)"
  [[ -z "$output" ]] || fail "rejected mise directory must not produce a selected candidate: $output"

  rm -rf "$fixture"
}

test_shared_chezmoi_resolver_rejects_mise_control_byte_output() {
  local fixture mise_bin mise_install_dir output rc=0

  make_temp_dir "chezmoi-mise-control-test"
  fixture="${REPLY:A}"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"$'\n''segment'
  write_resolver_mise "$mise_bin"
  write_resolver_executable "$mise_install_dir/chezmoi"

  output="$(resolver_probe "$fixture/home" "" "$mise_bin" "$mise_install_dir")" || rc=$?
  (( rc == 127 )) || fail "resolver must reject a mise control-byte output with rc=127 (got $rc)"
  [[ -z "$output" ]] || fail "rejected mise control-byte output must not produce a selected candidate: $output"

  rm -rf "$fixture"
}

test_shared_chezmoi_resolver_rejects_mise_nonzero_status_with_valid_output() {
  local fixture mise_bin mise_install_dir mise_candidate stderr output rc=0

  make_temp_dir "chezmoi-mise-status-test"
  fixture="${REPLY:A}"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"
  mise_candidate="$mise_install_dir/chezmoi"
  stderr="$fixture/stderr.log"
  write_resolver_mise "$mise_bin" 23
  write_resolver_executable "$mise_candidate"

  output="$(resolver_probe "$fixture/home" "" "$mise_bin" "$mise_install_dir" 2> "$stderr")" || rc=$?
  (( rc == 127 )) || fail "resolver must reject mise output when where exits non-zero (got $rc)"
  [[ -z "$output" ]] || fail "non-zero mise where must not produce a selected candidate: $output"
  [[ ! -s "$stderr" ]] || fail "mise stderr must not leak from resolver candidate lookup"

  rm -rf "$fixture"
}

test_shared_chezmoi_resolver_accepts_symlink_to_regular_executable() {
  local fixture path_candidate target mise_bin mise_install_dir output rc=0

  make_temp_dir "chezmoi-symlink-test"
  fixture="${REPLY:A}"
  path_candidate="$fixture/path/chezmoi"
  target="$fixture/target/chezmoi"
  mise_bin="$fixture/bin/mise"
  mise_install_dir="$fixture/mise-install"
  write_resolver_executable "$target"
  mkdir -p "$path_candidate:h"
  ln -s "$target" "$path_candidate"
  write_resolver_mise "$mise_bin"
  write_resolver_executable "$mise_install_dir/chezmoi"

  output="$(resolver_probe "$fixture/home" "$path_candidate" "$mise_bin" "$mise_install_dir")" || rc=$?
  (( rc == 0 )) || fail 'resolver must accept a symlink to a regular executable'
  [[ "$output" == "$path_candidate" ]] || fail "resolver must retain the valid symlink candidate: $output"

  rm -rf "$fixture"
}

test_multi_shell_source_skips_only_when_chezmoi_is_unavailable() {
  local fixture output python_bin rc=0

  make_temp_dir "chezmoi-source-skip-test"
  fixture="${REPLY:A}"
  output="$fixture/output.log"
  python_bin="$(command -v python3)"

  env -i HOME="$fixture/home" PATH="$fixture/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_PYTHON="$python_bin" DOTFILES_TEST_ZSH_BIN="$TEST_ZSH_BIN" \
    "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_multi_shell_config.sh" --selector source > "$output" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'source selector must remain not-applicable when chezmoi is unavailable'
  grep -Eq '^MATRIX_RESULT\|os=(linux|macos)\|shell=chezmoi\|target=chezmoi-source\|status=SKIP\|requirement=not-applicable\|reason=chezmoi-unavailable$' "$output" \
    || fail 'source selector must emit the explicit seven-field chezmoi skip row'

  rm -rf "$fixture"
}

test_multi_shell_source_skip_avoids_chezmoi_resolution() {
  local fixture output resolver_log python_bin rc=0

  make_temp_dir "chezmoi-source-explicit-skip-test"
  fixture="${REPLY:A}"
  output="$fixture/output.log"
  resolver_log="$fixture/resolver.log"
  python_bin="$(command -v python3)"
  mkdir -p "$fixture/bin"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'printf "%s\\n" invoked >> "$RESOLVER_LOG"'
    print -r -- 'exit 1'
  } > "$fixture/bin/mise"
  chmod +x "$fixture/bin/mise"

  env -i HOME="$fixture/home" PATH="$fixture/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    RESOLVER_LOG="$resolver_log" DOTFILES_TEST_PYTHON="$python_bin" DOTFILES_TEST_ZSH_BIN="$TEST_ZSH_BIN" \
    "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_multi_shell_config.sh" --selector source --skip-chezmoi > "$output" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'explicit source skip must return success'
  assert_output_contains "$output" 'MATRIX_RESULT|os='
  assert_output_contains "$output" '|shell=chezmoi|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=chezmoi-skipped'
  assert_not_exists "$resolver_log"
  assert_not_contains "$output" 'target=chezmoi-source|status=PASS'
  assert_output_contains "$output" 'multi-shell source checks passed'

  rm -rf "$fixture"
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
  assert_output_contains "$output" "source-resolver-skipped"
  assert_output_contains "$output" "MATRIX_RESULT|os=linux|shell=chezmoi|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=chezmoi-skipped"
  assert_output_contains "$output" "SKIP: chezmoi rendered-home checks disabled by --skip-chezmoi"
  assert_output_contains "$output" "SKIP: multi-shell render/runtime checks disabled by --skip-chezmoi"
  assert_output_contains "$output" "dotfiles tests passed"
  assert_not_contains "$output" "source-resolver-called"
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
  assert_output_contains "$output" "source-resolver-called"
  assert_output_contains "$output" "MATRIX_RESULT|os=linux|shell=chezmoi|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=chezmoi-unavailable"
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
  PATH="$repo:$PATH" BASH32_BIN="$fake_bash" BASH5_BIN="$fake_bash" "$TEST_ZSH_BIN" "$REPO_ROOT/tests/test_multi_shell_config.sh" --selector render > "$output" 2>&1
  exit_status=$?
  set -e

  (( exit_status != 0 )) || fail 'required Bash 5 skip must keep a non-zero status'
  assert_output_contains "$output" 'shell=bash5|target=rendered-home|status=SKIP|requirement=required|reason=bash5-unavailable'
  assert_output_contains "$output" 'shell=bash|target=hostile-repo-root|status=SKIP|requirement=not-applicable|reason=no-supported-bash'
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
  test_shared_chezmoi_resolver_finds_home_local_bin_outside_path
  test_shared_chezmoi_resolver_rejects_path_directory_and_falls_back_to_mise
  test_shared_chezmoi_resolver_rejects_home_directory_and_falls_back_to_mise
  test_shared_chezmoi_resolver_rejects_path_control_byte_and_falls_back_to_mise
  test_shared_chezmoi_resolver_rejects_home_control_byte_and_falls_back_to_mise
  test_shared_chezmoi_resolver_rejects_mise_directory_candidate
  test_shared_chezmoi_resolver_rejects_mise_control_byte_output
  test_shared_chezmoi_resolver_rejects_mise_nonzero_status_with_valid_output
  test_shared_chezmoi_resolver_accepts_symlink_to_regular_executable
  test_multi_shell_source_skips_only_when_chezmoi_is_unavailable
  test_multi_shell_source_skip_avoids_chezmoi_resolution
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
