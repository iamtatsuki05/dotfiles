#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"
readonly TEST_PYTHON_BIN="${DOTFILES_TEST_PYTHON:-python3}"

source "$TEST_DIR/lib/assertions.sh"

typeset -g SELECTOR=""
typeset -g SKIP_CHEZMOI=0
typeset -g CHEZMOI_BIN=""
typeset -g FIXTURE=""
typeset -g FAKE_BIN=""
typeset -g GCLOUD_LOG=""
typeset -g SECRET_MODE=""
typeset -g SECRET_DIGEST=""

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

matrix_os_name() {
  [[ "$OSTYPE" == darwin* ]] && print -r -- macos || print -r -- linux
}

emit_matrix_result() {
  local line="$1"
  print -r -- "$line"
  if [[ -n "${MATRIX_RESULT_LOG_DIR:-}" ]]; then
    mkdir -p "$MATRIX_RESULT_LOG_DIR"
    print -r -- "$line" >> "$MATRIX_RESULT_LOG_DIR/matrix-results.log"
  fi
}

file_digest() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else sha256sum "$1" | awk '{print $1}'; fi
}

bash_major() {
  /usr/bin/env -i HOME="$FIXTURE/bash-version-home" PATH="/bin:/usr/bin:/usr/sbin:/sbin" \
    "$1" --noprofile --norc -c 'printf "%s\n" "${BASH_VERSINFO[0]}"'
}

assert_bash_version_probe_isolated() {
  local startup="$FIXTURE/bash-version-startup" marker="$FIXTURE/bash-version-startup-marker" out="$FIXTURE/bash-version.log" rc=0
  print -r -- ": > ${(qqq)marker}" > "$startup"
  BASH_ENV="$startup" ENV="$startup" CDPATH="$marker" bash_major /bin/bash > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'isolated Bash version probe failed'
  grep -Eq '^[35]$' "$out" || fail 'Bash version probe did not report a supported major version'
  assert_not_exists "$marker"
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

validate_data() {
  local data_path="${1:-$REPO_ROOT/home/.chezmoidata.toml}"
  local nix_path="${2:-$REPO_ROOT/config/nix/home-manager/session.nix}"
  "$TEST_PYTHON_BIN" - "$data_path" "$nix_path" <<'PY'
import re, sys, tomllib
from pathlib import Path

SAFE_XDG = r"^\.[A-Za-z0-9._/-]+$"
SAFE_RELATIVE_PATH = r"^[A-Za-z0-9._/-]+$"
SAFE_ABSOLUTE_PATH = r"^/[A-Za-z0-9._/-]+$"

def safe_path_value(value, pattern, relative=False):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        return False
    if any(fragment in value for fragment in ("..", "//", ":")):
        return False
    return not relative or not value.startswith(("/", "~"))

try:
    data = tomllib.loads(Path(sys.argv[1]).read_text())
except Exception as exc:
    raise SystemExit("invalid canonical TOML: " + type(exc).__name__)
if set(data) != {"shell"}:
    raise SystemExit("unexpected top-level data key")
s = data["shell"]
if set(s) != {"editor", "xdg", "path", "aliases", "mise"}:
    raise SystemExit("unexpected shell data key")
if not isinstance(s["editor"], str) or s["editor"] != "nvim" or not re.fullmatch(r"[A-Za-z0-9._+-]+", s["editor"]):
    raise SystemExit("unsafe editor")
if set(s["xdg"]) != {"config", "cache", "data", "state"}:
    raise SystemExit("unexpected XDG key")
for value in s["xdg"].values():
    if not safe_path_value(value, SAFE_XDG):
        raise SystemExit("unsafe XDG suffix")
p = s["path"]
required = {"home_relative", "darwin_homebrew", "linuxbrew_user_relative", "linuxbrew_system", "user_profile_root", "user_profile_suffix", "absolute", "state_relative"}
if set(p) != required:
    raise SystemExit("unexpected PATH key")
for key in ("home_relative", "darwin_homebrew", "linuxbrew_user_relative", "linuxbrew_system", "absolute"):
    if not isinstance(p[key], list) or len(p[key]) != 2 or any(not isinstance(value, str) for value in p[key]) or len(p[key]) != len(set(p[key])) or not p[key]:
        raise SystemExit("malformed PATH list")
    for value in p[key]:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise SystemExit("PATH control byte")
        if key in ("home_relative", "linuxbrew_user_relative") and not safe_path_value(value, SAFE_RELATIVE_PATH, relative=True):
            raise SystemExit("unsafe home-relative PATH")
        if key not in ("home_relative", "linuxbrew_user_relative") and not safe_path_value(value, SAFE_ABSOLUTE_PATH):
            raise SystemExit("absolute PATH is not absolute")
if not safe_path_value(p["user_profile_root"], SAFE_ABSOLUTE_PATH):
    raise SystemExit("unsafe user profile root")
if not safe_path_value(p["user_profile_suffix"], SAFE_RELATIVE_PATH, relative=True):
    raise SystemExit("unsafe user profile suffix")
if not safe_path_value(p["state_relative"], SAFE_RELATIVE_PATH, relative=True):
    raise SystemExit("unsafe state-relative PATH")
if p["user_profile_root"] != "/etc/profiles/per-user" or p["user_profile_suffix"] != "bin" or p["state_relative"] != "nix/profile/bin":
    raise SystemExit("PATH mapping changed")
if s["aliases"] != {"ginit": ["gcloud", "init"], "gauth": ["gcloud", "auth", "login"], "gls": ["gcloud", "compute", "instances", "list"]}:
    raise SystemExit("aliases changed")
for name, argv in s["aliases"].items():
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) or not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._+%:@/-]+", value) for value in argv):
        raise SystemExit("unsafe alias name or argv")
if s["mise"] != {"fish": "interactive-only", "csh": "unsupported-activation", "tcsh": "unsupported-activation", "shim_root_precedence": ["MISE_DATA_DIR", "XDG_DATA_HOME", "HOME"]}:
    raise SystemExit("mise policy changed")
nix = Path(sys.argv[2]).read_text()
for key, suffix in [("EDITOR", None), ("XDG_CONFIG_HOME", s["xdg"]["config"]), ("XDG_CACHE_HOME", s["xdg"]["cache"]), ("XDG_DATA_HOME", s["xdg"]["data"]), ("XDG_STATE_HOME", s["xdg"]["state"])]:
    expected = 'EDITOR = "nvim";' if key == "EDITOR" else key + ' = "$' + '{homeDirectory}/' + suffix + '";'
    if expected not in nix:
        raise SystemExit("Nix parity missing: " + key)
PY
}

test_source() {
  local common="$REPO_ROOT/home/.chezmoitemplates/dotfiles-shell-common.sh"
  local wrapper="$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl"
  assert_file "$REPO_ROOT/home/.chezmoidata.toml"
  assert_file "$common"
  assert_file "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl"
  assert_file "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl"
  assert_same_file "$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl" "$common"
  assert_contains "$common" 'if [ -z "${DOTFILES_REPO_ROOT:-}" ]; then'
  assert_not_contains "$common" '__DOTFILES_REPO_ROOT__'
  assert_contains "$common" 'DOTFILES_REPO_ROOT={{ $dotfilesRepoRoot.prequoted }}'
  assert_contains "$common" 'export DOTFILES_REPO_ROOT'
  assert_contains "$wrapper" '{{- $dotfilesRepoRoot := dict "raw" $repoRoot "prequoted" (shellQuote $repoRoot) -}}'
  assert_contains "$wrapper" '{{- $shellCommonContext := dict "shell" .shell "dotfilesRepoRoot" $dotfilesRepoRoot -}}'
  assert_contains "$wrapper" '{{- includeTemplate ".chezmoitemplates/dotfiles-shell-common.sh" $shellCommonContext -}}'
  assert_not_contains "$wrapper" 'includeTemplate ".chezmoitemplates/dotfiles-shell-common.sh" .'
  assert_not_contains "$common" '.chezmoi'
  local root_context_line shell_context_line
  root_context_line="$(grep -F 'dict "raw" $repoRoot "prequoted" (shellQuote $repoRoot)' "$wrapper")"
  shell_context_line="$(grep -F 'dict "shell" .shell "dotfilesRepoRoot" $dotfilesRepoRoot' "$wrapper")"
  [[ "$root_context_line" == '{{- $dotfilesRepoRoot := dict "raw" $repoRoot "prequoted" (shellQuote $repoRoot) -}}' ]] \
    || fail 'repo-root context must contain only raw and prequoted data'
  [[ "$shell_context_line" == '{{- $shellCommonContext := dict "shell" .shell "dotfilesRepoRoot" $dotfilesRepoRoot -}}' ]] \
    || fail 'shell common context must contain only shell and repo-root data'
  for native_template in "$common" "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl"; do
    assert_contains "$native_template" '$safeXdgPath := "^\\.[A-Za-z0-9._/-]+$"'
    assert_contains "$native_template" '$safeRelativePath := "^[A-Za-z0-9._/-]+$"'
    assert_contains "$native_template" '$safeAbsolutePath := "^/[A-Za-z0-9._/-]+$"'
    assert_contains "$native_template" 'contains ".."'
    assert_contains "$native_template" 'contains "//"'
  done
  assert_contains "$wrapper" 'regexMatch "[[:cntrl:]]" $repoRoot'
  assert_not_contains "$wrapper" 'replace "__DOTFILES_REPO_ROOT__"'
  [[ "$(grep -Fc 'shellQuote $repoRoot' "$wrapper")" -eq 1 ]] || fail 'repo root must be shell-quoted exactly once in the wrapper context'
  assert_not_contains "$wrapper" '{{ include ".chezmoitemplates/dotfiles-shell-common.sh"'
  assert_contains "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" 'status is-interactive'
  assert_contains "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" 'DOTFILES_MISE_ACTIVATE_FISH_FAILED'
  assert_contains "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" 'return $dotfiles_mise_activation_status'
  assert_contains "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" 'mise activate fish'
  assert_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl" '$path:q'
  assert_not_contains "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" 'secrets.env'
  assert_not_contains "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" 'DOTFILES_REPO_ROOT'
  assert_not_contains "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" 'eval '
  assert_not_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl" 'secrets.env'
  assert_not_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl" 'DOTFILES_REPO_ROOT'
  assert_not_contains "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl" 'eval '
  assert_not_exists "$REPO_ROOT/home/dot_cshrc"
  assert_not_exists "$REPO_ROOT/home/dot_tcshrc"
  assert_not_exists "$REPO_ROOT/home/private_dot_config/fish/config.fish"
  local multi_shell_test="$REPO_ROOT/tests/test_multi_shell_config.sh"
  assert_contains "$multi_shell_test" '"$fish_bin" --no-config -c'
  assert_contains "$multi_shell_test" '"$fish_bin" --no-config -i -c'
  assert_contains "$multi_shell_test" 'source "$HOME/.config/fish/conf.d/uv.env.fish"'
  assert_contains "$multi_shell_test" 'source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"'
  for fish_env_name in HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME MISE_DATA_DIR TMPDIR SHELL PATH; do
    assert_contains "$multi_shell_test" "$fish_env_name="
  done
  validate_data
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/unknown.toml"
  print -r -- '[shell.unknown]' >> "$FIXTURE/unknown.toml"
  print -r -- 'value = "rejected"' >> "$FIXTURE/unknown.toml"
  if validate_data "$FIXTURE/unknown.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" >/dev/null 2>&1; then
    fail "unknown canonical data fields must be rejected"
  fi
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/unsafe.toml"
  "$TEST_PYTHON_BIN" - "$FIXTURE/unsafe.toml" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
path.write_text(path.read_text().replace('config = ".config"', 'config = "../escape"', 1))
PY
  if validate_data "$FIXTURE/unsafe.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" >/dev/null 2>&1; then
    fail "unsafe canonical path must be rejected"
  fi
  local mutation out sentinel
  for mutation in duplicate duplicate-alias secret command absolute tilde colon empty control; do
    out="$FIXTURE/schema-$mutation.log"
    sentinel="schema-$mutation-sentinel"
    cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/schema-$mutation.toml"
    "$TEST_PYTHON_BIN" - "$FIXTURE/schema-$mutation.toml" "$mutation" "$sentinel" <<'PY'
from pathlib import Path
import sys

path, mutation, sentinel = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
if mutation == "duplicate":
    text = text.replace('editor = "nvim"\n', 'editor = "nvim"\neditor = "duplicate"\n', 1)
elif mutation == "duplicate-alias":
    text = text.replace('ginit = ["gcloud", "init"]\n', 'ginit = ["gcloud", "init"]\nginit = ["gcloud", "duplicate"]\n', 1)
elif mutation == "secret":
    text += f'\n[shell.secret] \nvalue = "{sentinel}"\n'
elif mutation == "command":
    text = text.replace('editor = "nvim"', f'editor = "$(touch {sentinel})"', 1)
elif mutation == "absolute":
    text = text.replace('home_relative = [".local/bin", ".nix-profile/bin"]', 'home_relative = ["/absolute", ".nix-profile/bin"]', 1)
elif mutation == "tilde":
    text = text.replace('home_relative = [".local/bin", ".nix-profile/bin"]', 'home_relative = ["~/.local/bin", ".nix-profile/bin"]', 1)
elif mutation == "colon":
    text = text.replace('home_relative = [".local/bin", ".nix-profile/bin"]', 'home_relative = [".local/bin:bad", ".nix-profile/bin"]', 1)
elif mutation == "empty":
    text = text.replace('home_relative = [".local/bin", ".nix-profile/bin"]', 'home_relative = ["", ".nix-profile/bin"]', 1)
elif mutation == "control":
    text = text.replace('config = ".config"', 'config = "\\u0001bad"', 1)
else:
    raise SystemExit("unknown test mutation")
path.write_text(text)
PY
    if validate_data "$FIXTURE/schema-$mutation.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" > "$out" 2>&1; then
      fail "canonical schema mutation must be rejected: $mutation"
    fi
    assert_not_contains "$out" "$sentinel"
  done
  for mutation in xdg-double-dot xdg-double-slash absolute-double-dot absolute-double-slash home-relative-double-dot home-relative-double-slash profile-root-double-dot profile-root-double-slash profile-suffix-double-dot profile-suffix-double-slash state-relative-double-dot state-relative-double-slash; do
    out="$FIXTURE/schema-$mutation.log"
    sentinel="schema-$mutation-sentinel"
    cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/schema-$mutation.toml"
    mutate_hostile_data "$FIXTURE/schema-$mutation.toml" "$mutation" "$sentinel"
    if validate_data "$FIXTURE/schema-$mutation.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" > "$out" 2>&1; then
      fail "source path contract mutation must be rejected: $mutation"
    fi
    assert_not_contains "$out" "$sentinel"
  done
  for key in home_relative darwin_homebrew linuxbrew_user_relative linuxbrew_system absolute; do
    for length in short long; do
      out="$FIXTURE/schema-path-$key-$length.log"
      cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/schema-path-$key-$length.toml"
      "$TEST_PYTHON_BIN" - "$FIXTURE/schema-path-$key-$length.toml" "$key" "$length" <<'PY'
from pathlib import Path
import sys

path, key, length = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
old_values = {
    "home_relative": '[".local/bin", ".nix-profile/bin"]',
    "darwin_homebrew": '["/opt/homebrew/sbin", "/opt/homebrew/bin"]',
    "linuxbrew_user_relative": '[".linuxbrew/sbin", ".linuxbrew/bin"]',
    "linuxbrew_system": '["/home/linuxbrew/.linuxbrew/sbin", "/home/linuxbrew/.linuxbrew/bin"]',
    "absolute": '["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]',
}
old = f"{key} = {old_values[key]}"
if length == "short":
    new = old[:old.rfind(",")] + "]"
else:
    new = old[:-1] + ', "/extra"]'
if text.count(old) != 1:
    raise SystemExit("PATH mutation target missing")
path.write_text(text.replace(old, new, 1))
PY
      if validate_data "$FIXTURE/schema-path-$key-$length.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" > "$out" 2>&1; then
        fail "canonical PATH list length must be rejected: $key/$length"
      fi
    done
  done
  local policy
  for policy in shim-extra shim-missing shim-reorder; do
    out="$FIXTURE/schema-$policy.log"
    cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/schema-$policy.toml"
    mutate_policy "$FIXTURE/schema-$policy.toml" "$policy"
    if validate_data "$FIXTURE/schema-$policy.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" > "$out" 2>&1; then
      fail "shim_root_precedence shape must be rejected by source validator: $policy"
    fi
  done
}

copy_source() {
  local root="$1"
  mkdir -p "$root/home/.chezmoitemplates" "$root/home/private_dot_config/shell" "$root/home/private_dot_config/fish/conf.d"
  cp "$REPO_ROOT/.chezmoiroot" "$root/.chezmoiroot"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$root/home/.chezmoidata.toml"
  cp "$REPO_ROOT/home/.chezmoitemplates/bash_profile" "$root/home/.chezmoitemplates/bash_profile"
  cp "$REPO_ROOT/home/.chezmoitemplates/bashrc" "$root/home/.chezmoitemplates/bashrc"
  cp "$REPO_ROOT/home/.chezmoitemplates/dotfiles-shell-common.sh" "$root/home/.chezmoitemplates/dotfiles-shell-common.sh"
  cp "$REPO_ROOT/home/dot_bash_profile.tmpl" "$root/home/dot_bash_profile.tmpl"
  cp "$REPO_ROOT/home/dot_bashrc.tmpl" "$root/home/dot_bashrc.tmpl"
  cp "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl" "$root/home/private_dot_config/shell/"
  cp "$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl" "$root/home/private_dot_config/shell/"
  cp "$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl" "$root/home/private_dot_config/fish/conf.d/"
  cp "$REPO_ROOT/home/private_dot_config/shell/create_private_secrets.env" "$root/home/private_dot_config/shell/"
}

mutate_data() {
  "$TEST_PYTHON_BIN" - "$1" "$FIXTURE" <<'PY'
from pathlib import Path
import sys
path, fixture = Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
for old, new in {
    'editor = "nvim"': 'editor = "fixture-editor"',
    'config = ".config"': 'config = ".fixture-config"',
    'cache = ".cache"': 'cache = ".fixture-cache"',
    'data = ".local/share"': 'data = ".fixture-data"',
    'state = ".local/state"': 'state = ".fixture-state"',
    'home_relative = [".local/bin", ".nix-profile/bin"]': 'home_relative = [".fixture-bin", ".nix-profile/bin"]',
    'darwin_homebrew = ["/opt/homebrew/sbin", "/opt/homebrew/bin"]': f'darwin_homebrew = ["{fixture}/homebrew-sbin", "{fixture}/homebrew-bin"]',
    'absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]': f'absolute = ["{fixture}/absolute-one", "{fixture}/absolute-two"]',
}.items():
    if text.count(old) != 1:
        raise SystemExit("canonical mutation target missing")
    text = text.replace(old, new)
path.write_text(text)
PY
}

mutate_policy() {
  "$TEST_PYTHON_BIN" - "$1" "$2" <<'PY'
from pathlib import Path
import sys

path, mutation = Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
replacements = {
    "fish": ('fish = "interactive-only"', 'fish = "invalid-policy"'),
    "csh": ('\ncsh = "unsupported-activation"', '\ncsh = "invalid-policy"'),
    "tcsh": ('tcsh = "unsupported-activation"', 'tcsh = "invalid-policy"'),
    "shim": ('shim_root_precedence = ["MISE_DATA_DIR", "XDG_DATA_HOME", "HOME"]', 'shim_root_precedence = ["INVALID", "XDG_DATA_HOME", "HOME"]'),
    "shim-extra": ('shim_root_precedence = ["MISE_DATA_DIR", "XDG_DATA_HOME", "HOME"]', 'shim_root_precedence = ["MISE_DATA_DIR", "XDG_DATA_HOME", "HOME", "EXTRA"]'),
    "shim-missing": ('shim_root_precedence = ["MISE_DATA_DIR", "XDG_DATA_HOME", "HOME"]', 'shim_root_precedence = ["MISE_DATA_DIR", "XDG_DATA_HOME"]'),
    "shim-reorder": ('shim_root_precedence = ["MISE_DATA_DIR", "XDG_DATA_HOME", "HOME"]', 'shim_root_precedence = ["MISE_DATA_DIR", "HOME", "XDG_DATA_HOME"]'),
}
old, new = replacements[mutation]
if text.count(old) != 1:
    raise SystemExit("canonical policy mutation target missing")
path.write_text(text.replace(old, new, 1))
PY
}

mutate_hostile_data() {
  "$TEST_PYTHON_BIN" - "$1" "$2" "$3" <<'PY'
from pathlib import Path
import sys

path, mutation, marker = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
replacements = {
    "absolute": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', f'absolute = ["/tmp/$(touch {marker})", "/nix/var/nix/profiles/default/bin"]'),
    "absolute-backtick": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', f'absolute = ["/tmp/`touch {marker}`", "/nix/var/nix/profiles/default/bin"]'),
    "absolute-semicolon": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', f'absolute = ["/tmp/bad;touch {marker}", "/nix/var/nix/profiles/default/bin"]'),
    "absolute-quote": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', 'absolute = ["/tmp/\'bad", "/nix/var/nix/profiles/default/bin"]'),
    "absolute-traversal": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', 'absolute = ["/tmp/../escape", "/nix/var/nix/profiles/default/bin"]'),
    "absolute-double-dot": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', 'absolute = ["/tmp/foo..bar", "/nix/var/nix/profiles/default/bin"]'),
    "absolute-double-slash": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', 'absolute = ["/tmp/foo//bar", "/nix/var/nix/profiles/default/bin"]'),
    "absolute-duplicate": ('absolute = ["/run/current-system/sw/bin", "/nix/var/nix/profiles/default/bin"]', 'absolute = ["/run/current-system/sw/bin", "/run/current-system/sw/bin"]'),
    "home-relative": ('home_relative = [".local/bin", ".nix-profile/bin"]', f'home_relative = [".local/$(touch {marker})", ".nix-profile/bin"]'),
    "home-relative-traversal": ('home_relative = [".local/bin", ".nix-profile/bin"]', 'home_relative = ["../escape", ".nix-profile/bin"]'),
    "home-relative-double-dot": ('home_relative = [".local/bin", ".nix-profile/bin"]', 'home_relative = [".local/foo..bar", ".nix-profile/bin"]'),
    "home-relative-double-slash": ('home_relative = [".local/bin", ".nix-profile/bin"]', 'home_relative = [".local//bin", ".nix-profile/bin"]'),
    "linuxbrew-relative": ('linuxbrew_user_relative = [".linuxbrew/sbin", ".linuxbrew/bin"]', f'linuxbrew_user_relative = [".linuxbrew/$(touch {marker})", ".linuxbrew/bin"]'),
    "linuxbrew-system": ('linuxbrew_system = ["/home/linuxbrew/.linuxbrew/sbin", "/home/linuxbrew/.linuxbrew/bin"]', f'linuxbrew_system = ["/home/linuxbrew/.linuxbrew/$(touch {marker})", "/home/linuxbrew/.linuxbrew/bin"]'),
    "darwin-homebrew": ('darwin_homebrew = ["/opt/homebrew/sbin", "/opt/homebrew/bin"]', f'darwin_homebrew = ["/opt/homebrew/$(touch {marker})", "/opt/homebrew/bin"]'),
    "xdg": ('config = ".config"', f'config = ".$(touch {marker})"'),
    "xdg-control": ('config = ".config"', 'config = "\\u0001bad"'),
    "profile-root": ('user_profile_root = "/etc/profiles/per-user"', f'user_profile_root = "/etc/profiles/$(touch {marker})"'),
    "profile-root-double-dot": ('user_profile_root = "/etc/profiles/per-user"', 'user_profile_root = "/etc/profiles/foo..bar"'),
    "profile-root-double-slash": ('user_profile_root = "/etc/profiles/per-user"', 'user_profile_root = "/etc/profiles//foo"'),
    "profile-suffix": ('user_profile_suffix = "bin"', f'user_profile_suffix = "$(touch {marker})"'),
    "profile-suffix-double-dot": ('user_profile_suffix = "bin"', 'user_profile_suffix = "foo..bar"'),
    "profile-suffix-double-slash": ('user_profile_suffix = "bin"', 'user_profile_suffix = "foo//bar"'),
    "state-relative": ('state_relative = "nix/profile/bin"', f'state_relative = "nix/$(touch {marker})"'),
    "state-relative-double-dot": ('state_relative = "nix/profile/bin"', 'state_relative = "nix/foo..bar"'),
    "state-relative-double-slash": ('state_relative = "nix/profile/bin"', 'state_relative = "nix//profile/bin"'),
    "xdg-double-dot": ('config = ".config"', 'config = ".config..bad"'),
    "xdg-double-slash": ('config = ".config"', 'config = ".config//bad"'),
    "alias-argv": ('ginit = ["gcloud", "init"]', f'ginit = ["gcloud", "$(touch {marker})"]'),
    "alias-name": ('ginit = ["gcloud", "init"]', '"bad;name" = ["gcloud", "init"]'),
}
old, new = replacements[mutation]
if text.count(old) != 1:
    raise SystemExit(f"hostile mutation target missing: {mutation}")
path.write_text(text.replace(old, new, 1))
PY
}

run_hostile_render_rejection() {
  local mutation="$1" src="$FIXTURE/hostile-$1-source" dest="$FIXTURE/hostile-$1-home" marker="$FIXTURE/hostile-$1-marker" log="$FIXTURE/hostile-$1-render.log"
  copy_source "$src"
  mutate_hostile_data "$src/home/.chezmoidata.toml" "$mutation" "$marker"
  if run_apply "$src" "$dest" "$src" > "$log" 2>&1; then
    fail "hostile canonical data must fail before render: $mutation"
  fi
  assert_not_exists "$dest/.config/shell/dotfiles-shell-common.sh"
  assert_not_exists "$marker"
  assert_output_lacks_token "$log" "$marker" 'hostile canonical data leaked its marker into render diagnostics'
}

write_fakes() {
  FAKE_BIN="$FIXTURE/bin"
  GCLOUD_LOG="$FIXTURE/gcloud.log"
  mkdir -p "$FAKE_BIN"
  : > "$GCLOUD_LOG"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- "printf '%s\\n' \"\$*\" >> \"$GCLOUD_LOG\""
    print -r -- 'case "$*" in "init"|"auth login"|"compute instances list") exit 0 ;; *) exit 97 ;; esac'
  } > "$FAKE_BIN/gcloud"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- 'if [ "${1:-}" != activate ]; then exit 97; fi'
    print -r -- "if [ \"\$2\" = bash ]; then printf '%s\\n' 'activate bash' >> \"$FIXTURE/mise.log\"; printf '%s\\n' 'DOTFILES_BASH_ACTIVATED=yes'; exit 0; fi"
    print -r -- 'if [ "$2" != fish ]; then exit 97; fi'
    print -r -- "printf '%s\\n' 'activate fish' >> \"$FIXTURE/mise.log\""
    print -r -- 'case "${FAKE_MISE_MODE:-success}" in'
    print -r -- 'success) printf "%s\n" "function dotfiles_fake_activation" "    set -gx DOTFILES_FISH_ACTIVATED yes" "end" ;;'
    print -r -- 'failure) exit 19 ;; empty) ;; invalid) printf "%s\\n" "if" ;; *) exit 98 ;; esac'
  } > "$FAKE_BIN/mise"
  chmod +x "$FAKE_BIN/gcloud" "$FAKE_BIN/mise"
  : > "$FIXTURE/mise.log"
}

run_apply() {
  local src="$1" dest="$2" root="$3" rc=0 config="$FIXTURE/chezmoi-$(basename "$dest").toml" log="$FIXTURE/chezmoi-$(basename "$dest").log"
  local -a root_env=("DOTFILES_REPO_ROOT=$root")
  [[ "$root" == __NO_OVERRIDE__ ]] && root_env=()
  : > "$config"
  mkdir -p "$dest"
  env -i HOME="$dest" XDG_CONFIG_HOME="$dest/.config" XDG_CACHE_HOME="$dest/.cache" XDG_DATA_HOME="$dest/.local/share" XDG_STATE_HOME="$dest/.local/state" MISE_DATA_DIR="$dest/.local/share/mise" TMPDIR="$FIXTURE/tmp" PATH="/bin:/usr/bin:/usr/sbin:/sbin" "${root_env[@]}" \
    "$CHEZMOI_BIN" -S "$src" -D "$dest" --cache "$FIXTURE/cache-$(basename "$dest")" --config "$config" --persistent-state "$FIXTURE/state-$(basename "$dest").boltdb" --refresh-externals=never --force --no-tty apply > "$log" 2>&1 || rc=$?
  (( rc == 0 )) || { sed -n '1,100p' "$log" >&2; return "$rc"; }
}

assert_rendered() {
  local dest="$1" allow_repo_marker="${2:-0}" common="$dest/.config/shell/dotfiles-shell-common.sh" fish="$dest/.config/fish/conf.d/zz-dotfiles.fish" csh="$dest/.config/shell/dotfiles-shell-common.csh"
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

run_bash_probe() {
  local bin="$1" home="$2" out="$3" rc=0
  env -i HOME="$home" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$bin" USER=fixture-user "$bin" --noprofile --norc -c '
    shopt -s expand_aliases
    . "$HOME/.bash_profile"; . "$HOME/.bash_profile"
    ginit; gauth; gls
    printf "shell=%s\nroot=%s\neditor=%s\nconfig=%s\ncache=%s\ndata=%s\nstate=%s\nsecret=%s\nbash_activated=%s\npath=%s\n" "$dotfiles_shell_name" "$DOTFILES_REPO_ROOT" "$EDITOR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "${DOTFILES_FOREIGN_SECRET_SENTINEL:-missing}" "${DOTFILES_BASH_ACTIVATED:-missing}" "$PATH"
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

assert_shell_output() {
  local out="$1" home="$2" shell_kind="${3:-bash}"
  assert_output_contains "$out" 'editor=fixture-editor'
  assert_output_contains "$out" 'secret=foreign-secret'
  if [[ "$shell_kind" == bash ]]; then
    assert_output_contains "$out" 'bash_activated=yes'
  fi
  assert_output_contains "$out" "config=$home/.fixture-config"
  assert_output_contains "$out" "cache=$home/.fixture-cache"
  assert_output_contains "$out" "data=$home/.fixture-data"
  assert_output_contains "$out" "state=$home/.fixture-state"
  assert_output_contains "$out" 'gt=function gr=function gs=function'
  assert_output_contains "$out" "$home/.fixture-bin"
}

assert_posix_path_order() {
  local out="$1" home="$2" fixture_root="$3" path_line candidate count
  local -a expected_candidates=(
    "$fixture_root/absolute-two"
    "$fixture_root/absolute-one"
    "$home/.nix-profile/bin"
    "$home/.fixture-bin"
    "$fixture_root/homebrew-bin"
    "$fixture_root/homebrew-sbin"
  )
  path_line="$(grep '^path=' "$out" | sed 's/^path=//')"
  [[ "$path_line" == *"$fixture_root/absolute-two:$fixture_root/absolute-one:$home/.nix-profile/bin:$home/.fixture-bin:$fixture_root/homebrew-bin:$fixture_root/homebrew-sbin:"* ]] \
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
    fail "gcloud alias argv changed"
  fi
}

assert_bash_activation() {
  local out="$1" activation_count
  assert_output_contains "$out" 'bash_activated=yes'
  activation_count="$(grep -c '^activate bash$' "$FIXTURE/mise.log" 2>/dev/null || true)"
  (( activation_count > 0 )) || fail 'Bash common did not invoke official mise activation'
  [[ "$(grep -c '^activate fish$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 0 ]] || fail 'Bash common invoked Fish mise activation'
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
        emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=chezmoi-source|status=FAIL|requirement=required|reason=runtime-smoke-failed"
        matrix_status=1
        continue
      fi
      assert_shell_output "$out" "$dest"
      assert_bash_activation "$out"
      assert_output_contains "$out" "root=$src"
      assert_posix_path_order "$out" "$dest" "$FIXTURE"
      assert_alias_log
      emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=chezmoi-source|status=PASS|requirement=required|reason=rendered-artifact"
    elif [[ "$expected_major" == 3 && "$OSTYPE" != darwin* ]]; then
      emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash3|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=macos-only"
    else
      emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=chezmoi-source|status=SKIP|requirement=required|reason=bash${expected_major}-unavailable"
      matrix_status=1
    fi
  done

  if [[ ! -x "$TEST_ZSH_BIN" || ! -f "$TEST_ZSH_BIN" ]]; then
    emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=zsh|target=chezmoi-source|status=SKIP|requirement=required|reason=zsh-unavailable"
    return 1
  fi

  out="$FIXTURE/source-zsh.log"
  : > "$GCLOUD_LOG"
  smoke_status=0
  run_zsh_probe "$dest" "$out" || smoke_status=$?
  if (( smoke_status != 0 )); then
    emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=zsh|target=chezmoi-source|status=FAIL|requirement=required|reason=runtime-smoke-failed"
    matrix_status=1
  else
    assert_shell_output "$out" "$dest" zsh
    assert_output_contains "$out" "root=$src"
    assert_posix_path_order "$out" "$dest" "$FIXTURE"
    assert_alias_log
    emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=zsh|target=chezmoi-source|status=PASS|requirement=required|reason=rendered-artifact"
  fi

  return "$matrix_status"
}

run_source() {
  local src="$FIXTURE/source-state-source" dest="$FIXTURE/source-state-home" matrix_os="$(matrix_os_name)" rc=0
  mkdir -p "$FIXTURE/tmp" "$dest/.config/shell" "$dest/.fixture-config/shell" "$dest/.fixture-bin" "$dest/.nix-profile/bin" \
    "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" "$FIXTURE/absolute-one" "$FIXTURE/absolute-two"
  copy_source "$src"
  mutate_data "$src/home/.chezmoidata.toml"
  write_fakes
  print -r -- 'export DOTFILES_FOREIGN_SECRET_SENTINEL=foreign-secret' > "$dest/.fixture-config/shell/secrets.env"
  chmod 600 "$dest/.fixture-config/shell/secrets.env"
  if ! run_apply "$src" "$dest" "$src"; then
    emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=chezmoi|target=chezmoi-source|status=FAIL|requirement=required|reason=apply-failed"
    return 1
  fi
  assert_rendered "$dest" 1
  emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=chezmoi|target=chezmoi-source|status=PASS|requirement=required|reason=rendered-artifact"
  run_source_matrix "$src" "$dest" || rc=$?
  return "$rc"
}

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
      if ( $?DOTFILES_FOREIGN_SECRET_SENTINEL ) then
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
  assert_output_contains "$out" "editor=fixture-editor"
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
  local out="$1" home="$2"
  assert_output_contains "$out" 'path_count=12'
  assert_output_contains "$out" "path[1]=$FIXTURE/mise data/shims"
  assert_output_contains "$out" "path[2]=$FIXTURE/absolute-two"
  assert_output_contains "$out" "path[3]=$FIXTURE/absolute-one"
  assert_output_contains "$out" "path[4]=$home/.nix-profile/bin"
  assert_output_contains "$out" "path[5]=$home/.fixture-bin"
  assert_output_contains "$out" "path[6]=$FIXTURE/homebrew-bin"
  assert_output_contains "$out" "path[7]=$FIXTURE/homebrew-sbin"
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

  rm -rf -- "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" "$FIXTURE/absolute-one" "$FIXTURE/absolute-two"
  out="$FIXTURE/csh-shims-all-missing.log"
  env -i HOME="$all_missing_home" PATH="$base_path" SHELL="$shell_bin" \
    MISE_DATA_DIR= XDG_DATA_HOME="$all_missing_xdg" DOTFILES_CSH_ADAPTER="$home/.config/shell/dotfiles-shell-common.csh" \
    "$shell_bin" -f -c 'if ( $?USER ) unsetenv USER; if ( $?LOGNAME ) unsetenv LOGNAME; source "$DOTFILES_CSH_ADAPTER"; source "$DOTFILES_CSH_ADAPTER"; echo "data=$XDG_DATA_HOME"; echo "path=$PATH"' > "$out" 2>&1 || rc=$?
  (( rc == 0 )) || fail 'csh all shim candidates missing smoke failed'
  assert_output_contains "$out" "data=$all_missing_xdg"
  assert_output_contains "$out" "path=$base_path"
  assert_not_contains "$out" "$all_missing_xdg/mise/shims"
  assert_not_contains "$out" "$all_missing_home/.fixture-data/mise/shims"
  mkdir -p "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" "$FIXTURE/absolute-one" "$FIXTURE/absolute-two"

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

run_csh_checks() {
  local shell_bin="$1" home="$2"
  run_csh_shared_smoke "$shell_bin" "$home"
  run_csh_shim_precedence_smoke "$shell_bin" "$home"
  run_csh_user_cases_smoke "$shell_bin" "$home"
}

emit_csh_matrix_skip() {
  local shell_name="$1" target="$2" reason="$3"
  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=$shell_name|target=$target|status=SKIP|requirement=not-applicable|reason=$reason"
}

run_csh_matrix_for() {
  local shell_name="$1" shell_bin="$2" home="$3" target="$4" reason="${5:-runtime-smoke}" result_status=0
  if ( run_csh_checks "$shell_bin" "$home" ); then
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=$shell_name|target=$target|status=PASS|requirement=required|reason=$reason"
  else
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=$shell_name|target=$target|status=FAIL|requirement=required|reason=runtime-smoke-failed"
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

run_render() {
  local src="$FIXTURE/source" dest="$FIXTURE/home" hostile_src="$FIXTURE/hostile-src" hostile_dest="$FIXTURE/hostile-home"
  local hostile_root="-hostile root __DOTFILES_REPO_ROOT__ with 'single' \"double\" ; echo BAD \$(touch $FIXTURE/side-effect) \`touch $FIXTURE/backtick-side-effect\` ! \\"
  local runtime_override="-runtime root with 'single' \"double\" ; \$(touch $FIXTURE/runtime-side-effect) \`touch $FIXTURE/runtime-backtick-side-effect\` ! \\"
  local bin out rc=0 secret="$dest/.config/shell/secrets.env" mutated_secret="$dest/.fixture-config/shell/secrets.env"
  local foreign_uv="$dest/.config/fish/conf.d/uv.env.fish" foreign_csh="$dest/.cshrc" foreign_tcsh="$dest/.tcshrc"
  mkdir -p "$FIXTURE/tmp" "$dest/.config/fish/conf.d" "$dest/.config/shell" "$dest/.fixture-bin" "$dest/.nix-profile/bin" \
    "$dest/.fixture-config/shell" "$FIXTURE/homebrew-sbin" "$FIXTURE/homebrew-bin" "$FIXTURE/absolute-one" "$FIXTURE/absolute-two"
  copy_source "$src"; mutate_data "$src/home/.chezmoidata.toml"; write_fakes
  assert_file_mode_portability
  assert_bash_version_probe_isolated
  print -r -- 'set -gx UV_FOREIGN_SENTINEL foreign' > "$dest/.config/fish/conf.d/uv.env.fish"
  print -r -- 'foreign-csh' > "$dest/.cshrc"; print -r -- 'foreign-tcsh' > "$dest/.tcshrc"
  print -r -- 'export DOTFILES_FOREIGN_SECRET_SENTINEL=foreign-secret' > "$secret"; chmod 600 "$secret"
  print -r -- 'export DOTFILES_FOREIGN_SECRET_SENTINEL=foreign-secret' > "$mutated_secret"; chmod 640 "$mutated_secret"
  SECRET_MODE="$(file_mode "$secret")"; SECRET_DIGEST="$(file_digest "$secret")"
  local mutated_secret_mode="$(file_mode "$mutated_secret")" mutated_secret_digest="$(file_digest "$mutated_secret")"
  local foreign_uv_mode="$(file_mode "$foreign_uv")" foreign_uv_digest="$(file_digest "$foreign_uv")"
  local foreign_csh_mode="$(file_mode "$foreign_csh")" foreign_csh_digest="$(file_digest "$foreign_csh")"
  local foreign_tcsh_mode="$(file_mode "$foreign_tcsh")" foreign_tcsh_digest="$(file_digest "$foreign_tcsh")"
  run_apply "$src" "$dest" "$src"
  assert_rendered "$dest"; assert_file "$dest/.config/fish/conf.d/zz-dotfiles.fish"; assert_file "$dest/.config/shell/dotfiles-shell-common.csh"
  assert_not_exists "$dest/.chezmoidata.toml"
  [[ "$(file_mode "$secret")" == "$SECRET_MODE" && "$(file_digest "$secret")" == "$SECRET_DIGEST" ]] || fail "foreign secret target changed"
  [[ "$(file_mode "$mutated_secret")" == "$mutated_secret_mode" && "$(file_digest "$mutated_secret")" == "$mutated_secret_digest" ]] || fail "foreign mutated-config secret target changed"
  assert_file_content "$dest/.config/fish/conf.d/uv.env.fish" 'set -gx UV_FOREIGN_SENTINEL foreign'
  assert_file_content "$dest/.cshrc" 'foreign-csh'; assert_file_content "$dest/.tcshrc" 'foreign-tcsh'
  [[ "$(file_mode "$foreign_uv")" == "$foreign_uv_mode" && "$(file_digest "$foreign_uv")" == "$foreign_uv_digest" ]] || fail "foreign Fish file metadata changed"
  [[ "$(file_mode "$foreign_csh")" == "$foreign_csh_mode" && "$(file_digest "$foreign_csh")" == "$foreign_csh_digest" ]] || fail "foreign csh file metadata changed"
  [[ "$(file_mode "$foreign_tcsh")" == "$foreign_tcsh_mode" && "$(file_digest "$foreign_tcsh")" == "$foreign_tcsh_digest" ]] || fail "foreign tcsh file metadata changed"
  assert_not_exists "$dest/.config/fish/config.fish"
  local default_dest="$FIXTURE/default-home" default_out="$FIXTURE/default-root.log" default_source_root
  mkdir -p "$default_dest/.config/shell"
  copy_source "$FIXTURE/default-source"; mutate_data "$FIXTURE/default-source/home/.chezmoidata.toml"
  run_apply "$FIXTURE/default-source" "$default_dest" __NO_OVERRIDE__
  assert_rendered "$default_dest"
  default_source_root="$(cd "$FIXTURE/default-source/home" && pwd)"
  env -i HOME="$default_dest" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" USER=fixture-user \
    "$TEST_ZSH_BIN" -f -c '. "$HOME/.config/shell/dotfiles-shell-common.sh"; print -r -- "root=$DOTFILES_REPO_ROOT"' > "$default_out"
  assert_output_contains "$default_out" "root=$default_source_root"
  local hostile_source="$FIXTURE/-source root with 'single' \"double\" ; \$(touch $FIXTURE/source-dir-side-effect) \`touch $FIXTURE/source-dir-backtick-side-effect\` __DOTFILES_REPO_ROOT__ !"
  local hostile_source_dest="$FIXTURE/hostile-source-home" hostile_source_root
  local hostile_source_override="-override root with 'single' \"double\" ; \$(touch $FIXTURE/source-override-side-effect) \`touch $FIXTURE/source-override-backtick-side-effect\` __DOTFILES_REPO_ROOT__ ! \\"
  copy_source "$hostile_source"; mutate_data "$hostile_source/home/.chezmoidata.toml"; run_apply "$hostile_source" "$hostile_source_dest" __NO_OVERRIDE__
  assert_rendered "$hostile_source_dest" 1
  hostile_source_root="$(cd "$hostile_source/home" && pwd)"
  assert_not_exists "$FIXTURE/source-dir-side-effect"
  assert_not_exists "$FIXTURE/source-dir-backtick-side-effect"

  local policy policy_src policy_dest policy_log
  for policy in fish csh tcsh shim shim-extra shim-missing shim-reorder; do
    policy_src="$FIXTURE/policy-$policy-source"; policy_dest="$FIXTURE/policy-$policy-home"; policy_log="$FIXTURE/policy-$policy.log"
    copy_source "$policy_src"; mutate_data "$policy_src/home/.chezmoidata.toml"; mutate_policy "$policy_src/home/.chezmoidata.toml" "$policy"
    if run_apply "$policy_src" "$policy_dest" "$policy_src" > "$policy_log" 2>&1; then
      fail "unknown shell.mise policy must fail render: $policy"
    fi
  done
  local hostile_mutation
  for hostile_mutation in absolute absolute-backtick absolute-semicolon absolute-quote absolute-traversal absolute-double-dot absolute-double-slash absolute-duplicate home-relative home-relative-traversal home-relative-double-dot home-relative-double-slash linuxbrew-relative linuxbrew-system darwin-homebrew xdg xdg-double-dot xdg-double-slash xdg-control profile-root profile-root-double-dot profile-root-double-slash profile-suffix profile-suffix-double-dot profile-suffix-double-slash state-relative state-relative-double-dot state-relative-double-slash alias-argv alias-name; do
    run_hostile_render_rejection "$hostile_mutation"
  done
  if select_bash 3; then bin="$REPLY"; out="$FIXTURE/bash3.log"; : > "$GCLOUD_LOG"; : > "$FIXTURE/mise.log"; run_bash_probe "$bin" "$dest" "$out"; assert_shell_output "$out" "$dest"; assert_bash_activation "$out"; assert_output_contains "$out" "root=$src"; assert_posix_path_order "$out" "$dest" "$FIXTURE"; run_posix_env_policy_probe bash "$bin" "$dest" custom "$FIXTURE/bash3-custom.log"; assert_posix_env_policy "$FIXTURE/bash3-custom.log" "$dest" custom; run_posix_env_policy_probe bash "$bin" "$dest" empty "$FIXTURE/bash3-empty.log"; assert_posix_env_policy "$FIXTURE/bash3-empty.log" "$dest" empty; run_posix_root_policy_cases bash "$bin" "$dest" bash3 "$src" "$runtime_override" "$FIXTURE/runtime-side-effect" "$FIXTURE/runtime-backtick-side-effect"; run_posix_root_policy_cases bash "$bin" "$hostile_source_dest" bash3-hostile "$hostile_source_root" "$hostile_source_override" "$FIXTURE/source-override-side-effect" "$FIXTURE/source-override-backtick-side-effect"; assert_alias_log; emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash3|target=rendered-home|status=PASS|requirement=required|reason=rendered-common"
  elif [[ "$OSTYPE" != darwin* ]]; then emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash3|target=rendered-home|status=SKIP|requirement=not-applicable|reason=macos-only"
  else emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash3|target=rendered-home|status=SKIP|requirement=required|reason=bash3-unavailable"; rc=1; fi
  if select_bash 5; then bin="$REPLY"; out="$FIXTURE/bash5.log"; : > "$GCLOUD_LOG"; : > "$FIXTURE/mise.log"; run_bash_probe "$bin" "$dest" "$out"; assert_shell_output "$out" "$dest"; assert_bash_activation "$out"; assert_output_contains "$out" "root=$src"; assert_posix_path_order "$out" "$dest" "$FIXTURE"; run_posix_env_policy_probe bash "$bin" "$dest" custom "$FIXTURE/bash5-custom.log"; assert_posix_env_policy "$FIXTURE/bash5-custom.log" "$dest" custom; run_posix_env_policy_probe bash "$bin" "$dest" empty "$FIXTURE/bash5-empty.log"; assert_posix_env_policy "$FIXTURE/bash5-empty.log" "$dest" empty; run_posix_root_policy_cases bash "$bin" "$dest" bash5 "$src" "$runtime_override" "$FIXTURE/runtime-side-effect" "$FIXTURE/runtime-backtick-side-effect"; run_posix_root_policy_cases bash "$bin" "$hostile_source_dest" bash5-hostile "$hostile_source_root" "$hostile_source_override" "$FIXTURE/source-override-side-effect" "$FIXTURE/source-override-backtick-side-effect"; assert_alias_log; emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash5|target=rendered-home|status=PASS|requirement=required|reason=rendered-common"
  else emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash5|target=rendered-home|status=SKIP|requirement=required|reason=bash5-unavailable"; rc=1; fi
  [[ -x "$TEST_ZSH_BIN" ]] || fail "zsh unavailable"
  out="$FIXTURE/zsh.log"; : > "$GCLOUD_LOG"; run_zsh_probe "$dest" "$out"; assert_shell_output "$out" "$dest" zsh; assert_output_contains "$out" "root=$src"; assert_posix_path_order "$out" "$dest" "$FIXTURE"; run_posix_env_policy_probe zsh "$TEST_ZSH_BIN" "$dest" custom "$FIXTURE/zsh-custom.log"; assert_posix_env_policy "$FIXTURE/zsh-custom.log" "$dest" custom; run_posix_env_policy_probe zsh "$TEST_ZSH_BIN" "$dest" empty "$FIXTURE/zsh-empty.log"; assert_posix_env_policy "$FIXTURE/zsh-empty.log" "$dest" empty; run_posix_root_policy_cases zsh "$TEST_ZSH_BIN" "$dest" zsh "$src" "$runtime_override" "$FIXTURE/runtime-side-effect" "$FIXTURE/runtime-backtick-side-effect"; run_posix_root_policy_cases zsh "$TEST_ZSH_BIN" "$hostile_source_dest" zsh-hostile "$hostile_source_root" "$hostile_source_override" "$FIXTURE/source-override-side-effect" "$FIXTURE/source-override-backtick-side-effect"; assert_alias_log
  emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=zsh|target=rendered-home|status=PASS|requirement=required|reason=rendered-common"
  local csh_runtime="" tcsh_runtime=""
  resolve_shell_binary csh && csh_runtime="$REPLY"
  resolve_shell_binary tcsh && tcsh_runtime="$REPLY"
  local csh_matrix_dir="$FIXTURE/csh-matrix-results" caller_matrix_dir="${MATRIX_RESULT_LOG_DIR:-}"
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
  local fish_bin="" fish_noninteractive_status=0 fish_process_status=0 fish_activation_flag
  if resolve_fish; then
    fish_bin="$REPLY"
    out="$FIXTURE/fish-non-interactive.log"; : > "$FIXTURE/mise.log"; fish_noninteractive_status=0
    env -i HOME="$dest" XDG_CONFIG_HOME="$dest/.config" XDG_CACHE_HOME="$dest/.fixture-cache" XDG_DATA_HOME="$dest/.fixture-data" XDG_STATE_HOME="$dest/.fixture-state" MISE_DATA_DIR="$dest/.fixture-data/mise" TMPDIR="$FIXTURE/tmp" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$fish_bin" \
      "$fish_bin" --no-config -c '
      source "$HOME/.config/fish/conf.d/uv.env.fish"
      source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"
      echo "editor=$EDITOR"
      echo "config=$XDG_CONFIG_HOME"
      echo "cache=$XDG_CACHE_HOME"
      echo "data=$XDG_DATA_HOME"
      echo "state=$XDG_STATE_HOME"
      echo "foreign=$UV_FOREIGN_SENTINEL"
      echo "mise=(command -v mise)"
      if functions -q ginit; echo "ginit=present"; else; echo "ginit=absent"; end
      echo "activation_failed=$DOTFILES_MISE_ACTIVATE_FISH_FAILED"
      echo "path=$PATH"
    ' > "$out" 2>&1 || fish_noninteractive_status=$?
    (( fish_noninteractive_status == 0 )) || { sed -n '1,120p' "$out" >&2; fail 'Fish non-interactive runtime failed'; }
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
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=fish|target=non-interactive|status=PASS|requirement=required|reason=rendered-conf-d"
    : > "$FIXTURE/mise.log"; : > "$GCLOUD_LOG"; fish_process_status=0
    env -i HOME="$dest" XDG_CONFIG_HOME="$dest/.config" XDG_CACHE_HOME="$dest/.fixture-cache" XDG_DATA_HOME="$dest/.fixture-data" XDG_STATE_HOME="$dest/.fixture-state" MISE_DATA_DIR="$dest/.fixture-data/mise" TMPDIR="$FIXTURE/tmp" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$fish_bin" \
      "$fish_bin" --no-config -i -c 'source "$HOME/.config/fish/conf.d/uv.env.fish"; source "$HOME/.config/fish/conf.d/zz-dotfiles.fish"; ginit; gauth; gls; echo mise=(command -v mise); echo activated=$DOTFILES_FISH_ACTIVATED; echo activation_failed=$DOTFILES_MISE_ACTIVATE_FISH_FAILED; echo interactive=yes' > "$FIXTURE/fish-interactive.log" 2>&1 || fish_process_status=$?
    assert_output_contains "$FIXTURE/fish-interactive.log" 'interactive=yes'
    assert_output_contains "$FIXTURE/fish-interactive.log" "mise=$FAKE_BIN/mise"
    fish_activation_flag="$(grep '^activation_failed=' "$FIXTURE/fish-interactive.log" | sed 's/^activation_failed=//' | tail -1)"
    if [[ "$fish_activation_flag" == 1 ]]; then
      emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=fish|target=interactive|status=FAIL|requirement=required|reason=mise-activate-fish-failed"
      rc=1
    else
      (( fish_process_status == 0 )) || { sed -n '1,120p' "$FIXTURE/fish-interactive.log" >&2; fail 'Fish interactive runtime failed'; }
      assert_output_contains "$FIXTURE/fish-interactive.log" 'activated=yes'
      assert_output_contains "$FIXTURE/fish-interactive.log" 'activation_failed=0'
      [[ "$(grep -c '^activate fish$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 1 ]] || fail 'Fish activation count is not one'
      assert_alias_log
      emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=fish|target=interactive|status=PASS|requirement=required|reason=official-activation"
    fi
    local fish_failure_config="$FIXTURE/fish-failure-config" fish_failure_status fish_failure_stdout fish_failure_stderr
    mkdir -p "$fish_failure_config"
    for fish_mode in failure empty invalid; do
      fish_failure_stdout="$FIXTURE/fish-$fish_mode.stdout"; fish_failure_stderr="$FIXTURE/fish-$fish_mode.stderr"
      : > "$FIXTURE/mise.log"; fish_failure_status=0
      env -i HOME="$dest" XDG_CONFIG_HOME="$fish_failure_config" XDG_CACHE_HOME="$FIXTURE/fish-failure-cache" XDG_DATA_HOME="$FIXTURE/fish-failure-data" XDG_STATE_HOME="$FIXTURE/fish-failure-state" MISE_DATA_DIR="$FIXTURE/fish-failure-data/mise" TMPDIR="$FIXTURE/tmp" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" SHELL="$fish_bin" \
        DOTFILES_FISH_ADAPTER="$dest/.config/fish/conf.d/zz-dotfiles.fish" FAKE_MISE_MODE="$fish_mode" \
        "$fish_bin" --no-config -i -c 'source "$DOTFILES_FISH_ADAPTER"; set source_status $status; echo "source_status=$source_status"; echo "activation_failed=$DOTFILES_MISE_ACTIVATE_FISH_FAILED"; true; echo "after_status=$status"' > "$fish_failure_stdout" 2> "$fish_failure_stderr" || fish_failure_status=$?
      (( fish_failure_status == 0 )) || { sed -n '1,120p' "$fish_failure_stdout" >&2; sed -n '1,120p' "$fish_failure_stderr" >&2; fail "Fish $fish_mode failure probe child exited non-zero"; }
      assert_output_contains "$fish_failure_stdout" 'source_status=1'
      assert_output_contains "$fish_failure_stdout" 'activation_failed=1'
      assert_output_contains "$fish_failure_stdout" 'after_status=0'
      assert_output_contains "$fish_failure_stderr" 'dotfiles: mise activate fish failed'
      assert_output_lacks_token "$fish_failure_stdout" 'dotfiles: mise activate fish failed' 'Fish activation failure diagnostic must be emitted on stderr'
      [[ "$(grep -c '^activate fish$' "$FIXTURE/mise.log" 2>/dev/null || true)" -eq 1 ]] || fail "Fish $fish_mode activation count is not one"
    done
  else
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=fish|target=non-interactive|status=SKIP|requirement=not-applicable|reason=temporary-fish-runtime-unavailable"
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=fish|target=interactive|status=SKIP|requirement=not-applicable|reason=temporary-fish-runtime-unavailable"
  fi
  copy_source "$hostile_src"; mutate_data "$hostile_src/home/.chezmoidata.toml"; run_apply "$hostile_src" "$hostile_dest" "$hostile_root"
  assert_rendered "$hostile_dest" 1
  if select_bash 3; then
    bin="$REPLY"
  elif select_bash 5; then
    bin="$REPLY"
  else
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash|target=hostile-repo-root|status=SKIP|requirement=not-applicable|reason=no-supported-bash"
    bin=""
  fi
  if [[ -n "$bin" ]]; then
    out="$FIXTURE/hostile.log"; env -i HOME="$hostile_dest" PATH="$FAKE_BIN:/bin:/usr/bin:/usr/sbin:/sbin" USER=fixture-user "$bin" --noprofile --norc -c '. "$HOME/.config/shell/dotfiles-shell-common.sh"; printf "root=%s\n" "$DOTFILES_REPO_ROOT"' > "$out"
    assert_output_has_token "$out" "root=$hostile_root" 'hostile repo-root shell quoting check failed'; assert_not_exists "$FIXTURE/side-effect"
    assert_not_exists "$FIXTURE/backtick-side-effect"
    emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=bash|target=hostile-repo-root|status=PASS|requirement=required|reason=shell-quoted-assignment"
  fi
  local control_src="$FIXTURE/control-src" control_dest="$FIXTURE/control-home" control_root="$FIXTURE/root"$'\n'"bad"
  copy_source "$control_src"; mutate_data "$control_src/home/.chezmoidata.toml"
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
  parse_selector "$@" || return
  FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/multi-shell-config.XXXXXX")"
  FIXTURE="${FIXTURE:A}"
  trap '[[ -n "$FIXTURE" ]] && rm -rf -- "$FIXTURE"' EXIT HUP INT TERM
  case "$SELECTOR" in
    source)
      test_source
      if (( SKIP_CHEZMOI )); then
        emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=chezmoi|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=chezmoi-skipped"
        print -r -- 'multi-shell source checks passed'
        return 0
      fi
      if ! resolve_chezmoi; then
        emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=chezmoi|target=chezmoi-source|status=SKIP|requirement=not-applicable|reason=chezmoi-unavailable"
        print -r -- 'multi-shell source checks passed'
        return 0
      fi
      CHEZMOI_BIN="$REPLY"
      run_source
      print -r -- 'multi-shell source checks passed'
      ;;
    render)
      if ! resolve_chezmoi; then
        emit_matrix_result "MATRIX_RESULT|os=$(matrix_os_name)|shell=chezmoi|target=multi-shell|status=SKIP|requirement=required|reason=chezmoi-unavailable"
        return 1
      fi
      CHEZMOI_BIN="$REPLY"
      run_render
      ;;
  esac
}

main "$@"
