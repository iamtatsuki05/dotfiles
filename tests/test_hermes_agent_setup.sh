#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SETUP_HERMES_SCRIPT="$REPO_ROOT/scripts/setup_hermes_agent.sh"
readonly UPDATE_MANAGED_VERSIONS_SCRIPT="$REPO_ROOT/scripts/update_managed_versions.sh"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

source "$TEST_DIR/lib/assertions.sh"

copy_setup_script() {
  local repo="$1"

  mkdir -p "$repo/scripts/lib"
  cp "$SETUP_HERMES_SCRIPT" "$repo/scripts/setup_hermes_agent.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"
  chmod +x "$repo/scripts/setup_hermes_agent.sh"
}

test_setup_updates_existing_install() {
  local repo
  local bin_dir
  local log_file

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  log_file="$repo/hermes.log"

  mkdir -p "$bin_dir"
  copy_setup_script "$repo"

  cat > "$bin_dir/hermes" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "hermes:\$*" >> "$log_file"
EOF
  chmod +x "$bin_dir/hermes"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/setup_hermes_agent.sh" > "$repo/output.log" 2>&1

  assert_contains "$log_file" 'hermes:update --yes'
  assert_output_contains "$repo/output.log" "Found Hermes Agent at $bin_dir/hermes"

  rm -rf "$repo"
}

test_setup_check_only_does_not_install() {
  local repo
  local bin_dir
  local log_file

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  log_file="$repo/hermes.log"

  mkdir -p "$bin_dir"
  copy_setup_script "$repo"

  cat > "$bin_dir/hermes" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "hermes:\$*" >> "$log_file"
EOF
  chmod +x "$bin_dir/hermes"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/setup_hermes_agent.sh" --check > "$repo/output.log" 2>&1

  assert_contains "$log_file" 'hermes:update --check'
  assert_not_contains "$log_file" 'hermes:update --yes'

  rm -rf "$repo"
}

test_update_only_skips_when_hermes_is_missing() {
  local repo
  local bin_dir

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"

  mkdir -p "$bin_dir"
  copy_setup_script "$repo"

  cat > "$bin_dir/curl" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "curl:\$*" >> "$repo/curl.log"
EOF
  chmod +x "$bin_dir/curl"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/setup_hermes_agent.sh" --update-only > "$repo/output.log" 2>&1

  assert_output_contains "$repo/output.log" "Hermes Agent is not installed; skipping"
  assert_not_exists "$repo/curl.log"

  rm -rf "$repo"
}

test_setup_runs_downloaded_installer_when_hermes_is_missing() {
  local repo
  local bin_dir
  local installer_source

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  installer_source="$repo/install-source.sh"

  mkdir -p "$bin_dir"
  copy_setup_script "$repo"

  cat > "$installer_source" <<EOF
#!/usr/bin/env bash
set -euo pipefail
printf 'installer-ran\n' >> "$repo/installer.log"
EOF

  # Stub curl so the test never reaches the network: it copies a local file
  # instead of downloading, mirroring the -o contract the script relies on.
  cat > "$bin_dir/curl" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
output_path=""
while (( \$# )); do
  case "\$1" in
    -o)
      shift
      output_path="\$1"
      ;;
    *)
      ;;
  esac
  shift
done
cp "$installer_source" "\$output_path"
EOF
  chmod +x "$bin_dir/curl"

  PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/setup_hermes_agent.sh" \
    --installer-url "https://example.invalid/install.sh" > "$repo/output.log" 2>&1

  assert_contains "$repo/installer.log" 'installer-ran'
  assert_output_contains "$repo/output.log" 'Downloading the Hermes Agent installer from https://example.invalid/install.sh'

  rm -rf "$repo"
}

test_managed_update_script_delegates_hermes_scope() {
  local repo
  local home_dir
  local bin_dir
  local log_file

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  log_file="$repo/update.log"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/command.sh" "$repo/scripts/lib/command.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew.sh" "$repo/scripts/lib/homebrew.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew_fallback.sh" "$repo/scripts/lib/homebrew_fallback.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"

  cat > "$repo/scripts/setup_hermes_agent.sh" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
print -r -- "setup_hermes_agent:\$*" >> "$log_file"
EOF
  chmod +x \
    "$repo/scripts/update_managed_versions.sh" \
    "$repo/scripts/setup_hermes_agent.sh"

  HOME="$home_dir" USER=dotfiles-test PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only hermes \
    > "$repo/output.log" 2>&1

  assert_contains "$log_file" 'setup_hermes_agent:--update-only'
  assert_output_contains "$repo/output.log" 'Updating Hermes Agent, which ships outside Nix and mise'

  rm -rf "$repo"
}

test_managed_update_script_rejects_nix_input_with_hermes_scope() {
  local repo
  local home_dir
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/command.sh" "$repo/scripts/lib/command.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew.sh" "$repo/scripts/lib/homebrew.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew_fallback.sh" "$repo/scripts/lib/homebrew_fallback.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"
  chmod +x "$repo/scripts/update_managed_versions.sh"

  HOME="$home_dir" USER=dotfiles-test \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" --only hermes --nix-input nixpkgs \
    > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status != 0 )) || fail "expected --only hermes --nix-input nixpkgs to fail"
  assert_output_contains "$repo/output.log" 'ERROR: --nix-input cannot be used with --only hermes'

  rm -rf "$repo"
}

main() {
  test_setup_updates_existing_install
  test_setup_check_only_does_not_install
  test_update_only_skips_when_hermes_is_missing
  test_setup_runs_downloaded_installer_when_hermes_is_missing
  test_managed_update_script_delegates_hermes_scope
  test_managed_update_script_rejects_nix_input_with_hermes_scope
  echo "hermes agent setup tests passed"
}

main "$@"
