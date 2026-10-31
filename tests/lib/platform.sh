# Shared platform and runtime discovery helpers for shell tests.

is_test_macos() {
  [[ "$(uname -s 2>/dev/null)" == Darwin ]]
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

emit_matrix_record() {
  local os="$1" shell="$2" target="$3" result_status="$4" requirement="$5" reason="$6"

  emit_matrix_result "MATRIX_RESULT|os=$os|shell=$shell|target=$target|status=$result_status|requirement=$requirement|reason=$reason"
}

bash_major() {
  local bash_bin="$1"

  /usr/bin/env -i HOME=/tmp/dotfiles-bash-version-probe PATH="/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" --noprofile --norc -c 'printf "%s\n" "${BASH_VERSINFO[0]}"'
}

select_bash() {
  local wanted="$1" candidate actual
  local -a candidates=()

  if [[ "$wanted" == 3 ]]; then
    [[ -n "${BASH32_BIN:-}" ]] && candidates=("$BASH32_BIN") || candidates=(/bin/bash)
  else
    [[ -n "${BASH5_BIN:-}" ]] && candidates=("$BASH5_BIN") || candidates=(/run/current-system/sw/bin/bash /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash)
  fi
  for candidate in "${candidates[@]}"; do
    [[ "$candidate" == /* && -x "$candidate" ]] || continue
    actual="$(bash_major "$candidate" 2>/dev/null)" || continue
    [[ "$actual" == "$wanted" ]] && { REPLY="$candidate"; return 0; }
  done
  return 1
}

resolve_fish() {
  local candidate

  candidate="$(command -v fish 2>/dev/null || true)"
  if [[ "$candidate" == /* && -x "$candidate" ]]; then
    REPLY="$candidate"
    return 0
  fi
  if [[ -d /nix/store ]]; then
    setopt localoptions nullglob
    for candidate in /nix/store/*fish*/bin/fish; do
      if [[ "$candidate" == /nix/store/* && -x "$candidate" ]]; then
        REPLY="$candidate"
        return 0
      fi
    done
  fi
  return 1
}

resolve_shell_binary() {
  local shell_name="$1" shell_path

  shell_path="$(command -v "$shell_name" 2>/dev/null || true)"
  [[ "$shell_path" == /* && -x "$shell_path" ]] || return 1
  REPLY="$shell_path"
}

shell_binary_realpath() {
  local shell_path="$1"

  if command -v realpath >/dev/null 2>&1; then
    realpath -- "$shell_path"
  else
    print -r -- "$shell_path"
  fi
}

shell_binary_inode() {
  local shell_path="$1"

  if stat -c '%d:%i' "$shell_path" >/dev/null 2>&1; then
    stat -c '%d:%i' "$shell_path"
  else
    stat -f '%d:%i' "$shell_path"
  fi
}

shell_binaries_are_same() {
  local first="$1" second="$2"

  [[ "$(shell_binary_realpath "$first")" == "$(shell_binary_realpath "$second")" ]] \
    || [[ "$(shell_binary_inode "$first")" == "$(shell_binary_inode "$second")" ]]
}

file_mode() {
  local mode

  mode="$(stat -c '%a' "$1" 2>/dev/null)" || mode=""
  if [[ "$mode" =~ '^[0-7]{3,4}$' ]]; then
    print -r -- "$mode"
    return 0
  fi
  mode="$(stat -f '%Lp' "$1" 2>/dev/null)" || mode=""
  if [[ "$mode" =~ '^[0-7]{3,4}$' ]]; then
    print -r -- "$mode"
    return 0
  fi
  return 1
}

assert_file_mode_portability() {
  local root stat_dir stat_bin target stat_log mode output rc=0 regression_status=0

  make_temp_dir file-mode-portability
  root="$REPLY"
  stat_dir="$root/bin"
  stat_bin="$stat_dir/stat"
  target="$root/target"
  stat_log="$root/stat.log"
  mkdir -p "$stat_dir"
  print -r -- 'synthetic file-mode target' > "$target"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- 'printf "%s %s\n" "${1:-}" "${2:-}" >> "$FAKE_STAT_LOG"'
    print -r -- 'case "${FAKE_STAT_MODE:-}" in'
    print -r -- '  gnu)'
    print -r -- '    if [ "${1:-}" = "-f" ]; then printf "%s\n" "File: synthetic-target" "Blocks: $$" "600"; exit 0; fi'
    print -r -- '    if [ "${1:-}" = "-c" ]; then printf "%s\n" "600"; exit 0; fi'
    print -r -- '    ;;'
    print -r -- '  bsd)'
    print -r -- '    if [ "${1:-}" = "-c" ]; then exit 2; fi'
    print -r -- '    if [ "${1:-}" = "-f" ]; then printf "%s\n" "600"; exit 0; fi'
    print -r -- '    ;;'
    print -r -- '  invalid)'
    print -r -- '    printf "%s\n" "not-a-mode"'
    print -r -- '    exit 0'
    print -r -- '    ;;'
    print -r -- '  *) exit 64 ;;'
    print -r -- 'esac'
    print -r -- 'exit 64'
  } > "$stat_bin"
  chmod +x "$stat_bin"

  (
    for mode in gnu bsd; do
      : > "$stat_log"
      output="$(
        PATH="$stat_dir:/bin:/usr/bin:/usr/sbin:/sbin"
        FAKE_STAT_MODE="$mode"
        FAKE_STAT_LOG="$stat_log"
        export PATH FAKE_STAT_MODE FAKE_STAT_LOG
        rehash
        file_mode "$target"
      )" || { rc=$?; fail "file_mode $mode portability probe failed with status $rc"; }
      [[ "$output" == 600 ]] || fail "file_mode $mode must return a canonical mode"
      if [[ "$mode" == gnu ]]; then
        assert_file_content "$stat_log" '-c %a'
      else
        [[ "$(sed -n '1p' "$stat_log")" == '-c %a' ]] || fail 'BSD mode probe must try GNU stat first'
        [[ "$(sed -n '2p' "$stat_log")" == '-f %Lp' ]] || fail 'BSD mode probe must fall back to BSD stat'
      fi
    done

    : > "$stat_log"
    rc=0
    output="$(
      PATH="$stat_dir:/bin:/usr/bin:/usr/sbin:/sbin"
      FAKE_STAT_MODE=invalid
      FAKE_STAT_LOG="$stat_log"
      export PATH FAKE_STAT_MODE FAKE_STAT_LOG
      rehash
      file_mode "$target"
    )" || rc=$?
    (( rc != 0 )) || fail 'file_mode must fail when both stat formats are invalid'
    [[ "$(sed -n '1p' "$stat_log")" == '-c %a' ]] || fail 'invalid mode probe must try GNU stat first'
    [[ "$(sed -n '2p' "$stat_log")" == '-f %Lp' ]] || fail 'invalid mode probe must try BSD stat before failing'
  ) || regression_status=$?

  rm -rf "$root"
  return "$regression_status"
}
