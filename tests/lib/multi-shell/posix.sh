# Bash and Zsh runtime probes share the POSIX shell contract.

assert_bash_version_probe_isolated() {
  local startup="$FIXTURE/bash-version-startup"
  local marker="$FIXTURE/bash-version-startup-marker"
  local out="$FIXTURE/bash-version.log"
  local rc=0

  print -r -- ": > ${(qqq)marker}" > "$startup"
  BASH_ENV="$startup" ENV="$startup" CDPATH="$marker" bash_major /bin/bash > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'isolated Bash version probe failed'
  grep -Eq '^[35]$' "$out" || fail 'Bash version probe did not report a supported major version'
  assert_not_exists "$marker"
}

run_bash_probe() {
  local bin="$1" home="$2" out="$3" rc=0

  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$bin" USER=fixture-user "$bin" --noprofile --norc -c '
    shopt -s expand_aliases
    . "$HOME/.bash_profile"; . "$HOME/.bash_profile"
    ginit; gauth; gls
    printf "shell=%s\nroot=%s\neditor=%s\nconfig=%s\ncache=%s\ndata=%s\nstate=%s\nsecret=%s\npath=%s\n" "$dotfiles_shell_name" "$DOTFILES_REPO_ROOT" "$EDITOR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "${DOTFILES_FOREIGN_SECRET_SENTINEL:-missing}" "$PATH"
    printf "gt=%s gr=%s gs=%s\n" "$(type -t gt)" "$(type -t gr)" "$(type -t gs)"
  ' > "$out" 2>&1 || rc=$?
  return "$rc"
}

run_zsh_probe() {
  local home="$1" out="$2" rc=0

  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$TEST_ZSH_BIN" USER=fixture-user "$TEST_ZSH_BIN" -f -c '
    source "$HOME/.config/shell/dotfiles-shell-common.sh"; source "$HOME/.config/shell/dotfiles-shell-common.sh"
    eval ginit; eval gauth; eval gls
    print -r -- "shell=$dotfiles_shell_name"; print -r -- "root=$DOTFILES_REPO_ROOT"
    print -r -- "editor=$EDITOR"; print -r -- "config=$XDG_CONFIG_HOME"; print -r -- "cache=$XDG_CACHE_HOME"; print -r -- "data=$XDG_DATA_HOME"; print -r -- "state=$XDG_STATE_HOME"; print -r -- "path=$PATH"
    print -r -- "secret=${DOTFILES_FOREIGN_SECRET_SENTINEL:-missing}"
    whence -w gt >/dev/null 2>&1 && gt_type=function || gt_type=missing
    whence -w gr >/dev/null 2>&1 && gr_type=function || gr_type=missing
    whence -w gs >/dev/null 2>&1 && gs_type=function || gs_type=missing
    print -r -- "gt=$gt_type gr=$gr_type gs=$gs_type"
  ' > "$out" 2>&1 || rc=$?
  return "$rc"
}

run_posix_mise_activation_probe() {
  local shell_kind="$1" shell_bin="$2" home="$3" out="$4" rc=0

  if [[ "$shell_kind" == bash ]]; then
    env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" USER=fixture-user \
      "$shell_bin" --noprofile --norc -i -c '
        . "$HOME/.config/shell/dotfiles-shell-common.sh"
        . "$HOME/.config/shell/dotfiles-shell-common.sh"
        printf "activated=%s\n" "${DOTFILES_BASH_ACTIVATED:-missing}"
      ' > "$out" 2>&1 || rc=$?
  else
    env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" USER=fixture-user \
      "$shell_bin" -f -i -c '
        source "$HOME/.config/shell/dotfiles-shell-common.sh"
        source "$HOME/.config/shell/dotfiles-shell-common.sh"
        print -r -- "activated=${DOTFILES_ZSH_ACTIVATED:-missing}"
      ' > "$out" 2>&1 || rc=$?
  fi
  return "$rc"
}

run_posix_mise_failure_probe() {
  local shell_kind="$1" shell_bin="$2" home="$3" out="$4" err="$5" rc=0

  : > "$FIXTURE/mise.log"
  if [[ "$shell_kind" == bash ]]; then
    env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" USER=fixture-user FAKE_MISE_MODE=failure \
      "$shell_bin" --noprofile --norc -i -c '
        . "$HOME/.config/shell/dotfiles-shell-common.sh"
        source_status=$?
        printf "source_status=%s\n" "$source_status"
        true
        printf "after_status=%s\n" "$?"
      ' > "$out" 2> "$err" || rc=$?
  else
    env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" USER=fixture-user FAKE_MISE_MODE=failure \
      "$shell_bin" -f -i -c '
        source "$HOME/.config/shell/dotfiles-shell-common.sh"
        source_status=$status
        print -r -- "source_status=$source_status"
        true
        print -r -- "after_status=$status"
      ' > "$out" 2> "$err" || rc=$?
  fi
  (( rc == 0 )) || fail "$shell_kind mise failure probe child exited non-zero"
  assert_output_contains "$out" 'source_status=19'
  assert_output_contains "$out" 'after_status=0'
  assert_output_contains "$err" "dotfiles: mise activate $shell_kind failed"
  [[ "$(grep -c "^activate $shell_kind$" "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 1 ]] \
    || fail "$shell_kind activation failure count is not one"
}

run_posix_user_unset_probe() {
  local shell_kind="$1" shell_bin="$2" home="$3" out="$4" rc=0

  if [[ "$shell_kind" == bash ]]; then
    env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
      "$shell_bin" --noprofile --norc -c '
        set -u
        . "$HOME/.config/shell/dotfiles-shell-common.sh"
        printf "user_unset=yes\npath=%s\n" "$PATH"
      ' > "$out" 2>&1 || rc=$?
  else
    env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
      "$shell_bin" -f -c '
        set -u
        source "$HOME/.config/shell/dotfiles-shell-common.sh"
        print -r -- "user_unset=yes"
        print -r -- "path=$PATH"
      ' > "$out" 2>&1 || rc=$?
  fi
  (( rc == 0 )) || fail "$shell_kind common must tolerate USER unset under nounset"
  assert_output_contains "$out" 'user_unset=yes'
  assert_not_contains "$out" '/etc/profiles/per-user//bin'
}

run_posix_secret_status_probe() {
  local shell_kind="$1" shell_bin="$2" source_home="$3" mode="$4" out="$5" err="$6"
  local probe_home="$FIXTURE/$shell_kind-secret-$mode-home"
  local secret_path="$probe_home/.config/shell/secrets.env"
  local expected_status=0 rc=0

  mkdir -p "$probe_home/.config/shell"
  cp "$source_home/.config/shell/dotfiles-shell-common.sh" "$probe_home/.config/shell/dotfiles-shell-common.sh"
  case "$mode" in
    absent)
      ;;
    success)
      print -r -- ':' > "$secret_path"
      ;;
    return19)
      print -r -- 'return 19' > "$secret_path"
      expected_status=19
      ;;
    syntax)
      print -r -- 'if (' > "$secret_path"
      if [[ "$shell_kind" == bash ]]; then
        if [[ "$(bash_major "$shell_bin")" == 3 ]]; then
          expected_status=1
        else
          expected_status=2
        fi
      else
        expected_status=126
      fi
      ;;
    *)
      fail "unknown secrets.env status mode: $mode"
      ;;
  esac
  if [[ "$shell_kind" == bash ]]; then
    env -i HOME="$probe_home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
      "$shell_bin" --noprofile --norc -c '
        . "$HOME/.config/shell/dotfiles-shell-common.sh"
        source_status=$?
        printf "source_status=%s\n" "$source_status"
        true
        printf "after_status=%s\n" "$?"
      ' > "$out" 2> "$err" || rc=$?
  else
    env -i HOME="$probe_home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$shell_bin" \
      "$shell_bin" -f -c '
        source "$HOME/.config/shell/dotfiles-shell-common.sh"
        source_status=$status
        print -r -- "source_status=$source_status"
        true
        print -r -- "after_status=$status"
      ' > "$out" 2> "$err" || rc=$?
  fi
  (( rc == 0 )) || fail "$shell_kind secrets.env $mode probe child exited non-zero"
  assert_output_contains "$out" "source_status=$expected_status"
  assert_output_contains "$out" 'after_status=0'
}

assert_shell_output() {
  local out="$1" home="$2" shell_kind="${3:-bash}"

  assert_output_contains "$out" 'editor=fixture-editor'
  assert_output_contains "$out" 'secret=foreign-secret'
  assert_output_contains "$out" "config=$home/.fixture-config"
  assert_output_contains "$out" "cache=$home/.fixture-cache"
  assert_output_contains "$out" "data=$home/.fixture-data"
  assert_output_contains "$out" "state=$home/.fixture-state"
  assert_output_contains "$out" 'gt=function gr=function gs=function'
  assert_output_contains "$out" "$home/.fixture-bin"
}

posix_brew_bin() {
  local home="$1"

  if is_test_macos; then
    print -r -- "$FIXTURE/homebrew-bin"
  else
    print -r -- "$home/.fixture-linuxbrew/bin"
  fi
}

posix_brew_sbin() {
  local home="$1"

  if is_test_macos; then
    print -r -- "$FIXTURE/homebrew-sbin"
  else
    print -r -- "$home/.fixture-linuxbrew/sbin"
  fi
}

assert_posix_path_order() {
  local out="$1" home="$2" path_line candidate count brew_bin brew_sbin
  local -a expected_candidates=(
    "$FIXTURE/absolute-two"
    "$FIXTURE/absolute-one"
    "$home/.nix-profile/bin"
    "$home/.fixture-bin"
  )

  brew_bin="$(posix_brew_bin "$home")"
  brew_sbin="$(posix_brew_sbin "$home")"
  expected_candidates+=("$brew_bin" "$brew_sbin")
  path_line="$(grep '^path=' "$out" | sed 's/^path=//')"
  [[ "$path_line" == *"$FIXTURE/absolute-two:$FIXTURE/absolute-one:$home/.nix-profile/bin:$home/.fixture-bin:$brew_bin:$brew_sbin:"* ]] \
    || fail "POSIX PATH precedence changed: $path_line"
  for candidate in "${expected_candidates[@]}"; do
    count="$(printf '%s' "$path_line" | tr ':' '\n' | awk -v target="$candidate" '$0 == target { count++ } END { print count + 0 }')"
    [[ "$count" == 1 ]] || fail "POSIX PATH candidate count changed: $candidate=$count"
  done
}

run_posix_env_policy_probe() {
  local shell_kind="$1" shell_bin="$2" home="$3" mode="$4" out="$5" rc=0
  local -a env_args=(
    "HOME=$home"
    "PATH=/bin:/usr/bin:/usr/sbin:/sbin"
    "SHELL=$shell_bin"
    "USER=fixture-user"
  )

  if [[ "$mode" == custom ]]; then
    env_args+=(
      "EDITOR=preserved-editor"
      "XDG_CONFIG_HOME=$FIXTURE/custom config"
      "XDG_CACHE_HOME=$FIXTURE/custom cache"
      "XDG_DATA_HOME=$FIXTURE/custom data"
      "XDG_STATE_HOME=$FIXTURE/custom state"
    )
  else
    env_args+=(EDITOR= XDG_CONFIG_HOME= XDG_CACHE_HOME= XDG_DATA_HOME= XDG_STATE_HOME=)
  fi
  if [[ "$shell_kind" == bash ]]; then
    env -i "${env_args[@]}" "$shell_bin" --noprofile --norc -c '
      . "$HOME/.config/shell/dotfiles-shell-common.sh"
      printf "editor=%s\nconfig=%s\ncache=%s\ndata=%s\nstate=%s\n" "$EDITOR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"
    ' > "$out" 2>&1 || rc=$?
  else
    env -i "${env_args[@]}" "$shell_bin" -f -c '
      source "$HOME/.config/shell/dotfiles-shell-common.sh"
      print -r -- "editor=$EDITOR"; print -r -- "config=$XDG_CONFIG_HOME"; print -r -- "cache=$XDG_CACHE_HOME"; print -r -- "data=$XDG_DATA_HOME"; print -r -- "state=$XDG_STATE_HOME"
    ' > "$out" 2>&1 || rc=$?
  fi
  return "$rc"
}

run_posix_root_policy_probe() {
  local shell_kind="$1" shell_bin="$2" home="$3" mode="$4" expected="$5" out="$6" rc=0
  local -a env_args=(
    "HOME=$home"
    "PATH=/bin:/usr/bin:/usr/sbin:/sbin"
    "SHELL=$shell_bin"
    "USER=fixture-user"
  )

  case "$mode" in
    override) env_args+=("DOTFILES_REPO_ROOT=$expected") ;;
    empty) env_args+=("DOTFILES_REPO_ROOT=") ;;
    unset) ;;
    *) fail "unknown DOTFILES_REPO_ROOT policy mode: $mode" ;;
  esac
  if [[ "$shell_kind" == bash ]]; then
    env -i "${env_args[@]}" "$shell_bin" --noprofile --norc -c '
      . "$HOME/.config/shell/dotfiles-shell-common.sh"
      printf "root=%s\n" "$DOTFILES_REPO_ROOT"
    ' > "$out" 2>&1 || rc=$?
  else
    env -i "${env_args[@]}" "$shell_bin" -f -c '
      source "$HOME/.config/shell/dotfiles-shell-common.sh"
      print -r -- "root=$DOTFILES_REPO_ROOT"
    ' > "$out" 2>&1 || rc=$?
  fi
  return "$rc"
}

assert_posix_root_policy() {
  local out="$1" expected="$2"

  assert_output_contains "$out" "root=$expected"
}

run_posix_root_policy_cases() {
  local shell_kind="$1" shell_bin="$2" home="$3" label="$4" default_root="$5" runtime_override="$6" side_effect="$7" secondary_side_effect="${8:-}"

  run_posix_root_policy_probe "$shell_kind" "$shell_bin" "$home" override "$runtime_override" "$FIXTURE/$label-root-override.log"
  assert_posix_root_policy "$FIXTURE/$label-root-override.log" "$runtime_override"
  run_posix_root_policy_probe "$shell_kind" "$shell_bin" "$home" empty "$default_root" "$FIXTURE/$label-root-empty.log"
  assert_posix_root_policy "$FIXTURE/$label-root-empty.log" "$default_root"
  run_posix_root_policy_probe "$shell_kind" "$shell_bin" "$home" unset "$default_root" "$FIXTURE/$label-root-unset.log"
  assert_posix_root_policy "$FIXTURE/$label-root-unset.log" "$default_root"
  assert_not_exists "$side_effect"
  [[ -z "$secondary_side_effect" ]] || assert_not_exists "$secondary_side_effect"
}

assert_posix_env_policy() {
  local out="$1" home="$2" mode="$3"

  if [[ "$mode" == custom ]]; then
    assert_output_contains "$out" 'editor=preserved-editor'
    assert_output_contains "$out" "config=$FIXTURE/custom config"
    assert_output_contains "$out" "cache=$FIXTURE/custom cache"
    assert_output_contains "$out" "data=$FIXTURE/custom data"
    assert_output_contains "$out" "state=$FIXTURE/custom state"
  else
    assert_output_contains "$out" 'editor=fixture-editor'
    assert_output_contains "$out" "config=$home/.fixture-config"
    assert_output_contains "$out" "cache=$home/.fixture-cache"
    assert_output_contains "$out" "data=$home/.fixture-data"
    assert_output_contains "$out" "state=$home/.fixture-state"
  fi
}

assert_alias_log() {
  local expected=$'init\nauth login\ncompute instances list'

  if [[ "$(<"$GCLOUD_LOG")" != "$expected" ]]; then
    print -u2 -r -- "actual-gcloud-log=$(sed -n '1,20p' "$GCLOUD_LOG")"
    fail 'gcloud alias argv changed'
  fi
}

assert_bash_activation() {
  local out="$1" activation_count

  assert_output_contains "$out" 'activated=yes'
  activation_count="$(grep -c '^activate bash$' "$FIXTURE/mise.log" 2>/dev/null || true)"
  [[ "$activation_count" -eq 1 ]] || fail "Bash common activation count changed: $activation_count"
  [[ "$(grep -c '^activate fish$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 0 ]] || fail 'Bash common invoked Fish mise activation'
  [[ "$(grep -c '^activate zsh$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 0 ]] || fail 'Bash common invoked Zsh mise activation'
}

assert_zsh_activation() {
  local out="$1" activation_count

  assert_output_contains "$out" 'activated=yes'
  activation_count="$(grep -c '^activate zsh$' "$FIXTURE/mise.log" 2>/dev/null || true)"
  [[ "$activation_count" -eq 1 ]] || fail "Zsh common activation count changed: $activation_count"
  [[ "$(grep -c '^activate bash$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 0 ]] || fail 'Zsh common invoked Bash mise activation'
  [[ "$(grep -c '^activate fish$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 0 ]] || fail 'Zsh common invoked Fish mise activation'
}

run_source_matrix() {
  local src="$1" dest="$2" matrix_os="$(matrix_os_name)" expected_major bin out smoke_status matrix_status=0

  for expected_major in 3 5; do
    if select_bash "$expected_major"; then
      bin="$REPLY"
      out="$FIXTURE/source-bash${expected_major}.log"
      : > "$GCLOUD_LOG"
      : > "$FIXTURE/mise.log"
      smoke_status=0
      run_bash_probe "$bin" "$dest" "$out" || smoke_status=$?
      if (( smoke_status != 0 )); then
        emit_matrix_record "$matrix_os" "bash${expected_major}" chezmoi-source FAIL required runtime-smoke-failed
        matrix_status=1
        continue
      fi
      assert_shell_output "$out" "$dest" bash
      assert_output_contains "$out" "root=$src"
      assert_posix_path_order "$out" "$dest"
      assert_alias_log
      : > "$FIXTURE/mise.log"
      run_posix_mise_activation_probe bash "$bin" "$dest" "$FIXTURE/source-bash${expected_major}-activation.log"
      assert_bash_activation "$FIXTURE/source-bash${expected_major}-activation.log"
      run_posix_user_unset_probe bash "$bin" "$dest" "$FIXTURE/source-bash${expected_major}-user-unset.log"
      run_posix_mise_failure_probe bash "$bin" "$dest" "$FIXTURE/source-bash${expected_major}-mise-failure.log" "$FIXTURE/source-bash${expected_major}-mise-failure.err"
      emit_matrix_record "$matrix_os" "bash${expected_major}" chezmoi-source PASS required rendered-artifact
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_matrix_record "$matrix_os" bash3 chezmoi-source SKIP not-applicable macos-only
    else
      emit_matrix_record "$matrix_os" "bash${expected_major}" chezmoi-source SKIP required "bash${expected_major}-unavailable"
      matrix_status=1
    fi
  done

  if [[ ! -x "$TEST_ZSH_BIN" || ! -f "$TEST_ZSH_BIN" ]]; then
    emit_matrix_record "$matrix_os" zsh chezmoi-source SKIP required zsh-unavailable
    return 1
  fi

  out="$FIXTURE/source-zsh.log"
  : > "$GCLOUD_LOG"
  : > "$FIXTURE/mise.log"
  smoke_status=0
  run_zsh_probe "$dest" "$out" || smoke_status=$?
  if (( smoke_status != 0 )); then
    emit_matrix_record "$matrix_os" zsh chezmoi-source FAIL required runtime-smoke-failed
    matrix_status=1
  else
    assert_shell_output "$out" "$dest" zsh
    assert_output_contains "$out" "root=$src"
    assert_posix_path_order "$out" "$dest"
    assert_alias_log
    : > "$FIXTURE/mise.log"
    run_posix_mise_activation_probe zsh "$TEST_ZSH_BIN" "$dest" "$FIXTURE/source-zsh-activation.log"
    assert_zsh_activation "$FIXTURE/source-zsh-activation.log"
    run_posix_user_unset_probe zsh "$TEST_ZSH_BIN" "$dest" "$FIXTURE/source-zsh-user-unset.log"
    run_posix_mise_failure_probe zsh "$TEST_ZSH_BIN" "$dest" "$FIXTURE/source-zsh-mise-failure.log" "$FIXTURE/source-zsh-mise-failure.err"
    emit_matrix_record "$matrix_os" zsh chezmoi-source PASS required rendered-artifact
  fi

  return "$matrix_status"
}

run_bash_render_case() {
  local expected_major="$1" bin="$2" dest="$3" src="$4" hostile_source_dest="$5" hostile_source_root="$6" runtime_override="$7" hostile_source_override="$8"
  local out="$FIXTURE/bash${expected_major}.log" smoke_status=0 secret_mode

  : > "$GCLOUD_LOG"
  : > "$FIXTURE/mise.log"
  run_bash_probe "$bin" "$dest" "$out" || smoke_status=$?
  if (( smoke_status != 0 )); then
    emit_matrix_record "$(matrix_os_name)" "bash${expected_major}" rendered-home FAIL required smoke-failed
    return 1
  fi
  assert_shell_output "$out" "$dest" bash
  assert_output_contains "$out" "root=$src"
  assert_posix_path_order "$out" "$dest"
  : > "$FIXTURE/mise.log"
  run_posix_mise_activation_probe bash "$bin" "$dest" "$FIXTURE/bash${expected_major}-activation.log"
  assert_bash_activation "$FIXTURE/bash${expected_major}-activation.log"
  run_posix_user_unset_probe bash "$bin" "$dest" "$FIXTURE/bash${expected_major}-user-unset.log"
  run_posix_mise_failure_probe bash "$bin" "$dest" "$FIXTURE/bash${expected_major}-mise-failure.log" "$FIXTURE/bash${expected_major}-mise-failure.err"
  for secret_mode in absent success return19 syntax; do
    run_posix_secret_status_probe bash "$bin" "$dest" "$secret_mode" "$FIXTURE/bash${expected_major}-secret-$secret_mode.log" "$FIXTURE/bash${expected_major}-secret-$secret_mode.err"
  done
  run_posix_env_policy_probe bash "$bin" "$dest" custom "$FIXTURE/bash${expected_major}-custom.log"
  assert_posix_env_policy "$FIXTURE/bash${expected_major}-custom.log" "$dest" custom
  run_posix_env_policy_probe bash "$bin" "$dest" empty "$FIXTURE/bash${expected_major}-empty.log"
  assert_posix_env_policy "$FIXTURE/bash${expected_major}-empty.log" "$dest" empty
  run_posix_root_policy_cases bash "$bin" "$dest" "bash${expected_major}" "$src" "$runtime_override" "$FIXTURE/runtime-side-effect" "$FIXTURE/runtime-backtick-side-effect"
  run_posix_root_policy_cases bash "$bin" "$hostile_source_dest" "bash${expected_major}-hostile" "$hostile_source_root" "$hostile_source_override" "$FIXTURE/source-override-side-effect" "$FIXTURE/source-override-backtick-side-effect"
  assert_alias_log
  emit_matrix_record "$(matrix_os_name)" "bash${expected_major}" rendered-home PASS required rendered-common
}

run_zsh_render_case() {
  local dest="$1" src="$2" hostile_source_dest="$3" hostile_source_root="$4" runtime_override="$5" hostile_source_override="$6"
  local out="$FIXTURE/zsh.log" smoke_status=0 secret_mode

  : > "$GCLOUD_LOG"
  : > "$FIXTURE/mise.log"
  run_zsh_probe "$dest" "$out" || smoke_status=$?
  if (( smoke_status != 0 )); then
    emit_matrix_record "$(matrix_os_name)" zsh rendered-home FAIL required smoke-failed
    return 1
  fi
  assert_shell_output "$out" "$dest" zsh
  assert_output_contains "$out" "root=$src"
  assert_posix_path_order "$out" "$dest"
  : > "$FIXTURE/mise.log"
  run_posix_mise_activation_probe zsh "$TEST_ZSH_BIN" "$dest" "$FIXTURE/zsh-activation.log"
  assert_zsh_activation "$FIXTURE/zsh-activation.log"
  run_posix_user_unset_probe zsh "$TEST_ZSH_BIN" "$dest" "$FIXTURE/zsh-user-unset.log"
  run_posix_mise_failure_probe zsh "$TEST_ZSH_BIN" "$dest" "$FIXTURE/zsh-mise-failure.log" "$FIXTURE/zsh-mise-failure.err"
  for secret_mode in absent success return19 syntax; do
    run_posix_secret_status_probe zsh "$TEST_ZSH_BIN" "$dest" "$secret_mode" "$FIXTURE/zsh-secret-$secret_mode.log" "$FIXTURE/zsh-secret-$secret_mode.err"
  done
  run_posix_env_policy_probe zsh "$TEST_ZSH_BIN" "$dest" custom "$FIXTURE/zsh-custom.log"
  assert_posix_env_policy "$FIXTURE/zsh-custom.log" "$dest" custom
  run_posix_env_policy_probe zsh "$TEST_ZSH_BIN" "$dest" empty "$FIXTURE/zsh-empty.log"
  assert_posix_env_policy "$FIXTURE/zsh-empty.log" "$dest" empty
  run_posix_root_policy_cases zsh "$TEST_ZSH_BIN" "$dest" zsh "$src" "$runtime_override" "$FIXTURE/runtime-side-effect" "$FIXTURE/runtime-backtick-side-effect"
  run_posix_root_policy_cases zsh "$TEST_ZSH_BIN" "$hostile_source_dest" zsh-hostile "$hostile_source_root" "$hostile_source_override" "$FIXTURE/source-override-side-effect" "$FIXTURE/source-override-backtick-side-effect"
  assert_alias_log
  emit_matrix_record "$(matrix_os_name)" zsh rendered-home PASS required rendered-common
}

run_posix_render_matrix() {
  local dest="$1" src="$2" hostile_source_dest="$3" hostile_source_root="$4" runtime_override="$5" hostile_source_override="$6"
  local expected_major bin rc=0

  for expected_major in 3 5; do
    if select_bash "$expected_major"; then
      bin="$REPLY"
      run_bash_render_case "$expected_major" "$bin" "$dest" "$src" "$hostile_source_dest" "$hostile_source_root" "$runtime_override" "$hostile_source_override" || rc=1
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_matrix_record "$(matrix_os_name)" bash3 rendered-home SKIP not-applicable macos-only
    else
      emit_matrix_record "$(matrix_os_name)" "bash${expected_major}" rendered-home SKIP required "bash${expected_major}-unavailable"
      rc=1
    fi
  done

  if [[ ! -x "$TEST_ZSH_BIN" || ! -f "$TEST_ZSH_BIN" ]]; then
    emit_matrix_record "$(matrix_os_name)" zsh rendered-home SKIP required zsh-unavailable
    rc=1
  else
    run_zsh_render_case "$dest" "$src" "$hostile_source_dest" "$hostile_source_root" "$runtime_override" "$hostile_source_override" || rc=1
  fi

  return "$rc"
}
