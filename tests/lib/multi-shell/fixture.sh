# Fixture assertions and hostile render helpers for the multi-shell tests.

assert_rendered() {
  local dest="$1"
  local allow_repo_marker="${2:-0}"
  local common="$dest/.config/shell/dotfiles-shell-common.sh"
  local fish="$dest/.config/fish/conf.d/zz-dotfiles.fish"
  local csh="$dest/.config/shell/dotfiles-shell-common.csh"

  assert_file "$common"
  assert_not_contains "$common" '{{'
  assert_not_contains "$common" '}}'
  if (( allow_repo_marker == 0 )); then
    assert_not_contains "$common" '__DOTFILES_REPO_ROOT__'
  fi
  assert_contains "$common" 'fixture-editor'
  assert_contains "$common" '$HOME/.fixture-bin'
  assert_contains "$common" '$HOME/.fixture-config'
  assert_contains "$common" '$HOME/.fixture-cache'
  assert_contains "$common" '$HOME/.fixture-data'
  assert_contains "$common" '$HOME/.fixture-state'
  assert_not_contains "$fish" '{{'
  assert_not_contains "$fish" '}}'
  assert_contains "$fish" 'fixture-editor'
  assert_contains "$fish" '$HOME/.fixture-config'
  assert_contains "$fish" '$HOME/.fixture-bin'
  assert_not_contains "$csh" '{{'
  assert_not_contains "$csh" '}}'
  assert_contains "$csh" 'fixture-editor'
  assert_contains "$csh" '$HOME/.fixture-config'
  assert_contains "$csh" '$HOME/.fixture-data/mise/shims'
  assert_contains "$csh" '$HOME/.fixture-bin'
}

assert_output_has_token() {
  local output_file="$1" expected="$2" failure="$3"

  grep -Fq -- "$expected" "$output_file" || fail "$failure"
}

assert_output_lacks_token() {
  local output_file="$1" unexpected="$2" failure="$3"

  ! grep -Fq -- "$unexpected" "$output_file" || fail "$failure"
}

run_hostile_render_rejection() {
  local mutation="$1"
  local src="$FIXTURE/hostile-$mutation-source"
  local dest="$FIXTURE/hostile-$mutation-home"
  local marker="$FIXTURE/hostile-$mutation-marker"
  local log="$FIXTURE/hostile-$mutation-render.log"

  copy_source "$src"
  mutate_hostile_data "$src/home/.chezmoidata.toml" "$mutation" "$marker"
  if run_apply "$src" "$dest" "$src" > "$log" 2>&1; then
    fail "hostile canonical data must fail before render: $mutation"
  fi
  assert_not_exists "$dest/.config/shell/dotfiles-shell-common.sh"
  assert_not_exists "$marker"
  assert_output_lacks_token "$log" "$marker" 'hostile canonical data leaked its marker into render diagnostics'
}

run_mise_global_config_shell_probe() {
  local shell_kind="$1" shell_bin="$2" home="$3" mode="$4" expected_global="$5" out="$6" err="$7" canonical_state="$8"
  local rc=0
  local -a env_args=(
    "HOME=$home"
    "USER=fixture-user"
    "XDG_CONFIG_HOME=$FIXTURE/custom mise xdg"
    "XDG_CACHE_HOME=$FIXTURE/mise-cache"
    "XDG_DATA_HOME=$FIXTURE/mise-data"
    "XDG_STATE_HOME=$FIXTURE/mise-state"
    "MISE_DATA_DIR=$FIXTURE/mise-data"
    "MISE_TRUSTED_CONFIG_PATHS=$home"
    "MISE_BIN=$MISE_REAL_BIN"
    "PATH=${MISE_REAL_BIN:h}:/bin:/usr/bin:/usr/sbin:/sbin"
  )

  case "$mode" in
    unset) ;;
    empty) env_args+=("MISE_GLOBAL_CONFIG_FILE=") ;;
    override) env_args+=("MISE_GLOBAL_CONFIG_FILE=$home/alternate-mise.toml") ;;
    *) fail "unknown MISE_GLOBAL_CONFIG_FILE mode: $mode" ;;
  esac
  case "$shell_kind" in
    bash)
      env -i "${env_args[@]}" "$shell_bin" --noprofile --norc -c '
        . "$HOME/.config/shell/dotfiles-shell-common.sh"
        printf "global=%s\n" "${MISE_GLOBAL_CONFIG_FILE:-unset}"
        "$MISE_BIN" config ls --json -C "$HOME/empty"
      ' > "$out" 2> "$err" || rc=$?
      ;;
    zsh)
      env -i "${env_args[@]}" "$shell_bin" -f -c '
        source "$HOME/.config/shell/dotfiles-shell-common.sh"
        print -r -- "global=${MISE_GLOBAL_CONFIG_FILE:-unset}"
        "$MISE_BIN" config ls --json -C "$HOME/empty"
      ' > "$out" 2> "$err" || rc=$?
      ;;
    fish)
      env -i "${env_args[@]}" "$shell_bin" --no-config -c '
        source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"
        if set -q MISE_GLOBAL_CONFIG_FILE
          echo "global=$MISE_GLOBAL_CONFIG_FILE"
        else
          echo global=unset
        end
        "$MISE_BIN" config ls --json -C "$HOME/empty"
      ' > "$out" 2> "$err" || rc=$?
      ;;
    csh|tcsh)
      env -i "${env_args[@]}" "$shell_bin" -f -c '
        source "$HOME/.config/shell/dotfiles-shell-common.csh"
        set df_mgc = "`printenv MISE_GLOBAL_CONFIG_FILE`"
        if ( "$df_mgc" != "" ) then
          echo "global=$df_mgc"
        else
          echo global=unset
        endif
        unset df_mgc
        "$MISE_BIN" config ls --json -C "$HOME/empty"
      ' > "$out" 2> "$err" || rc=$?
      ;;
    *)
      fail "unknown shell kind: $shell_kind"
      ;;
  esac
  if (( rc != 0 )); then
    sed -n '1,120p' "$out" >&2
    sed -n '1,120p' "$err" >&2
    fail "$shell_kind MISE_GLOBAL_CONFIG_FILE probe failed"
  fi
  assert_output_contains "$out" "global=$expected_global"
  "$TEST_PYTHON_BIN" - "$out" "$expected_global" "$canonical_state" "$home" <<'PY'
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
expected = sys.argv[2]
canonical_state = sys.argv[3]
home = sys.argv[4]
json_start = text.find("[")
if json_start < 0:
    raise SystemExit("mise config ls did not emit JSON")
entries = json.loads(text[json_start:])
paths = [entry.get("path") for entry in entries]
if expected in ("unset", ""):
    if paths:
        raise SystemExit("unset MISE_GLOBAL_CONFIG_FILE unexpectedly loaded a config")
elif canonical_state == "present" and expected == f"{home}/alternate-mise.toml":
    expected_paths = {f"{home}/.config/mise/config.toml", expected}
    if set(paths) != expected_paths:
        raise SystemExit(f"mise config ls selected {paths!r}, expected both canonical and explicit paths")
elif paths != [expected]:
    raise SystemExit(f"mise config ls selected {paths!r}, expected {[expected]!r}")
PY
}

run_mise_global_config_checks() {
  local source_home="$1"
  local mise_bin="$(command -v mise 2>/dev/null || true)"
  local probe_home="$FIXTURE/mise-routing-home"
  local canonical="$probe_home/.config/mise/config.toml"
  local alternate="$probe_home/alternate-mise.toml"
  local canonical_state expected_global shell_kind shell_bin canonical_mode mode out err
  local -a shells=()

  if [[ "$mise_bin" != /* || ! -x "$mise_bin" ]]; then
    print -r -- 'SKIP: real mise unavailable for MISE_GLOBAL_CONFIG_FILE routing checks'
    return 0
  fi
  MISE_REAL_BIN="$mise_bin"
  mkdir -p "$probe_home/.config/shell" "$probe_home/.config/fish/conf.d" "$probe_home/.config/mise" "$probe_home/empty"
  cp "$source_home/.config/shell/dotfiles-shell-common.sh" "$probe_home/.config/shell/dotfiles-shell-common.sh"
  cp "$source_home/.config/fish/conf.d/zz-dotfiles.fish" "$probe_home/.config/fish/conf.d/zz-dotfiles.fish"
  cp "$source_home/.config/shell/dotfiles-shell-common.csh" "$probe_home/.config/shell/dotfiles-shell-common.csh"
  {
    print -r -- '[tasks.shell-routing-probe]'
    print -r -- 'run = "true"'
  } > "$canonical"
  {
    print -r -- '[tasks.shell-routing-override]'
    print -r -- 'run = "true"'
  } > "$alternate"

  if select_bash 5; then
    shells+=("bash:$REPLY")
  elif select_bash 3; then
    shells+=("bash:$REPLY")
  fi
  shells+=("zsh:$TEST_ZSH_BIN")
  if resolve_fish; then
    shells+=("fish:$REPLY")
  fi
  if resolve_shell_binary csh; then
    shells+=("csh:$REPLY")
  fi
  if resolve_shell_binary tcsh; then
    shells+=("tcsh:$REPLY")
  fi
  (( ${#shells[@]} > 0 )) || fail 'no shell runtime available for MISE_GLOBAL_CONFIG_FILE routing checks'

  for canonical_state in present absent; do
    if [[ "$canonical_state" == present ]]; then
      {
        print -r -- '[tasks.shell-routing-probe]'
        print -r -- 'run = "true"'
      } > "$canonical"
    else
      rm -f -- "$canonical"
    fi
    for mode in unset empty override; do
      if [[ "$mode" == override ]]; then
        expected_global="$alternate"
      elif [[ "$canonical_state" == present ]]; then
        expected_global="$canonical"
      elif [[ "$mode" == empty ]]; then
        expected_global=""
      else
        expected_global=unset
      fi
      for shell_kind in "${shells[@]}"; do
        shell_bin="${shell_kind#*:}"
        shell_kind="${shell_kind%%:*}"
        out="$FIXTURE/mise-routing-$canonical_state-$mode-$shell_kind.log"
        err="$FIXTURE/mise-routing-$canonical_state-$mode-$shell_kind.err"
        run_mise_global_config_shell_probe "$shell_kind" "$shell_bin" "$probe_home" "$mode" "$expected_global" "$out" "$err" "$canonical_state"
      done
    done
  done
  print -r -- 'mise global config routing checks passed'
}
