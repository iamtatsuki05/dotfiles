#!/usr/bin/env zsh

# Scripts under test resolve config paths as "${XDG_CONFIG_HOME:-$HOME/.config}".
# Tests isolate HOME but an inherited XDG_CONFIG_HOME still points at the
# developer's real ~/.config, so fixture runs of update_managed_versions.sh
# overwrote the real ~/.config/mise/config.toml. Drop the XDG base directory
# variables before any test runs so only the test's HOME is ever written.
unset XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

is_valid_executable_file() {
  local candidate="${1:-}"

  [[ "$candidate" == /* ]] || return 1
  [[ "$candidate" != *[[:cntrl:]]* ]] || return 1
  [[ -f "$candidate" && -x "$candidate" ]]
}

resolve_chezmoi() {
  local candidate mise_bin install_dir

  candidate="$(command -v chezmoi 2>/dev/null || true)"
  if is_valid_executable_file "$candidate"; then
    REPLY="$candidate"
    return 0
  fi

  if [[ -n "${HOME:-}" ]]; then
    candidate="$HOME/.local/bin/chezmoi"
    if is_valid_executable_file "$candidate"; then
      REPLY="$candidate"
      return 0
    fi
  fi

  mise_bin="$(command -v mise 2>/dev/null || true)"
  if is_valid_executable_file "$mise_bin"; then
    if install_dir="$("$mise_bin" where chezmoi@latest 2>/dev/null)"; then
      if [[ "$install_dir" == /* && "$install_dir" != *[[:cntrl:]]* ]]; then
        candidate="$install_dir/chezmoi"
        if is_valid_executable_file "$candidate"; then
          REPLY="$candidate"
          return 0
        fi
      fi
    fi
  fi

  return 127
}

file_mode() {
  local mode
  mode="$(stat -c '%a' "$1" 2>/dev/null)" || mode=""
  if [[ "$mode" =~ '^[0-7]{3,4}$' ]]; then
    print -r -- "$mode"
    return 0
  fi
  mode="$(stat -f '%Lp' "$1" 2>/dev/null)" || mode=""
  if [[ "$mode" =~ '^[0-7]{3,4}$' ]]; then
    print -r -- "$mode"
    return 0
  fi
  return 1
}

assert_file_mode_portability() {
  local root stat_dir stat_bin target stat_log mode output rc=0 regression_status=0

  make_temp_dir file-mode-portability
  root="$REPLY"
  stat_dir="$root/bin"
  stat_bin="$stat_dir/stat"
  target="$root/target"
  stat_log="$root/stat.log"
  mkdir -p "$stat_dir"
  print -r -- 'synthetic file-mode target' > "$target"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- 'printf "%s %s\n" "${1:-}" "${2:-}" >> "$FAKE_STAT_LOG"'
    print -r -- 'case "${FAKE_STAT_MODE:-}" in'
    print -r -- '  gnu)'
    print -r -- '    if [ "${1:-}" = "-f" ]; then printf "%s\n" "File: synthetic-target" "Blocks: $$" "600"; exit 0; fi'
    print -r -- '    if [ "${1:-}" = "-c" ]; then printf "%s\n" "600"; exit 0; fi'
    print -r -- '    ;;'
    print -r -- '  bsd)'
    print -r -- '    if [ "${1:-}" = "-c" ]; then exit 2; fi'
    print -r -- '    if [ "${1:-}" = "-f" ]; then printf "%s\n" "600"; exit 0; fi'
    print -r -- '    ;;'
    print -r -- '  invalid)'
    print -r -- '    printf "%s\n" "not-a-mode"'
    print -r -- '    exit 0'
    print -r -- '    ;;'
    print -r -- '  *) exit 64 ;;'
    print -r -- 'esac'
    print -r -- 'exit 64'
  } > "$stat_bin"
  chmod +x "$stat_bin"

  (
    for mode in gnu bsd; do
      : > "$stat_log"
      output="$(
        PATH="$stat_dir:/bin:/usr/bin:/usr/sbin:/sbin"
        FAKE_STAT_MODE="$mode"
        FAKE_STAT_LOG="$stat_log"
        export PATH FAKE_STAT_MODE FAKE_STAT_LOG
        rehash
        file_mode "$target"
      )" || { rc=$?; fail "file_mode $mode portability probe failed with status $rc"; }
      [[ "$output" == 600 ]] || fail "file_mode $mode must return a canonical mode"
      if [[ "$mode" == gnu ]]; then
        assert_file_content "$stat_log" '-c %a'
      else
        [[ "$(sed -n '1p' "$stat_log")" == '-c %a' ]] || fail 'BSD mode probe must try GNU stat first'
        [[ "$(sed -n '2p' "$stat_log")" == '-f %Lp' ]] || fail 'BSD mode probe must fall back to BSD stat'
      fi
    done

    : > "$stat_log"
    rc=0
    output="$(
      PATH="$stat_dir:/bin:/usr/bin:/usr/sbin:/sbin"
      FAKE_STAT_MODE=invalid
      FAKE_STAT_LOG="$stat_log"
      export PATH FAKE_STAT_MODE FAKE_STAT_LOG
      rehash
      file_mode "$target"
    )" || rc=$?
    (( rc != 0 )) || fail 'file_mode must fail when both stat formats are invalid'
    [[ "$(sed -n '1p' "$stat_log")" == '-c %a' ]] || fail 'invalid mode probe must try GNU stat first'
    [[ "$(sed -n '2p' "$stat_log")" == '-f %Lp' ]] || fail 'invalid mode probe must try BSD stat before failing'
  ) || regression_status=$?

  rm -rf "$root"
  return "$regression_status"
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_executable() {
  local file_path="$1"
  [[ -x "$file_path" ]] || fail "expected executable file: $file_path"
}

assert_not_exists() {
  local target_path="$1"
  [[ ! -e "$target_path" ]] || fail "expected path not to exist: $target_path"
}

assert_same_file() {
  local expected="$1"
  local actual="$2"

  assert_file "$expected"
  assert_file "$actual"
  cmp "$expected" "$actual" >/dev/null || fail "expected $actual to match $expected"
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

assert_file_content() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  [[ "$(cat "$file_path")" == "$expected" ]] || fail "expected $file_path to be: $expected"
}

assert_output_contains() {
  local output_file="$1"
  local expected="$2"

  if grep -Fq -- "$expected" "$output_file"; then
    return 0
  fi

  echo "FAIL: expected output to contain: $expected" >&2
  echo "--- output: $output_file ---" >&2
  sed -n '1,160p' "$output_file" >&2
  echo "--- end output ---" >&2
  exit 1
}

assert_contains_text() {
  local text="$1"
  local expected="$2"

  [[ "$text" == *"$expected"* ]] || fail "expected output to contain: $expected"
}

assert_not_contains_text() {
  local text="$1"
  local unexpected="$2"

  [[ "$text" != *"$unexpected"* ]] || fail "expected output not to contain: $unexpected"
}

make_temp_dir() {
  local prefix="${1:-dotfiles-test}"
  local candidate
  local attempts=0

  while (( attempts < 10 )); do
    candidate="${TMPDIR:-/tmp}/$prefix-$$-$RANDOM-$RANDOM"
    if mkdir "$candidate" 2>/dev/null; then
      REPLY="$candidate"
      return 0
    fi
    attempts=$((attempts + 1))
  done

  fail "failed to create temporary directory"
}

make_temp_file() {
  make_temp_dir "${1:-dotfiles-test}"
  rmdir "$REPLY"
  : > "$REPLY"
}
