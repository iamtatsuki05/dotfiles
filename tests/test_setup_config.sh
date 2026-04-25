#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SETUP_SCRIPT="$REPO_ROOT/scripts/setup_config.sh"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

create_fixture_repo() {
  local repo="$1"

  mkdir -p \
    "$repo/config/alacritty" \
    "$repo/config/codex" \
    "$repo/config/ghostty" \
    "$repo/config/mise" \
    "$repo/config/nix" \
    "$repo/config/shell"

  print -r -- 'window.opacity = 0.95' > "$repo/config/alacritty/alacritty.toml"
  print -r -- 'sandbox_mode = "workspace-write"' > "$repo/config/codex/config.toml.base"
  print -r -- 'theme = dark' > "$repo/config/ghostty/config"
  print -r -- 'repo = "__DOTFILES_REPO_ROOT__"' > "$repo/config/mise/config.toml"
  print -r -- 'experimental-features = nix-command flakes' > "$repo/config/nix/nix.conf"
  print -r -- 'export DEVIN_API_KEY=test-key' > "$repo/config/shell/secrets.env"
  cat > "$repo/config/shell/dotfiles-shell-common.tmpl" <<'EOF'
export DOTFILES_REPO_ROOT="${DOTFILES_REPO_ROOT:-__DOTFILES_REPO_ROOT__}"
if command -v mise >/dev/null 2>&1; then
  dotfiles_shell_name=bash
  eval "$(command mise activate "$dotfiles_shell_name")"
fi
if [ -r "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env" ]; then
  . "${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env"
fi
EOF
  cat > "$repo/config/shell/bashrc.tmpl" <<'EOF'
if [ -r "${XDG_CONFIG_HOME:-$HOME/.config}/shell/dotfiles-shell-common.sh" ]; then
  . "${XDG_CONFIG_HOME:-$HOME/.config}/shell/dotfiles-shell-common.sh"
fi
EOF
  cat > "$repo/config/shell/bash_profile.tmpl" <<'EOF'
if [ -r "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi
EOF
}

test_setup_config_renders_dynamic_bash_files() {
  local repo
  local home_dir
  local xdg_config_home
  repo="$(mktemp -d)"
  home_dir="$(mktemp -d)"
  xdg_config_home="$home_dir/.config"

  create_fixture_repo "$repo"

  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" "$TEST_ZSH_BIN" "$SETUP_SCRIPT" --repo-root "$repo" >/dev/null

  assert_file "$home_dir/.bashrc"
  assert_file "$home_dir/.bash_profile"
  assert_file "$xdg_config_home/shell/dotfiles-shell-common.sh"
  assert_contains "$home_dir/.bashrc" 'dotfiles-shell-common.sh'
  assert_contains "$xdg_config_home/shell/dotfiles-shell-common.sh" "$repo"
  assert_not_contains "$xdg_config_home/shell/dotfiles-shell-common.sh" "__DOTFILES_REPO_ROOT__"
  assert_contains "$xdg_config_home/shell/dotfiles-shell-common.sh" 'mise activate "$dotfiles_shell_name"'
  assert_contains "$xdg_config_home/shell/dotfiles-shell-common.sh" '.config}/shell/secrets.env'
  assert_contains "$home_dir/.bash_profile" '. "$HOME/.bashrc"'
  assert_file "$xdg_config_home/mise/config.toml"
  assert_contains "$xdg_config_home/mise/config.toml" "$repo"
  assert_not_contains "$xdg_config_home/mise/config.toml" "__DOTFILES_REPO_ROOT__"
  assert_file "$xdg_config_home/shell/secrets.env"

  bash -n "$home_dir/.bashrc"
  bash -n "$home_dir/.bash_profile"

  rm -rf "$repo" "$home_dir"
}

main() {
  test_setup_config_renders_dynamic_bash_files
  echo "setup config tests passed"
}

main "$@"
