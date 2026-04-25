#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"

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

assert_same_file() {
  local expected="$1"
  local actual="$2"

  assert_file "$expected"
  assert_file "$actual"
  cmp "$expected" "$actual" >/dev/null || fail "expected $actual to match $expected"
}

assert_same_file_or_home_fallback() {
  local expected="$1"
  local home_relative_path="$2"
  local actual="$3"

  assert_file "$actual"

  if [[ -f "$expected" ]] && cmp "$expected" "$actual" >/dev/null; then
    return
  fi

  assert_same_file "$HOME/$home_relative_path" "$actual"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

test_chezmoi_root_points_to_home() {
  assert_file "$REPO_ROOT/.chezmoiroot"
  [[ "$(cat "$REPO_ROOT/.chezmoiroot")" == "home" ]] || fail ".chezmoiroot should point to home"
}

test_copied_source_state_matches_current_sources() {
  assert_same_file_or_home_fallback "$REPO_ROOT/dotfiles/.zshrc" ".zshrc" "$REPO_ROOT/home/dot_zshrc"
  assert_same_file "$REPO_ROOT/dotfiles/.tmux.conf" "$REPO_ROOT/home/dot_tmux.conf"
  assert_same_file "$REPO_ROOT/config/alacritty/alacritty.toml" "$REPO_ROOT/home/private_dot_config/alacritty/alacritty.toml"
  assert_same_file "$REPO_ROOT/config/ghostty/config" "$REPO_ROOT/home/private_dot_config/ghostty/config"
  assert_same_file "$REPO_ROOT/config/nix/nix.conf" "$REPO_ROOT/home/private_dot_config/nix/nix.conf"
  assert_same_file "$REPO_ROOT/config/nvim/init.vim" "$REPO_ROOT/home/private_dot_config/nvim/init.vim"
  assert_same_file "$REPO_ROOT/config/shell/secrets.env.example" "$REPO_ROOT/home/private_dot_config/shell/create_private_secrets.env"
  assert_same_file "$REPO_ROOT/config/mise/config.toml" "$REPO_ROOT/home/.chezmoitemplates/mise-config.toml"
  assert_same_file "$REPO_ROOT/config/shell/bashrc.tmpl" "$REPO_ROOT/home/.chezmoitemplates/bashrc"
  assert_same_file "$REPO_ROOT/config/shell/bash_profile.tmpl" "$REPO_ROOT/home/.chezmoitemplates/bash_profile"
  assert_not_exists "$REPO_ROOT/home/dot_Brewfile.tmpl"
  assert_not_exists "$REPO_ROOT/home/.chezmoitemplates/Brewfile"
  assert_not_exists "$REPO_ROOT/home/.chezmoitemplates/Brewfile.cli"
}

test_templates_keep_repo_root_behavior() {
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" '__DOTFILES_REPO_ROOT__'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" 'DOTFILES_REPO_ROOT'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" '.chezmoi.sourceDir'
  assert_contains "$REPO_ROOT/home/dot_bashrc.tmpl" '__DOTFILES_REPO_ROOT__'
  assert_contains "$REPO_ROOT/home/dot_bashrc.tmpl" '.chezmoitemplates/bashrc'
  assert_contains "$REPO_ROOT/home/dot_bash_profile.tmpl" '.chezmoitemplates/bash_profile'
}

main() {
  test_chezmoi_root_points_to_home
  test_copied_source_state_matches_current_sources
  test_templates_keep_repo_root_behavior
  echo "chezmoi source state tests passed"
}

main "$@"
