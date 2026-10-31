run_macos_feature_probe() {
  local shell_kind="$1" shell_bin="$2" home="$3" out="$4" rc=0
  local -a probe_env=(
    "HOME=$home" "USER=fixture-user"
    "PATH=$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin"
  )

  case "$shell_kind" in
    bash)
      env -i "${probe_env[@]}" "$shell_bin" --noprofile --norc -c '
        . "$HOME/.config/shell/dotfiles-shell-common.sh" || exit $?
        printf "editor=%s\npath=%s\n" "$EDITOR" "$PATH"
        alias intel arm 2>/dev/null || true
      ' > "$out" 2>&1 || rc=$?
      ;;
    zsh)
      env -i "${probe_env[@]}" "$shell_bin" -f -c '
        source "$HOME/.config/shell/dotfiles-shell-common.sh" || exit $?
        print -r -- "editor=$EDITOR" "path=$PATH"
        alias intel arm 2>/dev/null || true
      ' > "$out" 2>&1 || rc=$?
      ;;
    fish)
      env -i "${probe_env[@]}" "$shell_bin" --no-config -c '
        source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"; or exit $status
        printf "editor=%s\npath=%s\n" "$EDITOR" (string join : $PATH)
      ' > "$out" 2>&1 || rc=$?
      ;;
    csh|tcsh)
      env -i "${probe_env[@]}" "$shell_bin" -f -c '
        source "$HOME/.config/shell/dotfiles-shell-common.csh"
        if ( $status != 0 ) exit $status
        echo "editor=$EDITOR"
        printenv PATH
      ' > "$out" 2>&1 || rc=$?
      ;;
    *) fail "unsupported feature probe shell: $shell_kind" ;;
  esac
  return "$rc"
}

run_macos_feature_checks() {
  local src="$FIXTURE/features-source" dest="$FIXTURE/features-home"
  local os enabled shell_kind shell_bin label out matrix_status=0
  local matrix_os="$(matrix_os_name)"
  local -a shells=("zsh:$TEST_ZSH_BIN")

  for label in 3 5; do
    if select_bash "$label"; then
      shells+=("bash:$REPLY")
    elif [[ "$label" == 3 ]] && ! is_test_macos; then
      emit_matrix_record "$matrix_os" bash3 macos-features SKIP not-applicable macos-only
    else
      emit_matrix_record "$matrix_os" "bash$label" macos-features SKIP required "bash$label-unavailable"
      matrix_status=1
    fi
  done
  for shell_kind in fish csh tcsh; do
    if resolve_shell_binary "$shell_kind"; then
      shells+=("$shell_kind:$REPLY")
    fi
  done
  copy_source "$src"
  mutate_data "$src/home/.chezmoidata.toml"
  mkdir -p "$dest/.fixture-bin" "$dest/.fixture-linuxbrew/bin" "$dest/.fixture-linuxbrew/sbin"
  for os in Darwin Linux; do
    {
      print -r -- '#!/bin/sh'
      print -r -- "case \"\$*\" in -s) printf '%s\\n' '$os' ;; -m) printf '%s\\n' arm64 ;; *) exit 97 ;; esac"
    } > "$FAKE_BIN/uname"
    chmod +x "$FAKE_BIN/uname"
    for enabled in false true; do
      "$TEST_PYTHON_BIN" - "$src/home/.chezmoidata.toml" "$enabled" <<'PY'
from pathlib import Path
import re
import sys

path, enabled = Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
if re.search(r"^macos = (true|false)$", text, re.M):
    text = re.sub(r"^macos = (true|false)$", f"macos = {enabled}", text, flags=re.M)
else:
    text += f"\n[features]\nmacos = {enabled}\n"
path.write_text(text)
PY
      run_apply "$src" "$dest" "$src" || fail 'feature fixture render failed'
      for label in "${shells[@]}"; do
        shell_kind="${label%%:*}"
        shell_bin="${label#*:}"
        out="$FIXTURE/features-$os-$enabled-$shell_kind-${shell_bin:t}.log"
        if ! run_macos_feature_probe "$shell_kind" "$shell_bin" "$dest" "$out"; then
          sed -n '1,70p' "$out" >&2
          emit_matrix_record "$matrix_os" "$shell_kind" "features-$os-$enabled" FAIL required runtime-smoke-failed
          matrix_status=1
          continue
        fi
        assert_output_contains "$out" 'editor=fixture-editor'
        assert_output_contains "$out" "$dest/.fixture-bin"
        if [[ "$os" == Darwin && "$enabled" == true ]]; then
          assert_output_contains "$out" "$FIXTURE/homebrew-bin"
          assert_output_contains "$out" "$FIXTURE/homebrew-sbin"
        else
          assert_not_contains "$out" "$FIXTURE/homebrew-bin"
          assert_not_contains "$out" "$FIXTURE/homebrew-sbin"
        fi
        if [[ "$os" == Linux ]]; then
          assert_output_contains "$out" "$dest/.fixture-linuxbrew/bin"
        else
          assert_not_contains "$out" "$dest/.fixture-linuxbrew/bin"
        fi
        if [[ "$shell_kind" == bash || "$shell_kind" == zsh ]]; then
          if [[ "$os" == Darwin && "$enabled" == true ]]; then
            assert_output_contains "$out" '/usr/bin/arch -x86_64'
            assert_output_contains "$out" '/usr/bin/arch -arm64'
          else
            assert_not_contains "$out" '/usr/bin/arch '
          fi
        fi
      done
    done
  done
  rm "$FAKE_BIN/uname"
  if (( matrix_status == 0 )); then
    print -r -- 'macOS feature on/off checks passed (simulated Darwin/Linux)'
  else
    print -u2 -r -- 'macOS feature on/off checks failed (simulated Darwin/Linux)'
  fi
  run_feature_flag_rejection_checks
  return "$matrix_status"
}

run_feature_flag_rejection_checks() {
  local mode src dest out nix_bin="$(command -v nix-instantiate 2>/dev/null || true)"

  for mode in missing string number extra; do
    src="$FIXTURE/features-$mode-source"
    dest="$FIXTURE/features-$mode-home"
    out="$FIXTURE/features-$mode.log"
    copy_source "$src"
    mkdir -p "$src/config/nix"
    cp "$REPO_ROOT/config/nix/features.nix" "$src/config/nix/"
    "$TEST_PYTHON_BIN" - "$src/home/.chezmoidata.toml" "$mode" <<'PY'
from pathlib import Path
import re
import sys

path, mode = Path(sys.argv[1]), sys.argv[2]
replacement = {
    "missing": "",
    "string": 'macos = "false"',
    "number": "macos = 0",
    "extra": "macos = false\nunknown = true",
}[mode]
text, count = re.subn(r"^macos = (true|false)$", lambda match: replacement, path.read_text(), flags=re.M)
if count != 1:
    raise SystemExit("feature mutation target missing")
path.write_text(text)
PY
    if run_apply "$src" "$dest" "$src" > "$out" 2>&1; then
      fail "malformed feature flag must fail render: $mode"
    fi
    assert_not_exists "$dest/.config/shell/dotfiles-shell-common.sh"
    if [[ "$nix_bin" == /* && -x "$nix_bin" ]]; then
      if env -i HOME="$dest" PATH="${nix_bin:h}:/usr/bin:/bin" \
        "$nix_bin" --eval --json --strict --expr "import \"$src/config/nix/features.nix\"" > "$out" 2>&1; then
        fail "malformed feature flag must fail Nix evaluation: $mode"
      fi
      assert_output_contains "$out" 'assertion'
    fi
  done
  if [[ "$nix_bin" != /* || ! -x "$nix_bin" ]]; then
    print -r -- 'SKIP: nix-instantiate unavailable for feature flag rejection checks'
  fi
  print -r -- 'renderer rejected malformed feature flags'
}
