#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

source "$TEST_DIR/lib/assertions.sh"

is_test_macos() {
  [[ "$OSTYPE" == darwin* ]]
}

matrix_os_name() {
  if is_test_macos; then
    print -r -- macos
  else
    print -r -- linux
  fi
}

emit_matrix_result() {
  local line="$1"

  print -r -- "$line"
  if [[ -n "${MATRIX_RESULT_LOG_DIR:-}" ]]; then
    mkdir -p "$MATRIX_RESULT_LOG_DIR"
    print -r -- "$line" >> "$MATRIX_RESULT_LOG_DIR/matrix-results.log"
  fi
}

emit_required_bash_skip() {
  local target="$1"
  local expected_major="$2"

  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=${target}|status=SKIP|requirement=required|reason=bash${expected_major}-unavailable"
}

emit_not_applicable_bash_skip() {
  local target="$1"
  local expected_major="$2"

  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash${expected_major}|target=${target}|status=SKIP|requirement=not-applicable|reason=macos-only"
}

bash_major_version() {
  local bash_bin="$1"

  "$bash_bin" -c 'printf "%s\n" "${BASH_VERSINFO[0]}"'
}

select_bash_for_major() {
  local expected_major="$1"
  local candidate
  local actual_major
  local -a candidates=()

  if [[ "$expected_major" == 3 ]]; then
    if [[ -n "${BASH32_BIN:-}" ]]; then
      candidates=("$BASH32_BIN")
    elif is_test_macos; then
      candidates=(/bin/bash)
    fi
  else
    if [[ -n "${BASH5_BIN:-}" ]]; then
      candidates=("$BASH5_BIN")
    else
      candidates=(/run/current-system/sw/bin/bash /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash)
    fi
  fi

  for candidate in "${candidates[@]}"; do
    [[ "$candidate" == /* && -x "$candidate" ]] || continue
    actual_major="$(bash_major_version "$candidate" 2>/dev/null)" || continue
    if [[ "$actual_major" == "$expected_major" ]]; then
      REPLY="$candidate"
      return 0
    fi
  done

  return 1
}

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
  assert_contains "$REPO_ROOT/home/.chezmoitemplates/mise-config.toml" '[tasks.chezmoi-status]'
  assert_contains "$REPO_ROOT/home/dot_bashrc.tmpl" '.chezmoitemplates/bashrc'
  assert_contains "$REPO_ROOT/home/dot_bash_profile.tmpl" '.chezmoitemplates/bash_profile'
  assert_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" '__DOTFILES_REPO_ROOT__'
  assert_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" '.chezmoitemplates/dotfiles-shell-common.sh'
}

test_shell_common_loads_in_zsh_when_git_helper_aliases_exist() {
  local output

  output="$(
    SHELL_COMMON_TEMPLATE_FILE="$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl" \
      "$TEST_ZSH_BIN" -fc '
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

test_shell_common_exposes_claude_account_command() {
  local output

  output="$(
    SHELL_COMMON_TEMPLATE_FILE="$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl" \
      "$TEST_ZSH_BIN" -fc '
        . "$SHELL_COMMON_TEMPLATE_FILE"
        whence -w claude-account
        functions claude-account
      '
  )"

  assert_contains_text "$output" "claude-account: function"
  assert_contains_text "$output" "scripts/claude_account.sh"
}

test_shell_common_sources_with_stable_bash_and_zsh_fixtures() {
  local bash_bin="${1:-}"
  local expected_major="${2:-}"
  local emit_zsh_result="${3:-1}"
  local fixture
  local home_dir
  local local_bin
  local nix_bin
  local sentinel_one
  local sentinel_two
  local bash_output
  local zsh_output
  local bash_version
  local zsh_version
  local bash_major
  local matrix_os="linux"

  make_temp_dir
  fixture="$REPLY"
  home_dir="$fixture/home"
  local_bin="$home_dir/.local/bin"
  nix_bin="$home_dir/.nix-profile/bin"
  sentinel_one="$fixture/sentinel-one"
  sentinel_two="$fixture/sentinel-two"
  bash_output="$fixture/bash-output.log"
  zsh_output="$fixture/zsh-output.log"
  [[ -n "$bash_bin" ]] || fail "source-state fixture requires an explicit Bash binary"
  [[ -n "$expected_major" ]] || fail "source-state fixture requires an expected Bash major"
  bash_version="$("$bash_bin" --version | head -1)"
  zsh_version="$("$TEST_ZSH_BIN" --version | head -1)"
  mkdir -p "$local_bin" "$nix_bin" "$sentinel_one" "$sentinel_two"

  HOME="$home_dir" USER=dotfiles-test XDG_CONFIG_HOME="$fixture/config" \
    DOTFILES_REPO_ROOT="$fixture/repo" \
    PATH="$sentinel_one:$sentinel_two:/bin:/usr/bin" \
    "$bash_bin" --noprofile --norc -c '
      . "$1"
      . "$1"
      printf "shell=%s\n" "$dotfiles_shell_name"
      printf "root=%s\n" "$DOTFILES_REPO_ROOT"
      printf "path=%s\n" "$PATH"
      printf "local_count=%s\n" "$(printf "%s\n" "$PATH" | tr ":" "\n" | awk -v target="$HOME/.local/bin" "\$0 == target { count++ } END { print count + 0 }")"
      printf "nix_count=%s\n" "$(printf "%s\n" "$PATH" | tr ":" "\n" | awk -v target="$HOME/.nix-profile/bin" "\$0 == target { count++ } END { print count + 0 }")"
      printf "functions=%s,%s,%s\n" "$(type -t gt)" "$(type -t gr)" "$(type -t gs)"
    ' _ "$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl" > "$bash_output"

  HOME="$home_dir" USER=dotfiles-test XDG_CONFIG_HOME="$fixture/config" \
    DOTFILES_REPO_ROOT="$fixture/repo" \
    PATH="$sentinel_one:$sentinel_two:/bin:/usr/bin" \
    "$TEST_ZSH_BIN" -f -c '
      source "$1"
      source "$1"
      print -r -- "shell=$dotfiles_shell_name"
      print -r -- "root=$DOTFILES_REPO_ROOT"
      print -r -- "path=$PATH"
      print -r -- "local_count=$(print -r -- "$PATH" | tr ":" "\n" | awk -v target="$HOME/.local/bin" "\$0 == target { count++ } END { print count + 0 }")"
      print -r -- "nix_count=$(print -r -- "$PATH" | tr ":" "\n" | awk -v target="$HOME/.nix-profile/bin" "\$0 == target { count++ } END { print count + 0 }")"
      gt_type="$(whence -w gt | awk "{print \$2}")"
      gr_type="$(whence -w gr | awk "{print \$2}")"
      gs_type="$(whence -w gs | awk "{print \$2}")"
      print -r -- "functions=$gt_type,$gr_type,$gs_type"
    ' _ "$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl" > "$zsh_output"

  print -r -- "Bash: $bash_version"
  print -r -- "zsh: $zsh_version"
  bash_major="$(bash_major_version "$bash_bin")"
  [[ "$bash_major" == "$expected_major" ]] \
    || fail "source-state fixture ran Bash $bash_major, expected Bash $expected_major ($bash_version)"
  case "$bash_major" in
    3|5) ;;
    *) fail "source-state fixture must run Bash 3.x or 5.x: $bash_version" ;;
  esac
  assert_output_contains "$bash_output" "shell=bash"
  assert_output_contains "$bash_output" "root=$fixture/repo"
  assert_output_contains "$bash_output" "local_count=1"
  assert_output_contains "$bash_output" "nix_count=1"
  assert_output_contains "$bash_output" "functions=function,function,function"
  assert_output_contains "$zsh_output" "shell=zsh"
  assert_output_contains "$zsh_output" "root=$fixture/repo"
  assert_output_contains "$zsh_output" "local_count=1"
  assert_output_contains "$zsh_output" "nix_count=1"
  assert_output_contains "$zsh_output" "functions=function,function,function"
  case "$(grep '^path=' "$bash_output" | sed 's/^path=//')" in
    *"$nix_bin"*"$local_bin"*"$sentinel_one"*"$sentinel_two"*) ;;
    *) fail "Bash common PATH order was not preserved";;
  esac
  case "$(grep '^path=' "$zsh_output" | sed 's/^path=//')" in
    *"$nix_bin"*"$local_bin"*"$sentinel_one"*"$sentinel_two"*) ;;
    *) fail "zsh common PATH order was not preserved";;
  esac

  [[ "$OSTYPE" == darwin* ]] && matrix_os="macos"
  emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${bash_major}|target=chezmoi-source|status=PASS|requirement=required|reason=$bash_bin"
  if (( emit_zsh_result )); then
    emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=zsh|target=chezmoi-source|status=PASS|requirement=required|reason=$TEST_ZSH_BIN"
  fi

  rm -rf "$fixture"
}

run_source_bash_matrix() {
  local expected_major
  local bash_bin
  local matrix_status=0
  local zsh_result_emitted=0

  if [[ ! -x "$TEST_ZSH_BIN" || ! -f "$TEST_ZSH_BIN" ]]; then
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=zsh|target=chezmoi-source|status=SKIP|requirement=required|reason=zsh-unavailable"
    return 1
  fi

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      bash_bin="$REPLY"
      if (( zsh_result_emitted == 0 )); then
        test_shell_common_sources_with_stable_bash_and_zsh_fixtures "$bash_bin" "$expected_major" 1 || matrix_status=1
        zsh_result_emitted=1
      else
        test_shell_common_sources_with_stable_bash_and_zsh_fixtures "$bash_bin" "$expected_major" 0 || matrix_status=1
      fi
    elif [[ "$expected_major" == 3 && ! is_test_macos ]]; then
      emit_not_applicable_bash_skip 'chezmoi-source' "$expected_major"
    else
      emit_required_bash_skip 'chezmoi-source' "$expected_major"
      matrix_status=1
    fi
  done

  (( matrix_status == 0 )) || fail "source-state Bash matrix failed"
}

main() {
  test_chezmoi_root_points_to_home
  test_copied_source_state_matches_current_sources
  test_templates_keep_repo_root_behavior
  if [[ -x "$TEST_ZSH_BIN" && -f "$TEST_ZSH_BIN" ]]; then
    test_shell_common_loads_in_zsh_when_git_helper_aliases_exist
    test_shell_common_exposes_claude_account_command
  fi
  run_source_bash_matrix
  echo "chezmoi source state tests passed"
}

main "$@"
