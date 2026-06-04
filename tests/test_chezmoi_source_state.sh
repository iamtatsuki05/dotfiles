#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"

source "$TEST_DIR/lib/assertions.sh"

test_chezmoi_root_points_to_home() {
  assert_file "$REPO_ROOT/.chezmoiroot"
  [[ "$(cat "$REPO_ROOT/.chezmoiroot")" == "home" ]] || fail ".chezmoiroot should point to home"
}

test_copied_source_state_matches_current_sources() {
  assert_same_file "$REPO_ROOT/dotfiles/.tmux.conf" "$REPO_ROOT/home/dot_tmux.conf"
  assert_same_file "$REPO_ROOT/config/alacritty/alacritty.toml" "$REPO_ROOT/home/private_dot_config/alacritty/alacritty.toml"
  assert_same_file "$REPO_ROOT/config/ghostty/config" "$REPO_ROOT/home/private_dot_config/ghostty/config"
  assert_same_file "$REPO_ROOT/config/nix/nix.conf" "$REPO_ROOT/home/private_dot_config/nix/nix.conf"
  assert_same_file "$REPO_ROOT/config/zellij/config.kdl" "$REPO_ROOT/home/private_dot_config/zellij/config.kdl"
  assert_same_file "$REPO_ROOT/config/shell/secrets.env.example" "$REPO_ROOT/home/private_dot_config/shell/create_private_secrets.env"
  assert_same_file "$REPO_ROOT/config/shell/bashrc.tmpl" "$REPO_ROOT/home/.chezmoitemplates/bashrc"
  assert_same_file "$REPO_ROOT/config/shell/bash_profile.tmpl" "$REPO_ROOT/home/.chezmoitemplates/bash_profile"
  assert_same_file "$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl" "$REPO_ROOT/home/.chezmoitemplates/dotfiles-shell-common.sh"
  assert_not_exists "$REPO_ROOT/home/dot_Brewfile.tmpl"
  assert_not_exists "$REPO_ROOT/home/dot_zshrc"
  assert_not_exists "$REPO_ROOT/home/private_dot_config/nvim/init.vim"
  assert_not_exists "$REPO_ROOT/home/.chezmoitemplates/Brewfile"
  assert_not_exists "$REPO_ROOT/home/.chezmoitemplates/Brewfile.cli"
  assert_contains "$REPO_ROOT/config/zellij/config.kdl" "session_serialization true"
  assert_contains "$REPO_ROOT/config/zellij/config.kdl" "serialize_pane_viewport true"
  assert_contains "$REPO_ROOT/config/zellij/config.kdl" "serialization_interval 10"
}

test_templates_keep_repo_root_behavior() {
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" '__DOTFILES_REPO_ROOT__'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" 'DOTFILES_REPO_ROOT'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" '.chezmoi.sourceDir'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" 'replace "__MISE_OPEN__" "{{"'
  assert_contains "$REPO_ROOT/home/.chezmoitemplates/mise-config.toml" '__MISE_OPEN__ version __MISE_CLOSE__'
  assert_contains "$REPO_ROOT/home/.chezmoitemplates/mise-config.toml" '[tasks.agent-skill-update]'
  assert_contains "$REPO_ROOT/home/dot_bashrc.tmpl" '.chezmoitemplates/bashrc'
  assert_contains "$REPO_ROOT/home/dot_bash_profile.tmpl" '.chezmoitemplates/bash_profile'
  assert_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" '__DOTFILES_REPO_ROOT__'
  assert_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" '.chezmoitemplates/dotfiles-shell-common.sh'
}

test_shell_common_loads_in_zsh_when_git_helper_aliases_exist() {
  local output

  output="$(
    SHELL_COMMON_TEMPLATE_FILE="$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl" \
      zsh -fc '
        alias gt="git tag"
        alias gr="git remote"
        alias gs="git status"
        . "$SHELL_COMMON_TEMPLATE_FILE"
        whence -w gt
        whence -w gr
        whence -w gs
      '
  )"

  assert_contains_text "$output" "gt: function"
  assert_contains_text "$output" "gr: function"
  assert_contains_text "$output" "gs: function"
}

main() {
  test_chezmoi_root_points_to_home
  test_copied_source_state_matches_current_sources
  test_templates_keep_repo_root_behavior
  test_shell_common_loads_in_zsh_when_git_helper_aliases_exist
  echo "chezmoi source state tests passed"
}

main "$@"
