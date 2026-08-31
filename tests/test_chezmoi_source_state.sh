#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"

source "$TEST_DIR/lib/assertions.sh"

test_chezmoi_root_points_to_home() {
  assert_file "$REPO_ROOT/.chezmoiroot"
  [[ "$(cat "$REPO_ROOT/.chezmoiroot")" == home ]] || fail ".chezmoiroot should point to home"
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

test_templates_keep_static_integrity() {
  local wrapper="$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl"
  local fish="$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl"
  local csh="$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl"

  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" '__DOTFILES_REPO_ROOT__'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" 'DOTFILES_REPO_ROOT'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" '.chezmoi.sourceDir'
  assert_contains "$REPO_ROOT/home/private_dot_config/mise/private_config.toml.tmpl" 'replace "__MISE_OPEN__" "{{"'
  assert_contains "$REPO_ROOT/home/.chezmoitemplates/mise-config.toml" '__MISE_OPEN__ version __MISE_CLOSE__'
  assert_contains "$REPO_ROOT/home/.chezmoitemplates/mise-config.toml" '[tasks.agent-skill-update]'
  assert_contains "$REPO_ROOT/home/.chezmoitemplates/mise-config.toml" '[tasks.chezmoi-status]'
  assert_contains "$REPO_ROOT/home/dot_bashrc.tmpl" '.chezmoitemplates/bashrc'
  assert_contains "$REPO_ROOT/home/dot_bash_profile.tmpl" '.chezmoitemplates/bash_profile'
  assert_contains "$wrapper" 'includeTemplate ".chezmoitemplates/dotfiles-shell-common.sh" $shellCommonContext'
  assert_not_contains "$wrapper" 'includeTemplate ".chezmoitemplates/dotfiles-shell-common.sh" .'
  assert_not_contains "$REPO_ROOT/home/.chezmoitemplates/dotfiles-shell-common.sh" '.chezmoi'
  assert_not_contains "$wrapper" 'replace "__DOTFILES_REPO_ROOT__"'
  assert_not_contains "$wrapper" '{{ include ".chezmoitemplates/dotfiles-shell-common.sh"'
  assert_contains "$REPO_ROOT/home/.chezmoidata.toml" '[shell]'
  assert_contains "$REPO_ROOT/home/.chezmoidata.toml" '[shell.aliases]'
  assert_contains "$REPO_ROOT/home/.chezmoidata.toml" '[shell.mise]'
  assert_not_contains "$REPO_ROOT/home/.chezmoitemplates/dotfiles-shell-common.sh" '__DOTFILES_REPO_ROOT__'
  assert_contains "$fish" 'status is-interactive'
  assert_contains "$csh" '$?prompt'
  assert_not_contains "$fish" 'secrets.env'
  assert_not_contains "$csh" 'secrets.env'
}

test_common_template_is_not_executed_raw() {
  local forbidden_variable="SHELL_COMMON_TEMPLATE_"'FILE='
  local forbidden_source='source "$REPO_ROOT/config/'"shell/"
  local obsolete_helper="run_source_"'bash_matrix'
  assert_not_contains "$REPO_ROOT/tests/test_chezmoi_source_state.sh" "$forbidden_variable"
  assert_not_contains "$REPO_ROOT/tests/test_chezmoi_source_state.sh" "$forbidden_source"
  assert_not_contains "$REPO_ROOT/tests/test_chezmoi_source_state.sh" "$obsolete_helper"
  assert_contains "$REPO_ROOT/tests/test_multi_shell_config.sh" '--selector source|render'
  assert_contains "$REPO_ROOT/tests/test_multi_shell_config.sh" 'run_source_matrix'
  assert_contains "$REPO_ROOT/tests/test_multi_shell_config.sh" 'target=chezmoi-source'
}

main() {
  test_chezmoi_root_points_to_home
  test_copied_source_state_matches_current_sources
  test_templates_keep_static_integrity
  test_common_template_is_not_executed_raw
  print -r -- 'chezmoi source state tests passed'
}

main "$@"
