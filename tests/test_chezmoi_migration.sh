#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly APPLY_SCRIPT="$REPO_ROOT/scripts/chezmoi_apply.sh"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_not_exists() {
  local target_path="$1"
  [[ ! -e "$target_path" ]] || fail "expected path not to exist: $target_path"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_file_content() {
  local file_path="$1"
  local expected="$2"

  [[ "$(cat "$file_path")" == "$expected" ]] || fail "expected $file_path to be: $expected"
}

create_fake_chezmoi() {
  local bin_dir="$1"
  local log_file="$2"

  mkdir -p "$bin_dir"
  {
    print -r -- "#!$TEST_ZSH_BIN"
    print -r -- 'set -euo pipefail'
    print -r -- "print -r -- \"DOTFILES_PROFILE=\${DOTFILES_PROFILE:-}\" >> ${(qqq)log_file}"
    print -r -- "print -r -- \"DOTFILES_REPO_ROOT=\${DOTFILES_REPO_ROOT:-}\" >> ${(qqq)log_file}"
    print -r -- "print -r -- \"\$*\" >> ${(qqq)log_file}"
  } > "$bin_dir/chezmoi"
  chmod +x "$bin_dir/chezmoi"
}

create_fake_mise() {
  local bin_dir="$1"
  local log_file="$2"
  local install_dir="$bin_dir/mise-install"

  mkdir -p "$bin_dir"
  mkdir -p "$install_dir"
  {
    print -r -- "#!$TEST_ZSH_BIN"
    print -r -- 'set -euo pipefail'
    print -r -- 'if [[ "${1:-}" == "where" ]]; then'
    print -r -- "  print -r -- ${(qqq)install_dir}"
    print -r -- '  exit 0'
    print -r -- 'fi'
    print -r -- "print -r -- \"MISE:\$*\" >> ${(qqq)log_file}"
  } > "$bin_dir/mise"
  {
    print -r -- "#!$TEST_ZSH_BIN"
    print -r -- 'set -euo pipefail'
    print -r -- "print -r -- \"DOTFILES_PROFILE=\${DOTFILES_PROFILE:-}\" >> ${(qqq)log_file}"
    print -r -- "print -r -- \"DOTFILES_REPO_ROOT=\${DOTFILES_REPO_ROOT:-}\" >> ${(qqq)log_file}"
    print -r -- "print -r -- \"CHEZMOI:\$*\" >> ${(qqq)log_file}"
  } > "$install_dir/chezmoi"
  chmod +x "$bin_dir/mise"
  chmod +x "$install_dir/chezmoi"
}

create_fake_home_chezmoi() {
  local home_dir="$1"
  local log_file="$2"

  mkdir -p "$home_dir/.local/bin"
  {
    print -r -- "#!$TEST_ZSH_BIN"
    print -r -- 'set -euo pipefail'
    print -r -- "print -r -- \"DOTFILES_PROFILE=\${DOTFILES_PROFILE:-}\" >> ${(qqq)log_file}"
    print -r -- "print -r -- \"DOTFILES_REPO_ROOT=\${DOTFILES_REPO_ROOT:-}\" >> ${(qqq)log_file}"
    print -r -- "print -r -- \"HOME_CHEZMOI:\$*\" >> ${(qqq)log_file}"
  } > "$home_dir/.local/bin/chezmoi"
  chmod +x "$home_dir/.local/bin/chezmoi"
}

create_chezmoi_source_repo() {
  local repo="$1"

  print -r -- "home" > "$repo/.chezmoiroot"
  mkdir -p "$repo/home"
}

test_chezmoi_apply_uses_repo_source_in_dry_run() {
  local repo
  local bin_dir
  local log_file
  local xdg_config_home
  local expected_profile
  repo="$(mktemp -d)"
  bin_dir="$repo/bin"
  log_file="$repo/chezmoi.log"
  xdg_config_home="$repo/xdg"
  create_chezmoi_source_repo "$repo"
  create_fake_chezmoi "$bin_dir" "$log_file"

  if [[ "$OSTYPE" == darwin* ]]; then
    expected_profile="full"
  else
    expected_profile="cli"
  fi

  XDG_CONFIG_HOME="$xdg_config_home" PATH="$bin_dir:$PATH" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" --repo-root "$repo" --dry-run >/dev/null

  assert_contains "$log_file" "-S $repo apply -n -v"
  assert_contains "$log_file" "DOTFILES_PROFILE=$expected_profile"

  rm -rf "$repo"
}

test_chezmoi_apply_can_mark_chezmoi_as_default_manager() {
  local repo
  local bin_dir
  local log_file
  local manager_file
  local profile_file
  repo="$(mktemp -d)"
  bin_dir="$repo/bin"
  log_file="$repo/chezmoi.log"
  manager_file="$repo/manager"
  profile_file="$repo/profile"
  create_chezmoi_source_repo "$repo"
  create_fake_chezmoi "$bin_dir" "$log_file"

  PATH="$bin_dir:$PATH" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" \
    --repo-root "$repo" \
    --manager-file "$manager_file" \
    --profile-file "$profile_file" \
    --cli-only \
    --mark-default >/dev/null

  assert_contains "$log_file" "-S $repo apply -v"
  assert_contains "$log_file" "DOTFILES_PROFILE=cli"
  assert_contains "$log_file" "DOTFILES_REPO_ROOT=$repo"
  assert_file_content "$manager_file" "chezmoi"
  assert_file_content "$profile_file" "cli"

  rm -rf "$repo"
}

test_chezmoi_dry_run_does_not_write_default_manager_marker() {
  local repo
  local bin_dir
  local log_file
  local manager_file
  local profile_file
  repo="$(mktemp -d)"
  bin_dir="$repo/bin"
  log_file="$repo/chezmoi.log"
  manager_file="$repo/manager"
  profile_file="$repo/profile"
  create_chezmoi_source_repo "$repo"
  create_fake_chezmoi "$bin_dir" "$log_file"

  PATH="$bin_dir:$PATH" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" \
    --repo-root "$repo" \
    --manager-file "$manager_file" \
    --profile-file "$profile_file" \
    --cli-only \
    --dry-run \
    --mark-default >/dev/null

  assert_contains "$log_file" "-S $repo apply -n -v"
  assert_not_exists "$manager_file"
  assert_not_exists "$profile_file"

  rm -rf "$repo"
}

test_chezmoi_apply_passes_profile_to_templates() {
  local repo
  local bin_dir
  local log_file
  repo="$(mktemp -d)"
  bin_dir="$repo/bin"
  log_file="$repo/chezmoi.log"
  create_chezmoi_source_repo "$repo"
  create_fake_chezmoi "$bin_dir" "$log_file"

  PATH="$bin_dir:$PATH" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" --repo-root "$repo" --cli-only --dry-run >/dev/null

  assert_contains "$log_file" "DOTFILES_PROFILE=cli"
  assert_contains "$log_file" "DOTFILES_REPO_ROOT=$repo"

  rm -rf "$repo"
}

test_chezmoi_apply_falls_back_to_mise_install_when_chezmoi_is_not_on_path() {
  local repo
  local bin_dir
  local log_file
  local home_dir
  repo="$(mktemp -d)"
  bin_dir="$repo/bin"
  log_file="$repo/mise.log"
  home_dir="$repo/home-dir"
  create_chezmoi_source_repo "$repo"
  mkdir -p "$home_dir"
  create_fake_mise "$bin_dir" "$log_file"

  HOME="$home_dir" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin:/opt/homebrew/bin" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" --repo-root "$repo" --cli-only --dry-run >/dev/null

  assert_contains "$log_file" "DOTFILES_PROFILE=cli"
  assert_contains "$log_file" "DOTFILES_REPO_ROOT=$repo"
  assert_contains "$log_file" "CHEZMOI:-S $repo apply -n -v"

  rm -rf "$repo"
}

test_chezmoi_apply_falls_back_to_home_local_bin_when_not_on_path() {
  local repo
  local home_dir
  local log_file
  repo="$(mktemp -d)"
  home_dir="$(mktemp -d)"
  log_file="$repo/chezmoi.log"
  create_chezmoi_source_repo "$repo"
  create_fake_home_chezmoi "$home_dir" "$log_file"

  HOME="$home_dir" PATH="/bin:/usr/bin:/usr/sbin:/sbin:/opt/homebrew/bin" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" --repo-root "$repo" --cli-only --dry-run >/dev/null

  assert_contains "$log_file" "DOTFILES_PROFILE=cli"
  assert_contains "$log_file" "DOTFILES_REPO_ROOT=$repo"
  assert_contains "$log_file" "HOME_CHEZMOI:-S $repo apply -n -v"

  rm -rf "$repo" "$home_dir"
}

main() {
  test_chezmoi_apply_uses_repo_source_in_dry_run
  test_chezmoi_apply_can_mark_chezmoi_as_default_manager
  test_chezmoi_dry_run_does_not_write_default_manager_marker
  test_chezmoi_apply_passes_profile_to_templates
  test_chezmoi_apply_falls_back_to_mise_install_when_chezmoi_is_not_on_path
  test_chezmoi_apply_falls_back_to_home_local_bin_when_not_on_path
  echo "chezmoi apply tests passed"
}

main "$@"
