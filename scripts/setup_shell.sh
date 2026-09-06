#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
readonly REPO_ROOT

MODE="apply"
FORCE=0
CHEZMOI_BIN=""
TEMP_DIR=""
HOME_CANONICAL=""
BRIDGE_PATH=""
BRIDGE_DIR=""
BRIDGE_EXPECTED=""
BRIDGE_ACTION="none"
CSH_BLOCK_EXPECTED=""
CSH_TARGET_ACTION=""
declare -a CHEZMOI_ARGS=()
declare -a SHELL_TARGETS=()
declare -a CSH_RC_TARGETS=()
declare -a CSH_RC_ACTIONS=()

readonly CSH_MARKER_TOKEN='dotfiles:csh-adapter'
readonly CSH_BLOCK_START='# >>> dotfiles:csh-adapter >>>'
readonly CSH_BLOCK_END='# <<< dotfiles:csh-adapter <<<'

usage() {
  cat <<'EOF'
Usage: bash scripts/setup_shell.sh [options]

Apply the shell-only chezmoi targets without Nix, zsh, mise, or package setup.

Options:
  --dry-run  Render the shell-only apply plan without changing the destination.
  --verify   Verify shell-only targets, csh/tcsh startup blocks, and any custom-XDG Fish bridge.
  --force    Overwrite inconsistent regular targets or bridge; keep csh/tcsh rc content fail-fast.
  -h, --help Show this help.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

cleanup() {
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run)
        [[ "$MODE" == apply ]] || die '--dry-run cannot be combined with --verify'
        MODE="dry-run"
        ;;
      --verify)
        [[ "$MODE" == apply ]] || die '--verify cannot be combined with --dry-run'
        MODE="verify"
        ;;
      --force)
        FORCE=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        die "unknown argument: $1"
        ;;
    esac
    shift
  done

  if [[ "$MODE" == verify && "$FORCE" == 1 ]]; then
    die '--force cannot be combined with --verify'
  fi
}

validate_home() {
  if [[ -z "${HOME:-}" || "$HOME" != /* || "$HOME" == *[[:cntrl:]]* ]]; then
    die 'HOME must be an absolute path without control bytes'
  fi
  [[ -d "$HOME" ]] || die "HOME does not exist: $HOME"
  HOME_CANONICAL="$(cd "$HOME" && pwd -P)" || die "failed to canonicalize HOME: $HOME"
  [[ "$HOME_CANONICAL" != *[[:cntrl:]]* ]] || die 'canonical HOME contains control bytes'
}

validate_source_state() {
  [[ -f "$REPO_ROOT/.chezmoiroot" && -d "$REPO_ROOT/home" ]] || {
    die "chezmoi source state is incomplete in $REPO_ROOT (expected .chezmoiroot and home/)"
  }
}

is_executable_file() {
  local candidate="$1"

  [[ "$candidate" == /* && "$candidate" != *[[:cntrl:]]* && -f "$candidate" && -x "$candidate" ]]
}

resolve_chezmoi() {
  local candidate=""

  if [[ -n "${DOTFILES_SETUP_CHEZMOI_BIN:-}" ]]; then
    candidate="$DOTFILES_SETUP_CHEZMOI_BIN"
  else
    candidate="$(command -v chezmoi 2>/dev/null || true)"
  fi
  if is_executable_file "$candidate"; then
    CHEZMOI_BIN="$candidate"
    return 0
  fi

  candidate="$HOME/.local/bin/chezmoi"
  if is_executable_file "$candidate"; then
    CHEZMOI_BIN="$candidate"
    return 0
  fi

  die 'chezmoi is required; install it with the host package manager or the official installer, then rerun setup_shell.sh'
}

create_temp_state() {
  local temp_root="${TMPDIR:-/tmp}"

  [[ -d "$temp_root" ]] || die "TMPDIR does not exist: $temp_root"
  TEMP_DIR="$(mktemp -d "$temp_root/dotfiles-setup-shell.XXXXXX")" || {
    die "failed to create a temporary directory under $temp_root"
  }
  chmod 700 "$TEMP_DIR"
  trap cleanup EXIT HUP INT TERM
  : > "$TEMP_DIR/chezmoi.toml"
  chmod 600 "$TEMP_DIR/chezmoi.toml"

  CHEZMOI_ARGS=(
    -S "$REPO_ROOT"
    -D "$HOME_CANONICAL"
    --cache "$TEMP_DIR/cache"
    --config "$TEMP_DIR/chezmoi.toml"
    --persistent-state "$TEMP_DIR/state.boltdb"
    --refresh-externals=never
    --no-tty
    --no-pager
    --color=false
  )
}

run_chezmoi() {
  DOTFILES_REPO_ROOT="$REPO_ROOT" "$CHEZMOI_BIN" "${CHEZMOI_ARGS[@]}" "$@"
}

set_shell_targets() {
  SHELL_TARGETS=(
    "$HOME_CANONICAL/.bashrc"
    "$HOME_CANONICAL/.bash_profile"
    "$HOME_CANONICAL/.config/shell/dotfiles-shell-common.sh"
    "$HOME_CANONICAL/.config/fish/conf.d/zz-dotfiles.fish"
    "$HOME_CANONICAL/.config/shell/dotfiles-shell-common.csh"
    "$HOME_CANONICAL/.config/mise/config.toml"
  )
}

set_csh_rc_targets() {
  CSH_RC_TARGETS=("$HOME_CANONICAL/.cshrc")
  if [[ -e "$HOME_CANONICAL/.tcshrc" || -L "$HOME_CANONICAL/.tcshrc" ]]; then
    CSH_RC_TARGETS+=("$HOME_CANONICAL/.tcshrc")
  fi
}

is_allowed_system_symlink() {
  local link_path="$1"
  local canonical_path

  case "$link_path" in
    /tmp|/var)
      canonical_path="$(cd "$link_path" 2>/dev/null && pwd -P)" || return 1
      case "$canonical_path" in
        /private/tmp|/private/var|/usr/var)
          return 0
          ;;
      esac
      ;;
  esac
  return 1
}

validate_parent_chain() {
  local path="$1"
  local stop_root="$2"
  local label="$3"
  local check_all_writable="$4"
  local current="$path"
  local found_existing=0

  while :; do
    if [[ "$current" == "$stop_root" ]]; then
      if [[ ! -d "$current" || -L "$current" ]]; then
        printf 'ERROR: %s parent is not a real directory: %s\n' "$label" "$current" >&2
        return 1
      fi
      if [[ "$check_all_writable" == 1 && ( ! -w "$current" || ! -x "$current" ) ]]; then
        printf 'ERROR: %s parent is not writable: %s\n' "$label" "$current" >&2
        return 1
      fi
      return 0
    fi
    if [[ "$current" == / ]]; then
      return 0
    fi

    if [[ -L "$current" ]]; then
      if ! is_allowed_system_symlink "$current"; then
        printf 'ERROR: %s parent symlink is not allowed: %s\n' "$label" "$current" >&2
        return 1
      fi
      current="$(cd "$current" 2>/dev/null && pwd -P)" || {
        printf 'ERROR: failed to resolve %s parent symlink: %s\n' "$label" "$current" >&2
        return 1
      }
    fi

    if [[ -e "$current" ]]; then
      if [[ ! -d "$current" ]]; then
        printf 'ERROR: %s parent is not a directory: %s\n' "$label" "$current" >&2
        return 1
      fi
      if [[ "$check_all_writable" == 1 || "$found_existing" == 0 ]]; then
        if [[ ! -w "$current" || ! -x "$current" ]]; then
          printf 'ERROR: %s parent is not writable: %s\n' "$label" "$current" >&2
          return 1
        fi
      fi
      found_existing=1
    fi

    current="${current%/*}"
    [[ -n "$current" ]] || current="/"
  done
}

validate_parent_directory() {
  local target="$1"
  local parent_dir="${target%/*}"

  validate_parent_chain "$parent_dir" "$HOME_CANONICAL" 'shell target' 1
}

preflight_target() {
  local target="$1"
  local status_output
  local status_log="$TEMP_DIR/status.log"

  if [[ ! -e "$target" && ! -L "$target" ]]; then
    return 0
  fi
  if [[ -L "$target" || -d "$target" || ! -f "$target" ]]; then
    printf 'ERROR: refusing to replace non-regular shell target: %s\n' "$target" >&2
    return 1
  fi

  if ! status_output="$(run_chezmoi status --path-style relative "$target" 2>"$status_log")"; then
    printf 'ERROR: failed to inspect existing shell target: %s\n' "$target" >&2
    sed -n '1,40p' "$status_log" >&2
    return 1
  fi
  if [[ -n "$status_output" && "$FORCE" != 1 ]]; then
    printf 'ERROR: refusing to overwrite existing shell target %s (chezmoi status: %s)\n' \
      "$target" "$status_output" >&2
    return 1
  fi
}

prepare_bridge_context() {
  local xdg_config_home="${XDG_CONFIG_HOME:-}"
  local bridge_root

  if [[ -z "$xdg_config_home" || "$xdg_config_home" == "$HOME/.config" || "$xdg_config_home" == "$HOME/.config/" || "$xdg_config_home" == "$HOME_CANONICAL/.config" || "$xdg_config_home" == "$HOME_CANONICAL/.config/" ]]; then
    return 0
  fi
  if [[ "$xdg_config_home" != /* || "$xdg_config_home" == *[[:cntrl:]]* ]]; then
    printf 'ERROR: XDG_CONFIG_HOME must be absolute and free of control bytes for the Fish bridge: %s\n' \
      "$xdg_config_home" >&2
    return 1
  fi

  if [[ "$xdg_config_home" == "$HOME"/* ]]; then
    bridge_root="$HOME_CANONICAL${xdg_config_home#"$HOME"}"
  elif [[ "$xdg_config_home" == "$HOME_CANONICAL"/* ]]; then
    bridge_root="$xdg_config_home"
  else
    bridge_root="$xdg_config_home"
  fi
  BRIDGE_DIR="$bridge_root/fish/conf.d"
  BRIDGE_PATH="$BRIDGE_DIR/zz-dotfiles-canonical.fish"
  BRIDGE_EXPECTED="$TEMP_DIR/zz-dotfiles-canonical.fish"
  printf '%s\n' \
    'set -l dotfiles_canonical_fish "$HOME/.config/fish/conf.d/zz-dotfiles.fish"' \
    'if test -r "$dotfiles_canonical_fish"' \
    '    source "$dotfiles_canonical_fish"' \
    'end' > "$BRIDGE_EXPECTED"
  chmod 600 "$BRIDGE_EXPECTED"

  if [[ "$BRIDGE_DIR" == "$HOME_CANONICAL" || "$BRIDGE_DIR" == "$HOME_CANONICAL"/* ]]; then
    validate_parent_chain "$BRIDGE_DIR" "$HOME_CANONICAL" 'Fish XDG bridge' 1 || return 1
  else
    validate_parent_chain "$BRIDGE_DIR" / 'Fish XDG bridge' 0 || return 1
  fi

  BRIDGE_ACTION="create"
  if [[ -L "$BRIDGE_PATH" || -d "$BRIDGE_PATH" ]]; then
    printf 'ERROR: refusing to replace non-regular Fish XDG bridge: %s\n' "$BRIDGE_PATH" >&2
    return 1
  fi
  if [[ -f "$BRIDGE_PATH" ]]; then
    if cmp -s "$BRIDGE_EXPECTED" "$BRIDGE_PATH"; then
      BRIDGE_ACTION="none"
    elif [[ "$FORCE" == 1 ]]; then
      BRIDGE_ACTION="replace"
    else
      printf 'ERROR: Fish XDG bridge already exists with different content: %s\n' "$BRIDGE_PATH" >&2
      return 1
    fi
  fi
}

prepare_csh_block() {
  CSH_BLOCK_EXPECTED="$TEMP_DIR/dotfiles-csh-adapter.block"
  printf '%s\n' \
    "$CSH_BLOCK_START" \
    'if ( ! $?df_csh_loaded ) then' \
    '  if ( -r "$HOME/.config/shell/dotfiles-shell-common.csh" ) then' \
    '    source "$HOME/.config/shell/dotfiles-shell-common.csh"' \
    '    if ( $status == 0 ) set df_csh_loaded = 1' \
    '  endif' \
    'endif' \
    "$CSH_BLOCK_END" > "$CSH_BLOCK_EXPECTED"
  chmod 600 "$CSH_BLOCK_EXPECTED"
}

preflight_csh_rc_target() {
  local target="$1"
  local start_count end_count marker_count start_line end_line
  local actual_block expected_block linked_path

  CSH_TARGET_ACTION="none"
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    CSH_TARGET_ACTION="create"
    return 0
  fi
  if [[ -L "$target" || -d "$target" || ! -f "$target" ]]; then
    printf 'ERROR: refusing to replace non-regular csh startup file: %s\n' "$target" >&2
    return 1
  fi
  linked_path="$(find "$target" -prune -links +1 -print)" || {
    printf 'ERROR: failed to inspect csh startup file links: %s\n' "$target" >&2
    return 1
  }
  if [[ -n "$linked_path" ]]; then
    printf 'ERROR: refusing to modify hard-linked csh startup file: %s\n' "$target" >&2
    return 1
  fi
  if [[ ! -r "$target" ]]; then
    printf 'ERROR: csh startup file is not readable: %s\n' "$target" >&2
    return 1
  fi
  if [[ "$MODE" != verify && ! -w "$target" ]]; then
    printf 'ERROR: csh startup file is not writable: %s\n' "$target" >&2
    return 1
  fi

  marker_count="$(grep -F -c -- "$CSH_MARKER_TOKEN" "$target" || true)"
  if [[ "$marker_count" == 0 ]]; then
    CSH_TARGET_ACTION="append"
    return 0
  fi

  start_count="$(grep -F -x -c -- "$CSH_BLOCK_START" "$target" || true)"
  end_count="$(grep -F -x -c -- "$CSH_BLOCK_END" "$target" || true)"
  if [[ "$marker_count" != 2 || "$start_count" != 1 || "$end_count" != 1 ]]; then
    printf 'ERROR: csh startup managed block is malformed, duplicated, or foreign: %s\n' "$target" >&2
    return 1
  fi

  start_line="$(grep -F -n -x -- "$CSH_BLOCK_START" "$target" | cut -d: -f1)"
  end_line="$(grep -F -n -x -- "$CSH_BLOCK_END" "$target" | cut -d: -f1)"
  if [[ -z "$start_line" || -z "$end_line" || "$end_line" -le "$start_line" ]]; then
    printf 'ERROR: csh startup managed block is malformed, duplicated, or foreign: %s\n' "$target" >&2
    return 1
  fi

  actual_block="$(sed -n "${start_line},${end_line}p" "$target")"
  expected_block="$(cat "$CSH_BLOCK_EXPECTED")"
  if [[ "$actual_block" != "$expected_block" ]]; then
    printf 'ERROR: csh startup managed block is malformed, duplicated, or foreign: %s\n' "$target" >&2
    return 1
  fi

  CSH_TARGET_ACTION="none"
}

prepare_csh_context() {
  local target
  local csh_preflight_failed=0

  validate_parent_chain "$HOME_CANONICAL" "$HOME_CANONICAL" 'csh startup' 1 || csh_preflight_failed=1
  prepare_csh_block
  CSH_RC_ACTIONS=()
  for target in "${CSH_RC_TARGETS[@]}"; do
    preflight_csh_rc_target "$target" || csh_preflight_failed=1
    CSH_RC_ACTIONS+=("$CSH_TARGET_ACTION")
  done
  (( csh_preflight_failed == 0 )) || return 1
}

preflight_all() {
  local target
  local preflight_failed=0

  for target in "${SHELL_TARGETS[@]}"; do
    validate_parent_directory "$target" || preflight_failed=1
    if [[ "$MODE" != verify ]]; then
      preflight_target "$target" || preflight_failed=1
    fi
  done
  prepare_bridge_context || preflight_failed=1
  prepare_csh_context || preflight_failed=1
  (( preflight_failed == 0 )) || return 1
}

verify_bridge() {
  if [[ -z "$BRIDGE_PATH" ]]; then
    return 0
  fi
  case "$BRIDGE_ACTION" in
    none)
      return 0
      ;;
    create)
      printf 'ERROR: Fish XDG bridge is missing: %s\n' "$BRIDGE_PATH" >&2
      return 1
      ;;
    replace)
      printf 'ERROR: Fish XDG bridge differs from the expected loader: %s\n' "$BRIDGE_PATH" >&2
      return 1
      ;;
    *)
      printf 'ERROR: invalid Fish XDG bridge state: %s\n' "$BRIDGE_ACTION" >&2
      return 1
      ;;
  esac
}

verify_csh_startup() {
  local index target action
  local verify_status=0

  for index in "${!CSH_RC_TARGETS[@]}"; do
    target="${CSH_RC_TARGETS[$index]}"
    action="${CSH_RC_ACTIONS[$index]}"
    if [[ "$action" != none ]]; then
      printf 'ERROR: csh startup managed block is missing: %s\n' "$target" >&2
      verify_status=1
    fi
  done
  return "$verify_status"
}

render_shell_targets() {
  local show_plan="${1:-0}"
  local -a apply_args
  local target

  apply_args=(--dry-run apply --parent-dirs '--exclude=scripts,externals')

  if [[ "$FORCE" == 1 ]]; then
    apply_args+=(--force)
  fi
  for target in "${SHELL_TARGETS[@]}"; do
    apply_args+=("$target")
  done
  if [[ "$show_plan" == 1 ]]; then
    run_chezmoi "${apply_args[@]}" || {
      printf 'ERROR: shell-only chezmoi render preflight failed\n' >&2
      return 1
    }
  elif ! run_chezmoi "${apply_args[@]}" >/dev/null; then
    printf 'ERROR: shell-only chezmoi render preflight failed\n' >&2
    return 1
  fi
}

apply_shell_targets() {
  local -a apply_args
  local target

  apply_args=(apply --parent-dirs '--exclude=scripts,externals')

  if [[ "$FORCE" == 1 ]]; then
    apply_args+=(--force)
  fi
  for target in "${SHELL_TARGETS[@]}"; do
    apply_args+=("$target")
  done
  run_chezmoi "${apply_args[@]}"
}

write_bridge() {
  [[ -n "$BRIDGE_PATH" ]] || return 0
  case "$BRIDGE_ACTION" in
    none)
      return 0
      ;;
    create|replace)
      mkdir -p "$BRIDGE_DIR"
      cp "$BRIDGE_EXPECTED" "$BRIDGE_PATH"
      chmod 0644 "$BRIDGE_PATH"
      printf 'Fish XDG bridge: %s\n' "$BRIDGE_PATH"
      ;;
    *)
      die "invalid Fish XDG bridge action: $BRIDGE_ACTION"
      ;;
  esac
}

write_csh_startup() {
  local index target action last_byte

  for index in "${!CSH_RC_TARGETS[@]}"; do
    target="${CSH_RC_TARGETS[$index]}"
    action="${CSH_RC_ACTIONS[$index]}"
    case "$action" in
      none)
        ;;
      create)
        cp "$CSH_BLOCK_EXPECTED" "$target"
        chmod 0644 "$target"
        printf 'csh startup: %s (created)\n' "$target"
        ;;
      append)
        if [[ -s "$target" ]]; then
          last_byte="$(tail -c 1 "$target" 2>/dev/null || true)"
          if [[ -n "$last_byte" ]]; then
            printf '\n' >> "$target"
          fi
        fi
        cat "$CSH_BLOCK_EXPECTED" >> "$target"
        printf 'csh startup: %s (appended)\n' "$target"
        ;;
      *)
        die "invalid csh startup action: $action"
        ;;
    esac
  done
}

verify_shell_targets() {
  local verify_status=0

  run_chezmoi verify "${SHELL_TARGETS[@]}" || verify_status=$?
  verify_bridge || verify_status=1
  verify_csh_startup || verify_status=1
  return "$verify_status"
}

main() {
  parse_args "$@"
  validate_home
  validate_source_state
  resolve_chezmoi
  create_temp_state
  set_shell_targets
  set_csh_rc_targets
  preflight_all

  case "$MODE" in
    verify)
      verify_shell_targets
      ;;
    dry-run)
      printf 'Dry-run: shell-only setup\n'
      for target in "${SHELL_TARGETS[@]}"; do
        printf 'Plan: target=%s action=chezmoi-apply\n' "$target"
      done
      for index in "${!CSH_RC_TARGETS[@]}"; do
        printf 'Plan: target=%s action=csh-startup-%s\n' \
          "${CSH_RC_TARGETS[$index]}" "${CSH_RC_ACTIONS[$index]}"
      done
      render_shell_targets 1
      if [[ "$BRIDGE_ACTION" == create ]]; then
        printf 'Fish XDG bridge (would create): %s\n' "$BRIDGE_PATH"
      elif [[ "$BRIDGE_ACTION" == replace ]]; then
        printf 'Fish XDG bridge (would replace): %s\n' "$BRIDGE_PATH"
      fi
      ;;
    apply)
      render_shell_targets 0
      apply_shell_targets
      write_bridge
      write_csh_startup
      printf 'Shell-only setup completed\n'
      ;;
    *)
      die "invalid setup mode: $MODE"
      ;;
  esac
}

main "$@"
