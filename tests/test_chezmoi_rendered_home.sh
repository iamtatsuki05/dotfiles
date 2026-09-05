#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly TEST_PYTHON_BIN="${DOTFILES_TEST_PYTHON:-python3}"

source "$TEST_DIR/lib/assertions.sh"
source "$TEST_DIR/lib/platform.sh"
source "$TEST_DIR/lib/chezmoi.sh"
source "$TEST_DIR/lib/multi-shell/schema.sh"

typeset -g RENDER_TEMP_DIR=""

cleanup_render_temp() {
  [[ -n "$RENDER_TEMP_DIR" ]] && rm -rf -- "$RENDER_TEMP_DIR"
}

trap cleanup_render_temp EXIT HUP INT TERM

test_chezmoi_renders_cli_profile_into_temp_home() {
  local temp_dir
  local temp_home
  local temp_config
  local bash_output
  local apply_status=0
  local matrix_os="$(matrix_os_name)"

  make_temp_dir chezmoi-rendered-home
  temp_dir="${REPLY:A}"
  RENDER_TEMP_DIR="$temp_dir"
  temp_home="$temp_dir/home"
  temp_config="$temp_dir/chezmoi.toml"
  bash_output="$temp_dir/bash-output"
  mkdir -p "$temp_home"
  : > "$temp_config"
  assert_file_mode_portability

  if ! resolve_chezmoi >/dev/null 2>&1; then
    emit_matrix_record "$matrix_os" chezmoi rendered-home SKIP required chezmoi-unavailable
    return 1
  fi
  CHEZMOI_BIN="$REPLY"
  run_chezmoi_apply "$REPO_ROOT" "$temp_home" "$REPO_ROOT" "$temp_dir/cache" "$temp_config" \
    "$temp_dir/chezmoistate.boltdb" "$temp_dir/chezmoi.log" cli "$temp_dir" || apply_status=$?
  if (( apply_status != 0 )); then
    sed -n '1,120p' "$temp_dir/chezmoi.log" >&2
    emit_matrix_record "$matrix_os" chezmoi rendered-home FAIL required apply-failed
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

  require_python_tomllib
  local marker_root="$temp_dir/root __MISE_OPEN__ __MISE_CLOSE__"
  local marker_home="$temp_dir/marker-home"
  local marker_config="$temp_dir/marker-chezmoi.toml"
  local marker_log="$temp_dir/marker-chezmoi.log"
  run_chezmoi_apply "$REPO_ROOT" "$marker_home" "$marker_root" "$temp_dir/marker-cache" "$marker_config" \
    "$temp_dir/marker-state.boltdb" "$marker_log" cli "$temp_dir" || {
    sed -n '1,120p' "$marker_log" >&2
    fail 'chezmoi apply failed for literal mise placeholder root'
  }
  assert_file "$marker_home/.config/mise/config.toml"
  "$TEST_PYTHON_BIN" - "$marker_home/.config/mise/config.toml" "$marker_root" <<'PY'
import sys
import tomllib
from pathlib import Path

config = tomllib.loads(Path(sys.argv[1]).read_text())
expected_root = sys.argv[2]
task_dirs = [task.get("dir") for task in config.get("tasks", {}).values() if isinstance(task, dict) and "dir" in task]
if expected_root not in task_dirs:
    raise SystemExit("literal mise placeholder root was not preserved in task dirs")
if not any("__MISE_OPEN__" in value and "__MISE_CLOSE__" in value for value in task_dirs):
    raise SystemExit("literal mise placeholder markers were decoded in task dirs")
PY

  mkdir -p "$temp_home/.local/bin"
  local matrix_status=0
  run_rendered_bash_matrix "$temp_home" "$bash_output" "$matrix_os" || matrix_status=$?

  return "$matrix_status"
}

run_rendered_bash_matrix() {
  local temp_home="$1"
  local bash_output="$2"
  local matrix_os="$3"
  local expected_major
  local bash_bin
  local bash_version
  local actual_major
  local smoke_status
  local startup_file="$temp_home/bash-env-startup"
  local startup_marker="$temp_home/bash-env-startup-marker"
  local cdpath_marker="$temp_home/bash-cdpath-marker"
  local matrix_status=0

  print -r -- ": > ${(qqq)startup_marker}; : > ${(qqq)cdpath_marker}" > "$startup_file"

  for expected_major in 3 5; do
    if select_bash "$expected_major"; then
      bash_bin="$REPLY"
      bash_version="$("$bash_bin" --version | head -1)"
      actual_major="$(bash_major "$bash_bin")"
      smoke_status=0
      rm -f -- "$startup_marker" "$cdpath_marker"
      BASH_ENV="$startup_file" ENV="$startup_file" CDPATH="$cdpath_marker" \
        env -i \
        HOME="$temp_home" \
        XDG_CONFIG_HOME="$temp_home/.config" \
        USER=dotfiles-test \
        DOTFILES_REPO_ROOT="$REPO_ROOT" \
        SHELL="$bash_bin" \
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
      assert_not_exists "$startup_marker"
      assert_not_exists "$cdpath_marker"
      if (( smoke_status != 0 )); then
        emit_matrix_record "$matrix_os" "bash${expected_major}" rendered-home FAIL required smoke-failed
        matrix_status=1
        continue
      fi
      assert_contains "$bash_output" "dotfiles_shell_name=bash"
      assert_contains "$bash_output" "DOTFILES_REPO_ROOT=$REPO_ROOT"
      assert_contains "$bash_output" "local_bin_in_path=yes"
      emit_matrix_record "$matrix_os" "bash$actual_major" rendered-home PASS required "$bash_version"
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_matrix_record "$matrix_os" "bash${expected_major}" rendered-home SKIP not-applicable macos-only
    else
      emit_matrix_record "$matrix_os" "bash${expected_major}" rendered-home SKIP required "bash${expected_major}-unavailable"
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
