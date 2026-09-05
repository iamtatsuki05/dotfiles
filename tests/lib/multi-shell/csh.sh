# csh/tcsh optional adapter checks and identity-aware matrix.

run_csh_shared_smoke() {
  local shell_bin="$1" home="$2" out="$FIXTURE/csh.log" rc=0

  mkdir -p "$FIXTURE/mise data/shims"
  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
    MISE_DATA_DIR="$FIXTURE/mise data" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c '
      if ( $?USER ) unsetenv USER
      if ( $?LOGNAME ) unsetenv LOGNAME
      source "$DOTFILES_CSH_ADAPTER"
      source "$DOTFILES_CSH_ADAPTER"
      echo "editor=$EDITOR"
      echo "config=$XDG_CONFIG_HOME"
      echo "cache=$XDG_CACHE_HOME"
      echo "data=$XDG_DATA_HOME"
      echo "state=$XDG_STATE_HOME"
      echo "path=$PATH"
      echo "path_count=$#path"
      if ( $#path >= 1 ) echo "path[1]=$path[1]"
      if ( $#path >= 2 ) echo "path[2]=$path[2]"
      if ( $#path >= 3 ) echo "path[3]=$path[3]"
      if ( $#path >= 4 ) echo "path[4]=$path[4]"
      if ( $#path >= 5 ) echo "path[5]=$path[5]"
      if ( $#path >= 6 ) echo "path[6]=$path[6]"
      if ( $#path >= 7 ) echo "path[7]=$path[7]"
      if ( "`printenv DOTFILES_FOREIGN_SECRET_SENTINEL`" != "" ) then
        echo "secret=read"
      else
        echo "secret=unread"
      endif
      alias | grep "^ginit[[:space:]]" >& /dev/null
      if ( $status == 0 ) then
        echo "ginit=present"
      else
        echo "ginit=absent"
      endif
    ' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || { sed -n '1,120p' "$out" >&2; fail 'csh/tcsh non-interactive smoke failed'; }
  assert_output_contains "$out" 'editor=fixture-editor'
  assert_output_contains "$out" "config=$home/.fixture-config"
  assert_output_contains "$out" "cache=$home/.fixture-cache"
  assert_output_contains "$out" "data=$home/.fixture-data"
  assert_output_contains "$out" "state=$home/.fixture-state"
  assert_output_contains "$out" "$home/.fixture-bin"
  assert_output_contains "$out" "$FIXTURE/mise data/shims"
  assert_output_contains "$out" 'ginit=absent'
  assert_output_contains "$out" 'secret=unread'
  assert_not_contains "$out" '/etc/profiles/per-user//bin'
  assert_csh_path_order "$out" "$home"

  : > "$GCLOUD_LOG"
  out="$FIXTURE/csh-interactive.log"
  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" USER=fixture-user \
    MISE_DATA_DIR="$FIXTURE/mise data" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -i -c 'set prompt = fixture; source "$DOTFILES_CSH_ADAPTER"; eval ginit; eval gauth; eval gls; echo interactive=yes' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || { sed -n '1,120p' "$out" >&2; fail 'csh/tcsh interactive smoke failed'; }
  assert_output_contains "$out" 'interactive=yes'
  assert_alias_log
}

assert_csh_path_order() {
  local out="$1" home="$2" brew_bin brew_sbin

  if is_test_macos; then
    brew_bin="$FIXTURE/homebrew-bin"
    brew_sbin="$FIXTURE/homebrew-sbin"
  else
    brew_bin="$home/.fixture-linuxbrew/bin"
    brew_sbin="$home/.fixture-linuxbrew/sbin"
  fi
  assert_output_contains "$out" 'path_count=12'
  assert_output_contains "$out" "path[1]=$FIXTURE/mise data/shims"
  assert_output_contains "$out" "path[2]=$FIXTURE/absolute-two"
  assert_output_contains "$out" "path[3]=$FIXTURE/absolute-one"
  assert_output_contains "$out" "path[4]=$home/.nix-profile/bin"
  assert_output_contains "$out" "path[5]=$home/.fixture-bin"
  assert_output_contains "$out" "path[6]=$brew_bin"
  assert_output_contains "$out" "path[7]=$brew_sbin"
  [[ "$(grep -Fc "path=$FIXTURE/mise data/shims" "$out")" -eq 1 ]] || fail 'csh PATH line missing'
  [[ "$(grep -Fc "path[1]=$FIXTURE/mise data/shims" "$out")" -eq 1 ]] || fail 'csh shim was duplicated'
  [[ "$(grep -Fc "path[5]=$home/.fixture-bin" "$out")" -eq 1 ]] || fail 'csh home candidate was duplicated'
}

assert_output_path_entry_count() {
  local out="$1" expected="$2" expected_count="$3" path_line actual_count

  path_line="$(grep '^path=' "$out" | sed 's/^path=//' | tail -1)"
  actual_count="$(printf '%s' "$path_line" | tr ':' '\n' | awk -v target="$expected" '$0 == target { count++ } END { print count + 0 }')"
  [[ "$actual_count" == "$expected_count" ]] || fail "csh PATH candidate count changed: $expected=$actual_count expected=$expected_count"
}

run_csh_shim_precedence_smoke() {
  local shell_bin="$1" home="$2" out="$FIXTURE/csh-shims.log" rc=0
  local xdg_root="$FIXTURE/xdg data" missing_root="$FIXTURE/missing mise" explicit_missing="$FIXTURE/explicit missing xdg"
  local all_missing_home="$FIXTURE/all-missing-home" all_missing_xdg="$FIXTURE/all-missing-xdg"
  local resolution_root resolution_shim resolution_log
  resolution_root="$FIXTURE/shim resolution"
  resolution_shim="$resolution_root/mise/shims/gcloud"
  resolution_log="$FIXTURE/shim-resolution.log"
  local base_path="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin"
  local home_root="$home/.fixture-data/mise"

  mkdir -p "$xdg_root/mise/shims" "$home_root/shims" "$explicit_missing" "$all_missing_home" "$all_missing_xdg"

  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
    MISE_DATA_DIR="$missing_root" XDG_DATA_HOME="$xdg_root" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c 'if ( $?USER ) unsetenv USER; if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; echo "data=$XDG_DATA_HOME"; echo "path=$PATH"' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'csh strict MISE_DATA_DIR smoke failed'
  assert_output_contains "$out" "data=$xdg_root"
  assert_not_contains "$out" "$xdg_root/mise/shims"
  assert_not_contains "$out" "$home_root/shims"

  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
    MISE_DATA_DIR= XDG_DATA_HOME="$xdg_root" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c 'if ( $?USER ) unsetenv USER; if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; echo "data=$XDG_DATA_HOME"; echo "path=$PATH"' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'csh XDG_DATA_HOME shim smoke failed'
  assert_output_contains "$out" "data=$xdg_root"
  assert_output_contains "$out" "$xdg_root/mise/shims"

  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
    MISE_DATA_DIR= XDG_DATA_HOME= DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c 'if ( $?USER ) unsetenv USER; if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; echo "data=$XDG_DATA_HOME"; echo "path=$PATH"' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'csh HOME shim smoke failed'
  assert_output_contains "$out" "data=$home/.fixture-data"
  assert_output_contains "$out" "$home_root/shims"

  out="$FIXTURE/csh-shims-explicit-missing.log"
  env -i HOME="$home" PATH="$base_path" SHELL="$shell_bin" \
    MISE_DATA_DIR= XDG_DATA_HOME="$explicit_missing" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c 'if ( $?USER ) unsetenv USER; if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; source "$DOTFILES_CSH_ADAPTER"; echo "data=$XDG_DATA_HOME"; echo "path=$PATH"' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'csh explicit XDG missing shim smoke failed'
  assert_output_contains "$out" "data=$explicit_missing"
  assert_not_contains "$out" "$explicit_missing/mise/shims"
  assert_not_contains "$out" "$home_root/shims"
  assert_output_path_entry_count "$out" "$explicit_missing/mise/shims" 0
  assert_output_path_entry_count "$out" "$home_root/shims" 0

  rm -rf -- "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" "$FIXTURE/absolute-one" "$FIXTURE/absolute-two" \
    "$FIXTURE/linuxbrew-system-sbin" "$FIXTURE/linuxbrew-system-bin" "$home/.fixture-linuxbrew"
  out="$FIXTURE/csh-shims-all-missing.log"
  env -i HOME="$all_missing_home" PATH="$base_path" SHELL="$shell_bin" \
    MISE_DATA_DIR= XDG_DATA_HOME="$all_missing_xdg" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c 'if ( $?USER ) unsetenv USER; if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; source "$DOTFILES_CSH_ADAPTER"; echo "data=$XDG_DATA_HOME"; echo "path=$PATH"' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'csh all shim candidates missing smoke failed'
  assert_output_contains "$out" "data=$all_missing_xdg"
  assert_output_contains "$out" "path=$base_path"
  assert_not_contains "$out" "$all_missing_xdg/mise/shims"
  assert_not_contains "$out" "$all_missing_home/.fixture-data/mise/shims"
  mkdir -p "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" "$FIXTURE/absolute-one" "$FIXTURE/absolute-two" \
    "$FIXTURE/linuxbrew-system-sbin" "$FIXTURE/linuxbrew-system-bin" "$home/.fixture-linuxbrew/sbin" "$home/.fixture-linuxbrew/bin"

  mkdir -p "$resolution_root/mise/shims"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- '[ "${1:-}" = shim-probe ] || exit 97'
    print -r -- "printf '%s\\n' shim-probe > ${(qqq)resolution_log}"
  } > "$resolution_shim"
  chmod +x "$resolution_shim"
  out="$FIXTURE/csh-shim-resolution.log"
  env -i HOME="$home" PATH="$base_path" SHELL="$shell_bin" \
    MISE_DATA_DIR= XDG_DATA_HOME="$resolution_root" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c 'if ( $?USER ) unsetenv USER; if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; source "$DOTFILES_CSH_ADAPTER"; echo "path=$PATH"; echo "gcloud=`which gcloud`"; gcloud shim-probe' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'csh shim command resolution smoke failed'
  assert_output_contains "$out" "gcloud=$resolution_shim"
  assert_output_contains "$out" "$resolution_root/mise/shims"
  assert_output_path_entry_count "$out" "$resolution_root/mise/shims" 1
  assert_file_content "$resolution_log" 'shim-probe'
}

run_csh_user_cases_smoke() {
  local shell_bin="$1" home="$2" out="$FIXTURE/csh-user.log" rc=0 user_assignment

  for user_assignment in 'USER=' 'USER=fixture-user'; do
    env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
      "$user_assignment" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
      "$shell_bin" -f -c 'if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; echo "path=$PATH"' > "$out" 2>&1 || rc=$?
    (( rc == 0 )) || fail 'csh USER unset/empty/non-empty fixture failed'
    assert_not_contains "$out" '/etc/profiles/per-user//bin'
    assert_not_contains "$out" '/etc/profiles/per-user/fixture-user/bin'
    rc=0
  done
}

run_csh_checks() {
  local shell_bin="$1" home="$2"

  run_csh_shared_smoke "$shell_bin" "$home"
  run_csh_shim_precedence_smoke "$shell_bin" "$home"
  run_csh_user_cases_smoke "$shell_bin" "$home"
}

emit_csh_matrix_skip() {
  local shell_name="$1" target="$2" reason="$3"

  emit_matrix_record "$(matrix_os_name)" "$shell_name" "$target" SKIP not-applicable "$reason"
}

run_csh_matrix_for() {
  local shell_name="$1" shell_bin="$2" home="$3" target="$4" reason="${5:-runtime-smoke}" result_status=0

  if ( run_csh_checks "$shell_bin" "$home" ); then
    emit_matrix_record "$(matrix_os_name)" "$shell_name" "$target" PASS required "$reason"
  else
    emit_matrix_record "$(matrix_os_name)" "$shell_name" "$target" FAIL required runtime-smoke-failed
    result_status=1
  fi
  return "$result_status"
}

run_csh_matrix() {
  local csh_bin="${1:-}" tcsh_bin="${2:-}" home="$3" csh_status=0 tcsh_status=0

  if [[ -z "$csh_bin" && -z "$tcsh_bin" ]]; then
    emit_csh_matrix_skip csh csh-command runtime-unavailable
    emit_csh_matrix_skip tcsh tcsh-runtime runtime-unavailable
    return 0
  fi
  if [[ -z "$csh_bin" ]]; then
    emit_csh_matrix_skip csh csh-command runtime-unavailable
    run_csh_matrix_for tcsh "$tcsh_bin" "$home" tcsh-runtime || tcsh_status=$?
    return "$tcsh_status"
  fi
  if [[ -z "$tcsh_bin" ]]; then
    run_csh_matrix_for csh "$csh_bin" "$home" csh-command || csh_status=$?
    emit_csh_matrix_skip tcsh tcsh-runtime runtime-unavailable
    return "$csh_status"
  fi
  if shell_binaries_are_same "$csh_bin" "$tcsh_bin"; then
    run_csh_matrix_for csh "$csh_bin" "$home" csh-command shared-csh-tcsh-binary || csh_status=$?
    emit_csh_matrix_skip tcsh tcsh-runtime duplicate-csh-command-binary
    emit_csh_matrix_skip csh csh-runtime genuine-csh-unverified
    return "$csh_status"
  fi
  run_csh_matrix_for csh "$csh_bin" "$home" csh-command || csh_status=$?
  run_csh_matrix_for tcsh "$tcsh_bin" "$home" tcsh-runtime || tcsh_status=$?
  (( csh_status == 0 && tcsh_status == 0 ))
}

run_csh_distinct_binary_probe() {
  local actual_csh actual_tcsh wrapper_csh="$FIXTURE/csh-wrapper" wrapper_tcsh="$FIXTURE/tcsh-wrapper"
  local marker="$FIXTURE/tcsh-wrapper-invoked" out="$FIXTURE/distinct-csh-matrix.log"

  resolve_shell_binary csh || return 0
  actual_csh="$REPLY"
  resolve_shell_binary tcsh || return 0
  actual_tcsh="$REPLY"
  {
    print -r -- '#!/bin/sh'
    print -r -- "exec $actual_csh \"\$@\""
  } > "$wrapper_csh"
  {
    print -r -- '#!/bin/sh'
    print -r -- ": > $marker"
    print -r -- 'exit 99'
  } > "$wrapper_tcsh"
  chmod +x "$wrapper_csh" "$wrapper_tcsh"
  if MATRIX_RESULT_LOG_DIR="$FIXTURE/distinct-csh-matrix-results" run_csh_matrix "$wrapper_csh" "$wrapper_tcsh" "$FIXTURE/home" > "$out" 2>&1; then
    fail 'distinct csh/tcsh matrix must not pass when tcsh execution fails'
  fi
  assert_file "$marker"
  assert_output_contains "$out" 'shell=csh|target=csh-command|status=PASS|requirement=required'
  assert_output_contains "$out" 'shell=tcsh|target=tcsh-runtime|status=FAIL|requirement=required'
}
