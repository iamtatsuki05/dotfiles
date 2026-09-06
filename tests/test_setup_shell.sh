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

assert_file_prefix() {
  local actual_file="$1"
  local prefix_file="$2"
  local prefix_bytes prefix_copy

  assert_file "$actual_file"
  assert_file "$prefix_file"
  prefix_bytes="$(wc -c < "$prefix_file" | tr -d '[:space:]')"
  prefix_copy="$TEST_ROOT/prefix.$RANDOM"
  dd if="$actual_file" of="$prefix_copy" bs=1 count="$prefix_bytes" 2>/dev/null || {
    fail "could not read $prefix_bytes prefix bytes from $actual_file"
  }
  assert_same_file "$prefix_file" "$prefix_copy"
}

assert_csh_startup_block() {
  local file_path="$1"
  local start_count end_count

  assert_file "$file_path"
  start_count="$(grep -Fxc -- '# >>> dotfiles:csh-adapter >>>' "$file_path" || true)"
  end_count="$(grep -Fxc -- '# <<< dotfiles:csh-adapter <<<' "$file_path" || true)"
  [[ "$start_count" == 1 ]] || fail "expected one csh startup block start in $file_path, got $start_count"
  [[ "$end_count" == 1 ]] || fail "expected one csh startup block end in $file_path, got $end_count"
  assert_contains "$file_path" 'if ( ! $?df_csh_loaded ) then'
  assert_contains "$file_path" 'source "$HOME/.config/shell/dotfiles-shell-common.csh"'
  assert_contains "$file_path" 'if ( $status == 0 ) set df_csh_loaded = 1'
  assert_not_contains "$file_path" 'setenv df_csh_loaded'
}

write_csh_startup_block_fixture() {
  local file_path="$1"

  printf '%s\n' \
    '# >>> dotfiles:csh-adapter >>>' \
    'if ( ! $?df_csh_loaded ) then' \
    '  if ( -r "$HOME/.config/shell/dotfiles-shell-common.csh" ) then' \
    '    source "$HOME/.config/shell/dotfiles-shell-common.csh"' \
    '    if ( $status == 0 ) set df_csh_loaded = 1' \
    '  endif' \
    'endif' \
    '# <<< dotfiles:csh-adapter <<<' > "$file_path"
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

assert_no_csh_startup_files() {
  local home="$1"

  assert_not_exists "$home/.cshrc"
  assert_not_exists "$home/.tcshrc"
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

write_counting_csh_adapter() {
  local home="$1"

  printf '%s\n' \
    'if ( $?df_test_hits ) then' \
    '  @ df_test_hits++' \
    'else' \
    '  set df_test_hits = 1' \
    'endif' > "$home/.config/shell/dotfiles-shell-common.csh"
}

run_csh_startup_count() {
  local shell_bin="$1"
  local home="$2"
  local output="$3"

  env -i \
    HOME="$home" \
    PATH="$(test_path)" \
    SHELL="$shell_bin" \
    USER=dotfiles-test \
    "$shell_bin" -c '
      if ( $?df_test_hits ) then
        echo "adapter_hits=$df_test_hits"
      else
        echo adapter_hits=unset
      endif
    ' > "$output" 2>&1
  assert_output_contains "$output" 'adapter_hits=1'
}

test_csh_startup_presence_matrix() {
  local fixture home output mode

  if [[ -z "$CSH_BIN" && -z "$TCSH_BIN" ]]; then
    if [[ "$TEST_REQUIRE_TCSH" == 1 || "$TEST_REQUIRE_CSH" == 1 ]]; then
      fail 'csh/tcsh runtime is required for startup loading tests but unavailable'
    fi
    printf 'SKIP: csh/tcsh runtimes unavailable for startup loading tests\n'
    return 0
  fi

  for mode in neither csh-only tcsh-only both; do
    fixture="$(new_fixture)"
    home="$fixture/home"
    case "$mode" in
      csh-only)
        printf '%s' '# foreign csh body without a final newline' > "$home/.cshrc"
        ;;
      tcsh-only)
        printf '%s\n' '# foreign tcsh body' > "$home/.tcshrc"
        ;;
      both)
        printf '%s' '# foreign csh body without a final newline' > "$home/.cshrc"
        printf '%s\n' 'source "$HOME/.cshrc"' > "$home/.tcshrc"
        ;;
    esac

    output="$fixture/apply.log"
    run_setup "$home" __UNSET__ "$output"
    write_counting_csh_adapter "$home"
    if [[ -n "$CSH_BIN" ]]; then
      run_csh_startup_count "$CSH_BIN" "$home" "$fixture/csh-startup.log"
    fi
    if [[ -n "$TCSH_BIN" ]]; then
      run_csh_startup_count "$TCSH_BIN" "$home" "$fixture/tcsh-startup.log"
    fi
    assert_file "$home/.cshrc"
    assert_csh_startup_block "$home/.cshrc"
    case "$mode" in
      neither|csh-only)
        assert_not_exists "$home/.tcshrc"
        ;;
      tcsh-only|both)
        assert_file "$home/.tcshrc"
        assert_csh_startup_block "$home/.tcshrc"
        ;;
    esac
  done
}

test_csh_startup_preserves_later_settings() {
  local fixture home

  fixture="$(new_fixture)"
  home="$fixture/home"
  run_setup "$home" __UNSET__ "$fixture/first.log"
  printf '%s\n' '# user settings added after setup' 'set custom_shell_setting = yes' >> "$home/.cshrc"
  cp "$home/.cshrc" "$fixture/cshrc.snapshot"
  run_setup "$home" __UNSET__ "$fixture/verify.log" --verify || {
    sed -n '1,40p' "$fixture/verify.log" >&2
    fail 'valid user settings after the managed block must be accepted'
  }
  run_setup "$home" __UNSET__ "$fixture/rerun.log"
  assert_same_file "$fixture/cshrc.snapshot" "$home/.cshrc"
  assert_csh_startup_block "$home/.cshrc"
}

test_csh_startup_invalid_state_is_fail_fast() {
  local fixture home output case_name snapshot foreign foreign_snapshot

  for case_name in malformed duplicate foreign-marker; do
    fixture="$(new_fixture)"
    home="$fixture/home"
    case "$case_name" in
      malformed)
        printf '%s\n' \
          'foreign csh prefix' \
          '# >>> dotfiles:csh-adapter >>>' \
          'source "$HOME/foreign.csh"' > "$home/.cshrc"
        ;;
      duplicate)
        printf '%s\n' 'foreign csh prefix' > "$home/.cshrc"
        write_csh_startup_block_fixture "$fixture/block"
        printf '\n' >> "$home/.cshrc"
        cat "$fixture/block" >> "$home/.cshrc"
        printf '\n' >> "$home/.cshrc"
        cat "$fixture/block" >> "$home/.cshrc"
        ;;
      foreign-marker)
        printf '%s\n' \
          'foreign csh prefix' \
          '# >>> dotfiles:csh-adapter >>>' \
          'source "$HOME/foreign.csh"' \
          '# <<< dotfiles:csh-adapter <<<' > "$home/.cshrc"
        ;;
    esac
    snapshot="$fixture/cshrc.snapshot"
    cp "$home/.cshrc" "$snapshot"
    output="$fixture/$case_name.log"
    run_setup_expect_failure "$home" __UNSET__ "$output"
    assert_same_file "$snapshot" "$home/.cshrc"
    assert_no_shell_targets "$home"
    assert_not_exists "$home/.tcshrc"
    assert_output_contains "$output" 'csh startup'

    if [[ "$case_name" == malformed ]]; then
      output="$fixture/$case_name-force.log"
      run_setup_expect_failure "$home" __UNSET__ "$output" --force
      assert_same_file "$snapshot" "$home/.cshrc"
      assert_no_shell_targets "$home"
    fi
  done

  fixture="$(new_fixture)"
  home="$fixture/home"
  foreign="$fixture/foreign-cshrc"
  foreign_snapshot="$fixture/foreign-cshrc.snapshot"
  printf '%s\n' 'foreign csh target' > "$foreign"
  cp "$foreign" "$foreign_snapshot"
  ln -s "$foreign" "$home/.cshrc"
  output="$fixture/symlink.log"
  run_setup_expect_failure "$home" __UNSET__ "$output"
  [[ -L "$home/.cshrc" ]] || fail 'cshrc symlink was changed'
  assert_same_file "$foreign_snapshot" "$foreign"
  assert_no_shell_targets "$home"
  assert_output_contains "$output" 'non-regular csh startup file'

  fixture="$(new_fixture)"
  home="$fixture/home"
  foreign="$fixture/foreign-tcshrc"
  foreign_snapshot="$fixture/foreign-tcshrc.snapshot"
  printf '%s\n' 'foreign tcsh target' > "$foreign"
  cp "$foreign" "$foreign_snapshot"
  ln -s "$foreign" "$home/.tcshrc"
  output="$fixture/tcsh-symlink.log"
  run_setup_expect_failure "$home" __UNSET__ "$output"
  [[ -L "$home/.tcshrc" ]] || fail 'tcshrc symlink was changed'
  assert_same_file "$foreign_snapshot" "$foreign"
  assert_no_shell_targets "$home"
  assert_output_contains "$output" 'non-regular csh startup file'

  fixture="$(new_fixture)"
  home="$fixture/home"
  mkdir -p "$home/.cshrc"
  output="$fixture/directory.log"
  run_setup_expect_failure "$home" __UNSET__ "$output"
  [[ -d "$home/.cshrc" ]] || fail 'cshrc directory was changed'
  assert_no_shell_targets "$home"
  assert_output_contains "$output" 'non-regular csh startup file'
}

test_csh_startup_rejects_hard_links() {
  local fixture home mode alias_target snapshot output

  for mode in shared-rc external-alias; do
    fixture="$(new_fixture)"
    home="$fixture/home"
    printf '%s\n' '# shared user configuration' > "$home/.cshrc"
    snapshot="$fixture/cshrc.snapshot"
    cp "$home/.cshrc" "$snapshot"
    if [[ "$mode" == shared-rc ]]; then
      alias_target="$home/.tcshrc"
    else
      alias_target="$fixture/external-file"
    fi
    ln "$home/.cshrc" "$alias_target"
    output="$fixture/hard-link.log"
    run_setup_expect_failure "$home" __UNSET__ "$output"
    assert_output_contains "$output" 'hard-linked csh startup file'
    assert_same_file "$snapshot" "$home/.cshrc"
    assert_same_file "$snapshot" "$alias_target"
    [[ "$home/.cshrc" -ef "$alias_target" ]] || fail 'hard-link relationship was changed'
    assert_no_shell_targets "$home"
    run_setup_expect_failure "$home" __UNSET__ "$fixture/hard-link-force.log" --force
    assert_same_file "$snapshot" "$alias_target"
    assert_no_shell_targets "$home"
  done
}

test_help_and_dry_run() {
  local fixture home output csh_before tcsh_before csh_mode tcsh_mode

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
  assert_no_csh_startup_files "$home"
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

  csh_before="$fixture/cshrc.before-dry-run"
  tcsh_before="$fixture/tcshrc.before-dry-run"
  printf '%s' 'foreign csh dry-run body without a final newline' > "$home/.cshrc"
  printf '%s\n' 'foreign tcsh dry-run body' > "$home/.tcshrc"
  cp "$home/.cshrc" "$csh_before"
  cp "$home/.tcshrc" "$tcsh_before"
  chmod 640 "$home/.cshrc"
  chmod 600 "$home/.tcshrc"
  csh_mode="$(file_mode "$home/.cshrc")"
  tcsh_mode="$(file_mode "$home/.tcshrc")"
  output="$fixture/dry-run-existing-rc.log"
  run_setup "$home" __UNSET__ "$output" --dry-run
  assert_file_prefix "$home/.cshrc" "$csh_before"
  assert_same_file "$home/.tcshrc" "$tcsh_before"
  [[ "$(file_mode "$home/.cshrc")" == "$csh_mode" ]] || fail 'dry-run changed cshrc mode'
  [[ "$(file_mode "$home/.tcshrc")" == "$tcsh_mode" ]] || fail 'dry-run changed tcshrc mode'
  assert_not_contains "$home/.cshrc" 'dotfiles:csh-adapter'
  assert_not_contains "$home/.tcshrc" 'dotfiles:csh-adapter'
  assert_output_contains "$output" "Plan: target=$home/.cshrc action=csh-startup-append"
  assert_output_contains "$output" "Plan: target=$home/.tcshrc action=csh-startup-append"
}

test_apply_allowlist_and_runtimes() {
  local fixture home output custom_xdg secret_before profile_before foreign_fish secret_mode
  local csh_before tcsh_before csh_mode tcsh_mode

  fixture="$(new_fixture)"
  home="$fixture/home"
  mkdir -p "$home/.config/fish" "$home/.config/shell" "$home/.config/mise"
  profile_before="$fixture/profile.before"
  secret_before="$fixture/secrets.before"
  foreign_fish="$fixture/fish-config.before"
  printf '%s\n' 'foreign profile' > "$home/.profile"
  printf '%s\n' 'foreign secret' > "$home/.config/shell/secrets.env"
  printf '%s\n' 'foreign fish config' > "$home/.config/fish/config.fish"
  printf '%s' 'foreign csh body without a final newline' > "$home/.cshrc"
  printf '%s\n' 'foreign tcsh' > "$home/.tcshrc"
  printf '%s\n' 'foreign mise file' > "$home/.config/mise/foreign.toml"
  cp "$home/.profile" "$profile_before"
  cp "$home/.config/shell/secrets.env" "$secret_before"
  cp "$home/.config/fish/config.fish" "$foreign_fish"
  csh_before="$fixture/csh.before"
  tcsh_before="$fixture/tcsh.before"
  cp "$home/.cshrc" "$csh_before"
  cp "$home/.tcshrc" "$tcsh_before"
  chmod 640 "$home/.cshrc"
  chmod 600 "$home/.tcshrc"
  csh_mode="$(file_mode "$home/.cshrc")"
  tcsh_mode="$(file_mode "$home/.tcshrc")"
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
  assert_file_prefix "$home/.cshrc" "$csh_before"
  assert_file_prefix "$home/.tcshrc" "$tcsh_before"
  [[ "$(file_mode "$home/.cshrc")" == "$csh_mode" ]] || fail 'cshrc mode changed'
  [[ "$(file_mode "$home/.tcshrc")" == "$tcsh_mode" ]] || fail 'tcshrc mode changed'
  assert_csh_startup_block "$home/.cshrc"
  assert_csh_startup_block "$home/.tcshrc"
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
  local fixture home output custom_xdg bridge snapshot csh_snapshot bridge_mode

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
  csh_snapshot="$fixture/cshrc.snapshot"
  cp "$home/.cshrc" "$csh_snapshot"
  printf '%s\n' 'managed csh block removed by fixture' > "$home/.cshrc"
  run_setup_expect_failure "$home" "$custom_xdg" "$fixture/csh-verify-failure.log" --verify
  assert_exact_content "$home/.cshrc" 'managed csh block removed by fixture'
  cp "$csh_snapshot" "$home/.cshrc"

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
  test_csh_startup_presence_matrix
  test_csh_startup_preserves_later_settings
  test_csh_startup_invalid_state_is_fail_fast
  test_csh_startup_rejects_hard_links
  test_conflict_preflight
  test_verify_force_and_clean_rerun
  printf 'setup_shell tests passed\n'
}

main "$@"
