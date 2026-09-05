#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly TEST_PYTHON_BIN="${DOTFILES_TEST_PYTHON:-python3}"
readonly NIX_INSTANTIATE="${NIX_INSTANTIATE_BIN:-$(command -v nix-instantiate 2>/dev/null || true)}"

source "$TEST_DIR/lib/assertions.sh"

if [[ "$NIX_INSTANTIATE" != /* || ! -x "$NIX_INSTANTIATE" ]]; then
  print -r -- 'SKIP: nix-instantiate is not installed for macOS Nix feature checks'
  exit 0
fi

make_temp_dir macos-nix-features
readonly FIXTURE="$REPLY"
trap 'rm -rf -- "$FIXTURE"' EXIT

write_feature_fixture() {
  local name="$1" os="$2" enabled="$3"
  local fixture="$FIXTURE/$name"

  mkdir -p "$fixture/config/nix/darwin" "$fixture/config/nix/home-manager" "$fixture/home"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$fixture/home/.chezmoidata.toml"
  cp "$REPO_ROOT/config/nix/features.nix" "$fixture/config/nix/features.nix"
  cp "$REPO_ROOT/config/nix/darwin/default.nix" "$fixture/config/nix/darwin/default.nix"
  cp "$REPO_ROOT/config/nix/home-manager/default.nix" "$fixture/config/nix/home-manager/default.nix"
  cp "$REPO_ROOT/config/nix/gui-packages.nix" "$fixture/config/nix/gui-packages.nix"
  cp "$REPO_ROOT/config/nix/gui-common-package-names.nix" "$fixture/config/nix/gui-common-package-names.nix"
  cp "$REPO_ROOT/config/nix/gui-macos-package-names.nix" "$fixture/config/nix/gui-macos-package-names.nix"
  cp "$REPO_ROOT/config/nix/gui-linux-package-names.nix" "$fixture/config/nix/gui-linux-package-names.nix"

  # This boundary stub lets the real gui-packages module expose selected names
  # without evaluating the unrelated package derivations it normally imports.
  print -r -- '{ packageNames, ... }: packageNames' > "$fixture/config/nix/package-list.nix"

  "$TEST_PYTHON_BIN" - "$fixture/home/.chezmoidata.toml" "$enabled" <<'PY'
from pathlib import Path
import re
import sys

path, enabled = Path(sys.argv[1]), sys.argv[2]
text, count = re.subn(r"^macos = (true|false)$", f"macos = {enabled}", path.read_text(), flags=re.M)
if count != 1:
    raise SystemExit("feature fixture is missing the canonical macos flag")
path.write_text(text)
PY

  print -r -- "$fixture"
}

eval_nix() {
  local fixture="$1" expression="$2" output="$3" error="$4"

  if ! env -i HOME="$fixture/home" PATH="${NIX_INSTANTIATE:h}:/usr/bin:/bin" \
    "$NIX_INSTANTIATE" --eval --json --strict --expr "$expression" > "$output" 2> "$error"; then
    print -u2 -r -- "Nix evaluation failed for $fixture"
    sed -n '1,100p' "$error" >&2
    return 1
  fi
}

assert_json_array() {
  local output="$1" expected="$2" message="$3"

  "$TEST_PYTHON_BIN" - "$output" "$expected" "$message" <<'PY'
import json
import sys
from pathlib import Path

actual = json.loads(Path(sys.argv[1]).read_text())
expected = json.loads(sys.argv[2])
if actual != expected:
    raise SystemExit(f"{sys.argv[3]}: {actual!r} != {expected!r}")
PY
}

assert_json_object() {
  local output="$1" expected="$2" message="$3"

  "$TEST_PYTHON_BIN" - "$output" "$expected" "$message" <<'PY'
import json
import sys
from pathlib import Path

actual = json.loads(Path(sys.argv[1]).read_text())
expected = json.loads(sys.argv[2])
if actual != expected:
    raise SystemExit(f"{sys.argv[3]}: {actual!r} != {expected!r}")
PY
}

test_darwin_imports_follow_macos_feature_flag() {
  local enabled fixture output error expression

  for enabled in false true; do
    fixture="$(write_feature_fixture "darwin-$enabled" Darwin "$enabled")"
    output="$FIXTURE/darwin-imports-$enabled.json"
    error="$FIXTURE/darwin-imports-$enabled.err"
    expression="
      let
        module = import \"$fixture/config/nix/darwin/default.nix\" {
          lib = { optionals = condition: values: if condition then values else [ ]; };
        };
      in map builtins.baseNameOf module.imports
    "
    eval_nix "$fixture" "$expression" "$output" "$error"
    if [[ "$enabled" == true ]]; then
      assert_json_array "$output" '["base.nix", "defaults.nix", "homebrew.nix", "auto-update.nix"]' \
        'enabled Darwin feature imports'
    else
      assert_json_array "$output" '["base.nix"]' 'disabled Darwin feature imports'
    fi
  done
}

test_gui_package_selection_is_platform_and_flag_scoped() {
  local enabled os fixture output error expression

  for os in darwin linux; do
    for enabled in false true; do
      fixture="$(write_feature_fixture "gui-$os-$enabled" "$os" "$enabled")"
      output="$FIXTURE/gui-$os-$enabled.json"
      error="$FIXTURE/gui-$os-$enabled.err"
      if [[ "$os" == darwin ]]; then
        expression="
          let
            pkgs = {
              lib.optionals = condition: values: if condition then values else [ ];
              stdenv.hostPlatform = { isDarwin = true; isLinux = false; };
            };
          in import \"$fixture/config/nix/gui-packages.nix\" { inherit pkgs; }
        "
      else
        expression="
          let
            pkgs = {
              lib.optionals = condition: values: if condition then values else [ ];
              stdenv.hostPlatform = { isDarwin = false; isLinux = true; };
            };
          in import \"$fixture/config/nix/gui-packages.nix\" { inherit pkgs; }
        "
      fi
      eval_nix "$fixture" "$expression" "$output" "$error"
      if [[ "$os" == darwin && "$enabled" == false ]]; then
        assert_json_array "$output" '[]' 'disabled macOS GUI package selection'
      elif [[ "$os" == darwin ]]; then
        "$TEST_PYTHON_BIN" - "$output" <<'PY'
import json
import sys
from pathlib import Path

packages = json.loads(Path(sys.argv[1]).read_text())
for required in ("slack", "raycast"):
    if required not in packages:
        raise SystemExit(f"enabled macOS GUI package selection lacks {required!r}: {packages!r}")
PY
      else
        "$TEST_PYTHON_BIN" - "$output" <<'PY'
import json
import sys
from pathlib import Path

packages = json.loads(Path(sys.argv[1]).read_text())
for required in ("slack", "ghostty"):
    if required not in packages:
        raise SystemExit(f"Linux GUI package selection lacks {required!r}: {packages!r}")
if "raycast" in packages:
    raise SystemExit(f"Linux GUI package selection leaked a macOS package: {packages!r}")
PY
      fi
    done
  done
}

test_full_package_output_source_wiring() {
  assert_contains "$REPO_ROOT/flake.nix" 'cliPackages = import ./config/nix/packages.nix'
  assert_contains "$REPO_ROOT/flake.nix" 'guiPackages = import ./config/nix/gui-packages.nix'
  assert_contains "$REPO_ROOT/flake.nix" 'paths = cliPackages ++ guiPackages;'
}

test_home_manager_gui_default_and_copy_apps_follow_macos_flag() {
  local enabled os fixture output error expression

  for os in darwin linux; do
    for enabled in false true; do
      fixture="$(write_feature_fixture "home-manager-$os-$enabled" "$os" "$enabled")"
      output="$FIXTURE/home-manager-$os-$enabled.json"
      error="$FIXTURE/home-manager-$os-$enabled.err"
      if [[ "$os" == darwin ]]; then
        expression="
          let
            module = import \"$fixture/config/nix/home-manager/default.nix\" {
              config = { dotfiles.enableGuiApps = true; };
              lib = {
                mkOption = option: option;
                types = { bool = \"bool\"; enum = values: \"enum\"; };
              };
              pkgs.stdenv.hostPlatform = { isDarwin = true; isLinux = false; };
              username = \"fixture-user\";
              homeDirectory = \"$fixture/home\";
              profile = \"full\";
              enableGuiApps = true;
            };
          in {
            guiDefault = module.options.dotfiles.enableGuiApps.default;
            copyApps = module.config.targets.darwin.copyApps.enable;
            profileDefault = module.options.dotfiles.profile.default;
          }
        "
      else
        expression="
          let
            module = import \"$fixture/config/nix/home-manager/default.nix\" {
              config = { dotfiles.enableGuiApps = true; };
              lib = {
                mkOption = option: option;
                types = { bool = \"bool\"; enum = values: \"enum\"; };
              };
              pkgs.stdenv.hostPlatform = { isDarwin = false; isLinux = true; };
              username = \"fixture-user\";
              homeDirectory = \"$fixture/home\";
              profile = \"full\";
              enableGuiApps = true;
            };
          in {
            guiDefault = module.options.dotfiles.enableGuiApps.default;
            copyApps = module.config.targets.darwin.copyApps.enable;
            profileDefault = module.options.dotfiles.profile.default;
          }
        "
      fi
      eval_nix "$fixture" "$expression" "$output" "$error"
      if [[ "$os" == darwin && "$enabled" == false ]]; then
        assert_json_object "$output" '{"guiDefault": false, "copyApps": false, "profileDefault": "full"}' \
          'disabled macOS Home Manager GUI behavior'
      elif [[ "$os" == darwin ]]; then
        assert_json_object "$output" '{"guiDefault": true, "copyApps": true, "profileDefault": "full"}' \
          'enabled macOS Home Manager GUI behavior'
      else
        assert_json_object "$output" '{"guiDefault": true, "copyApps": false, "profileDefault": "full"}' \
          'Linux Home Manager GUI behavior'
      fi
    done
  done
}

test_darwin_imports_follow_macos_feature_flag
test_gui_package_selection_is_platform_and_flag_scoped
test_full_package_output_source_wiring
test_home_manager_gui_default_and_copy_apps_follow_macos_flag
print -r -- 'macOS Nix feature checks passed'
