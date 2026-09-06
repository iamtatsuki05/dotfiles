#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"
readonly TEST_PYTHON_BIN="${DOTFILES_TEST_PYTHON:-python3}"

source "$TEST_DIR/lib/assertions.sh"
source "$TEST_DIR/lib/platform.sh"
source "$TEST_DIR/lib/chezmoi.sh"
source "$TEST_DIR/lib/multi-shell/fixture.sh"
source "$TEST_DIR/lib/multi-shell/schema.sh"
source "$TEST_DIR/lib/multi-shell/posix.sh"
source "$TEST_DIR/lib/multi-shell/fish.sh"
source "$TEST_DIR/lib/multi-shell/csh.sh"
source "$TEST_DIR/lib/multi-shell/features.sh"

typeset -g SELECTOR=""
typeset -g SKIP_CHEZMOI=0
typeset -g CHEZMOI_BIN=""
typeset -g FIXTURE=""
typeset -g FAKE_BIN=""
typeset -g GCLOUD_LOG=""
typeset -g MISE_REAL_BIN=""

usage() {
  print -u2 -r -- 'Usage: zsh tests/test_multi_shell_config.sh --selector source|render [--skip-chezmoi]'
}

parse_selector() {
  local selector_seen=0

  while (( $# )); do
    case "$1" in
      --selector)
        if (( $# < 2 )); then
          usage
          return 2
        fi
        case "$2" in
          source|render) SELECTOR="$2"; selector_seen=1 ;;
          *) usage; return 2 ;;
        esac
        shift 2
        ;;
      --skip-chezmoi)
        SKIP_CHEZMOI=1
        shift
        ;;
      *)
        usage
        return 2
        ;;
    esac
  done
  if (( ! selector_seen )) || (( SKIP_CHEZMOI )) && [[ "$SELECTOR" != source ]]; then
    usage
    return 2
  fi
}

run_source() {
  local src="$FIXTURE/source-state-source"
  local dest="$FIXTURE/source-state-home"
  local matrix_os="$(matrix_os_name)"
  local rc=0

  mkdir -p "$FIXTURE/tmp" "$dest/.config/shell" "$dest/.fixture-config/shell" "$dest/.fixture-bin" \
    "$dest/.nix-profile/bin" "$dest/.fixture-linuxbrew/sbin" "$dest/.fixture-linuxbrew/bin" \
    "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" "$FIXTURE/absolute-one" "$FIXTURE/absolute-two" \
    "$FIXTURE/linuxbrew-system-sbin" "$FIXTURE/linuxbrew-system-bin"
  copy_source "$src"
  mutate_data "$src/home/.chezmoidata.toml"
  write_fakes
  print -r -- 'export DOTFILES_FOREIGN_SECRET_SENTINEL=foreign-secret' > "$dest/.config/shell/secrets.env"
  chmod 600 "$dest/.config/shell/secrets.env"
  if ! run_apply "$src" "$dest" "$src"; then
    emit_matrix_record "$matrix_os" chezmoi chezmoi-source FAIL required apply-failed
    return 1
  fi
  assert_rendered "$dest"
  emit_matrix_record "$matrix_os" chezmoi chezmoi-source PASS required rendered-artifact
  run_source_matrix "$src" "$dest" || rc=$?
  return "$rc"
}

run_render() {
  local src="$FIXTURE/source"
  local dest="$FIXTURE/home"
  local hostile_src="$FIXTURE/hostile-src"
  local hostile_dest="$FIXTURE/hostile-home"
  local hostile_root="-hostile root __DOTFILES_REPO_ROOT__ with 'single' \"double\" ; echo BAD \$(touch $FIXTURE/side-effect) \`touch $FIXTURE/backtick-side-effect\` ! "
  hostile_root+=$'\\'
  local runtime_override="-runtime root with 'single' \"double\" ; \$(touch $FIXTURE/runtime-side-effect) \`touch $FIXTURE/runtime-backtick-side-effect\` ! "
  runtime_override+=$'\\'
  local hostile_source_override="-override root with 'single' \"double\" ; \$(touch $FIXTURE/source-override-side-effect) \`touch $FIXTURE/source-override-backtick-side-effect\` __DOTFILES_REPO_ROOT__ ! "
  hostile_source_override+=$'\\'
  local bin out rc=0 apply_status=0
  local secret="$dest/.config/shell/secrets.env"
  local mutated_secret="$dest/.fixture-config/shell/secrets.env"
  local foreign_uv="$dest/.config/fish/conf.d/uv.env.fish"
  local foreign_csh="$dest/.cshrc"
  local foreign_tcsh="$dest/.tcshrc"
  local default_dest="$FIXTURE/default-home"
  local default_out="$FIXTURE/default-root.log"
  local default_source_root
  local hostile_source="$FIXTURE/-source root with 'single' \"double\" ; \$(touch $FIXTURE/source-dir-side-effect) \`touch $FIXTURE/source-dir-backtick-side-effect\` __DOTFILES_REPO_ROOT__ !"
  local hostile_source_dest="$FIXTURE/hostile-source-home"
  local hostile_source_root
  local policy policy_src policy_dest policy_log hostile_mutation
  local control_src="$FIXTURE/control-src"
  local control_dest="$FIXTURE/control-home"
  local control_root="$FIXTURE/root"$'\n'"bad"
  local caller_matrix_dir csh_matrix_dir csh_runtime="" tcsh_runtime=""
  local fish_status=0
  local -a hostile_mutations=(
    absolute absolute-backtick absolute-semicolon absolute-quote absolute-traversal
    absolute-double-dot absolute-double-slash absolute-duplicate home-relative
    home-relative-traversal home-relative-double-dot home-relative-double-slash
    linuxbrew-relative linuxbrew-system darwin-homebrew darwin-x86-homebrew xdg
    xdg-double-dot xdg-double-slash xdg-control profile-root profile-root-double-dot
    profile-root-double-slash profile-suffix profile-suffix-double-dot
    profile-suffix-double-slash state-relative state-relative-double-dot
    state-relative-double-slash alias-argv alias-name
  )

  mkdir -p "$FIXTURE/tmp" "$dest/.config/fish/conf.d" "$dest/.config/shell" "$dest/.fixture-bin" \
    "$dest/.nix-profile/bin" "$dest/.fixture-linuxbrew/sbin" "$dest/.fixture-linuxbrew/bin" \
    "$dest/.fixture-config/shell" "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" \
    "$FIXTURE/absolute-one" "$FIXTURE/absolute-two" "$FIXTURE/linuxbrew-system-sbin" "$FIXTURE/linuxbrew-system-bin"
  copy_source "$src"
  mutate_data "$src/home/.chezmoidata.toml"
  write_fakes
  assert_file_mode_portability
  assert_bash_version_probe_isolated
  print -r -- 'set -gx UV_FOREIGN_SENTINEL foreign' > "$foreign_uv"
  print -r -- 'foreign-csh' > "$foreign_csh"
  print -r -- 'foreign-tcsh' > "$foreign_tcsh"
  print -r -- 'export DOTFILES_FOREIGN_SECRET_SENTINEL=foreign-secret' > "$secret"
  chmod 600 "$secret"
  print -r -- 'export DOTFILES_FOREIGN_SECRET_SENTINEL=foreign-secret' > "$mutated_secret"
  chmod 640 "$mutated_secret"
  local secret_mode="$(file_mode "$secret")" secret_digest="$(file_digest "$secret")"
  local mutated_secret_mode="$(file_mode "$mutated_secret")" mutated_secret_digest="$(file_digest "$mutated_secret")"
  local foreign_uv_mode="$(file_mode "$foreign_uv")" foreign_uv_digest="$(file_digest "$foreign_uv")"
  local foreign_csh_mode="$(file_mode "$foreign_csh")" foreign_csh_digest="$(file_digest "$foreign_csh")"
  local foreign_tcsh_mode="$(file_mode "$foreign_tcsh")" foreign_tcsh_digest="$(file_digest "$foreign_tcsh")"

  if ! run_apply "$src" "$dest" "$src"; then
    emit_matrix_record "$(matrix_os_name)" chezmoi rendered-home FAIL required apply-failed
    return 1
  fi
  assert_rendered "$dest"
  assert_file "$dest/.config/fish/conf.d/zz-dotfiles.fish"
  assert_file "$dest/.config/shell/dotfiles-shell-common.csh"
  assert_not_exists "$dest/.chezmoidata.toml"
  [[ "$(file_mode "$secret")" == "$secret_mode" && "$(file_digest "$secret")" == "$secret_digest" ]] || fail 'foreign secret target changed'
  [[ "$(file_mode "$mutated_secret")" == "$mutated_secret_mode" && "$(file_digest "$mutated_secret")" == "$mutated_secret_digest" ]] || fail 'foreign mutated-config secret target changed'
  assert_file_content "$foreign_uv" 'set -gx UV_FOREIGN_SENTINEL foreign'
  assert_file_content "$foreign_csh" 'foreign-csh'
  assert_file_content "$foreign_tcsh" 'foreign-tcsh'
  [[ "$(file_mode "$foreign_uv")" == "$foreign_uv_mode" && "$(file_digest "$foreign_uv")" == "$foreign_uv_digest" ]] || fail 'foreign Fish file metadata changed'
  [[ "$(file_mode "$foreign_csh")" == "$foreign_csh_mode" && "$(file_digest "$foreign_csh")" == "$foreign_csh_digest" ]] || fail 'foreign csh file metadata changed'
  [[ "$(file_mode "$foreign_tcsh")" == "$foreign_tcsh_mode" && "$(file_digest "$foreign_tcsh")" == "$foreign_tcsh_digest" ]] || fail 'foreign tcsh file metadata changed'
  assert_not_exists "$dest/.config/fish/config.fish"
  run_mise_global_config_checks "$dest"

  mkdir -p "$default_dest/.config/shell"
  copy_source "$FIXTURE/default-source"
  mutate_data "$FIXTURE/default-source/home/.chezmoidata.toml"
  run_apply "$FIXTURE/default-source" "$default_dest" __NO_OVERRIDE__
  assert_rendered "$default_dest"
  default_source_root="$(cd "$FIXTURE/default-source" && pwd)"
  env -i HOME="$default_dest" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" USER=fixture-user \
    "$TEST_ZSH_BIN" -f -c '. "$HOME/.config/shell/dotfiles-shell-common.sh"; print -r -- "root=$DOTFILES_REPO_ROOT"' > "$default_out"
  assert_output_contains "$default_out" "root=$default_source_root"

  copy_source "$hostile_source"
  mutate_data "$hostile_source/home/.chezmoidata.toml"
  run_apply "$hostile_source" "$hostile_source_dest" __NO_OVERRIDE__
  assert_rendered "$hostile_source_dest" 1
  hostile_source_root="$(cd "$hostile_source" && pwd)"
  assert_not_exists "$FIXTURE/source-dir-side-effect"
  assert_not_exists "$FIXTURE/source-dir-backtick-side-effect"

  for policy in fish csh tcsh shim shim-extra shim-missing shim-reorder; do
    policy_src="$FIXTURE/policy-$policy-source"
    policy_dest="$FIXTURE/policy-$policy-home"
    policy_log="$FIXTURE/policy-$policy.log"
    copy_source "$policy_src"
    mutate_data "$policy_src/home/.chezmoidata.toml"
    mutate_policy "$policy_src/home/.chezmoidata.toml" "$policy"
    if run_apply "$policy_src" "$policy_dest" "$policy_src" > "$policy_log" 2>&1; then
      fail "unknown shell.mise policy must fail render: $policy"
    fi
  done
  for hostile_mutation in "${hostile_mutations[@]}"; do
    run_hostile_render_rejection "$hostile_mutation"
  done

  run_posix_render_matrix "$dest" "$src" "$hostile_source_dest" "$hostile_source_root" "$runtime_override" "$hostile_source_override" || rc=1

  resolve_shell_binary csh && csh_runtime="$REPLY"
  resolve_shell_binary tcsh && tcsh_runtime="$REPLY"
  csh_matrix_dir="$FIXTURE/csh-matrix-results"
  caller_matrix_dir="${MATRIX_RESULT_LOG_DIR:-}"
  MATRIX_RESULT_LOG_DIR="$csh_matrix_dir" run_csh_matrix "$csh_runtime" "$tcsh_runtime" "$dest" || rc=1
  assert_file "$csh_matrix_dir/matrix-results.log"
  if [[ -n "$caller_matrix_dir" ]]; then
    mkdir -p "$caller_matrix_dir"
    cat "$csh_matrix_dir/matrix-results.log" >> "$caller_matrix_dir/matrix-results.log"
  fi
  if [[ -n "$csh_runtime" && -n "$tcsh_runtime" ]] && shell_binaries_are_same "$csh_runtime" "$tcsh_runtime"; then
    assert_output_contains "$csh_matrix_dir/matrix-results.log" 'shell=csh|target=csh-command|status=PASS|requirement=required|reason=shared-csh-tcsh-binary'
    assert_output_contains "$csh_matrix_dir/matrix-results.log" 'shell=tcsh|target=tcsh-runtime|status=SKIP|requirement=not-applicable|reason=duplicate-csh-command-binary'
    assert_output_contains "$csh_matrix_dir/matrix-results.log" 'shell=csh|target=csh-runtime|status=SKIP|requirement=not-applicable|reason=genuine-csh-unverified'
    assert_not_contains "$csh_matrix_dir/matrix-results.log" 'shell=tcsh|target=tcsh-runtime|status=PASS'
    assert_not_contains "$csh_matrix_dir/matrix-results.log" 'shell=csh|target=csh-runtime|status=PASS'
  fi
  run_csh_distinct_binary_probe

  run_fish_render_checks "$dest" || fish_status=$?
  (( fish_status == 0 )) || rc=1
  run_macos_feature_checks || rc=1

  copy_source "$hostile_src"
  mutate_data "$hostile_src/home/.chezmoidata.toml"
  run_apply "$hostile_src" "$hostile_dest" "$hostile_root"
  assert_rendered "$hostile_dest" 1
  if select_bash 3; then
    bin="$REPLY"
  elif select_bash 5; then
    bin="$REPLY"
  else
    emit_matrix_record "$(matrix_os_name)" bash hostile-repo-root SKIP not-applicable no-supported-bash
    bin=""
  fi
  if [[ -n "$bin" ]]; then
    out="$FIXTURE/hostile.log"
    if env -i HOME="$hostile_dest" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" USER=fixture-user \
      "$bin" --noprofile --norc -c '. "$HOME/.config/shell/dotfiles-shell-common.sh"; printf "root=%s\n" "$DOTFILES_REPO_ROOT"' > "$out" 2>&1; then
      assert_output_has_token "$out" "root=$hostile_root" 'hostile repo-root shell quoting check failed'
      assert_not_exists "$FIXTURE/side-effect"
      assert_not_exists "$FIXTURE/backtick-side-effect"
      emit_matrix_record "$(matrix_os_name)" bash hostile-repo-root PASS required shell-quoted-assignment
    else
      emit_matrix_record "$(matrix_os_name)" bash hostile-repo-root FAIL required shell-smoke-failed
      rc=1
    fi
  fi

  copy_source "$control_src"
  mutate_data "$control_src/home/.chezmoidata.toml"
  if run_apply "$control_src" "$control_dest" "$control_root" > "$FIXTURE/control-render.log" 2>&1; then
    fail 'control-byte repo root should fail closed during render'
  fi
  assert_not_exists "$control_dest/.config/shell/dotfiles-shell-common.sh"

  if (( rc == 0 )); then
    print -r -- 'multi-shell render/runtime checks passed'
  else
    print -u2 -r -- 'multi-shell render/runtime checks failed'
  fi
  return "$rc"
}

main() {
  local rc=0

  parse_selector "$@" || return
  make_temp_dir multi-shell-config
  FIXTURE="${REPLY:A}"
  trap '[[ -n "${FIXTURE:-}" ]] && rm -rf -- "$FIXTURE"' EXIT HUP INT TERM
  case "$SELECTOR" in
    source)
      test_source
      if (( SKIP_CHEZMOI )); then
        emit_matrix_record "$(matrix_os_name)" chezmoi chezmoi-source SKIP not-applicable chezmoi-skipped
        print -r -- 'multi-shell source checks passed'
        return 0
      fi
      if ! resolve_chezmoi; then
        emit_matrix_record "$(matrix_os_name)" chezmoi chezmoi-source SKIP not-applicable chezmoi-unavailable
        print -r -- 'multi-shell source checks passed'
        return 0
      fi
      CHEZMOI_BIN="$REPLY"
      run_source || rc=$?
      if (( rc == 0 )); then
        print -r -- 'multi-shell source checks passed'
      else
        print -u2 -r -- 'multi-shell source checks failed'
      fi
      return "$rc"
      ;;
    render)
      if ! resolve_chezmoi; then
        emit_matrix_record "$(matrix_os_name)" chezmoi multi-shell SKIP required chezmoi-unavailable
        print -u2 -r -- 'multi-shell render/runtime checks failed'
        return 1
      fi
      CHEZMOI_BIN="$REPLY"
      run_render
      ;;
  esac
}

main "$@"
