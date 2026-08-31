#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"

source "$TEST_DIR/lib/assertions.sh"

is_test_macos() {
  [[ "$OSTYPE" == darwin* ]]
}

matrix_os_name() {
  if is_test_macos; then
    echo macos
  else
    echo linux
  fi
}

emit_matrix_result() {
  local line="$1"

  echo "$line"
  if [[ -n "${MATRIX_RESULT_LOG_DIR:-}" ]]; then
    mkdir -p "$MATRIX_RESULT_LOG_DIR"
    echo "$line" >> "$MATRIX_RESULT_LOG_DIR/matrix-results.log"
  fi
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

run_chezmoi() {
  local chezmoi_bin

  resolve_chezmoi || return
  chezmoi_bin="$REPLY"
  "$chezmoi_bin" "$@"
}

test_chezmoi_renders_cli_profile_into_temp_home() {
  local temp_dir
  local temp_home
  local temp_config
  local bash_output
  local apply_status=0
  local matrix_os="$(matrix_os_name)"

  temp_dir="$(mktemp -d)"
  temp_home="$temp_dir/home"
  temp_config="$temp_dir/chezmoi.toml"
  bash_output="$temp_dir/bash-output"
  mkdir -p "$temp_home"
  : > "$temp_config"
  assert_file_mode_portability

  if ! resolve_chezmoi >/dev/null 2>&1; then
    rm -rf "$temp_dir"
    emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=chezmoi|target=rendered-home|status=SKIP|requirement=required|reason=chezmoi-unavailable"
    return 1
  fi
  DOTFILES_PROFILE=cli DOTFILES_REPO_ROOT="$REPO_ROOT" run_chezmoi \
    -S "$REPO_ROOT" \
    -D "$temp_home" \
    --cache "$temp_dir/cache" \
    --config "$temp_config" \
    --persistent-state "$temp_dir/chezmoistate.boltdb" \
    --force \
    --no-tty \
    apply || apply_status=$?
  if (( apply_status != 0 )); then
    emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=chezmoi|target=rendered-home|status=FAIL|requirement=required|reason=apply-failed"
    rm -rf "$temp_dir"
    return 1
  fi

  assert_file "$temp_home/.bashrc"
  assert_file "$temp_home/.bash_profile"
  assert_file "$temp_home/.config/shell/dotfiles-shell-common.sh"
  assert_file "$temp_home/.config/fish/conf.d/zz-dotfiles.fish"
  assert_file "$temp_home/.config/shell/dotfiles-shell-common.csh"
  assert_same_file "$REPO_ROOT/dotfiles/.tmux.conf" "$temp_home/.tmux.conf"
  assert_not_exists "$temp_home/.Brewfile"
  assert_not_exists "$temp_home/.zshrc"
  assert_same_file "$REPO_ROOT/config/alacritty/alacritty.toml" "$temp_home/.config/alacritty/alacritty.toml"
  assert_same_file "$REPO_ROOT/config/ghostty/config" "$temp_home/.config/ghostty/config"
  assert_same_file "$REPO_ROOT/config/nix/nix.conf" "$temp_home/.config/nix/nix.conf"
  assert_same_file "$REPO_ROOT/config/zellij/config.kdl" "$temp_home/.config/zellij/config.kdl"
  assert_not_exists "$temp_home/.config/nvim/init.vim"
  assert_contains "$temp_home/.bashrc" 'dotfiles-shell-common.sh'
  assert_contains "$temp_home/.config/shell/dotfiles-shell-common.sh" "$REPO_ROOT"
  assert_not_contains "$temp_home/.config/shell/dotfiles-shell-common.sh" "__DOTFILES_REPO_ROOT__"
  assert_contains "$temp_home/.config/shell/dotfiles-shell-common.sh" '[ "$dotfiles_shell_name" = "bash" ]'
  assert_contains "$temp_home/.config/shell/dotfiles-shell-common.sh" 'fgcc()'
  assert_contains "$temp_home/.config/shell/dotfiles-shell-common.sh" 'fgcc_p()'
  assert_contains "$temp_home/.config/shell/dotfiles-shell-common.sh" 'claude-account()'
  assert_contains "$temp_home/.config/shell/dotfiles-shell-common.sh" 'scripts/claude_account.sh'
  assert_contains "$temp_home/.bash_profile" '. "$HOME/.bashrc"'
  assert_contains "$temp_home/.config/mise/config.toml" "$REPO_ROOT"
  assert_not_contains "$temp_home/.config/mise/config.toml" "__DOTFILES_REPO_ROOT__"
  assert_contains "$temp_home/.config/mise/config.toml" '{{ version }}'
  assert_contains "$temp_home/.config/mise/config.toml" "[tasks.agent-skill-update]"
  assert_contains "$temp_home/.config/mise/config.toml" "python3 scripts/agent_skill_upstreams.py update"
  assert_file "$temp_home/.config/shell/secrets.env"
  local secrets_mode="$(file_mode "$temp_home/.config/shell/secrets.env")"
  [[ "$secrets_mode" == 600 ]] || fail "rendered private secrets target should be owner-only"
  assert_not_exists "$temp_home/.cshrc"
  assert_not_exists "$temp_home/.tcshrc"

  mkdir -p "$temp_home/.local/bin"
  local matrix_status=0
  run_rendered_bash_matrix "$temp_home" "$bash_output" "$matrix_os" || matrix_status=$?

  rm -rf "$temp_dir"
  return "$matrix_status"
}

run_rendered_bash_matrix() {
  local temp_home="$1"
  local bash_output="$2"
  local matrix_os="$3"
  local expected_major
  local bash_bin
  local bash_version
  local bash_major
  local smoke_status
  local matrix_status=0

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      bash_bin="$REPLY"
      bash_version="$("$bash_bin" --version | head -1)"
      bash_major="$(bash_major_version "$bash_bin")"
      smoke_status=0
      HOME="$temp_home" \
        XDG_CONFIG_HOME="$temp_home/.config" \
        USER=dotfiles-test \
        DOTFILES_REPO_ROOT="$REPO_ROOT" \
        PATH="/bin:/usr/bin:/usr/sbin:/sbin" \
        "$bash_bin" --noprofile --norc -c '
          . "$HOME/.bash_profile"
          printf "dotfiles_shell_name=%s\n" "$dotfiles_shell_name"
          printf "DOTFILES_REPO_ROOT=%s\n" "$DOTFILES_REPO_ROOT"
          case ":$PATH:" in
            *":$HOME/.local/bin:"*) printf "local_bin_in_path=yes\n" ;;
            *) printf "local_bin_in_path=no\n" ;;
          esac
        ' > "$bash_output" 2>&1 || smoke_status=$?
      if (( smoke_status != 0 )); then
        emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=rendered-home|status=FAIL|requirement=required|reason=smoke-failed"
        matrix_status=1
        continue
      fi
      assert_contains "$bash_output" "dotfiles_shell_name=bash"
      assert_contains "$bash_output" "DOTFILES_REPO_ROOT=$REPO_ROOT"
      assert_contains "$bash_output" "local_bin_in_path=yes"
      emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash$bash_major|target=rendered-home|status=PASS|requirement=required|reason=$bash_version"
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=rendered-home|status=SKIP|requirement=not-applicable|reason=macos-only"
    else
      emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=rendered-home|status=SKIP|requirement=required|reason=bash${expected_major}-unavailable"
      matrix_status=1
    fi
  done

  return "$matrix_status"
}

main() {
  test_chezmoi_renders_cli_profile_into_temp_home
  echo "chezmoi rendered home tests passed"
}

main "$@"
