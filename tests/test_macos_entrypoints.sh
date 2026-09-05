#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="${0:A:h}"
readonly REPO_ROOT="${TEST_DIR:h}"

source "$TEST_DIR/lib/assertions.sh"
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

make_fixture_dir() {
  REPLY="$(mktemp -d /private/tmp/dotfiles-macos-entrypoint.XXXXXX)" || {
    fail 'failed to create macOS fixture root'
  }
}

write_feature_config() {
  local repo="$1"
  local value="$2"

  cat > "$repo/home/.chezmoidata.toml" <<EOF
[features]
macos = $value
EOF
}

write_invalid_feature_config() {
  local repo="$1"

  cat > "$repo/home/.chezmoidata.toml" <<'EOF'
[features]
macos = "false"
EOF
}

write_fake_brew() {
  local bin_dir="$1"

  cat > "$bin_dir/brew" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125
print -r -- "brew:$*" >> "$event_log"

reject_brew() {
  print -u2 -- "unexpected fake brew command: $*"
  exit 125
}

case "${1:-}" in
  update)
    [[ $# == 1 ]] || reject_brew "$@"
    ;;
  upgrade)
    [[ $# == 2 && "$2" == example-tool ]] || {
      [[ $# == 3 && "$2" == --cask && "$3" == example-gui ]] || reject_brew "$@"
    }
    ;;
  info)
    [[ $# == 4 && "$2" == --cask && "$3" == --json=v2 && "$4" == example-gui ]] || reject_brew "$@"
    print -r -- '{"casks":[{"token":"example-gui","full_token":"example-gui","auto_updates":false}]}'
    ;;
  *)
    reject_brew "$@"
    ;;
esac
EOF
  chmod +x "$bin_dir/brew"
}

write_fake_curl() {
  local bin_dir="$1"

  cat > "$bin_dir/curl" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125
print -r -- "curl:$*" >> "$event_log"
print -u2 -- "unexpected network access through fake curl: $*"
exit 125
EOF
  chmod +x "$bin_dir/curl"
}

write_fake_nix() {
  local bin_dir="$1"

  cat > "$bin_dir/nix" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125
print -r -- "nix:$*" >> "$event_log"

print -u2 -- "unexpected fake nix command: $*"
exit 125
EOF
  chmod +x "$bin_dir/nix"
}

write_fake_nix_installer() {
  local bin_dir="$1"

  cat > "$bin_dir/nix-installer" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125
print -r -- "nix-installer:$*" >> "$event_log"
print -u2 -- "unexpected fake Nix installer invocation: $*"
exit 125
EOF
  chmod +x "$bin_dir/nix-installer"
}

write_fake_mas() {
  local bin_dir="$1"

  cat > "$bin_dir/mas" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125
print -r -- "mas:$*" >> "$event_log"

case "${1:-}" in
  list)
    [[ $# == 1 ]] || exit 125
    ;;
  install)
    [[ $# == 2 && "$2" == <-> ]] || exit 125
    ;;
  *)
    print -u2 -- "unexpected fake mas command: $*"
    exit 125
    ;;
esac
EOF
  chmod +x "$bin_dir/mas"
}

write_common_fixture_commands() {
  local repo="$1"

  write_fake_brew "$repo/bin"
  write_fake_curl "$repo/bin"
  write_fake_nix "$repo/bin"
  write_fake_nix_installer "$repo/bin"
  write_fake_mas "$repo/bin"
  write_event_command "$repo/bin/pkgutil" pkgutil '[[ "$#" == 2 && "$1" == --pkg-info && "$2" == com.apple.pkg.RosettaUpdateAuto ]] || exit 125; exit 1'
  write_event_command "$repo/bin/softwareupdate" softwareupdate '[[ "$#" == 2 && "$1" == --install-rosetta && "$2" == --agree-to-license ]] || exit 125'
}

write_common_fixture_files() {
  local repo="$1"

  mkdir -p "$repo/home" "$repo/bin" "$repo/tmp" \
    "$repo/xdg/config" "$repo/xdg/cache" "$repo/xdg/data" "$repo/xdg/state" \
    "$repo/scripts/lib" "$repo/config/nix"
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
  # The production macOS helper uses /private/tmp; redirect fixture scratch
  # files so the isolated child stays below its dedicated sandbox root.
  cat >> "$repo/scripts/lib/runtime.sh" <<'EOF'
dotfiles_temporary_directory_root() {
  REPLY="${TMPDIR:-/tmp}"
}
EOF
  cat > "$repo/config/nix/homebrew-fallback.nix" <<'EOF'
{
  taps = [ ];
  brews = [ "fixture-cli-fallback" ];
  casks = [ "fixture-gui-fallback" ];
  vscode = [ ];
}
EOF
  write_common_fixture_commands "$repo"
}

write_event_command() {
  local command_path="$1"
  local event_name="$2"
  local command_body="${3:-exit 0}"

  cat > "$command_path" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "$event_name:\$*" >> "\$event_log"
$command_body
EOF
  chmod +x "$command_path"
}

write_fake_uname() {
  local bin_dir="$1"

  cat > "$bin_dir/uname" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
case "${1:-}" in
  -s) print -r -- Darwin ;;
  -m) print -r -- arm64 ;;
  *) print -r -- Darwin ;;
esac
EOF
  chmod +x "$bin_dir/uname"
}

write_fake_mise() {
  local bin_dir="$1"
  local tools_dir="$2"

  mkdir -p "$tools_dir"
  : > "$tools_dir/python3"
  : > "$tools_dir/uv"
  chmod +x "$tools_dir/python3" "$tools_dir/uv"
  cat > "$bin_dir/mise" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="\${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="\${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="\$(cd -P "\${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "\$event_log_parent" == "\$fixture_root" || "\$event_log_parent" == "\$fixture_root"/* ]] || exit 125
print -r -- "mise:\$*" >> "\$event_log"
case "\${1:-}" in
  which)
    [[ \$# == 2 ]] || exit 125
    case "\${2:-}" in
      python3) print -r -- "$tools_dir/python3" ;;
      uv) print -r -- "$tools_dir/uv" ;;
      *) exit 125 ;;
    esac
    ;;
  install)
    [[ \$# == 1 || ( \$# == 3 && "\$2" == python && "\$3" == uv ) ]] || exit 125
    ;;
  *)
    print -u2 -- "unexpected fake mise command: \$*"
    exit 125
    ;;
esac
EOF
  chmod +x "$bin_dir/mise"
}

write_fake_sudo_for_main() {
  local bin_dir="$1"

  cat > "$bin_dir/sudo" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125

reject_sudo() {
  print -u2 -- "unexpected fake sudo command: $*"
  exit 125
}

print -r -- "sudo:$*" >> "$event_log"
case "${1:-}" in
  -v)
    [[ $# == 1 ]] || reject_sudo "$@"
    ;;
  -n)
    [[ $# == 2 && "$2" == true ]] || reject_sudo "$@"
    ;;
  softwareupdate)
    [[ $# == 3 && "$2" == --install-rosetta && "$3" == --agree-to-license ]] || reject_sudo "$@"
    softwareupdate="$fixture_root/bin/softwareupdate"
    [[ -x "$softwareupdate" && -f "$softwareupdate" && ! -L "$softwareupdate" ]] || reject_sudo "$@"
    "$softwareupdate" "$2" "$3"
    ;;
  *)
    reject_sudo "$@"
    ;;
esac
EOF
  chmod +x "$bin_dir/sudo"
}

write_fake_sudo_for_nix() {
  local bin_dir="$1"

  cat > "$bin_dir/sudo" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125

reject_sudo() {
  print -u2 -- "unexpected fake sudo command: $*"
  exit 125
}

is_fixture_path() {
  local candidate="$1"
  local candidate_parent

  [[ "$candidate" == "$fixture_root" || "$candidate" == "$fixture_root"/* ]] || return 1
  [[ "$candidate" != *'/../'* && "$candidate" != */.. && "$candidate" != *'/./'* && "$candidate" != */. ]] || return 1
  candidate_parent="$(cd -P "${candidate:h}" 2>/dev/null && pwd -P)" || return 1
  [[ "$candidate_parent" == "$fixture_root" || "$candidate_parent" == "$fixture_root"/* ]]
}

is_allowed_flake_ref() {
  local flake_ref="$1"
  local flake_path="${flake_ref%%#*}"
  local flake_attr="${flake_ref#*#}"

  [[ "$flake_attr" == (aarch64|x86_64)-darwin-(cli|full) ]] || return 1
  [[ "$flake_path" == "path:$fixture_root" || "$flake_path" == "$fixture_root" ]]
}

print -r -- "sudo:$*" >> "$event_log"

if (( $# == 8 )) && [[ "$1" == env && "$2" == HOME=/var/root && "$3" == DOTFILES_USERNAME=fixture-user \
  && "$4" == darwin-rebuild && "$5" == switch && "$6" == --impure && "$7" == --flake ]]; then
  flake_ref="$8"
  is_allowed_flake_ref "$flake_ref" || reject_sudo "$@"
  darwin_rebuild="$fixture_root/bin/darwin-rebuild"
  [[ -x "$darwin_rebuild" && -f "$darwin_rebuild" && ! -L "$darwin_rebuild" ]] || reject_sudo "$@"
  "$darwin_rebuild" "$5" "$6" "$7" "$8"
  exit $?
fi

if (( $# == 14 )) && [[ "$1" == env && "$2" == HOME=/var/root && "$3" == DOTFILES_USERNAME=fixture-user \
  && "$5" == --extra-experimental-features && "$6" == "nix-command flakes" \
  && "$7" == run && "$8" == --impure && "${10}" == -- \
  && "${11}" == switch && "${12}" == --impure && "${13}" == --flake ]]; then
  nix_bin="$4"
  [[ "${nix_bin:t}" == nix ]] && is_fixture_path "$nix_bin" \
    && [[ -x "$nix_bin" && -f "$nix_bin" && ! -L "$nix_bin" ]] || reject_sudo "$@"
  [[ "$9" == "path:$fixture_root#darwin-rebuild" || "$9" == "$fixture_root#darwin-rebuild" ]] || reject_sudo "$@"
  is_allowed_flake_ref "${14}" || reject_sudo "$@"
  [[ "${14%%#*}" == "${9%%#*}" ]] || reject_sudo "$@"
  "$nix_bin" "$5" "$6" "$7" "$8" "$9" "${10}" "${11}" "${12}" "${13}" "${14}"
  exit $?
fi

if (( $# == 3 )) && [[ "$1" == mv ]] && is_fixture_path "$2" && is_fixture_path "$3" \
  && [[ "$3" == "$2.before-nix-darwin" && ! -L "$2" && ! -L "$3" ]]; then
  /bin/mv -- "$2" "$3"
  exit $?
fi

reject_sudo "$@"
EOF
  chmod +x "$bin_dir/sudo"
}

write_main_fixture() {
  local repo="$1"
  local log_file="$2"

  cp "$REPO_ROOT/main.sh" "$repo/main.sh"
  for script_name in nix_install.sh install_homebrew.sh install_mas_apps.sh \
    chezmoi_apply.sh setup_agent_files.sh setup_git_hooks.sh; do
    write_event_command "$repo/scripts/$script_name" "${script_name%.sh}"
  done
  write_fake_uname "$repo/bin"
  write_fake_sudo_for_main "$repo/bin"
  write_event_command "$repo/bin/pkgutil" pkgutil '[[ "$#" == 2 && "$1" == --pkg-info && "$2" == com.apple.pkg.RosettaUpdateAuto ]] || exit 125; exit 1'
  write_fake_mise "$repo/bin" "$repo/mise-tools"
  : > "$log_file"
}

run_main_fixture() {
  local repo="$1"
  local output_file="$2"
  local exit_status=0

  run_isolated "$repo" env -i \
    HOME="$repo/home" USER=fixture-user TMPDIR="$repo/tmp" \
    XDG_CONFIG_HOME="$repo/xdg/config" XDG_CACHE_HOME="$repo/xdg/cache" \
    XDG_DATA_HOME="$repo/xdg/data" XDG_STATE_HOME="$repo/xdg/state" \
    PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_FIXTURE_ROOT="$repo" \
    DOTFILES_TEST_EVENT_LOG="$repo/events.log" \
    DOTFILES_NIX_PROFILE_PATHS="$repo/bin" \
    DOTFILES_NIX_INSTALL_SHELL="$repo/bin/nix-installer" \
    DOTFILES_SKIP_SUDO_KEEPALIVE=0 DOTFILES_SKIP_ROSETTA_INSTALL=0 \
    /bin/zsh "$repo/main.sh" --profile full > "$output_file" 2>&1 || exit_status=$?
  REPLY="$exit_status"
}

test_main_gate_off_keeps_core_setup_and_profile() {
  local repo
  local output_file

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  write_feature_config "$repo" false
  write_main_fixture "$repo" "$repo/events.log"

  run_main_fixture "$repo" "$output_file"
  (( REPLY == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    fail "main.sh must succeed with features.macos=false"
  }

  assert_output_contains "$output_file" 'features.macos=false'
  assert_output_contains "$output_file" 'Profile: full'
  assert_output_contains "$output_file" 'Setup completed successfully!'
  assert_contains "$repo/events.log" 'sudo:-v'
  assert_contains "$repo/events.log" 'sudo:-n true'
  assert_contains "$repo/events.log" 'nix_install:--profile full'
  assert_contains "$repo/events.log" 'chezmoi_apply:--profile full'
  assert_contains "$repo/events.log" 'setup_agent_files:'
  assert_contains "$repo/events.log" 'setup_git_hooks:--profile full'
  assert_contains "$repo/events.log" 'mise:install python uv'
  assert_contains "$repo/events.log" 'mise:install'
  assert_not_contains "$repo/events.log" 'install_homebrew:'
  assert_not_contains "$repo/events.log" 'install_mas_apps:'
  assert_not_contains "$repo/events.log" 'pkgutil:'
  assert_not_contains "$repo/events.log" 'softwareupdate'

  rm -rf -- "$repo"
}

test_main_gate_on_retains_optional_setup() {
  local repo
  local output_file

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  write_feature_config "$repo" true
  write_main_fixture "$repo" "$repo/events.log"

  run_main_fixture "$repo" "$output_file"
  (( REPLY == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    fail "main.sh must retain the enabled macOS setup path"
  }

  assert_output_contains "$output_file" 'Profile: full'
  assert_output_contains "$output_file" 'Setup completed successfully!'
  assert_contains "$repo/events.log" 'sudo:softwareupdate --install-rosetta --agree-to-license'
  assert_contains "$repo/events.log" 'install_homebrew:--profile full'
  assert_contains "$repo/events.log" 'install_mas_apps:--profile full'
  assert_contains "$repo/events.log" 'nix_install:--profile full'

  rm -rf -- "$repo"
}

prepare_homebrew_fixture() {
  local repo="$1"
  local log_file="$2"

  cp "$REPO_ROOT/scripts/install_homebrew.sh" "$repo/scripts/install_homebrew.sh"
  : > "$log_file"
}

run_homebrew_fixture() {
  local repo="$1"
  local output_file="$2"
  shift 2
  local exit_status=0

  run_isolated "$repo" env -i \
    HOME="$repo/home" USER=fixture-user TMPDIR="$repo/tmp" \
    XDG_CONFIG_HOME="$repo/xdg/config" XDG_CACHE_HOME="$repo/xdg/cache" \
    XDG_DATA_HOME="$repo/xdg/data" XDG_STATE_HOME="$repo/xdg/state" \
    PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_FIXTURE_ROOT="$repo" \
    DOTFILES_TEST_EVENT_LOG="$repo/events.log" \
    /bin/zsh "$repo/scripts/install_homebrew.sh" "$@" > "$output_file" 2>&1 || exit_status=$?
  REPLY="$exit_status"
}

test_install_homebrew_gate_respects_off_and_on() {
  local repo
  local output_file

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  prepare_homebrew_fixture "$repo" "$repo/events.log"

  write_feature_config "$repo" false
  run_homebrew_fixture "$repo" "$output_file" --profile full --dry-run
  (( REPLY == 0 )) || fail "install_homebrew.sh must skip cleanly when the feature is disabled"
  assert_output_contains "$output_file" 'features.macos=false'
  assert_not_contains "$output_file" 'Homebrew already installed'
  assert_not_contains "$repo/events.log" 'brew:'
  assert_not_contains "$repo/events.log" 'curl:'

  : > "$repo/events.log"
  write_feature_config "$repo" true
  run_homebrew_fixture "$repo" "$output_file" --profile full --dry-run
  (( REPLY == 0 )) || fail "install_homebrew.sh must retain the enabled path"
  assert_output_contains "$output_file" 'Homebrew already installed at'
  assert_not_contains "$output_file" 'features.macos=false'
  assert_not_contains "$repo/events.log" 'curl:'

  rm -rf -- "$repo"
}

prepare_mas_fixture() {
  local repo="$1"
  local log_file="$2"

  cp "$REPO_ROOT/scripts/install_mas_apps.sh" "$repo/scripts/install_mas_apps.sh"
  cat > "$repo/config/nix/mas-apps.nix" <<'EOF'
{
  FixtureApp = 123456789;
}
EOF
  cat > "$repo/bin/nix" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125
print -r -- "nix:$*" >> "$event_log"
expected_expr='let apps = import '"$fixture_root"'/config/nix/mas-apps.nix; in builtins.concatStringsSep "\n" (map (name: name + "\t" + builtins.toString apps.${name}) (builtins.attrNames apps))'
[[ $# == 7 && "$1" == eval && "$2" == --raw && "$3" == --impure \
  && "$4" == --extra-experimental-features && "$5" == "nix-command flakes" \
  && "$6" == --expr && "$7" == "$expected_expr" ]] || {
  print -u2 -- "unexpected fake nix command: $*"
  exit 125
}
printf 'FixtureApp\t123456789\n'
EOF
  chmod +x "$repo/bin/nix"
  cat > "$repo/bin/mas" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
fixture_root="${DOTFILES_TEST_FIXTURE_ROOT:?}"
event_log="${DOTFILES_TEST_EVENT_LOG:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)" || exit 125
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || exit 125
print -r -- "mas:$*" >> "$event_log"
case "${1:-}" in
  list)
    [[ $# == 1 ]] || exit 125
    ;;
  install)
    [[ $# == 2 && "${2:-}" == 123456789 ]] || exit 125
    ;;
  *)
    print -u2 -- "unexpected fake mas command: $*"
    exit 125
    ;;
esac
EOF
  chmod +x "$repo/bin/mas"
  : > "$log_file"
}

run_mas_fixture() {
  local repo="$1"
  local output_file="$2"
  shift 2
  local exit_status=0

  run_isolated "$repo" env -i \
    HOME="$repo/home" USER=fixture-user TMPDIR="$repo/tmp" \
    XDG_CONFIG_HOME="$repo/xdg/config" XDG_CACHE_HOME="$repo/xdg/cache" \
    XDG_DATA_HOME="$repo/xdg/data" XDG_STATE_HOME="$repo/xdg/state" \
    PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_FIXTURE_ROOT="$repo" \
    DOTFILES_TEST_EVENT_LOG="$repo/events.log" \
    /bin/zsh "$repo/scripts/install_mas_apps.sh" "$@" > "$output_file" 2>&1 || exit_status=$?
  REPLY="$exit_status"
}

test_install_mas_apps_gate_respects_off_and_on() {
  local repo
  local output_file

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  prepare_mas_fixture "$repo" "$repo/events.log"

  write_feature_config "$repo" false
  run_mas_fixture "$repo" "$output_file" --profile full
  (( REPLY == 0 )) || fail "install_mas_apps.sh must skip cleanly when the feature is disabled"
  assert_output_contains "$output_file" 'features.macos=false'
  assert_not_contains "$repo/events.log" 'nix:'
  assert_not_contains "$repo/events.log" 'mas:'

  : > "$repo/events.log"
  write_feature_config "$repo" true
  run_mas_fixture "$repo" "$output_file" --profile full
  (( REPLY == 0 )) || {
    sed -n '1,160p' "$output_file" >&2
    fail "install_mas_apps.sh must retain the enabled path"
  }
  assert_output_contains "$output_file" 'Installed FixtureApp'
  assert_contains "$repo/events.log" 'nix:eval'
  assert_contains "$repo/events.log" 'mas:list'
  assert_contains "$repo/events.log" 'mas:install 123456789'

  rm -rf -- "$repo"
}

prepare_nix_fixture() {
  local repo="$1"
  local log_file="$2"

  cp "$REPO_ROOT/scripts/nix_install.sh" "$repo/scripts/nix_install.sh"
  write_event_command "$repo/bin/darwin-rebuild" darwin-rebuild '[[ "$#" == 4 && "$1" == switch && "$2" == --impure && "$3" == --flake ]] || exit 125'
  write_fake_sudo_for_nix "$repo/bin"
  mkdir -p "$repo/home/.config" "$repo/config-home" "$repo/etc/pam.d"
  print -r -- 'sudo-local' > "$repo/etc/pam.d/sudo_local"
  print -r -- 'bashrc' > "$repo/etc/bashrc"
  print -r -- 'zshrc' > "$repo/etc/zshrc"
  : > "$log_file"
}

run_nix_fixture() {
  local repo="$1"
  local output_file="$2"
  shift 2
  local exit_status=0

  run_isolated "$repo" env -i \
    HOME="$repo/home" USER=fixture-user TMPDIR="$repo/tmp" \
    XDG_CONFIG_HOME="$repo/config-home" XDG_CACHE_HOME="$repo/xdg/cache" \
    XDG_DATA_HOME="$repo/xdg/data" XDG_STATE_HOME="$repo/xdg/state" \
    PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_PATH="$repo/bin:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_TEST_FIXTURE_ROOT="$repo" \
    DOTFILES_TEST_EVENT_LOG="$repo/events.log" \
    DOTFILES_HOME_MANAGER_BACKUP_ARCHIVE_EPOCH=1700000000 \
    DOTFILES_DARWIN_SUDO_LOCAL_PATH="$repo/etc/pam.d/sudo_local" \
    DOTFILES_DARWIN_ETC_SHELL_RC_PATHS="$repo/etc/bashrc:$repo/etc/zshrc" \
    /bin/bash "$repo/scripts/nix_install.sh" "$@" > "$output_file" 2>&1 || exit_status=$?
  REPLY="$exit_status"
}

darwin_system_attr() {
  case "$(/usr/bin/uname -m)" in
    arm64|aarch64)
      print -r -- aarch64-darwin
      ;;
    x86_64|amd64)
      print -r -- x86_64-darwin
      ;;
    *)
      fail "unsupported macOS test architecture"
      ;;
  esac
}

test_nix_gate_only_skips_homebrew_and_touchid() {
  local repo
  local output_file
  local system_attr

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  prepare_nix_fixture "$repo" "$repo/events.log"
  system_attr="$(darwin_system_attr)"

  write_feature_config "$repo" false
  run_nix_fixture "$repo" "$output_file" --profile full --with-gui-apps
  (( REPLY == 0 )) || {
    sed -n '1,200p' "$output_file" >&2
    fail "nix_install.sh must retain Darwin switching when the feature is disabled"
  }

  assert_output_contains "$output_file" 'features.macos=false'
  assert_output_contains "$output_file" 'Nix profile: full'
  assert_output_contains "$output_file" "Flake output: ${system_attr}-full"
  assert_contains "$repo/events.log" 'darwin-rebuild:switch --impure --flake'
  assert_contains "$repo/events.log" "sudo:env HOME=/var/root DOTFILES_USERNAME=fixture-user darwin-rebuild switch"
  assert_contains "$repo/events.log" 'sudo:mv '
  assert_contains "$repo/events.log" "$repo/etc/bashrc $repo/etc/bashrc.before-nix-darwin"
  assert_contains "$repo/events.log" "$repo/etc/zshrc $repo/etc/zshrc.before-nix-darwin"
  assert_not_contains "$repo/events.log" "$repo/etc/pam.d/sudo_local $repo/etc/pam.d/sudo_local.before-nix-darwin"
  assert_file "$repo/etc/pam.d/sudo_local"
  assert_not_exists "$repo/etc/pam.d/sudo_local.before-nix-darwin"
  assert_not_exists "$repo/etc/bashrc"
  assert_not_exists "$repo/etc/zshrc"
  assert_file "$repo/etc/bashrc.before-nix-darwin"
  assert_file "$repo/etc/zshrc.before-nix-darwin"

  rm -rf -- "$repo"
}

test_nix_gate_on_retains_touchid_backup() {
  local repo
  local output_file
  local system_attr

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  prepare_nix_fixture "$repo" "$repo/events.log"
  system_attr="$(darwin_system_attr)"

  write_feature_config "$repo" true
  run_nix_fixture "$repo" "$output_file" --profile full --with-gui-apps
  (( REPLY == 0 )) || {
    sed -n '1,200p' "$output_file" >&2
    fail "nix_install.sh must retain the enabled Touch ID setup path"
  }

  assert_output_contains "$output_file" 'Nix profile: full'
  assert_output_contains "$output_file" "Flake output: ${system_attr}-full"
  assert_contains "$repo/events.log" "$repo/etc/pam.d/sudo_local $repo/etc/pam.d/sudo_local.before-nix-darwin"
  assert_not_exists "$repo/etc/pam.d/sudo_local"
  assert_file "$repo/etc/pam.d/sudo_local.before-nix-darwin"
  assert_contains "$repo/events.log" 'darwin-rebuild:switch --impure --flake'

  rm -rf -- "$repo"
}

test_invalid_feature_config_fails_before_external_actions() {
  local repo
  local output_file
  local exit_status

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  write_invalid_feature_config "$repo"
  write_main_fixture "$repo" "$repo/events.log"
  run_main_fixture "$repo" "$output_file"
  exit_status="$REPLY"
  (( exit_status != 0 )) || fail 'main.sh must reject malformed feature configuration'
  [[ ! -s "$repo/events.log" ]] || fail 'main.sh must perform no external action for malformed config'
  rm -rf -- "$repo"

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  write_invalid_feature_config "$repo"
  prepare_homebrew_fixture "$repo" "$repo/events.log"
  run_homebrew_fixture "$repo" "$output_file" --profile full
  exit_status="$REPLY"
  (( exit_status != 0 )) || fail 'install_homebrew.sh must reject malformed feature configuration'
  [[ ! -s "$repo/events.log" ]] || fail 'install_homebrew.sh must perform no external action for malformed config'
  rm -rf -- "$repo"

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  write_invalid_feature_config "$repo"
  prepare_mas_fixture "$repo" "$repo/events.log"
  run_mas_fixture "$repo" "$output_file" --profile full
  exit_status="$REPLY"
  (( exit_status != 0 )) || fail 'install_mas_apps.sh must reject malformed feature configuration'
  [[ ! -s "$repo/events.log" ]] || fail 'install_mas_apps.sh must perform no external action for malformed config'
  rm -rf -- "$repo"

  make_fixture_dir
  repo="$REPLY"
  output_file="$repo/output.log"
  write_common_fixture_files "$repo"
  write_invalid_feature_config "$repo"
  prepare_nix_fixture "$repo" "$repo/events.log"
  run_nix_fixture "$repo" "$output_file" --profile full --with-gui-apps
  exit_status="$REPLY"
  (( exit_status != 0 )) || fail 'nix_install.sh must reject malformed feature configuration'
  [[ ! -s "$repo/events.log" ]] || fail 'nix_install.sh must perform no external action for malformed config'
  assert_file "$repo/etc/pam.d/sudo_local"
  assert_file "$repo/etc/bashrc"
  assert_file "$repo/etc/zshrc"
  assert_not_exists "$repo/etc/pam.d/sudo_local.before-nix-darwin"
  assert_not_exists "$repo/etc/bashrc.before-nix-darwin"
  assert_not_exists "$repo/etc/zshrc.before-nix-darwin"
  rm -rf -- "$repo"
}

main() {
  skip_unless_macos "test_macos_entrypoints.sh" || return 0

  test_main_gate_off_keeps_core_setup_and_profile
  test_main_gate_on_retains_optional_setup
  test_install_homebrew_gate_respects_off_and_on
  test_install_mas_apps_gate_respects_off_and_on
  test_nix_gate_only_skips_homebrew_and_touchid
  test_nix_gate_on_retains_touchid_backup
  test_invalid_feature_config_fails_before_external_actions
  print -r -- 'macOS entrypoint feature gate tests passed'
}

main "$@"
