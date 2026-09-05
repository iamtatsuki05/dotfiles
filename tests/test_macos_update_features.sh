#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="${0:A:h}"
readonly REPO_ROOT="${TEST_DIR:h}"
readonly UPDATE_SCRIPT="$REPO_ROOT/scripts/update_managed_versions.sh"

TEST_ROOT=""

source "$TEST_DIR/lib/platform.sh"
source "$TEST_DIR/lib/isolated.sh"

skip_unless_macos() {
  local test_name="$1"

  if is_test_macos; then
    return 0
  fi

  print -r -- "SKIP: $test_name requires macOS"
  return 1
}

fail() {
  print -u2 -r -- "FAIL: $*"
  exit 1
}

cleanup() {
  if [[ -n "$TEST_ROOT" && -d "$TEST_ROOT" ]]; then
    rm -rf "$TEST_ROOT"
  fi
}

trap cleanup EXIT HUP INT TERM

assert_file() {
  [[ -f "$1" ]] || fail "expected file: $1"
}

assert_not_exists() {
  [[ ! -e "$1" && ! -L "$1" ]] || fail "expected path not to exist: $1"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  assert_file "$file_path"
  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

assert_output_contains() {
  local file_path="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$file_path" || {
    print -u2 -r -- "Output from $file_path:"
    sed -n '1,180p' "$file_path" >&2
    fail "expected output to contain: $expected"
  }
}

make_temp_root() {
  TEST_ROOT="$(mktemp -d /private/tmp/dotfiles-macos-update.XXXXXX)"
}

new_fixture() {
  local fixture

  fixture="$(mktemp -d "$TEST_ROOT/fixture.XXXXXX")"
  mkdir -p "$fixture/bin" "$fixture/home" "$fixture/tmp" \
    "$fixture/xdg/config" "$fixture/xdg/cache" "$fixture/xdg/data" "$fixture/xdg/state"
  REPLY="$fixture"
}

write_fake_commands() {
  local repo="$1"
  local with_brew="$2"
  local log_file="$repo/update.log"

  cat > "$repo/bin/nix" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "nix:\$*" >> "\$event_log"

reject_nix() {
  print -u2 -- "unexpected fake nix command: \$*"
  exit 125
}

case "\${1:-}" in
  eval)
    [[ \$# == 3 && "\$2" == --raw ]] || reject_nix "\$@"
    print -r -- "/nix/store/dotfiles-test"
    ;;
  flake)
    case "\${2:-}" in
      update)
        [[ \$# == 2 ]] || reject_nix "\$@"
        ;;
      lock)
        [[ \$# == 4 && "\$3" == --update-input && "\$4" == (nixpkgs|home-manager|nix-darwin) ]] || reject_nix "\$@"
        ;;
      *)
        reject_nix "\$@"
        ;;
    esac
    ;;
  profile)
    [[ \$# == 4 && "\$2" == list && "\$3" == --profile ]] || reject_nix "\$@"
    ;;
  *)
    reject_nix "\$@"
    ;;
esac
EOF
  cat > "$repo/bin/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "mise:\$*" >> "\$event_log"
[[ \$# == 3 && "\$1" == upgrade && "\$2" == --exclude && "\$3" == java ]] || {
  print -u2 -- "unexpected fake mise command: \$*"
  exit 125
}
EOF
  cat > "$repo/bin/brew" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "brew:\$*" >> "\$event_log"
if [[ "$with_brew" != 1 ]]; then
  print -u2 -- "fake brew is configured as unavailable"
  exit 127
fi

reject_brew() {
  print -u2 -- "unexpected fake brew command: \$*"
  exit 125
}

case "\${1:-}" in
  update)
    [[ \$# == 1 ]] || reject_brew "\$@"
    ;;
  upgrade)
    [[ \$# == 2 && "\$2" == example-tool ]] || {
      [[ \$# == 3 && "\$2" == --cask && "\$3" == example-gui ]] || reject_brew "\$@"
    }
    ;;
  info)
    [[ \$# == 4 && "\$2" == --cask && "\$3" == --json=v2 && "\$4" == example-gui ]] || reject_brew "\$@"
    print -r -- '{"casks":[{"token":"example-gui","full_token":"example-gui","auto_updates":false}]}'
    ;;
  *)
    reject_brew "\$@"
    ;;
esac
EOF
  cat > "$repo/bin/curl" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "curl:\$*" >> "\$event_log"
print -u2 -- "unexpected network access through fake curl: \$*"
exit 125
EOF
  chmod +x "$repo/bin/nix" "$repo/bin/mise" "$repo/bin/brew" "$repo/bin/curl"
}

write_fake_update_helpers() {
  local repo="$1"
  local log_file="$repo/update.log"

  cat > "$repo/scripts/nix_install.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "nix_install:\$*" >> "\$event_log"
case "\$#:\${1:-}:\${2:-}:\${3:-}" in
  2:--profile:cli:|2:--profile:full:|3:--profile:full:--with-gui-apps)
    ;;
  *)
    print -u2 -- "unexpected fake nix_install command: \$*"
    exit 125
    ;;
esac
EOF
  cat > "$repo/scripts/setup_hermes_agent.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "hermes:\$*" >> "\$event_log"
[[ \$# == 1 && "\$1" == --update-only ]] || {
  print -u2 -- "unexpected fake Hermes command: \$*"
  exit 125
}
EOF
  cat > "$repo/bin/nix-installer" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "nix-installer:\$*" >> "\$event_log"
print -u2 -- "unexpected fake Nix installer invocation: \$*"
exit 125
EOF
  chmod +x "$repo/scripts/nix_install.sh" "$repo/scripts/setup_hermes_agent.sh"
  chmod +x "$repo/bin/nix-installer"
}

create_fixture_repo() {
  local repo="$1"
  local feature_value="$2"
  local with_brew="$3"

  mkdir -p \
    "$repo/bin" \
    "$repo/scripts/lib" \
    "$repo/home/.chezmoitemplates" \
    "$repo/config/nix" \
    "$repo/config/mise" \
    "$repo/tmp" \
    "$repo/xdg/config" "$repo/xdg/cache" "$repo/xdg/data" "$repo/xdg/state"
  cp "$UPDATE_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew.sh" "$repo/scripts/lib/homebrew.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew_fallback.sh" "$repo/scripts/lib/homebrew_fallback.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"
  cp "$REPO_ROOT/scripts/lib/features.sh" "$repo/scripts/lib/features.sh"
  cat > "$repo/home/.zshenv" <<'EOF'
if [[ -n "${DOTFILES_TEST_PATH:-}" ]]; then
  export PATH="$DOTFILES_TEST_PATH"
fi
EOF
  write_fake_commands "$repo" "$with_brew"
  write_fake_update_helpers "$repo"

  cat > "$repo/home/.chezmoidata.toml" <<EOF
[features]
macos = $feature_value
EOF
  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [
  ];

  brews = [
    "example-tool"
  ];

  casks = [
    "example-gui"
  ];

  vscode = [
  ];
}
EOF
  cat > "$repo/config/mise/config.toml" <<'EOF'
[tools]
node = "lts"
EOF
  : > "$repo/home/.chezmoitemplates/mise-config.toml"
  cat >> "$repo/scripts/lib/runtime.sh" <<'EOF'
dotfiles_temporary_directory_root() {
  REPLY="${TMPDIR:-/tmp}"
}
EOF
  : > "$repo/update.log"
  chmod +x "$repo/scripts/update_managed_versions.sh"
}

run_update() {
  local repo="$1"
  local output_file="$2"
  local rc=0
  shift 2

  run_isolated "$repo" env -i \
    HOME="$repo/home" \
    PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    USER=dotfiles-test \
    TMPDIR="$repo/tmp" \
    XDG_CONFIG_HOME="$repo/xdg/config" \
    XDG_CACHE_HOME="$repo/xdg/cache" \
    XDG_DATA_HOME="$repo/xdg/data" \
    XDG_STATE_HOME="$repo/xdg/state" \
    DOTFILES_SHOW_PROGRESS=0 \
    DOTFILES_TEST_FIXTURE_ROOT="$repo" \
    DOTFILES_TEST_EVENT_LOG="$repo/update.log" \
    /bin/zsh "$repo/scripts/update_managed_versions.sh" "$@" > "$output_file" 2>&1 || rc=$?
  if (( rc != 0 )); then
    return "$rc"
  fi
}

assert_event_order() {
  local log_file="$1"
  local first="$2"
  local second="$3"
  local first_line second_line

  first_line="$(grep -n -F -- "$first" "$log_file" | head -1 | cut -d: -f1)"
  second_line="$(grep -n -F -- "$second" "$log_file" | head -1 | cut -d: -f1)"
  [[ -n "$first_line" && -n "$second_line" && "$first_line" -lt "$second_line" ]] \
    || fail "expected $first before $second"
}

test_feature_off_skips_homebrew_but_keeps_managed_update_flow() {
  local fixture repo output

  new_fixture
  fixture="$REPLY"
  repo="$fixture/repo"
  output="$fixture/output.log"
  mkdir -p "$repo"
  create_fixture_repo "$repo" false 1

  run_update "$repo" "$output" --only all --profile full --with-gui-apps

  assert_contains "$repo/update.log" 'nix:flake update'
  assert_contains "$repo/update.log" 'nix_install:--profile full'
  assert_not_contains "$repo/update.log" '--with-gui-apps'
  assert_contains "$repo/update.log" 'mise:upgrade --exclude java'
  assert_contains "$repo/update.log" 'hermes:--update-only'
  assert_not_contains "$repo/update.log" 'brew:'
  assert_output_contains "$output" 'features.macos=false'
  assert_output_contains "$output" 'ignoring --with-gui-apps'
  assert_not_contains "$output" 'falling back to the CLI Nix profile'
  assert_file "$repo/xdg/config/mise/config.toml"
  assert_event_order "$repo/update.log" 'nix_install:--profile full' 'mise:upgrade --exclude java'
  assert_event_order "$repo/update.log" 'mise:upgrade --exclude java' 'hermes:--update-only'
}

test_feature_on_preserves_homebrew_fallback_updates() {
  local fixture repo output

  new_fixture
  fixture="$REPLY"
  repo="$fixture/repo"
  output="$fixture/output.log"
  mkdir -p "$repo"
  create_fixture_repo "$repo" true 1

  run_update "$repo" "$output" --only all --profile full --with-gui-apps

  assert_contains "$repo/update.log" 'nix:flake update'
  assert_contains "$repo/update.log" 'nix_install:--profile full --with-gui-apps'
  assert_contains "$repo/update.log" 'brew:update'
  assert_contains "$repo/update.log" 'brew:upgrade example-tool'
  assert_contains "$repo/update.log" 'brew:info --cask --json=v2 example-gui'
  assert_contains "$repo/update.log" 'brew:upgrade --cask example-gui'
  assert_contains "$repo/update.log" 'mise:upgrade --exclude java'
  assert_contains "$repo/update.log" 'hermes:--update-only'
  assert_event_order "$repo/update.log" 'nix_install:--profile full --with-gui-apps' 'brew:update'
  assert_event_order "$repo/update.log" 'brew:update' 'mise:upgrade --exclude java'
  assert_event_order "$repo/update.log" 'mise:upgrade --exclude java' 'hermes:--update-only'
}

test_invalid_feature_configuration_has_zero_external_actions() {
  local fixture repo output rc=0

  new_fixture
  fixture="$REPLY"
  repo="$fixture/repo"
  output="$fixture/output.log"
  mkdir -p "$repo"
  create_fixture_repo "$repo" '"false"' 1

  run_update "$repo" "$output" --only all --profile full --with-gui-apps || rc=$?
  (( rc != 0 )) || fail 'invalid features.macos must fail before managed updates'
  assert_file "$repo/update.log"
  [[ ! -s "$repo/update.log" ]] || fail 'invalid feature configuration must not invoke external commands'
  assert_not_exists "$repo/xdg/config/mise/config.toml"
  assert_output_contains "$output" 'cannot read a valid [features] macos boolean'
}

test_lock_scope_remains_available_when_feature_is_disabled() {
  local fixture repo output

  new_fixture
  fixture="$REPLY"
  repo="$fixture/repo"
  output="$fixture/output.log"
  mkdir -p "$repo"
  create_fixture_repo "$repo" false 1

  run_update "$repo" "$output" --only lock --profile full --with-gui-apps

  assert_contains "$repo/update.log" 'nix:flake update'
  assert_not_contains "$repo/update.log" 'nix_install:'
  assert_not_contains "$repo/update.log" 'mise:'
  assert_not_contains "$repo/update.log" 'hermes:'
  assert_not_contains "$repo/update.log" 'brew:'
}

main() {
  skip_unless_macos "test_macos_update_features.sh" || return 0
  make_temp_root
  test_feature_off_skips_homebrew_but_keeps_managed_update_flow
  test_feature_on_preserves_homebrew_fallback_updates
  test_invalid_feature_configuration_has_zero_external_actions
  test_lock_scope_remains_available_when_feature_is_disabled
  print -r -- 'macOS managed update feature gate tests passed'
}

main "$@"
