#!/usr/bin/env bash

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$TEST_DIR/.." && pwd -P)"
FEATURES_LIB="$REPO_ROOT/scripts/lib/features.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-feature-flags.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT HUP INT TERM

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

[[ -f "$FEATURES_LIB" ]] || fail 'bootstrap feature loader is missing'
source "$FEATURES_LIB"

assert_valid() {
  local name="$1" input="$2" expected="$3" root="$TEST_ROOT/$1"
  local expected_status=0 actual_status=0

  mkdir -p "$root/home"
  printf '%s' "$input" > "$root/home/.chezmoidata.toml"
  DOTFILES_MACOS_FEATURES=invalid-inherited-value
  dotfiles_load_features "$root" > "$TEST_ROOT/out" 2> "$TEST_ROOT/err" || fail "$name was rejected"
  [[ ! -s "$TEST_ROOT/out" && ! -s "$TEST_ROOT/err" ]] || fail "$name emitted unexpected output"
  [[ "$DOTFILES_MACOS_FEATURES" == "$expected" ]] || fail "$name resolved to the wrong feature value"
  [[ "$expected" == true ]] || expected_status=1
  dotfiles_macos_features_enabled || actual_status=$?
  [[ "$actual_status" == "$expected_status" ]] || fail "$name predicate status was incorrect"
}

assert_invalid() {
  local name="$1" input="$2" root="$TEST_ROOT/$1" status=0

  mkdir -p "$root/home"
  printf '%s' "$input" > "$root/home/.chezmoidata.toml"
  DOTFILES_MACOS_FEATURES=true
  dotfiles_load_features "$root" > "$TEST_ROOT/out" 2> "$TEST_ROOT/err" || status=$?
  [[ "$status" == 2 ]] || fail "$name did not fail with a configuration error"
  [[ -z "${DOTFILES_MACOS_FEATURES+x}" ]] || fail "$name retained a stale feature value"
  [[ ! -s "$TEST_ROOT/out" && -s "$TEST_ROOT/err" ]] || fail "$name did not emit a bounded diagnostic"
  if grep -Fq 'secret-sentinel' "$TEST_ROOT/err"; then
    fail "$name leaked its invalid value"
  fi
}

assert_valid enabled $'[features]\nmacos = true\n' true
assert_valid disabled $'[features]\nmacos = false\n' false
assert_valid 'root with spaces' $'  [features] # flags\n\tmacos = false # comment\n' false
assert_valid crlf $'[features]\r\nmacos = true\r\n' true
assert_valid later-section $'[shell]\neditor = "nvim"\n[features]\nmacos = false\n[shell.xdg]\nconfig = ".config"\n' false
assert_valid comments $'# [features]\n# macos = false\n[features]\nmacos = true\n' true

assert_invalid missing $'[shell]\neditor = "nvim"\n'
assert_invalid empty $'[features]\n'
assert_invalid string $'[features]\nmacos = "false"\n'
assert_invalid number $'[features]\nmacos = 0\n'
assert_invalid extra $'[features]\nmacos = true\nunknown = false\n'
assert_invalid duplicate-key $'[features]\nmacos = true\nmacos = false\n'
assert_invalid duplicate-table $'[features]\nmacos = true\n[features]\nmacos = false\n'
assert_invalid nested-table $'[features]\nmacos = true\n[features.extra]\nvalue = true\n'
assert_invalid dotted-root $'features.macos = false\n[features]\nmacos = true\n'
assert_invalid quoted-duplicate $'["features"]\nmacos = false\n[features]\nmacos = true\n'
assert_invalid array-table $'[[features]]\nmacos = false\n[features]\nmacos = true\n'
assert_invalid trailing-value $'[features]\nmacos = true false\n'
assert_invalid secret-value $'[features]\nmacos = "secret-sentinel"\n'

status=0
dotfiles_load_features "$TEST_ROOT/not-found" > "$TEST_ROOT/out" 2> "$TEST_ROOT/err" || status=$?
[[ "$status" == 2 && -z "${DOTFILES_MACOS_FEATURES+x}" ]] || fail 'missing file did not fail closed'
status=0
dotfiles_macos_features_enabled > "$TEST_ROOT/out" 2> "$TEST_ROOT/err" || status=$?
[[ "$status" == 2 ]] || fail 'unloaded features were treated as disabled'

printf 'feature flag loader tests passed\n'
