# Canonical shell-data oracle and source/mutation checks.

require_python_tomllib() {
  "$TEST_PYTHON_BIN" -c 'import tomllib' >/dev/null 2>&1 \
    || fail "${TEST_PYTHON_BIN} must provide tomllib (Python >= 3.11 is required)"
}

validate_data() {
  local data_path="${1:-$REPO_ROOT/home/.chezmoidata.toml}"
  local nix_path="${2:-$REPO_ROOT/config/nix/home-manager/session.nix}"

  "$TEST_PYTHON_BIN" - "$data_path" "$nix_path" <<'PY'
import re
import sys
import tomllib
from pathlib import Path

SAFE_XDG = r"^\.[A-Za-z0-9._/-]+$"
SAFE_RELATIVE_PATH = r"^[A-Za-z0-9._/-]+$"
SAFE_ABSOLUTE_PATH = r"^/[A-Za-z0-9._/-]+$"
EXPECTED_PATH = {
    "home_local_bin": ".local/bin",
    "home_nix_profile_bin": ".nix-profile/bin",
    "darwin_arm64_homebrew_sbin": "/opt/homebrew/sbin",
    "darwin_arm64_homebrew_bin": "/opt/homebrew/bin",
    "darwin_x86_64_homebrew_sbin": "/usr/local/sbin",
    "darwin_x86_64_homebrew_bin": "/usr/local/bin",
    "linuxbrew_user_sbin": ".linuxbrew/sbin",
    "linuxbrew_user_bin": ".linuxbrew/bin",
    "linuxbrew_system_sbin": "/home/linuxbrew/.linuxbrew/sbin",
    "linuxbrew_system_bin": "/home/linuxbrew/.linuxbrew/bin",
    "nix_user_profile_root": "/etc/profiles/per-user",
    "nix_user_profile_bin": "bin",
    "nix_system_bin": "/run/current-system/sw/bin",
    "nix_default_bin": "/nix/var/nix/profiles/default/bin",
    "nix_state_relative": "nix/profile/bin",
}
RELATIVE_PATH_KEYS = {
    "home_local_bin",
    "home_nix_profile_bin",
    "linuxbrew_user_sbin",
    "linuxbrew_user_bin",
    "nix_user_profile_bin",
    "nix_state_relative",
}

try:
    data = tomllib.loads(Path(sys.argv[1]).read_text())
except Exception as exc:
    raise SystemExit("invalid canonical TOML: " + type(exc).__name__)

if set(data) != {"shell", "features"}:
    raise SystemExit("unexpected top-level data key")
features = data["features"]
if not isinstance(features, dict) or set(features) != {"macos"} or not isinstance(features["macos"], bool):
    raise SystemExit("invalid canonical feature flags")
s = data["shell"]
if set(s) != {"editor", "xdg", "path", "aliases", "mise"}:
    raise SystemExit("unexpected shell data key")
if not isinstance(s["editor"], str) or s["editor"] != "nvim" or not re.fullmatch(r"[A-Za-z0-9._+-]+", s["editor"]):
    raise SystemExit("unsafe editor")

if set(s["xdg"]) != {"config", "cache", "data", "state"}:
    raise SystemExit("unexpected XDG key")
for value in s["xdg"].values():
    if not isinstance(value, str) or not re.fullmatch(SAFE_XDG, value):
        raise SystemExit("unsafe XDG suffix")
    if any(fragment in value for fragment in ("..", "//", ":")):
        raise SystemExit("unsafe XDG suffix")

p = s["path"]
if set(p) != set(EXPECTED_PATH):
    raise SystemExit("unexpected named PATH key set")
for key, expected in EXPECTED_PATH.items():
    value = p[key]
    if not isinstance(value, str) or value != expected:
        raise SystemExit("PATH mapping changed: " + key)
    pattern = SAFE_RELATIVE_PATH if key in RELATIVE_PATH_KEYS else SAFE_ABSOLUTE_PATH
    if not re.fullmatch(pattern, value):
        raise SystemExit("unsafe PATH value: " + key)
    if any(fragment in value for fragment in ("..", "//", ":")):
        raise SystemExit("unsafe PATH value: " + key)
    if key in RELATIVE_PATH_KEYS and (value.startswith(("/", "~")) or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise SystemExit("unsafe relative PATH value: " + key)

if s["aliases"] != {"ginit": ["gcloud", "init"], "gauth": ["gcloud", "auth", "login"], "gls": ["gcloud", "compute", "instances", "list"]}:
    raise SystemExit("aliases changed")
for name, argv in s["aliases"].items():
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) or not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._+%:@/-]+", value) for value in argv):
        raise SystemExit("unsafe alias name or argv")

if s["mise"] != {"fish": "interactive-only", "csh": "unsupported-activation", "tcsh": "unsupported-activation", "shim_root_precedence": ["MISE_DATA_DIR", "XDG_DATA_HOME", "HOME"]}:
    raise SystemExit("mise policy changed")

PY
}

validate_nix_projection() {
  local nix_bin="$(command -v nix-instantiate 2>/dev/null || true)"
  local fixture_root="$FIXTURE/nix-projection"
  local canonical_root="$fixture_root/canonical"
  local mutated_root="$fixture_root/mutated"
  local canonical_data="$canonical_root/home/.chezmoidata.toml"
  local mutated_data="$mutated_root/home/.chezmoidata.toml"
  local canonical_home="$fixture_root/canonical-home"
  local mutated_home="$fixture_root/mutated-home"
  local canonical_session="$canonical_root/config/nix/home-manager/session.nix"
  local mutated_session="$mutated_root/config/nix/home-manager/session.nix"
  local canonical_output="$FIXTURE/nix-projection-canonical.json"
  local mutated_output="$FIXTURE/nix-projection-mutated.json"
  local expr rc=0

  if [[ "$nix_bin" != /* || ! -x "$nix_bin" ]]; then
    print -r -- 'SKIP: nix-instantiate unavailable for Nix shell-data projection evaluation'
    return 0
  fi
  mkdir -p "$canonical_root/home" "$canonical_root/config/nix/home-manager" \
    "$mutated_root/home" "$mutated_root/config/nix/home-manager" "$canonical_home" "$mutated_home"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$canonical_data"
  cp "$REPO_ROOT/config/nix/home-manager/session.nix" "$canonical_session"
  cp "$canonical_data" "$mutated_data"
  cp "$canonical_session" "$mutated_session"
  mutate_data "$mutated_data"

  expr="let session = import \"$canonical_session\" { homeDirectory = \"$canonical_home\"; }; in session.home.sessionVariables"
  env -i HOME="$fixture_root/nix-home" XDG_CONFIG_HOME="$fixture_root/nix-config" PATH="${nix_bin:h}:/usr/bin:/bin" \
    "$nix_bin" --eval --json --strict --expr "$expr" > "$canonical_output" 2>&1 || rc=$?
  if (( rc != 0 )); then
    sed -n '1,120p' "$canonical_output" >&2
    fail "Nix canonical shell-data projection evaluation failed: $rc"
  fi
  expr="let session = import \"$mutated_session\" { homeDirectory = \"$mutated_home\"; }; in session.home.sessionVariables"
  env -i HOME="$fixture_root/nix-home" XDG_CONFIG_HOME="$fixture_root/nix-config" PATH="${nix_bin:h}:/usr/bin:/bin" \
    "$nix_bin" --eval --json --strict --expr "$expr" > "$mutated_output" 2>&1 || rc=$?
  if (( rc != 0 )); then
    sed -n '1,120p' "$mutated_output" >&2
    fail "Nix mutated shell-data projection evaluation failed: $rc"
  fi

  "$TEST_PYTHON_BIN" - "$canonical_output" "$canonical_home" nvim .config .cache .local/share .local/state <<'PY'
import json
import sys
from pathlib import Path

actual = json.loads(Path(sys.argv[1]).read_text())
home, editor, config, cache, data, state = sys.argv[2:]
expected = {
    "EDITOR": editor,
    "XDG_CONFIG_HOME": f"{home}/{config}",
    "XDG_CACHE_HOME": f"{home}/{cache}",
    "XDG_DATA_HOME": f"{home}/{data}",
    "XDG_STATE_HOME": f"{home}/{state}",
}
if actual != expected:
    raise SystemExit(f"canonical Nix projection mismatch: {actual!r} != {expected!r}")
PY
  "$TEST_PYTHON_BIN" - "$mutated_output" "$mutated_home" fixture-editor .fixture-config .fixture-cache .fixture-data .fixture-state <<'PY'
import json
import sys
from pathlib import Path

actual = json.loads(Path(sys.argv[1]).read_text())
home, editor, config, cache, data, state = sys.argv[2:]
expected = {
    "EDITOR": editor,
    "XDG_CONFIG_HOME": f"{home}/{config}",
    "XDG_CACHE_HOME": f"{home}/{cache}",
    "XDG_DATA_HOME": f"{home}/{data}",
    "XDG_STATE_HOME": f"{home}/{state}",
}
if actual != expected:
    raise SystemExit(f"mutated Nix projection mismatch: {actual!r} != {expected!r}")
PY
}

validate_zsh_ui_completion() {
  local nix_bin="$(command -v nix-instantiate 2>/dev/null || true)"
  local ui_home="$FIXTURE/zsh-ui-home"
  local ui_source="$FIXTURE/zsh-ui-source"
  local ui_json="$FIXTURE/zsh-ui.json"
  local ui_script="$FIXTURE/zsh-ui.zsh"
  local out="$FIXTURE/zsh-ui.log"
  local expr enabled rc=0

  if [[ "$nix_bin" != /* || ! -x "$nix_bin" ]]; then
    print -r -- 'SKIP: nix-instantiate unavailable for Zsh UI completion evaluation'
    return 0
  fi
  mkdir -p "$ui_home/.ssh" "$ui_home/mac-arm-completions" "$ui_home/mac-intel-completions" \
    "$ui_home/.linuxbrew/share/zsh/site-functions" "$ui_home/bin" "$ui_source/config" "$ui_source/home"
  cp -R "$REPO_ROOT/config/nix" "$ui_source/config/"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$ui_source/home/"
  "$TEST_PYTHON_BIN" - "$ui_source/config/nix/home-manager/zsh.nix" "$ui_home" <<'PY'
from pathlib import Path
import sys

module, home = Path(sys.argv[1]), sys.argv[2]
text = module.read_text()
text = text.replace("/opt/homebrew/share/zsh/site-functions", f"{home}/mac-arm-completions")
text = text.replace("/usr/local/share/zsh/site-functions", f"{home}/mac-intel-completions")
module.write_text(text)
PY
  print -rl -- '#!/bin/sh' 'case "$*" in -s) echo Darwin ;; -m) echo arm64 ;; *) exit 97 ;; esac' > "$ui_home/bin/uname"
  chmod +x "$ui_home/bin/uname"
  print -r -- 'Host fixture-host' > "$ui_home/.ssh/config"
  expr="let ui = import \"$ui_source/config/nix/home-manager/zsh.nix\" {
    lib = {
      mkMerge = values: values;
      mkOrder = order: value: value;
      mkAfter = value: value;
      optionalString = condition: text: if condition then text else \"\";
    };
  }; in builtins.concatStringsSep \"\\n\" ui.programs.zsh.initContent"
  for enabled in false true; do
    "$TEST_PYTHON_BIN" - "$ui_source/home/.chezmoidata.toml" "$enabled" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
path.write_text(re.sub(r"^macos = (true|false)$", f"macos = {sys.argv[2]}", path.read_text(), flags=re.M))
PY
    env -i HOME="$ui_home" PATH="${nix_bin:h}:/usr/bin:/bin" \
      "$nix_bin" --eval --json --strict --expr "$expr" > "$ui_json"
    "$TEST_PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin))' < "$ui_json" > "$ui_script"
    env -i HOME="$ui_home" HOST=fixture-host PATH="$ui_home/bin:/usr/bin:/bin" DOTFILES_TEST_ZSH_INIT="$ui_script" \
      "$TEST_ZSH_BIN" -f -c '
        fpath=()
        source "$DOTFILES_TEST_ZSH_INIT"
        print -rl -- $fpath
        if (( ! $+functions[_ssh] )); then
          print -u2 -r -- "Zsh UI did not define SSH completion"
          exit 1
        fi
        compadd() { print -rl -- "$@"; }
        _ssh
      ' > "$out" 2>&1 || rc=$?
    if (( rc != 0 )); then
      sed -n '1,60p' "$out" >&2
      fail 'Zsh UI SSH completion probe failed'
    fi
    assert_output_contains "$out" 'fixture-host'
    assert_output_contains "$out" "$ui_home/.linuxbrew/share/zsh/site-functions"
    if [[ "$enabled" == true ]]; then
      assert_output_contains "$out" "$ui_home/mac-arm-completions"
      assert_output_contains "$out" "$ui_home/mac-intel-completions"
    else
      assert_not_contains "$out" "$ui_home/mac-arm-completions"
      assert_not_contains "$out" "$ui_home/mac-intel-completions"
    fi
  done
}

test_source() {
  local common="$REPO_ROOT/home/.chezmoitemplates/dotfiles-shell-common.sh"
  local wrapper="$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.sh.tmpl"
  local fish="$REPO_ROOT/home/private_dot_config/fish/conf.d/zz-dotfiles.fish.tmpl"
  local csh="$REPO_ROOT/home/private_dot_config/shell/dotfiles-shell-common.csh.tmpl"
  local validator="$REPO_ROOT/home/.chezmoitemplates/shell-data-validate"

  assert_file "$REPO_ROOT/home/.chezmoidata.toml"
  assert_file "$common"
  assert_file "$validator"
  assert_file "$fish"
  assert_file "$csh"
  assert_not_exists "$REPO_ROOT/config/shell/dotfiles-shell-common.tmpl"
  assert_contains "$common" 'if [ -z "${DOTFILES_REPO_ROOT:-}" ]; then'
  assert_not_contains "$common" '__DOTFILES_REPO_ROOT__'
  assert_contains "$common" 'DOTFILES_REPO_ROOT={{ $dotfilesRepoRoot.prequoted }}'
  assert_contains "$common" 'export DOTFILES_REPO_ROOT'
  assert_contains "$wrapper" '{{- $dotfilesRepoRoot := dict "raw" $repoRoot "prequoted" (shellQuote $repoRoot) -}}'
  assert_contains "$wrapper" '{{- $shellCommonContext := dict "shell" .shell "features" .features "dotfilesRepoRoot" $dotfilesRepoRoot -}}'
  assert_contains "$wrapper" 'includeTemplate ".chezmoitemplates/dotfiles-shell-common.sh" $shellCommonContext'
  assert_not_contains "$wrapper" 'includeTemplate ".chezmoitemplates/dotfiles-shell-common.sh" .'
  assert_not_contains "$common" '.chezmoi.sourceDir'
  assert_contains "$validator" 'home_local_bin'
  assert_contains "$validator" 'darwin_x86_64_homebrew_bin'
  assert_contains "$validator" 'nix_state_relative'
  for native_template in "$common" "$fish" "$csh"; do
    assert_contains "$native_template" 'shell-data-validate'
  done
  assert_contains "$wrapper" 'regexMatch "[[:cntrl:]]" $repoRoot'
  assert_not_contains "$wrapper" 'replace "__DOTFILES_REPO_ROOT__"'
  [[ "$(grep -Fc 'shellQuote $repoRoot' "$wrapper")" -eq 1 ]] || fail 'repo root must be shell-quoted exactly once in the wrapper context'
  assert_not_contains "$wrapper" '{{ include ".chezmoitemplates/dotfiles-shell-common.sh"'
  assert_contains "$fish" 'status is-interactive'
  assert_contains "$fish" 'DOTFILES_MISE_ACTIVATE_FISH_FAILED'
  assert_contains "$fish" 'return $dotfiles_mise_activation_status'
  assert_contains "$fish" 'mise activate fish'
  assert_contains "$csh" '$path:q'
  assert_not_contains "$fish" 'secrets.env'
  assert_not_contains "$fish" 'DOTFILES_REPO_ROOT'
  assert_not_contains "$fish" 'eval '
  assert_not_contains "$csh" 'secrets.env'
  assert_not_contains "$csh" 'DOTFILES_REPO_ROOT'
  assert_not_contains "$csh" 'eval '
  assert_not_exists "$REPO_ROOT/home/dot_cshrc"
  assert_not_exists "$REPO_ROOT/home/dot_tcshrc"
  assert_not_exists "$REPO_ROOT/home/private_dot_config/fish/config.fish"

  require_python_tomllib
  validate_data
  validate_nix_projection
  validate_zsh_ui_completion

  cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/unknown.toml"
  print -r -- '[shell.unknown]' >> "$FIXTURE/unknown.toml"
  print -r -- 'value = "rejected"' >> "$FIXTURE/unknown.toml"
  if validate_data "$FIXTURE/unknown.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" >/dev/null 2>&1; then
    fail 'unknown canonical data fields must be rejected'
  fi

  cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/unsafe.toml"
  "$TEST_PYTHON_BIN" - "$FIXTURE/unsafe.toml" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
path.write_text(path.read_text().replace('config = ".config"', 'config = "../escape"', 1))
PY
  if validate_data "$FIXTURE/unsafe.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" >/dev/null 2>&1; then
    fail 'unsafe canonical path must be rejected'
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
replacements = {
    "duplicate": ('editor = "nvim"\n', 'editor = "nvim"\neditor = "duplicate"\n'),
    "duplicate-alias": ('ginit = ["gcloud", "init"]\n', 'ginit = ["gcloud", "init"]\nginit = ["gcloud", "duplicate"]\n'),
    "command": ('editor = "nvim"', f'editor = "$(touch {sentinel})"'),
    "absolute": ('nix_system_bin = "/run/current-system/sw/bin"', 'nix_system_bin = "/absolute"'),
    "tilde": ('home_local_bin = ".local/bin"', 'home_local_bin = "~/.local/bin"'),
    "colon": ('home_local_bin = ".local/bin"', 'home_local_bin = ".local/bin:bad"'),
    "empty": ('home_local_bin = ".local/bin"', 'home_local_bin = ""'),
    "control": ('config = ".config"', 'config = "\\u0001bad"'),
}
if mutation == "secret":
    path.write_text(text + f'\n[shell.secret]\nvalue = "{sentinel}"\n')
    raise SystemExit(0)
old, new = replacements[mutation]
if text.count(old) != 1:
    raise SystemExit("canonical mutation target missing")
path.write_text(text.replace(old, new, 1))
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

  local path_key
  for path_key in home_local_bin home_nix_profile_bin darwin_arm64_homebrew_sbin darwin_arm64_homebrew_bin darwin_x86_64_homebrew_sbin darwin_x86_64_homebrew_bin linuxbrew_user_sbin linuxbrew_user_bin linuxbrew_system_sbin linuxbrew_system_bin nix_user_profile_root nix_user_profile_bin nix_system_bin nix_default_bin nix_state_relative; do
    for mutation in missing extra; do
      out="$FIXTURE/schema-path-$path_key-$mutation.log"
      cp "$REPO_ROOT/home/.chezmoidata.toml" "$FIXTURE/schema-path-$path_key-$mutation.toml"
      "$TEST_PYTHON_BIN" - "$FIXTURE/schema-path-$path_key-$mutation.toml" "$path_key" "$mutation" <<'PY'
from pathlib import Path
import sys

path, key, mutation = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
needle = next(line for line in text.splitlines(True) if line.startswith(key + " = "))
if mutation == "missing":
    text = text.replace(needle, "", 1)
else:
    text = text.replace(needle, needle + "duplicate_extra = \"extra\"\n", 1)
path.write_text(text)
PY
      if validate_data "$FIXTURE/schema-path-$path_key-$mutation.toml" "$REPO_ROOT/config/nix/home-manager/session.nix" > "$out" 2>&1; then
        fail "canonical named PATH key shape must be rejected: $path_key/$mutation"
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

mutate_data() {
  "$TEST_PYTHON_BIN" - "$1" "$FIXTURE" <<'PY'
from pathlib import Path
import sys

path, fixture = Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
replacements = {
    'editor = "nvim"': 'editor = "fixture-editor"',
    'config = ".config"': 'config = ".fixture-config"',
    'cache = ".cache"': 'cache = ".fixture-cache"',
    'data = ".local/share"': 'data = ".fixture-data"',
    'state = ".local/state"': 'state = ".fixture-state"',
    'home_local_bin = ".local/bin"': 'home_local_bin = ".fixture-bin"',
    'darwin_arm64_homebrew_sbin = "/opt/homebrew/sbin"': f'darwin_arm64_homebrew_sbin = "{fixture}/homebrew-sbin"',
    'darwin_arm64_homebrew_bin = "/opt/homebrew/bin"': f'darwin_arm64_homebrew_bin = "{fixture}/homebrew-bin"',
    'darwin_x86_64_homebrew_sbin = "/usr/local/sbin"': f'darwin_x86_64_homebrew_sbin = "{fixture}/homebrew-sbin"',
    'darwin_x86_64_homebrew_bin = "/usr/local/bin"': f'darwin_x86_64_homebrew_bin = "{fixture}/homebrew-bin"',
    'linuxbrew_user_sbin = ".linuxbrew/sbin"': 'linuxbrew_user_sbin = ".fixture-linuxbrew/sbin"',
    'linuxbrew_user_bin = ".linuxbrew/bin"': 'linuxbrew_user_bin = ".fixture-linuxbrew/bin"',
    'linuxbrew_system_sbin = "/home/linuxbrew/.linuxbrew/sbin"': f'linuxbrew_system_sbin = "{fixture}/linuxbrew-system-sbin"',
    'linuxbrew_system_bin = "/home/linuxbrew/.linuxbrew/bin"': f'linuxbrew_system_bin = "{fixture}/linuxbrew-system-bin"',
    'nix_user_profile_root = "/etc/profiles/per-user"': f'nix_user_profile_root = "{fixture}/profile-root"',
    'nix_system_bin = "/run/current-system/sw/bin"': f'nix_system_bin = "{fixture}/absolute-one"',
    'nix_default_bin = "/nix/var/nix/profiles/default/bin"': f'nix_default_bin = "{fixture}/absolute-two"',
}.items()
for old, new in replacements:
    if text.count(old) != 1:
        raise SystemExit("canonical mutation target missing")
    text = text.replace(old, new, 1)
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
    "absolute": ('nix_system_bin = "/run/current-system/sw/bin"', f'nix_system_bin = "/tmp/$(touch {marker})"'),
    "absolute-backtick": ('nix_system_bin = "/run/current-system/sw/bin"', f'nix_system_bin = "/tmp/`touch {marker}`"'),
    "absolute-semicolon": ('nix_system_bin = "/run/current-system/sw/bin"', f'nix_system_bin = "/tmp/bad;touch {marker}"'),
    "absolute-quote": ('nix_system_bin = "/run/current-system/sw/bin"', 'nix_system_bin = "/tmp/\'bad"'),
    "absolute-traversal": ('nix_system_bin = "/run/current-system/sw/bin"', 'nix_system_bin = "/tmp/../escape"'),
    "absolute-double-dot": ('nix_system_bin = "/run/current-system/sw/bin"', 'nix_system_bin = "/tmp/foo..bar"'),
    "absolute-double-slash": ('nix_system_bin = "/run/current-system/sw/bin"', 'nix_system_bin = "/tmp/foo//bar"'),
    "absolute-duplicate": ('nix_system_bin = "/run/current-system/sw/bin"', 'nix_system_bin = "/run/current-system/sw/bin"\nnix_default_bin = "/run/current-system/sw/bin"'),
    "home-relative": ('home_local_bin = ".local/bin"', f'home_local_bin = ".local/$(touch {marker})"'),
    "home-relative-traversal": ('home_local_bin = ".local/bin"', 'home_local_bin = "../escape"'),
    "home-relative-double-dot": ('home_local_bin = ".local/bin"', 'home_local_bin = ".local/foo..bar"'),
    "home-relative-double-slash": ('home_local_bin = ".local/bin"', 'home_local_bin = ".local//bin"'),
    "linuxbrew-relative": ('linuxbrew_user_bin = ".linuxbrew/bin"', f'linuxbrew_user_bin = ".linuxbrew/$(touch {marker})"'),
    "linuxbrew-system": ('linuxbrew_system_bin = "/home/linuxbrew/.linuxbrew/bin"', f'linuxbrew_system_bin = "/home/linuxbrew/.linuxbrew/$(touch {marker})"'),
    "darwin-homebrew": ('darwin_arm64_homebrew_bin = "/opt/homebrew/bin"', f'darwin_arm64_homebrew_bin = "/opt/homebrew/$(touch {marker})"'),
    "darwin-x86-homebrew": ('darwin_x86_64_homebrew_bin = "/usr/local/bin"', f'darwin_x86_64_homebrew_bin = "/usr/local/$(touch {marker})"'),
    "xdg": ('config = ".config"', f'config = ".$(touch {marker})"'),
    "xdg-double-dot": ('config = ".config"', 'config = ".config..bad"'),
    "xdg-double-slash": ('config = ".config"', 'config = ".config//bad"'),
    "xdg-control": ('config = ".config"', 'config = "\\u0001bad"'),
    "profile-root": ('nix_user_profile_root = "/etc/profiles/per-user"', f'nix_user_profile_root = "/etc/profiles/$(touch {marker})"'),
    "profile-root-double-dot": ('nix_user_profile_root = "/etc/profiles/per-user"', 'nix_user_profile_root = "/etc/profiles/foo..bar"'),
    "profile-root-double-slash": ('nix_user_profile_root = "/etc/profiles/per-user"', 'nix_user_profile_root = "/etc/profiles//foo"'),
    "profile-suffix": ('nix_user_profile_bin = "bin"', f'nix_user_profile_bin = "$(touch {marker})"'),
    "profile-suffix-double-dot": ('nix_user_profile_bin = "bin"', 'nix_user_profile_bin = "foo..bar"'),
    "profile-suffix-double-slash": ('nix_user_profile_bin = "bin"', 'nix_user_profile_bin = "foo//bar"'),
    "state-relative": ('nix_state_relative = "nix/profile/bin"', f'nix_state_relative = "nix/$(touch {marker})"'),
    "state-relative-double-dot": ('nix_state_relative = "nix/profile/bin"', 'nix_state_relative = "nix/foo..bar"'),
    "state-relative-double-slash": ('nix_state_relative = "nix/profile/bin"', 'nix_state_relative = "nix//profile/bin"'),
    "alias-argv": ('ginit = ["gcloud", "init"]', f'ginit = ["gcloud", "$(touch {marker})"]'),
    "alias-name": ('ginit = ["gcloud", "init"]', '"bad;name" = ["gcloud", "init"]'),
}
old, new = replacements[mutation]
if text.count(old) != 1:
    raise SystemExit(f"hostile mutation target missing: {mutation}")
path.write_text(text.replace(old, new, 1))
PY
}
