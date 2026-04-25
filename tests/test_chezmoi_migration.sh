#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly MIGRATION_SCRIPT="$REPO_ROOT/scripts/migrate_to_chezmoi.sh"
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

assert_dir() {
  local dir_path="$1"
  [[ -d "$dir_path" ]] || fail "expected directory: $dir_path"
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

create_fixture_repo() {
  local repo="$1"

  mkdir -p \
    "$repo/dotfiles" \
    "$repo/config/alacritty" \
    "$repo/config/ghostty" \
    "$repo/config/mise" \
    "$repo/config/nix" \
    "$repo/config/nvim" \
    "$repo/config/shell"

  print -r -- 'export TEST_ZSHRC=1' > "$repo/dotfiles/.zshrc"
  print -r -- 'set -g mouse on' > "$repo/dotfiles/.tmux.conf"
  print -r -- 'window.opacity = 0.95' > "$repo/config/alacritty/alacritty.toml"
  print -r -- 'theme = dark' > "$repo/config/ghostty/config"
  print -r -- 'repo = "__DOTFILES_REPO_ROOT__"' > "$repo/config/mise/config.toml"
  print -r -- 'experimental-features = nix-command flakes' > "$repo/config/nix/nix.conf"
  print -r -- 'set number' > "$repo/config/nvim/init.vim"
  print -r -- 'export API_KEY=""' > "$repo/config/shell/secrets.env.example"
}

create_fixture_home() {
  local home_dir="$1"

  mkdir -p "$home_dir"
  print -r -- 'export FALLBACK_ZSHRC=1' > "$home_dir/.zshrc"
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

test_apply_generates_chezmoi_source_state() {
  local repo
  repo="$(mktemp -d)"
  create_fixture_repo "$repo"

  "$TEST_ZSH_BIN" "$MIGRATION_SCRIPT" --repo-root "$repo" --apply >/dev/null

  assert_file "$repo/.chezmoiroot"
  [[ "$(cat "$repo/.chezmoiroot")" == "home" ]] || fail ".chezmoiroot should point to home"

  assert_dir "$repo/home"
  cmp "$repo/dotfiles/.zshrc" "$repo/home/dot_zshrc" >/dev/null
  cmp "$repo/dotfiles/.tmux.conf" "$repo/home/dot_tmux.conf" >/dev/null
  cmp "$repo/config/alacritty/alacritty.toml" "$repo/home/private_dot_config/alacritty/alacritty.toml" >/dev/null
  cmp "$repo/config/ghostty/config" "$repo/home/private_dot_config/ghostty/config" >/dev/null
  cmp "$repo/config/nix/nix.conf" "$repo/home/private_dot_config/nix/nix.conf" >/dev/null
  cmp "$repo/config/nvim/init.vim" "$repo/home/private_dot_config/nvim/init.vim" >/dev/null
  cmp "$repo/config/shell/secrets.env.example" "$repo/home/private_dot_config/shell/create_private_secrets.env" >/dev/null
  cmp "$repo/config/mise/config.toml" "$repo/home/.chezmoitemplates/mise-config.toml" >/dev/null

  assert_not_exists "$repo/home/dot_Brewfile.tmpl"
  assert_not_exists "$repo/home/.chezmoitemplates/Brewfile"
  assert_not_exists "$repo/home/.chezmoitemplates/Brewfile.cli"
  assert_contains "$repo/home/private_dot_config/mise/private_config.toml.tmpl" '__DOTFILES_REPO_ROOT__'
  assert_contains "$repo/home/private_dot_config/mise/private_config.toml.tmpl" 'DOTFILES_REPO_ROOT'
  assert_contains "$repo/home/private_dot_config/mise/private_config.toml.tmpl" '.chezmoi.sourceDir'
  rm -rf "$repo"
}

test_dry_run_does_not_write_source_state() {
  local repo
  local output
  repo="$(mktemp -d)"
  output="$repo/dry-run.log"
  create_fixture_repo "$repo"

  "$TEST_ZSH_BIN" "$MIGRATION_SCRIPT" --repo-root "$repo" --dry-run > "$output"

  assert_not_exists "$repo/.chezmoiroot"
  assert_not_exists "$repo/home"
  assert_contains "$output" "DRY-RUN"
  assert_contains "$output" ".chezmoiroot"
  assert_contains "$output" "home/dot_zshrc"

  rm -rf "$repo"
}

test_apply_uses_home_zshrc_when_repo_zshrc_is_missing() {
  local repo
  local home_dir
  repo="$(mktemp -d)"
  home_dir="$(mktemp -d)"
  create_fixture_repo "$repo"
  create_fixture_home "$home_dir"
  rm "$repo/dotfiles/.zshrc"

  HOME="$home_dir" "$TEST_ZSH_BIN" "$MIGRATION_SCRIPT" --repo-root "$repo" --apply >/dev/null

  cmp "$home_dir/.zshrc" "$repo/home/dot_zshrc" >/dev/null

  rm -rf "$repo" "$home_dir"
}

test_apply_can_prefer_home_zshrc_even_when_repo_zshrc_exists() {
  local repo
  local home_dir
  repo="$(mktemp -d)"
  home_dir="$(mktemp -d)"
  create_fixture_repo "$repo"
  create_fixture_home "$home_dir"

  HOME="$home_dir" "$TEST_ZSH_BIN" "$MIGRATION_SCRIPT" --repo-root "$repo" --apply --prefer-home-zshrc >/dev/null

  cmp "$home_dir/.zshrc" "$repo/home/dot_zshrc" >/dev/null

  rm -rf "$repo" "$home_dir"
}

test_chezmoi_apply_uses_repo_source_in_dry_run() {
  local repo
  local bin_dir
  local log_file
  repo="$(mktemp -d)"
  bin_dir="$repo/bin"
  log_file="$repo/chezmoi.log"
  print -r -- "home" > "$repo/.chezmoiroot"
  mkdir -p "$repo/home"
  create_fake_chezmoi "$bin_dir" "$log_file"

  PATH="$bin_dir:$PATH" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" --repo-root "$repo" --dry-run >/dev/null

  assert_contains "$log_file" "-S $repo apply -n -v"

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
  print -r -- "home" > "$repo/.chezmoiroot"
  mkdir -p "$repo/home"
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

test_chezmoi_apply_passes_profile_to_templates() {
  local repo
  local bin_dir
  local log_file
  repo="$(mktemp -d)"
  bin_dir="$repo/bin"
  log_file="$repo/chezmoi.log"
  print -r -- "home" > "$repo/.chezmoiroot"
  mkdir -p "$repo/home"
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
  print -r -- "home" > "$repo/.chezmoiroot"
  mkdir -p "$repo/home"
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
  print -r -- "home" > "$repo/.chezmoiroot"
  mkdir -p "$repo/home"
  create_fake_home_chezmoi "$home_dir" "$log_file"

  HOME="$home_dir" PATH="/bin:/usr/bin:/usr/sbin:/sbin:/opt/homebrew/bin" "$TEST_ZSH_BIN" "$APPLY_SCRIPT" --repo-root "$repo" --cli-only --dry-run >/dev/null

  assert_contains "$log_file" "DOTFILES_PROFILE=cli"
  assert_contains "$log_file" "DOTFILES_REPO_ROOT=$repo"
  assert_contains "$log_file" "HOME_CHEZMOI:-S $repo apply -n -v"

  rm -rf "$repo" "$home_dir"
}

main() {
  test_apply_generates_chezmoi_source_state
  test_dry_run_does_not_write_source_state
  test_apply_uses_home_zshrc_when_repo_zshrc_is_missing
  test_apply_can_prefer_home_zshrc_even_when_repo_zshrc_exists
  test_chezmoi_apply_uses_repo_source_in_dry_run
  test_chezmoi_apply_can_mark_chezmoi_as_default_manager
  test_chezmoi_apply_passes_profile_to_templates
  test_chezmoi_apply_falls_back_to_mise_install_when_chezmoi_is_not_on_path
  test_chezmoi_apply_falls_back_to_home_local_bin_when_not_on_path
  echo "chezmoi migration tests passed"
}

main "$@"
