#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SETUP_HERMES_SCRIPT="$REPO_ROOT/scripts/setup_hermes_agent.sh"
readonly UPDATE_MANAGED_VERSIONS_SCRIPT="$REPO_ROOT/scripts/update_managed_versions.sh"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"

source "$TEST_DIR/lib/assertions.sh"

emit_matrix_result() {
  local line="$1"

  print -r -- "$line"
  if [[ -n "${MATRIX_RESULT_LOG_DIR:-}" ]]; then
    mkdir -p "$MATRIX_RESULT_LOG_DIR"
    print -r -- "$line" >> "$MATRIX_RESULT_LOG_DIR/matrix-results.log"
  fi
}

is_test_macos() {
  [[ "$OSTYPE" == darwin* ]]
}

bash_major_version() {
  local bash_bin="$1"

  "$bash_bin" -c 'printf "%s\n" "${BASH_VERSINFO[0]}"'
}

select_bash_for_major() {
  local expected_major="$1"
  local candidate
  local actual_major
  local -a candidates=()

  if [[ "$expected_major" == "3" ]]; then
    if [[ -n "${BASH32_BIN:-}" ]]; then
      candidates=("$BASH32_BIN")
    elif is_test_macos; then
      candidates=(/bin/bash)
    fi
  else
    if [[ -n "${BASH5_BIN:-}" ]]; then
      candidates=("$BASH5_BIN")
    else
      candidates=(/run/current-system/sw/bin/bash /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash)
    fi
  fi

  for candidate in "${candidates[@]}"; do
    [[ "$candidate" == /* && -x "$candidate" ]] || continue
    actual_major="$(bash_major_version "$candidate" 2>/dev/null)" || continue
    if [[ "$actual_major" == "$expected_major" ]]; then
      REPLY="$candidate"
      return 0
    fi
  done

  return 1
}

copy_setup_script() {
  local repo="$1"

  mkdir -p "$repo/scripts/lib"
  cp "$SETUP_HERMES_SCRIPT" "$repo/scripts/setup_hermes_agent.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"
  chmod +x "$repo/scripts/setup_hermes_agent.sh"
}

write_strict_fake_bash() {
  local bin_dir="$1"

  cat > "$bin_dir/bash" <<'EOF'
#!/bin/zsh
set -euo pipefail
event_log="${HERMES_TEST_EVENT_LOG:?}"
target_bash="${HERMES_TEST_TARGET_BASH:?}"
fixture_root="${HERMES_TEST_FIXTURE_ROOT:?}"
fixture_root="$(cd -P "$fixture_root" && pwd -P)"
allowed_script="${HERMES_TEST_ALLOWED_BASH_SCRIPT:?}"
event_log_parent="$(cd -P "${event_log:h}" 2>/dev/null && pwd -P)"
[[ "$event_log_parent" == "$fixture_root" || "$event_log_parent" == "$fixture_root"/* ]] || {
  print -u2 -- "rejected fake bash event log: $event_log"
  exit 126
}
[[ "$target_bash" == /* && "${target_bash:t}" == bash && -x "$target_bash" && ! -d "$target_bash" ]] || {
  print -u2 -- "rejected fake bash target: $target_bash"
  exit 126
}
(( $# == 2 )) || {
  print -u2 -- "rejected fake bash argv: $*"
  exit 126
}
script_path="$1"
[[ "$script_path" == "$allowed_script" ]] || {
  print -u2 -- "rejected fake bash script: $script_path"
  exit 126
}
[[ "$script_path" == /* && "$script_path" != *'/../'* && "$script_path" != */.. \
  && "$script_path" != *'/./'* && "$script_path" != */. ]] || exit 126
script_parent="$(cd -P "${script_path:h}" 2>/dev/null && pwd -P)" || exit 126
[[ "$script_parent" == "$fixture_root" || "$script_parent" == "$fixture_root"/* ]] || exit 126
resolve_existing_path() {
  local candidate="$1"
  local candidate_dir
  local link_target

  while [[ -L "$candidate" ]]; do
    candidate_dir="$(cd -P "${candidate:h}" 2>/dev/null && pwd -P)" || return 1
    link_target="$(readlink "$candidate")" || return 1
    if [[ "$link_target" == /* ]]; then
      candidate="$link_target"
    else
      candidate="$candidate_dir/$link_target"
    fi
  done
  candidate_dir="$(cd -P "${candidate:h}" 2>/dev/null && pwd -P)" || return 1
  print -r -- "$candidate_dir/${candidate:t}"
}
resolved_script="$(resolve_existing_path "$script_path")" || exit 126
[[ "$resolved_script" == "$fixture_root"/* && -f "$resolved_script" ]] || exit 126
[[ "$2" == --update-only ]] || exit 126
print -r -- "bash:$script_path --update-only" >> "$event_log"
exec "$target_bash" "$script_path" --update-only
EOF
  chmod +x "$bin_dir/bash"
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

test_setup_hermes_runs_under_explicit_bash() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local bin_dir
  local log_file
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  log_file="$repo/hermes.log"
  mkdir -p "$bin_dir" "$repo/scripts/lib"
  cp "$SETUP_HERMES_SCRIPT" "$repo/scripts/setup_hermes_agent.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"
  cat > "$bin_dir/hermes" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "hermes:$*" >> "$HERMES_TEST_LOG"
EOF
  chmod +x "$bin_dir/hermes"

  HERMES_TEST_LOG="$log_file" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" "$repo/scripts/setup_hermes_agent.sh" --update-only \
    > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,120p' "$repo/output.log" >&2
    fail "expected explicit Bash Hermes update to succeed"
  }
  assert_contains "$log_file" 'hermes:update --yes'

  rm -rf "$repo"
}

test_setup_propagates_hermes_update_failure() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local bin_dir
  local event_log
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  mkdir -p "$bin_dir"
  copy_setup_script "$repo"

  cat > "$bin_dir/hermes" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "hermes:$*" >> "$HERMES_TEST_EVENT_LOG"
exit 41
EOF
  cat > "$bin_dir/curl" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "curl:$*" >> "$HERMES_TEST_EVENT_LOG"
exit 99
EOF
  chmod +x "$bin_dir/hermes" "$bin_dir/curl"

  HERMES_TEST_EVENT_LOG="$event_log" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" "$repo/scripts/setup_hermes_agent.sh" --update-only \
    > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status == 41 )) || fail "Hermes update failure must reach the setup helper caller"
  assert_contains "$event_log" 'hermes:update --yes'
  assert_not_contains "$event_log" 'curl:'

  rm -rf "$repo"
}

test_setup_runs_downloaded_installer_when_hermes_is_missing() {
  local repo
  local bin_dir
  local installer_source
  local real_cp
  local allowed_temp_root

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  installer_source="$repo/install-source.sh"
  real_cp="/bin/cp"
  [[ -x "$real_cp" ]] || real_cp="/usr/bin/cp"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"

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
(( \$# == 4 )) && [[ "\$1" == -fsSL && "\$2" == https://example.invalid/install.sh \
  && "\$3" == -o ]] || {
  print -u2 -- "rejected curl argv: \$*"
  exit 126
}
output_path="\$4"
parent="\${output_path%/*}"
[[ "\$parent" == "$allowed_temp_root"/dotfiles-hermes-installer."\$PPID".0 ]] || exit 126
case "\${output_path##*/}" in
  install.*) ;;
  *) exit 126 ;;
esac
[[ -f "\$output_path" && ! -L "\$output_path" ]] || exit 126
"$real_cp" "$installer_source" "\$output_path"
EOF
  chmod +x "$bin_dir/curl"

  HERMES_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$TEST_ZSH_BIN" "$repo/scripts/setup_hermes_agent.sh" \
    --installer-url "https://example.invalid/install.sh" > "$repo/output.log" 2>&1

  assert_contains "$repo/installer.log" 'installer-ran'
  assert_output_contains "$repo/output.log" 'Downloading the Hermes Agent installer from https://example.invalid/install.sh'

  rm -rf "$repo"
}

test_setup_installer_temp_is_private_and_cleans() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local bin_dir
  local event_log
  local installer_source
  local temp_dir
  local real_mkdir
  local real_mktemp
  local real_cp
  local allowed_temp_root
  local previous_umask
  local guard_status
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  installer_source="$repo/installer-source.sh"
  real_mkdir="/bin/mkdir"
  [[ -x "$real_mkdir" ]] || real_mkdir="/usr/bin/mkdir"
  real_mktemp="/usr/bin/mktemp"
  [[ -x "$real_mktemp" ]] || real_mktemp="/bin/mktemp"
  real_cp="/bin/cp"
  [[ -x "$real_cp" ]] || real_cp="/usr/bin/cp"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"
  previous_umask="$(umask)"
  mkdir -p "$bin_dir"
  copy_setup_script "$repo"
  cat > "$installer_source" <<EOF
#!/usr/bin/env bash
set -euo pipefail
printf 'installer-ran\n' >> "$repo/installer.log"
EOF
  cat > "$bin_dir/mkdir" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mkdir argv: \$*"
  exit 126
fi
temp_path="\$1"
[[ "\$temp_path" == "$allowed_temp_root"/dotfiles-hermes-installer."\$PPID".0 ]] || {
  print -u2 -- "rejected mkdir path: \$temp_path"
  exit 126
}
"$real_mkdir" "\$temp_path"
if [[ -d "\$temp_path" ]]; then
  if [[ "\$OSTYPE" == darwin* ]]; then
    mode="\$(stat -f '%Lp' "\$temp_path")"
  else
    mode="\$(stat -c '%a' "\$temp_path")"
  fi
  print -r -- "temp-dir=\$temp_path" >> "\$HERMES_TEST_EVENT_LOG"
  print -r -- "temp-dir-created-mode=\$mode" >> "\$HERMES_TEST_EVENT_LOG"
fi
EOF
  cat > "$bin_dir/mktemp" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mktemp argv: \$*"
  exit 126
fi
template="\$1"
parent="\${template%/*}"
[[ "\$parent" == "$allowed_temp_root"/dotfiles-hermes-installer."\$PPID".0 ]] || exit 126
[[ "\${template##*/}" == install.XXXXXX ]] || exit 126
result="\$("$real_mktemp" "\$template")"
if [[ "\$result" == "$allowed_temp_root"/dotfiles-hermes-installer."\$PPID".0/install.* ]]; then
  if [[ "\$OSTYPE" == darwin* ]]; then
    mode="\$(stat -f '%Lp' "\$result")"
  else
    mode="\$(stat -c '%a' "\$result")"
  fi
  print -r -- "installer-file=\$result" >> "\$HERMES_TEST_EVENT_LOG"
  print -r -- "installer-file-mode=\$mode" >> "\$HERMES_TEST_EVENT_LOG"
else
  print -u2 -- "rejected mktemp result: \$result"
  exit 126
fi
print -r -- "\$result"
EOF
  cat > "$bin_dir/curl" <<EOF
#!/bin/zsh
set -euo pipefail
(( \$# == 4 )) && [[ "\$1" == -fsSL && "\$2" == https://example.invalid/install.sh \
  && "\$3" == -o ]] || {
  print -u2 -- "rejected curl argv: \$*"
  exit 126
}
output_path="\$4"
parent="\${output_path%/*}"
[[ "\$parent" == "$allowed_temp_root"/dotfiles-hermes-installer."\$PPID".0 ]] || exit 126
case "\${output_path##*/}" in
  install.*) ;;
  *) exit 126 ;;
esac
[[ -f "\$output_path" && ! -L "\$output_path" ]] || {
  print -r -- 'installer-file-preexisting=no' >> "\$HERMES_TEST_EVENT_LOG"
  exit 92
}
print -r -- 'installer-file-preexisting=yes' >> "\$HERMES_TEST_EVENT_LOG"
"$real_cp" "$installer_source" "\$output_path"
EOF
  chmod +x "$bin_dir"/*

  umask 000
  HERMES_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" \
    HERMES_TEST_EVENT_LOG="$event_log" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" "$repo/scripts/setup_hermes_agent.sh" \
    --installer-url "https://example.invalid/install.sh" > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,160p' "$repo/output.log" >&2
    sed -n '1,160p' "$event_log" >&2
    fail "Hermes installer should use a private pre-created payload file"
  }
  assert_contains "$repo/installer.log" 'installer-ran'
  assert_contains "$event_log" 'temp-dir-created-mode=700'
  assert_contains "$event_log" 'installer-file-mode=600'
  assert_contains "$event_log" 'installer-file-preexisting=yes'
  temp_dir="$(grep '^temp-dir=' "$event_log" | head -1 | sed 's/^temp-dir=//' || true)"
  [[ -n "$temp_dir" ]] || fail "expected Hermes installer temp directory"
  assert_not_exists "$temp_dir"

  guard_status=0
  HERMES_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" HERMES_TEST_EVENT_LOG="$event_log" \
    "$bin_dir/curl" -fsSL https://example.invalid/install.sh -o "$repo/escaped" \
    > "$repo/curl-guard.log" 2>&1 || guard_status=$?
  (( guard_status != 0 )) || fail "fake curl must reject an out-of-fixture payload path"

  umask "$previous_umask"
  rm -rf "$repo"
}

test_setup_installer_temp_cleans_on_signal() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local bin_dir
  local event_log
  local temp_dir
  local real_mkdir
  local allowed_temp_root
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  real_mkdir="/bin/mkdir"
  [[ -x "$real_mkdir" ]] || real_mkdir="/usr/bin/mkdir"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"
  mkdir -p "$bin_dir"
  copy_setup_script "$repo"
  cat > "$bin_dir/mkdir" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )); then
  print -u2 -- "rejected mkdir argv: \$*"
  exit 126
fi
temp_path="\$1"
[[ "\$temp_path" == "$allowed_temp_root"/dotfiles-hermes-installer."\$PPID".0 ]] || {
  print -u2 -- "rejected mkdir path: \$temp_path"
  exit 126
}
"$real_mkdir" "\$temp_path"
print -r -- "temp-dir=\$temp_path" >> "\$HERMES_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/curl" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- 'curl-sends-term' >> "$HERMES_TEST_EVENT_LOG"
kill -TERM "$PPID"
sleep 1
exit 0
EOF
  chmod +x "$bin_dir"/*

  HERMES_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" \
    HERMES_TEST_EVENT_LOG="$event_log" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" "$repo/scripts/setup_hermes_agent.sh" \
    --installer-url "https://example.invalid/install.sh" > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status != 0 )) || fail "Hermes installer TERM must propagate a non-zero status"
  assert_contains "$event_log" 'curl-sends-term'
  temp_dir="$(grep '^temp-dir=' "$event_log" | head -1 | sed 's/^temp-dir=//' || true)"
  [[ -n "$temp_dir" ]] || fail "expected Hermes installer temp directory before TERM"
  assert_not_exists "$temp_dir"

  rm -rf "$repo"
}

test_setup_installer_temp_cleans_when_directory_creation_is_interrupted() {
  local bash_bin="${1:-/bin/bash}"
  local repo
  local bin_dir
  local event_log
  local temp_dir
  local real_mkdir
  local allowed_temp_root
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  real_mkdir="/bin/mkdir"
  [[ -x "$real_mkdir" ]] || real_mkdir="/usr/bin/mkdir"
  allowed_temp_root="/private/tmp"
  is_test_macos || allowed_temp_root="${TMPDIR:-/tmp}"
  mkdir -p "$bin_dir"
  copy_setup_script "$repo"
  cat > "$bin_dir/mkdir" <<EOF
#!/bin/zsh
set -euo pipefail
if (( \$# != 1 )) || [[ "\$1" != "$allowed_temp_root"/dotfiles-hermes-installer."\$PPID".0 ]]; then
  print -u2 -- "rejected mkdir argv: \$*"
  exit 126
fi
"$real_mkdir" "\$1"
print -r -- "temp-dir=\$1" >> "\$HERMES_TEST_EVENT_LOG"
print -r -- 'mkdir-sends-term' >> "\$HERMES_TEST_EVENT_LOG"
kill -TERM "\$PPID"
sleep 1
exit 0
EOF
  chmod +x "$bin_dir/mkdir"

  HERMES_TEST_ALLOWED_TEMP_ROOT="$allowed_temp_root" \
    HERMES_TEST_EVENT_LOG="$event_log" PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$bash_bin" "$repo/scripts/setup_hermes_agent.sh" \
    --installer-url "https://example.invalid/install.sh" > "$repo/output.log" 2>&1 || exit_status=$?

  temp_dir="$(grep '^temp-dir=' "$event_log" 2>/dev/null | head -1 | sed 's/^temp-dir=//' || true)"
  if (( exit_status == 0 )); then
    rm -rf "$repo"
    fail "Hermes installer TERM during directory creation must propagate a non-zero status"
  fi
  [[ -n "$temp_dir" ]] || {
    rm -rf "$repo"
    fail "expected Hermes installer temp directory before creation interruption"
  }
  if [[ -e "$temp_dir" ]]; then
    rm -rf "$temp_dir" "$repo"
    fail "Hermes installer directory must be removed when creation is interrupted"
  fi

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
  cp "$REPO_ROOT/scripts/lib/features.sh" "$repo/scripts/lib/features.sh"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$repo/home/.chezmoidata.toml"
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

test_managed_update_script_delegates_real_hermes_through_bash() {
  local target_bash="${1:-/bin/bash}"
  local repo
  local home_dir
  local bin_dir
  local event_log
  local repo_real
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  home_dir="$repo/home"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  repo_real="$(cd "$repo" && pwd -P)"

  mkdir -p "$repo/scripts/lib" "$repo/config/nix" "$home_dir" "$bin_dir"
  cp "$UPDATE_MANAGED_VERSIONS_SCRIPT" "$repo/scripts/update_managed_versions.sh"
  cp "$REPO_ROOT/scripts/lib/features.sh" "$repo/scripts/lib/features.sh"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$repo/home/.chezmoidata.toml"
  cp "$SETUP_HERMES_SCRIPT" "$repo/scripts/setup_hermes_agent.sh"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/scripts/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/command.sh" "$repo/scripts/lib/command.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew.sh" "$repo/scripts/lib/homebrew.sh"
  cp "$REPO_ROOT/scripts/lib/homebrew_fallback.sh" "$repo/scripts/lib/homebrew_fallback.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/scripts/lib/runtime.sh"

  write_strict_fake_bash "$bin_dir"
  cat > "$bin_dir/hermes" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "hermes:$*" >> "$HERMES_TEST_EVENT_LOG"
EOF
  cat > "$bin_dir/zsh" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "zsh:$*" >> "$HERMES_TEST_EVENT_LOG"
exit 99
EOF
  chmod +x "$bin_dir/bash" "$bin_dir/hermes" "$bin_dir/zsh"

  HERMES_TEST_EVENT_LOG="$event_log" HERMES_TEST_TARGET_BASH="$target_bash" \
    HERMES_TEST_ALLOWED_BASH_SCRIPT="$repo_real/scripts/setup_hermes_agent.sh" \
    HERMES_TEST_FIXTURE_ROOT="$repo" \
    HOME="$home_dir" USER=dotfiles-test \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    DOTFILES_SHOW_PROGRESS=0 \
    "$TEST_ZSH_BIN" "$repo/scripts/update_managed_versions.sh" \
    --only hermes --shell bash > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,160p' "$repo/output.log" >&2
    sed -n '1,160p' "$event_log" >&2
    fail "expected managed Hermes scope to succeed through Bash"
  }
  assert_contains "$event_log" "bash:$repo_real/scripts/setup_hermes_agent.sh --update-only"
  assert_contains "$event_log" 'hermes:update --yes'
  assert_not_contains "$event_log" 'zsh:'

  rm -rf "$repo"
}

test_setup_hermes_direct_copy_uses_bash_shebang() {
  local target_bash="${1:-/bin/bash}"
  local repo
  local bin_dir
  local event_log
  local script_copy
  local symlink_copy
  local exit_status=0

  make_temp_dir
  repo="$REPLY"
  bin_dir="$repo/bin"
  event_log="$repo/events.log"
  script_copy="$repo/setup_hermes_agent.sh"
  symlink_copy="$repo/link/setup_hermes_agent.sh"

  mkdir -p "$bin_dir" "$repo/lib" "$repo/link"
  cp "$SETUP_HERMES_SCRIPT" "$script_copy"
  cp "$REPO_ROOT/scripts/lib/setup_profile.sh" "$repo/lib/setup_profile.sh"
  cp "$REPO_ROOT/scripts/lib/runtime.sh" "$repo/lib/runtime.sh"
  chmod +x "$script_copy"
  ln -s "$script_copy" "$symlink_copy"
  write_strict_fake_bash "$bin_dir"
  cat > "$bin_dir/zsh" <<EOF
#!/bin/zsh
set -euo pipefail
print -r -- "zsh:\$*" >> "\$HERMES_TEST_EVENT_LOG"
print -u2 -- 'rejected fake zsh dispatch'
exit 125
EOF
  cat > "$bin_dir/hermes" <<'EOF'
#!/bin/zsh
set -euo pipefail
print -r -- "hermes:$*" >> "$HERMES_TEST_EVENT_LOG"
EOF
  chmod +x "$bin_dir/bash" "$bin_dir/zsh" "$bin_dir/hermes"

  HERMES_TEST_EVENT_LOG="$event_log" HERMES_TEST_TARGET_BASH="$target_bash" \
    HERMES_TEST_ALLOWED_BASH_SCRIPT="$script_copy" HERMES_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$script_copy" --update-only > "$repo/output.log" 2>&1 || exit_status=$?

  (( exit_status == 0 )) || {
    sed -n '1,120p' "$repo/output.log" >&2
    fail "expected direct Hermes copy to run"
  }
  assert_contains "$event_log" "bash:$script_copy --update-only"
  assert_not_contains "$event_log" 'zsh:'
  assert_contains "$event_log" 'hermes:update --yes'

  : > "$event_log"
  exit_status=0
  HERMES_TEST_EVENT_LOG="$event_log" HERMES_TEST_TARGET_BASH="$target_bash" \
    HERMES_TEST_ALLOWED_BASH_SCRIPT="$symlink_copy" HERMES_TEST_FIXTURE_ROOT="$repo" \
    PATH="$bin_dir:/bin:/usr/bin:/usr/sbin:/sbin" \
    "$symlink_copy" --update-only > "$repo/symlink-output.log" 2>&1 || exit_status=$?
  (( exit_status == 0 )) || {
    sed -n '1,120p' "$repo/symlink-output.log" >&2
    fail "expected direct Hermes symlink to resolve its target directory"
  }
  assert_contains "$event_log" "bash:$symlink_copy --update-only"
  assert_not_contains "$event_log" 'zsh:'
  assert_contains "$event_log" 'hermes:update --yes'

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
  cp "$REPO_ROOT/scripts/lib/features.sh" "$repo/scripts/lib/features.sh"
  cp "$REPO_ROOT/home/.chezmoidata.toml" "$repo/home/.chezmoidata.toml"
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

run_hermes_bash_matrix() {
  local expected_major
  local bash_bin
  local matrix_os="linux"
  local matrix_status=0

  is_test_macos && matrix_os="macos"

  for expected_major in 3 5; do
    if select_bash_for_major "$expected_major"; then
      bash_bin="$REPLY"
      test_setup_hermes_runs_under_explicit_bash "$bash_bin" || matrix_status=1
      test_setup_propagates_hermes_update_failure "$bash_bin" || matrix_status=1
      test_setup_hermes_direct_copy_uses_bash_shebang "$bash_bin" || matrix_status=1
      test_setup_installer_temp_is_private_and_cleans "$bash_bin" || matrix_status=1
      test_setup_installer_temp_cleans_on_signal "$bash_bin" || matrix_status=1
      test_setup_installer_temp_cleans_when_directory_creation_is_interrupted "$bash_bin" || matrix_status=1
      test_managed_update_script_delegates_real_hermes_through_bash "$bash_bin" || matrix_status=1
      (( matrix_status == 0 )) && emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=hermes|status=PASS|requirement=required|reason=$bash_bin"
    elif [[ "$expected_major" == 3 ]] && ! is_test_macos; then
      emit_matrix_result "MATRIX_RESULT|os=linux|shell=bash3|target=hermes|status=SKIP|requirement=not-applicable|reason=macos-only"
    else
      emit_matrix_result "MATRIX_RESULT|os=$matrix_os|shell=bash${expected_major}|target=hermes|status=SKIP|requirement=required|reason=bash${expected_major}-unavailable"
      matrix_status=1
    fi
  done
  (( matrix_status == 0 )) || fail "Hermes Bash matrix failed"
}

main() {
  if [[ "${1:-}" == "--focus" ]]; then
    case "${2:-}" in
      installer-temp)
        test_setup_installer_temp_is_private_and_cleans
        ;;
      installer-signal)
        test_setup_installer_temp_cleans_on_signal
        ;;
      installer-create-signal)
        test_setup_installer_temp_cleans_when_directory_creation_is_interrupted
        ;;
      *)
        fail "unknown focus: ${2:-}"
        ;;
    esac
    return 0
  fi

  test_setup_updates_existing_install
  test_setup_check_only_does_not_install
  test_update_only_skips_when_hermes_is_missing
  run_hermes_bash_matrix
  test_setup_runs_downloaded_installer_when_hermes_is_missing
  test_managed_update_script_delegates_hermes_scope
  test_managed_update_script_rejects_nix_input_with_hermes_scope
  echo "hermes agent setup tests passed"
}

main "$@"
