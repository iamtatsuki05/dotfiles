# Fish adapter runtime checks.

run_fish_render_checks() {
  local dest="$1"
  local fish_bin=""
  local out="$FIXTURE/fish-non-interactive.log"
  local fish_noninteractive_status=0 fish_process_status=0 fish_activation_flag
  local fish_failure_config fish_failure_status fish_failure_stdout fish_failure_stderr fish_mode
  local rc=0

  if ! resolve_fish; then
    emit_matrix_record "$(matrix_os_name)" fish non-interactive SKIP not-applicable temporary-fish-runtime-unavailable
    emit_matrix_record "$(matrix_os_name)" fish interactive SKIP not-applicable temporary-fish-runtime-unavailable
    return 0
  fi
  fish_bin="$REPLY"

  : > "$FIXTURE/mise.log"
  env -i HOME="$dest" USER=fixture-user XDG_CONFIG_HOME="$dest/.config" XDG_CACHE_HOME="$dest/.fixture-cache" \
    XDG_DATA_HOME="$dest/.fixture-data" XDG_STATE_HOME="$dest/.fixture-state" \
    MISE_DATA_DIR="$dest/.fixture-data/mise" TMPDIR="$FIXTURE/tmp" \
    PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$fish_bin" \
    "$fish_bin" --no-config -c '
      source "$HOME/.config/fish/conf.d/uv.env.fish"
      source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"
      echo "editor=$EDITOR"
      echo "config=$XDG_CONFIG_HOME"
      echo "cache=$XDG_CACHE_HOME"
      echo "data=$XDG_DATA_HOME"
      echo "state=$XDG_STATE_HOME"
      echo "foreign=$UV_FOREIGN_SENTINEL"
      echo "mise="(command -v mise)
      if functions -q ginit; echo "ginit=present"; else; echo "ginit=absent"; end
      echo "activation_failed=$DOTFILES_MISE_ACTIVATE_FISH_FAILED"
      echo "path=$PATH"
    ' > "$out" 2>&1 || fish_noninteractive_status=$?
  if (( fish_noninteractive_status != 0 )); then
    emit_matrix_record "$(matrix_os_name)" fish non-interactive FAIL required runtime-smoke-failed
    rc=1
  else
    assert_output_contains "$out" 'editor=fixture-editor'
    assert_output_contains "$out" "config=$dest/.config"
    assert_output_contains "$out" "cache=$dest/.fixture-cache"
    assert_output_contains "$out" "data=$dest/.fixture-data"
    assert_output_contains "$out" "state=$dest/.fixture-state"
    assert_output_contains "$out" 'foreign=foreign'
    assert_output_contains "$out" "mise=$FAKE_BIN/mise"
    assert_output_contains "$out" 'ginit=absent'
    assert_output_contains "$out" 'activation_failed=0'
    assert_output_contains "$out" "$dest/.fixture-bin"
    [[ ! -s "$FIXTURE/mise.log" ]] || fail 'Fish non-interactive mode must not invoke mise'
    emit_matrix_record "$(matrix_os_name)" fish non-interactive PASS required rendered-conf-d
  fi

  : > "$FIXTURE/mise.log"
  : > "$GCLOUD_LOG"
  out="$FIXTURE/fish-interactive.log"
  env -i HOME="$dest" USER=fixture-user XDG_CONFIG_HOME="$dest/.config" XDG_CACHE_HOME="$dest/.fixture-cache" \
    XDG_DATA_HOME="$dest/.fixture-data" XDG_STATE_HOME="$dest/.fixture-state" \
    MISE_DATA_DIR="$dest/.fixture-data/mise" TMPDIR="$FIXTURE/tmp" \
    PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$fish_bin" \
    "$fish_bin" --no-config -i -c 'source "$HOME/.config/fish/conf.d/uv.env.fish"; source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"; ginit; gauth; gls; echo mise=(command -v mise); echo activated=$DOTFILES_FISH_ACTIVATED; echo activation_failed=$DOTFILES_MISE_ACTIVATE_FISH_FAILED; echo interactive=yes' > "$out" 2>&1 || fish_process_status=$?
  if (( fish_process_status != 0 )); then
    emit_matrix_record "$(matrix_os_name)" fish interactive FAIL required runtime-smoke-failed
    rc=1
  else
    assert_output_contains "$out" 'interactive=yes'
    assert_output_contains "$out" "mise=$FAKE_BIN/mise"
    fish_activation_flag="$(grep '^activation_failed=' "$out" | sed 's/^activation_failed=//' | tail -1)"
    if [[ "$fish_activation_flag" == 1 ]]; then
      emit_matrix_record "$(matrix_os_name)" fish interactive FAIL required mise-activate-fish-failed
      rc=1
    else
      assert_output_contains "$out" 'activated=yes'
      assert_output_contains "$out" 'activation_failed=0'
      [[ "$(grep -c '^activate fish$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 1 ]] || fail 'Fish activation count is not one'
      assert_alias_log
      emit_matrix_record "$(matrix_os_name)" fish interactive PASS required official-activation
    fi
  fi

  fish_failure_config="$FIXTURE/fish-failure-config"
  mkdir -p "$fish_failure_config"
  for fish_mode in failure empty invalid; do
    fish_failure_stdout="$FIXTURE/fish-$fish_mode.stdout"
    fish_failure_stderr="$FIXTURE/fish-$fish_mode.stderr"
    : > "$FIXTURE/mise.log"
    fish_failure_status=0
    env -i HOME="$dest" USER=fixture-user XDG_CONFIG_HOME="$fish_failure_config" \
      XDG_CACHE_HOME="$FIXTURE/fish-failure-cache" XDG_DATA_HOME="$FIXTURE/fish-failure-data" \
      XDG_STATE_HOME="$FIXTURE/fish-failure-state" MISE_DATA_DIR="$FIXTURE/fish-failure-data/mise" \
      TMPDIR="$FIXTURE/tmp" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$fish_bin" \
      DOTFILES_FISH_ADAPTER="$dest/.config/fish/conf.d/zz-dotfiles.fish" FAKE_MISE_MODE="$fish_mode" \
      "$fish_bin" --no-config -i -c 'source "$DOTFILES_FISH_ADAPTER"; set source_status $status; echo "source_status=$source_status"; echo "activation_failed=$DOTFILES_MISE_ACTIVATE_FISH_FAILED"; true; echo "after_status=$status"' > "$fish_failure_stdout" 2> "$fish_failure_stderr" || fish_failure_status=$?
    (( fish_failure_status == 0 )) || fail "Fish $fish_mode failure probe child exited non-zero"
    assert_output_contains "$fish_failure_stdout" 'source_status=1'
    assert_output_contains "$fish_failure_stdout" 'activation_failed=1'
    assert_output_contains "$fish_failure_stdout" 'after_status=0'
    assert_output_contains "$fish_failure_stderr" 'dotfiles: mise activate fish failed'
    assert_output_lacks_token "$fish_failure_stdout" 'dotfiles: mise activate fish failed' 'Fish activation failure diagnostic must be emitted on stderr'
    [[ "$(grep -c '^activate fish$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 1 ]] || fail "Fish $fish_mode activation count is not one"
  done

  return "$rc"
}
