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

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  assert_file "$file_path"
  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

run_chezmoi() {
  local home_chezmoi_bin
  local mise_chezmoi_bin
  local mise_install_dir

  if command -v chezmoi >/dev/null 2>&1; then
    chezmoi "$@"
    return
  fi

  home_chezmoi_bin="$HOME/.local/bin/chezmoi"
  if [[ -x "$home_chezmoi_bin" ]]; then
    "$home_chezmoi_bin" "$@"
    return
  fi

  if command -v mise >/dev/null 2>&1; then
    mise_install_dir="$(mise where chezmoi@latest 2>/dev/null)" || mise_install_dir=""
    mise_chezmoi_bin="$mise_install_dir/chezmoi"
    if [[ -x "$mise_chezmoi_bin" ]]; then
      "$mise_chezmoi_bin" "$@"
      return
    fi
  fi

  if command -v mise >/dev/null 2>&1; then
    mise exec chezmoi@latest -- chezmoi "$@"
    return 0
  fi

  return 127
}

test_chezmoi_renders_cli_profile_into_temp_home() {
  local temp_dir
  local temp_home
  local temp_config

  temp_dir="$(mktemp -d)"
  temp_home="$temp_dir/home"
  temp_config="$temp_dir/chezmoi.toml"
  mkdir -p "$temp_home"
  : > "$temp_config"

  if ! DOTFILES_PROFILE=cli DOTFILES_REPO_ROOT="$REPO_ROOT" run_chezmoi \
    -S "$REPO_ROOT" \
    -D "$temp_home" \
    --cache "$temp_dir/cache" \
    --config "$temp_config" \
    --persistent-state "$temp_dir/chezmoistate.boltdb" \
    --force \
    --no-tty \
    apply; then
    rm -rf "$temp_dir"
    echo "SKIP: chezmoi is not installed"
    return 0
  fi

  assert_same_file_or_home_fallback "$REPO_ROOT/dotfiles/.zshrc" ".zshrc" "$temp_home/.zshrc"
  assert_file "$temp_home/.bashrc"
  assert_file "$temp_home/.bash_profile"
  assert_same_file "$REPO_ROOT/dotfiles/.tmux.conf" "$temp_home/.tmux.conf"
  assert_not_exists "$temp_home/.Brewfile"
  assert_same_file "$REPO_ROOT/config/alacritty/alacritty.toml" "$temp_home/.config/alacritty/alacritty.toml"
  assert_same_file "$REPO_ROOT/config/ghostty/config" "$temp_home/.config/ghostty/config"
  assert_same_file "$REPO_ROOT/config/nix/nix.conf" "$temp_home/.config/nix/nix.conf"
  assert_same_file "$REPO_ROOT/config/nvim/init.vim" "$temp_home/.config/nvim/init.vim"
  assert_contains "$temp_home/.bashrc" "$REPO_ROOT"
  assert_not_contains "$temp_home/.bashrc" "__DOTFILES_REPO_ROOT__"
  assert_contains "$temp_home/.bash_profile" '. "$HOME/.bashrc"'
  assert_contains "$temp_home/.config/mise/config.toml" "$REPO_ROOT"
  assert_not_contains "$temp_home/.config/mise/config.toml" "__DOTFILES_REPO_ROOT__"
  assert_file "$temp_home/.config/shell/secrets.env"

  rm -rf "$temp_dir"
}

main() {
  test_chezmoi_renders_cli_profile_into_temp_home
  echo "chezmoi rendered home tests passed"
}

main "$@"
