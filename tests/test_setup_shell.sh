#!/usr/bin/env bash

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
readonly TEST_DIR
REPO_ROOT="$(cd "$TEST_DIR/.." && pwd -P)"
readonly REPO_ROOT
readonly SETUP_SCRIPT="$REPO_ROOT/scripts/setup_shell.sh"
readonly TEST_REQUIRE_FISH="${DOTFILES_TEST_REQUIRE_FISH:-0}"
readonly TEST_REQUIRE_CSH="${DOTFILES_TEST_REQUIRE_CSH:-0}"
readonly TEST_REQUIRE_TCSH="${DOTFILES_TEST_REQUIRE_TCSH:-0}"

TEST_ROOT=""
CHEZMOI_BIN="${DOTFILES_TEST_CHEZMOI_BIN:-}"
FISH_BIN="${DOTFILES_TEST_FISH_BIN:-}"
CSH_BIN="${DOTFILES_TEST_CSH_BIN:-}"
TCSH_BIN="${DOTFILES_TEST_TCSH_BIN:-}"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "$TEST_ROOT" && -d "$TEST_ROOT" ]]; then
    rm -rf "$TEST_ROOT"
  fi
}

trap cleanup EXIT HUP INT TERM

assert_file() {
  [[ -f "$1" ]] || fail "expected file: $1"
}

assert_not_exists() {
  [[ ! -e "$1" && ! -L "$1" ]] || fail "expected path not to exist: $1"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  assert_file "$file_path"
  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

assert_output_contains() {
  local file_path="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$file_path" || {
    printf 'Output from %s:\n' "$file_path" >&2
    sed -n '1,160p' "$file_path" >&2
    fail "expected output to contain: $expected"
  }
}

assert_same_file() {
  assert_file "$1"
  assert_file "$2"
  cmp "$1" "$2" >/dev/null || fail "expected files to match: $1 and $2"
}

assert_exact_content() {
  local file_path="$1"
  local expected="$2"
  local actual

  assert_file "$file_path"
  actual="$(cat "$file_path")"
  [[ "$actual" == "$expected" ]] || fail "unexpected content in $file_path"
}

file_mode() {
  local mode

  mode="$(stat -c '%a' "$1" 2>/dev/null || true)"
  if [[ "$mode" =~ ^[0-7]{3,4}$ ]]; then
    printf '%s\n' "$mode"
    return 0
  fi
  mode="$(stat -f '%Lp' "$1" 2>/dev/null || true)"
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || fail "could not read mode: $1"
  printf '%s\n' "$mode"
}

resolve_executable() {
  local candidate="$1"

  [[ "$candidate" == /* && -f "$candidate" && -x "$candidate" ]] || return 1
  [[ "$candidate" != *[[:cntrl:]]* ]] || return 1
}

resolve_runtime_bins() {
  if [[ -z "$CHEZMOI_BIN" ]]; then
    CHEZMOI_BIN="$(command -v chezmoi 2>/dev/null || true)"
  fi
  resolve_executable "$CHEZMOI_BIN" || fail "chezmoi is required for setup_shell integration tests"

  if [[ -z "$FISH_BIN" ]]; then
    FISH_BIN="$(command -v fish 2>/dev/null || true)"
  fi
  if [[ -z "$CSH_BIN" ]]; then
    CSH_BIN="$(command -v csh 2>/dev/null || true)"
  fi
  if [[ -z "$TCSH_BIN" ]]; then
    TCSH_BIN="$(command -v tcsh 2>/dev/null || true)"
  fi

  [[ -z "$FISH_BIN" ]] || resolve_executable "$FISH_BIN" || fail "invalid Fish test runtime: $FISH_BIN"
  [[ -z "$CSH_BIN" ]] || resolve_executable "$CSH_BIN" || fail "invalid csh test runtime: $CSH_BIN"
  [[ -z "$TCSH_BIN" ]] || resolve_executable "$TCSH_BIN" || fail "invalid tcsh test runtime: $TCSH_BIN"
}

new_fixture() {
  local fixture

  fixture="$(mktemp -d "$TEST_ROOT/fixture.XXXXXX")"
  mkdir -p "$fixture/home" "$fixture/tmp"
  printf '%s\n' "$fixture"
}

test_path() {
  local chezmoi_dir

  chezmoi_dir="$(dirname "$CHEZMOI_BIN")"
  printf '%s:%s\n' "$chezmoi_dir" "/bin:/usr/bin:/usr/sbin:/sbin"
}

run_setup() {
  local home="$1"
  local xdg_config_home="$2"
  local output="$3"
  shift 3
  local -a env_args=(
    "HOME=$home"
    "PATH=$(test_path)"
    "SHELL=/bin/bash"
    "USER=dotfiles-test"
    "TMPDIR=$TEST_ROOT/tmp"
  )

  if [[ "$xdg_config_home" != __UNSET__ ]]; then
    env_args+=("XDG_CONFIG_HOME=$xdg_config_home")
  fi
  env -i "${env_args[@]}" /bin/bash "$SETUP_SCRIPT" "$@" > "$output" 2>&1
}

run_setup_expect_failure() {
  local home="$1"
  local xdg_config_home="$2"
  local output="$3"
  shift 3
  local rc=0

  run_setup "$home" "$xdg_config_home" "$output" "$@" || rc=$?
  (( rc != 0 )) || fail "setup_shell unexpectedly succeeded: $*"
}

assert_shell_targets() {
  local home="$1"

  for target in \
    "$home/.bashrc" \
    "$home/.bash_profile" \
    "$home/.config/shell/dotfiles-shell-common.sh" \
    "$home/.config/fish/conf.d/zz-dotfiles.fish" \
    "$home/.config/shell/dotfiles-shell-common.csh" \
    "$home/.config/mise/config.toml"
  do
    assert_file "$target"
  done
}

assert_no_shell_targets() {
  local home="$1"

  for target in \
    "$home/.bashrc" \
    "$home/.bash_profile" \
    "$home/.config/shell/dotfiles-shell-common.sh" \
    "$home/.config/fish/conf.d/zz-dotfiles.fish" \
    "$home/.config/shell/dotfiles-shell-common.csh" \
    "$home/.config/mise/config.toml"
  do
    assert_not_exists "$target"
  done
}

assert_unmanaged_targets_untouched() {
  local home="$1"

  assert_not_exists "$home/.tmux.conf"
  assert_not_exists "$home/.config/alacritty"
  assert_not_exists "$home/.config/ghostty"
  assert_not_exists "$home/.config/nix"
  assert_not_exists "$home/.config/zellij"
  assert_not_exists "$home/README.md"
  assert_not_exists "$home/README_JA.md"
}

run_bash_startup_smoke() {
  local home="$1"
  local output="$2"

  env -i \
    HOME="$home" \
    XDG_CONFIG_HOME="$home/.config" \
    PATH="$(test_path)" \
    SHELL=/bin/bash \
    USER=dotfiles-test \
    /bin/bash --noprofile --norc -c '
      . "$HOME/.bash_profile"
      printf "shell=%s\neditor=%s\nroot=%s\nconfig=%s\n" \
        "$dotfiles_shell_name" "$EDITOR" "$DOTFILES_REPO_ROOT" "$XDG_CONFIG_HOME"
    ' > "$output" 2>&1
  assert_output_contains "$output" 'shell=bash'
  assert_output_contains "$output" 'editor=nvim'
  assert_output_contains "$output" "root=$REPO_ROOT"
  assert_output_contains "$output" "config=$home/.config"
}

run_fish_smoke() {
  local home="$1"
  local output="$2"
  local fish_path="$FISH_BIN"

  if [[ -z "$fish_path" ]]; then
    if [[ "$TEST_REQUIRE_FISH" == 1 ]]; then
      fail 'Fish runtime is required but unavailable'
    fi
    printf 'SKIP: Fish runtime unavailable\n'
    return 0
  fi

  env -i \
    HOME="$home" \
    XDG_CONFIG_HOME="$home/.config" \
    XDG_CACHE_HOME="$home/.cache" \
    XDG_DATA_HOME="$home/.local/share" \
    XDG_STATE_HOME="$home/.local/state" \
    PATH="$(test_path)" \
    SHELL="$fish_path" \
    USER=dotfiles-test \
    "$fish_path" --no-config -c '
      source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"
      test "$EDITOR" = nvim
      functions -q ginit; and exit 1
      test "$DOTFILES_MISE_ACTIVATE_FISH_FAILED" = 0
      echo fish=non-interactive
    ' > "$output" 2>&1
  assert_output_contains "$output" 'fish=non-interactive'

  env -i \
    HOME="$home" \
    XDG_CONFIG_HOME="$home/.config" \
    XDG_CACHE_HOME="$home/.cache" \
    XDG_DATA_HOME="$home/.local/share" \
    XDG_STATE_HOME="$home/.local/state" \
    PATH="$(test_path)" \
    SHELL="$fish_path" \
    USER=dotfiles-test \
    "$fish_path" --no-config -i -c '
      source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"
      test "$EDITOR" = nvim
      functions -q ginit; or exit 1
      echo fish=interactive
    ' >> "$output" 2>&1
  assert_output_contains "$output" 'fish=interactive'
}

run_csh_smoke_one() {
  local shell_name="$1"
  local shell_path="$2"
  local home="$3"
  local output="$4"

  env -i \
    HOME="$home" \
    PATH="$(test_path)" \
    SHELL="$shell_path" \
    SHELL_NAME="$shell_name" \
    USER=dotfiles-test \
    DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_path" -f -c '
      source "$DOTFILES_CSH_ADAPTER"
      source "$DOTFILES_CSH_ADAPTER"
      echo "shell=$SHELL_NAME"
      echo "editor=$EDITOR"
      echo "path=$PATH"
    ' > "$output" 2>&1
  assert_output_contains "$output" 'editor=nvim'
  assert_output_contains "$output" "shell=$shell_name"
}

run_csh_smoke() {
  local home="$1"
  local output="$2"
  local ran=0

  if [[ -n "$CSH_BIN" ]]; then
    run_csh_smoke_one csh "$CSH_BIN" "$home" "$output"
    ran=1
  elif [[ "$TEST_REQUIRE_CSH" == 1 ]]; then
    fail 'csh runtime is required but unavailable'
  fi

  if [[ -n "$TCSH_BIN" ]]; then
    run_csh_smoke_one tcsh "$TCSH_BIN" "$home" "$output"
    ran=1
  elif [[ "$TEST_REQUIRE_TCSH" == 1 ]]; then
    fail 'tcsh runtime is required but unavailable'
  fi

  if (( ! ran )); then
    printf 'SKIP: csh/tcsh runtimes unavailable\n'
  fi
}

test_help_and_dry_run() {
  local fixture home output

  fixture="$(new_fixture)"
  home="$fixture/home"
  output="$fixture/help.log"
  env -i HOME="$home" PATH="$(test_path)" /bin/bash "$SETUP_SCRIPT" --help > "$output" 2>&1
  assert_output_contains "$output" 'Usage: bash scripts/setup_shell.sh'
  assert_output_contains "$output" '--dry-run'
  assert_output_contains "$output" '--verify'
  assert_output_contains "$output" '--force'

  output="$fixture/dry-run.log"
  run_setup "$home" __UNSET__ "$output" --dry-run
  assert_no_shell_targets "$home"
  assert_output_contains "$output" 'Dry-run'
  for target in \
    "$home/.bashrc" \
    "$home/.bash_profile" \
    "$home/.config/shell/dotfiles-shell-common.sh" \
    "$home/.config/fish/conf.d/zz-dotfiles.fish" \
    "$home/.config/shell/dotfiles-shell-common.csh" \
    "$home/.config/mise/config.toml"
  do
    assert_output_contains "$output" "Plan: target=$target action=chezmoi-apply"
  done
}

test_apply_allowlist_and_runtimes() {
  local fixture home output custom_xdg secret_before profile_before foreign_fish secret_mode

  fixture="$(new_fixture)"
  home="$fixture/home"
  mkdir -p "$home/.config/fish" "$home/.config/shell" "$home/.config/mise"
  profile_before="$fixture/profile.before"
  secret_before="$fixture/secrets.before"
  foreign_fish="$fixture/fish-config.before"
  printf '%s\n' 'foreign profile' > "$home/.profile"
  printf '%s\n' 'foreign secret' > "$home/.config/shell/secrets.env"
  printf '%s\n' 'foreign fish config' > "$home/.config/fish/config.fish"
  printf '%s\n' 'foreign csh' > "$home/.cshrc"
  printf '%s\n' 'foreign tcsh' > "$home/.tcshrc"
  printf '%s\n' 'foreign mise file' > "$home/.config/mise/foreign.toml"
  cp "$home/.profile" "$profile_before"
  cp "$home/.config/shell/secrets.env" "$secret_before"
  cp "$home/.config/fish/config.fish" "$foreign_fish"
  chmod 600 "$home/.config/shell/secrets.env"
  secret_mode="$(file_mode "$home/.config/shell/secrets.env")"

  output="$fixture/apply.log"
  run_setup "$home" __UNSET__ "$output"
  assert_shell_targets "$home"
  assert_unmanaged_targets_untouched "$home"
  assert_same_file "$profile_before" "$home/.profile"
  assert_same_file "$secret_before" "$home/.config/shell/secrets.env"
  [[ "$(file_mode "$home/.config/shell/secrets.env")" == "$secret_mode" ]] || fail 'secret mode changed'
  assert_same_file "$foreign_fish" "$home/.config/fish/config.fish"
  assert_exact_content "$home/.cshrc" 'foreign csh'
  assert_exact_content "$home/.tcshrc" 'foreign tcsh'
  assert_exact_content "$home/.config/mise/foreign.toml" 'foreign mise file'
  assert_contains "$home/.config/mise/config.toml" "$REPO_ROOT"
  assert_not_contains "$home/.config/mise/config.toml" '__DOTFILES_REPO_ROOT__'
  run_bash_startup_smoke "$home" "$fixture/bash.log"
  run_fish_smoke "$home" "$fixture/fish.log"
  run_csh_smoke "$home" "$fixture/csh.log"

  custom_xdg="$fixture/custom xdg"
  output="$fixture/custom-apply.log"
  run_setup "$home" "$custom_xdg" "$output"
  assert_file "$custom_xdg/fish/conf.d/zz-dotfiles-canonical.fish"
  assert_exact_content "$custom_xdg/fish/conf.d/zz-dotfiles-canonical.fish" 'set -l dotfiles_canonical_fish "$HOME/.config/fish/conf.d/zz-dotfiles.fish"
if test -r "$dotfiles_canonical_fish"
    source "$dotfiles_canonical_fish"
end'
  if [[ -n "$FISH_BIN" ]]; then
    env -i \
      HOME="$home" \
      XDG_CONFIG_HOME="$custom_xdg" \
      XDG_CACHE_HOME="$home/.cache" \
      XDG_DATA_HOME="$home/.local/share" \
      XDG_STATE_HOME="$home/.local/state" \
      PATH="$(test_path)" \
      SHELL="$FISH_BIN" \
      USER=dotfiles-test \
      "$FISH_BIN" -i -c 'functions -q ginit; or exit 1; test "$EDITOR" = nvim; echo fish=custom-xdg' > "$fixture/fish-custom.log" 2>&1
    assert_output_contains "$fixture/fish-custom.log" 'fish=custom-xdg'
  fi
}

test_conflict_preflight() {
  local fixture home output custom_xdg bridge

  fixture="$(new_fixture)"
  home="$fixture/home"
  mkdir -p "$home/.config/mise"
  printf '%s\n' 'foreign bashrc' > "$home/.bashrc"
  printf '%s\n' 'foreign mise config' > "$home/.config/mise/config.toml"
  output="$fixture/target-conflict.log"
  run_setup_expect_failure "$home" __UNSET__ "$output"
  assert_exact_content "$home/.bashrc" 'foreign bashrc'
  assert_exact_content "$home/.config/mise/config.toml" 'foreign mise config'
  assert_not_exists "$home/.bash_profile"
  assert_not_exists "$home/.config/shell"
  assert_not_exists "$home/.config/fish/conf.d/zz-dotfiles.fish"
  assert_output_contains "$output" 'refusing to overwrite'

  fixture="$(new_fixture)"
  home="$fixture/home"
  custom_xdg="$fixture/custom xdg"
  bridge="$custom_xdg/fish/conf.d/zz-dotfiles-canonical.fish"
  mkdir -p "${bridge%/*}"
  printf '%s\n' 'foreign bridge' > "$bridge"
  output="$fixture/bridge-conflict.log"
  run_setup_expect_failure "$home" "$custom_xdg" "$output"
  assert_exact_content "$bridge" 'foreign bridge'
  assert_no_shell_targets "$home"
  assert_output_contains "$output" 'Fish XDG bridge'
}

test_parent_chain_preflight() {
  local fixture home external output custom_xdg xdg_file xdg_link

  fixture="$(new_fixture)"
  home="$fixture/home"
  external="$fixture/external"
  mkdir -p "$external/.config/shell" "$external/.config/fish/conf.d" "$external/.config/mise"
  ln -s "$external/.config" "$home/.config"
  output="$fixture/target-parent-symlink.log"
  run_setup_expect_failure "$home" __UNSET__ "$output"
  [[ -L "$home/.config" ]] || fail 'target parent symlink was changed'
  assert_no_shell_targets "$home"
  assert_no_shell_targets "$external"
  assert_output_contains "$output" 'shell target parent symlink is not allowed'

  fixture="$(new_fixture)"
  home="$fixture/home"
  xdg_file="$fixture/xdg-parent-file"
  printf '%s\n' 'not a directory' > "$xdg_file"
  custom_xdg="$xdg_file/fish-config"
  output="$fixture/xdg-parent-file.log"
  run_setup_expect_failure "$home" "$custom_xdg" "$output" --dry-run
  assert_no_shell_targets "$home"
  assert_not_exists "$custom_xdg"
  assert_output_contains "$output" 'Fish XDG bridge parent is not a directory'

  fixture="$(new_fixture)"
  home="$fixture/home"
  external="$fixture/xdg-external"
  xdg_link="$fixture/xdg-link"
  mkdir -p "$external"
  ln -s "$external" "$xdg_link"
  custom_xdg="$xdg_link/fish-config"
  output="$fixture/xdg-parent-symlink.log"
  run_setup_expect_failure "$home" "$custom_xdg" "$output"
  [[ -L "$xdg_link" ]] || fail 'custom XDG parent symlink was changed'
  assert_no_shell_targets "$home"
  assert_not_exists "$custom_xdg"
  assert_output_contains "$output" 'Fish XDG bridge parent symlink is not allowed'
}

test_verify_force_and_clean_rerun() {
  local fixture home output custom_xdg bridge snapshot bridge_mode

  fixture="$(new_fixture)"
  home="$fixture/home"
  custom_xdg="$fixture/custom xdg"
  output="$fixture/first.log"
  run_setup "$home" "$custom_xdg" "$output"
  snapshot="$fixture/bashrc.snapshot"
  cp "$home/.bashrc" "$snapshot"
  bridge="$custom_xdg/fish/conf.d/zz-dotfiles-canonical.fish"
  run_setup "$home" "$custom_xdg" "$fixture/rerun.log"
  assert_same_file "$snapshot" "$home/.bashrc"
  assert_file "$bridge"
  bridge_mode="$(file_mode "$bridge")"
  [[ "$bridge_mode" == 644 ]] || fail "unexpected Fish XDG bridge mode: $bridge_mode"

  run_setup "$home" "$custom_xdg" "$fixture/verify.log" --verify
  printf '%s\n' 'modified by fixture' > "$home/.bashrc"
  run_setup_expect_failure "$home" "$custom_xdg" "$fixture/verify-failure.log" --verify
  assert_exact_content "$home/.bashrc" 'modified by fixture'
  run_setup "$home" "$custom_xdg" "$fixture/force.log" --force
  assert_same_file "$snapshot" "$home/.bashrc"

  printf '%s\n' 'foreign bridge' > "$bridge"
  run_setup_expect_failure "$home" "$custom_xdg" "$fixture/bridge-verify-failure.log" --verify
  assert_exact_content "$bridge" 'foreign bridge'
  run_setup_expect_failure "$home" "$custom_xdg" "$fixture/bridge-force-failure.log"
  assert_exact_content "$bridge" 'foreign bridge'
  run_setup "$home" "$custom_xdg" "$fixture/bridge-force.log" --force
  assert_file "$bridge"
  assert_output_contains "$fixture/bridge-force.log" 'Fish XDG bridge'
}

main() {
  TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/setup-shell-suite.XXXXXX")"
  TEST_ROOT="$(cd "$TEST_ROOT" && pwd -P)"
  mkdir -p "$TEST_ROOT/tmp"
  resolve_runtime_bins
  test_help_and_dry_run
  test_apply_allowlist_and_runtimes
  test_conflict_preflight
  test_verify_force_and_clean_rerun
  printf 'setup_shell tests passed\n'
}

main "$@"
