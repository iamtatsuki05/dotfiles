#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SCRIPT="$REPO_ROOT/scripts/claude_account.sh"
readonly PERSONAL_IDENTITY_SHA256="161bd44458076cffd6805741be8805902e457ef44297704e754f2af7a388cfc9"
readonly OTHER_IDENTITY_SHA256="f94b339c865053d2f777bc636c9656da0a6a55073734c1cd203eeb72cccbcc1c"

source "$TEST_DIR/lib/assertions.sh"

assert_line() {
  local file_path="$1"
  local expected="$2"

  assert_file "$file_path"
  grep -Fxq -- "$expected" "$file_path" || fail "expected $file_path to contain line: $expected"
}

setup_fixture() {
  make_temp_dir claude-account-test
  FIXTURE_ROOT="$REPLY"
  FIXTURE_HOME="$FIXTURE_ROOT/home"
  FIXTURE_BIN="$FIXTURE_ROOT/bin"
  CLAUDE_LOG="$FIXTURE_ROOT/claude.log"
  CLAUDE_CALL_LOG="$FIXTURE_ROOT/claude-calls.log"
  mkdir -p "$FIXTURE_HOME" "$FIXTURE_BIN"

  cat > "$FIXTURE_BIN/pgrep" <<'EOF'
#!/bin/sh
if [ "${CLAUDE_RUNNING_PROCESSES:-0}" -gt 0 ]; then
  seq 100 "$((99 + CLAUDE_RUNNING_PROCESSES))"
  exit 0
fi
exit 1
EOF

  cat > "$FIXTURE_BIN/claude" <<'EOF'
#!/bin/sh
if [ "${1:-}" = --version ]; then
  printf '%s (Claude Code)\n' "${CLAUDE_TEST_VERSION:-2.1.261}"
  exit 0
fi
printf '<%s>' "$@" >> "$CLAUDE_CALL_LOG"
printf '\n' >> "$CLAUDE_CALL_LOG"

if [ "${1:-}" = auth ] && [ "${2:-}" = login ]; then
  if [ -n "${CLAUDE_CODE_CUSTOM_OAUTH_URL:-}" ] || [ -n "${CLAUDE_CODE_HOST_CREDS_FILE:-}" ] || [ -n "${USE_STAGING_OAUTH:-}" ]; then
    exit 91
  fi
  exit "${CLAUDE_AUTH_LOGIN_EXIT:-0}"
fi

if [ "${1:-}" = auth ] && [ "${2:-}" = status ]; then
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    printf '{"loggedIn":true,"authMethod":"oauth_token","apiProvider":"firstParty","apiKeySource":null}\n'
  else
    printf '{"loggedIn":true,"authMethod":"%s","apiProvider":"%s","apiKeySource":%s,"email":"%s","orgId":"%s","subscriptionType":"%s"}\n' \
      "${CLAUDE_AUTH_STATUS_METHOD:-claude.ai}" \
      "${CLAUDE_AUTH_STATUS_PROVIDER:-firstParty}" \
      "${CLAUDE_AUTH_STATUS_API_KEY_SOURCE:-null}" \
      "${CLAUDE_AUTH_STATUS_EMAIL:-personal@example.test}" \
      "${CLAUDE_AUTH_STATUS_ORG:-org-personal}" \
      "${CLAUDE_AUTH_STATUS_SUBSCRIPTION:-max}"
  fi
  exit 0
fi

{
  if [ "${CLAUDE_EXPECT_STDIN:-0}" = 1 ]; then
    if IFS= read -r stdin_line; then
      printf 'stdin=%s\n' "$stdin_line"
    else
      printf 'stdin=<eof>\n'
    fi
  fi
  printf 'config_dir=%s\n' "${CLAUDE_CONFIG_DIR:-<unset>}"
  printf 'agent_view=%s\n' "${CLAUDE_CODE_DISABLE_AGENT_VIEW:-<unset>}"
  printf 'token=%s\n' "${CLAUDE_CODE_OAUTH_TOKEN:-<unset>}"
  printf 'api_key=%s\n' "${ANTHROPIC_API_KEY:-<unset>}"
  printf 'auth_token=%s\n' "${ANTHROPIC_AUTH_TOKEN:-<unset>}"
  printf 'base_url=%s\n' "${ANTHROPIC_BASE_URL:-<unset>}"
  printf 'login_command=%s\n' "${DISABLE_LOGIN_COMMAND:-<unset>}"
  printf 'logout_command=%s\n' "${DISABLE_LOGOUT_COMMAND:-<unset>}"
  printf 'subprocess_scrub=%s\n' "${CLAUDE_CODE_SUBPROCESS_ENV_SCRUB:-<unset>}"
  printf 'args='
  printf '<%s>' "$@"
  printf '\n'
} > "$CLAUDE_LOG"
EOF

  chmod +x "$FIXTURE_BIN/pgrep" "$FIXTURE_BIN/claude"
}

run_account() {
  HOME="$FIXTURE_HOME" \
    XDG_CONFIG_HOME="$FIXTURE_HOME/.config" \
    PATH="$FIXTURE_BIN:/bin:/usr/bin" \
    CLAUDE_LOG="$CLAUDE_LOG" \
    CLAUDE_CALL_LOG="$CLAUDE_CALL_LOG" \
    "$SCRIPT" "$@"
}

write_login_registry() {
  local profile="$1"
  local identity_sha256="$2"

  mkdir -p "$FIXTURE_HOME/.config/claude-account/accounts/personal"
  cat > "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json" <<EOF
{
  "version": 1,
  "profiles": {
    "$profile": {
      "identitySha256": "$identity_sha256",
      "subscriptionType": "max"
    }
  }
}
EOF
  chmod 600 "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json"
}

test_default_run_requires_matching_full_login_and_forwards_arguments() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  ANTHROPIC_API_KEY=api-secret \
    CLAUDE_CODE_OAUTH_TOKEN=inherited-setup-token \
    ANTHROPIC_AUTH_TOKEN=auth-secret \
    ANTHROPIC_BASE_URL=https://gateway.example.test \
    run_account personal --resume session-123 --model fable --dangerously-skip-permissions

  assert_line "$CLAUDE_LOG" "config_dir=$FIXTURE_HOME/.config/claude-account/accounts/personal"
  assert_line "$CLAUDE_LOG" "agent_view=1"
  assert_line "$CLAUDE_LOG" "token=<unset>"
  assert_line "$CLAUDE_LOG" "api_key=<unset>"
  assert_line "$CLAUDE_LOG" "auth_token=<unset>"
  assert_line "$CLAUDE_LOG" "base_url=<unset>"
  assert_line "$CLAUDE_LOG" "login_command=1"
  assert_line "$CLAUDE_LOG" "logout_command=1"
  assert_line "$CLAUDE_LOG" "subprocess_scrub=1"
  assert_line "$CLAUDE_LOG" "args=<--resume><session-123><--model><fable><--dangerously-skip-permissions>"
}

test_default_run_rejects_unregistered_login_profile_without_token_fallback() {
  setup_fixture

  if run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "unregistered full-login profile unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "full-login profile is not registered: personal"
  assert_contains "$FIXTURE_ROOT/output" "claude-account auth-login personal"
  assert_not_exists "$CLAUDE_LOG"
}

test_default_run_preserves_terminal_stdin_through_the_lock_holder() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  print -r -- "interactive-input" | CLAUDE_EXPECT_STDIN=1 run_account personal

  assert_line "$CLAUDE_LOG" "stdin=interactive-input"
}

test_default_run_rejects_shared_login_identity_mismatch() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  if CLAUDE_AUTH_STATUS_EMAIL=other@example.test run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "mismatched full-login identity unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "Claude login does not match profile: personal"
  assert_contains "$FIXTURE_ROOT/output" "claude-account auth-login personal"
  assert_not_exists "$CLAUDE_LOG"
}

test_default_run_rejects_same_email_in_a_different_organization() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  if CLAUDE_AUTH_STATUS_ORG=org-other run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "different organization with the same email unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "Claude login does not match profile: personal"
  assert_not_exists "$CLAUDE_LOG"
}

test_auth_login_registers_full_login_identity_without_storing_email() {
  setup_fixture

  run_account auth-login personal > "$FIXTURE_ROOT/output"

  assert_contains "$CLAUDE_CALL_LOG" "<auth><login>"
  assert_contains "$FIXTURE_ROOT/output" "Registered full-login profile: personal"
  assert_contains "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json" "$PERSONAL_IDENTITY_SHA256"
  assert_not_contains "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json" "personal@example.test"
  [[ "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json")" == 600 ]] || fail "login registry must be mode 600"
}

test_auth_login_allows_other_accounts_running() {
  setup_fixture
  CLAUDE_RUNNING_PROCESSES=2 run_account auth-login personal > "$FIXTURE_ROOT/output"
  assert_contains "$CLAUDE_CALL_LOG" "<auth><login>"
}

test_profiles_share_transcripts_but_not_login_state() {
  setup_fixture
  mkdir -p "$FIXTURE_HOME/.claude/projects"
  print -r -- 'existing transcript' > "$FIXTURE_HOME/.claude/projects/fixture.jsonl"
  print -r -- '{"model":"fable"}' > "$FIXTURE_HOME/.claude/settings.json"
  run_account auth-login personal >/dev/null
  run_account auth-login work >/dev/null
  local personal="$FIXTURE_HOME/.config/claude-account/accounts/personal"
  local work="$FIXTURE_HOME/.config/claude-account/accounts/work"
  [[ "$personal/projects/fixture.jsonl" -ef "$work/projects/fixture.jsonl" ]] || fail 'transcripts not shared'
  [[ "$personal/settings.json" -ef "$FIXTURE_HOME/.claude/settings.json" ]] || fail 'settings not shared'
  [[ ! "$personal/login-profiles.json" -ef "$work/login-profiles.json" ]] || fail 'identity registries shared'
  [[ ! -L "$personal/.claude.json" && ! -L "$work/.claude.json" ]] || fail 'OAuth metadata shared'
  run_account personal --resume fixture --model fable >/dev/null
  assert_line "$CLAUDE_LOG" "config_dir=$personal"
  run_account work --resume fixture --model fable >/dev/null
  assert_line "$CLAUDE_LOG" "config_dir=$work"
}

test_login_failure_does_not_register_profile() {
  setup_fixture
  if CLAUDE_AUTH_LOGIN_EXIT=1 run_account auth-login personal >/dev/null 2>&1; then
    fail 'failed auth login accepted'
  fi
  assert_not_exists "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json"
}

test_custom_oauth_and_host_credentials_are_removed_for_login() {
  setup_fixture
  CLAUDE_CODE_CUSTOM_OAUTH_URL=https://invalid.example \
    CLAUDE_CODE_HOST_CREDS_FILE=/tmp/fixture-host-creds \
    USE_STAGING_OAUTH=1 run_account auth-login personal >/dev/null
  assert_file "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json"
}

test_profile_directory_symlink_is_rejected() {
  setup_fixture
  mkdir -p "$FIXTURE_HOME/.config/claude-account/accounts" "$FIXTURE_ROOT/elsewhere"
  ln -s "$FIXTURE_ROOT/elsewhere" "$FIXTURE_HOME/.config/claude-account/accounts/personal"
  if run_account auth-login personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'profile symlink accepted'
  fi
  assert_not_exists "$CLAUDE_CALL_LOG"
}


test_auth_login_refuses_while_a_managed_session_holds_the_shared_login_lock() {
  setup_fixture
  local lock_dir="$FIXTURE_HOME/.config/claude-account/accounts/personal"
  local lock_file="$lock_dir/full-login.lock"
  local ready_file="$FIXTURE_ROOT/lock-ready"
  mkdir -p "$lock_dir"

  python3 - "$lock_file" "$ready_file" <<'PY' &
import fcntl
import os
import sys
import time

descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_SH)
open(sys.argv[2], "w").close()
time.sleep(30)
PY
  local holder_pid=$!
  for _ in {1..50}; do
    [[ -f "$ready_file" ]] && break
    sleep 0.02
  done
  [[ -f "$ready_file" ]] || fail "shared lock holder did not start"

  if run_account auth-login personal > "$FIXTURE_ROOT/output" 2>&1; then
    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true
    fail "auth login unexpectedly ignored the shared login lock"
  fi

  assert_contains "$FIXTURE_ROOT/output" "profile login lock is busy"
  assert_not_exists "$CLAUDE_CALL_LOG"
  if printf 'personal\n' | run_account auth-login personal --replace > "$FIXTURE_ROOT/output" 2>&1; then
    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true
    fail 'replacement ignored the shared login lock'
  fi
  assert_contains "$FIXTURE_ROOT/output" 'profile login lock is busy'
  assert_not_exists "$CLAUDE_CALL_LOG"
  if run_account repair personal > "$FIXTURE_ROOT/output" 2>&1; then
    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true
    fail 'repair ignored the shared login lock'
  fi
  assert_contains "$FIXTURE_ROOT/output" 'profile login lock is busy'
  if ! run_account auth-login work > "$FIXTURE_ROOT/other-output" 2>&1; then
    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true
    fail 'another profile was blocked by the personal lock'
  fi
  assert_contains "$CLAUDE_CALL_LOG" '<auth><login>'
  kill "$holder_pid" 2>/dev/null || true
  wait "$holder_pid" 2>/dev/null || true
}

test_unverified_old_cli_is_rejected_before_login() {
  setup_fixture
  if CLAUDE_TEST_VERSION=2.1.80 run_account auth-login personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'old CLI accepted for credential isolation'
  fi
  assert_not_exists "$CLAUDE_CALL_LOG"
}

test_case_alias_and_old_cli_list_are_rejected() {
  setup_fixture
  if LC_ALL=en_US.UTF-8 run_account auth-login Personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'case-insensitive alias accepted'
  fi
  assert_not_exists "$CLAUDE_CALL_LOG"
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
  if CLAUDE_TEST_VERSION=2.1.80 run_account list > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'list used an unverified old CLI'
  fi
}

test_list_does_not_read_credentials_during_login() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
  local lock_file="$FIXTURE_HOME/.config/claude-account/accounts/personal/full-login.lock"
  local ready_file="$FIXTURE_ROOT/exclusive-ready"
  python3 - "$lock_file" "$ready_file" <<'PY' &
import fcntl, os, sys, time
descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
time.sleep(30)
PY
  local holder_pid=$!
  for _ in {1..50}; do
    [[ -f "$ready_file" ]] && break
    sleep 0.02
  done
  [[ -f "$ready_file" ]] || fail 'exclusive lock holder did not start'
  local result=0
  run_account list > "$FIXTURE_ROOT/output" 2>&1 || result=$?
  kill "$holder_pid" 2>/dev/null || true
  wait "$holder_pid" 2>/dev/null || true
  [[ "$result" -ne 0 ]] || fail 'list ignored exclusive login lock'
  assert_not_exists "$CLAUDE_CALL_LOG"
}

test_auth_login_rejects_accidental_remap_of_existing_profile() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  if CLAUDE_AUTH_STATUS_EMAIL=other@example.test run_account auth-login personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "existing login profile was unexpectedly remapped"
  fi

  assert_contains "$FIXTURE_ROOT/output" "login identity does not match the registered profile: personal"
  assert_contains "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json" "$PERSONAL_IDENTITY_SHA256"
  assert_not_contains "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json" "$OTHER_IDENTITY_SHA256"
}

test_removed_setup_token_commands_fail_without_legacy_aliases() {
  setup_fixture

  for removed_command in add add-token token; do
    if run_account "$removed_command" personal > "$FIXTURE_ROOT/output" 2>&1; then
      fail "removed command unexpectedly succeeded: $removed_command"
    fi
    assert_contains "$FIXTURE_ROOT/output" "unknown command: $removed_command"
  done
}

test_list_marks_the_matching_shared_login_without_exposing_identity() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  run_account list > "$FIXTURE_ROOT/output"

  assert_line "$FIXTURE_ROOT/output" $'personal\tlogged in\tmax'
  assert_not_contains "$FIXTURE_ROOT/output" "personal@example.test"
}

test_authentication_settings_are_rejected_before_full_login_launch() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
  mkdir -p "$FIXTURE_HOME/.claude"
  print -r -- '{"env":{"ANTHROPIC_API_KEY":"must-not-be-read"}}' > "$FIXTURE_HOME/.claude/settings.json"

  if run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "authentication setting unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "authentication override in Claude settings"
  assert_not_contains "$FIXTURE_ROOT/output" "must-not-be-read"
  assert_not_exists "$CLAUDE_LOG"
}

test_settings_cannot_redirect_profile_or_enable_shared_daemon() {
  local variable
  for variable in CLAUDE_CONFIG_DIR CLAUDE_CODE_DISABLE_AGENT_VIEW; do
    setup_fixture
    write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
    mkdir -p "$FIXTURE_HOME/.claude"
    printf '{"env":{"%s":"0"}}\n' "$variable" > "$FIXTURE_HOME/.claude/settings.json"
    if run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
      fail "profile isolation overridden by settings: $variable"
    fi
    assert_not_exists "$CLAUDE_LOG"
  done
}

test_remote_launch_flags_fail_before_session_start() {
  local argument
  for argument in --remote-control=demo --cloud=demo --teleport=demo --environment=demo; do
    setup_fixture
    write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
    if run_account personal "$argument" > "$FIXTURE_ROOT/output" 2>&1; then
      fail "unsupported remote launch accepted: $argument"
    fi
    assert_not_exists "$CLAUDE_LOG"
  done
}

main() {
  test_login_completes_onboarding_without_copying_account_or_trust
  test_repair_requires_matching_identity_and_preserves_other_config
  test_default_clear_rejects_relative_config_without_deleting_files
  test_replace_requires_confirmation_and_preserves_mapping_on_failure
  test_replace_changes_only_the_selected_identity
  test_default_selection_routes_future_launches_and_can_be_cleared
  test_default_rejects_invalid_or_unregistered_selection_without_fallback
  test_default_run_requires_matching_full_login_and_forwards_arguments
  test_default_run_rejects_unregistered_login_profile_without_token_fallback
  test_default_run_preserves_terminal_stdin_through_the_lock_holder
  test_default_run_rejects_shared_login_identity_mismatch
  test_default_run_rejects_same_email_in_a_different_organization
  test_auth_login_registers_full_login_identity_without_storing_email
  test_auth_login_allows_other_accounts_running
  test_profiles_share_transcripts_but_not_login_state
  test_login_failure_does_not_register_profile
  test_custom_oauth_and_host_credentials_are_removed_for_login
  test_profile_directory_symlink_is_rejected
  test_unverified_old_cli_is_rejected_before_login
  test_case_alias_and_old_cli_list_are_rejected
  test_list_does_not_read_credentials_during_login
  test_auth_login_refuses_while_a_managed_session_holds_the_shared_login_lock
  test_auth_login_rejects_accidental_remap_of_existing_profile
  test_removed_setup_token_commands_fail_without_legacy_aliases
  test_list_marks_the_matching_shared_login_without_exposing_identity
  test_authentication_settings_are_rejected_before_full_login_launch
  test_settings_cannot_redirect_profile_or_enable_shared_daemon
  test_remote_launch_flags_fail_before_session_start
  echo "claude account tests passed"
}

test_login_completes_onboarding_without_copying_account_or_trust() {
  setup_fixture
  run_account auth-login personal > "$FIXTURE_ROOT/output"
  python3 - "$FIXTURE_HOME/.config/claude-account/accounts/personal/.claude.json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
assert path.exists(), 'login did not create onboarding state'
assert json.loads(path.read_text()) == {'hasCompletedOnboarding': True}
PY
}

test_repair_requires_matching_identity_and_preserves_other_config() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
  local config="$FIXTURE_HOME/.config/claude-account/accounts/personal/.claude.json"
  printf '%s\n' '{"oauthAccount":{"fixture":"keep"},"projects":{"fixture":{"hasTrustDialogAccepted":false}},"theme":"dark"}' > "$config"
  if CLAUDE_AUTH_STATUS_EMAIL=other@example.test run_account repair personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'repair accepted mismatching identity'
  fi
  assert_not_contains "$config" hasCompletedOnboarding
  run_account repair personal > "$FIXTURE_ROOT/output"
  python3 - "$config" <<'PY'
import json, sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text()) == {
    'oauthAccount': {'fixture': 'keep'},
    'projects': {'fixture': {'hasTrustDialogAccepted': False}},
    'theme': 'dark', 'hasCompletedOnboarding': True,
}
PY
  assert_not_contains "$CLAUDE_CALL_LOG" '<auth><login>'
}

test_default_clear_rejects_relative_config_without_deleting_files() {
  setup_fixture
  mkdir -p "$FIXTURE_ROOT/relative/claude-account"
  printf 'keep\n' > "$FIXTURE_ROOT/relative/claude-account/default-profile"
  if (cd "$FIXTURE_ROOT" && HOME="$FIXTURE_HOME" XDG_CONFIG_HOME=relative "$SCRIPT" default --clear) > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'default clear accepted a relative config path'
  fi
  assert_contains "$FIXTURE_ROOT/output" 'XDG_CONFIG_HOME must be an absolute path'
  assert_line "$FIXTURE_ROOT/relative/claude-account/default-profile" keep
}

test_replace_requires_confirmation_and_preserves_mapping_on_failure() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
  if printf 'wrong\n' | run_account auth-login personal --replace > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'replacement accepted wrong confirmation'
  fi
  assert_contains "$FIXTURE_ROOT/output" 'cancelled'
  assert_not_exists "$CLAUDE_CALL_LOG"
  if printf 'personal\n' | CLAUDE_AUTH_LOGIN_EXIT=7 run_account auth-login personal --replace > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'failed replacement login succeeded'
  fi
  assert_contains "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json" "$PERSONAL_IDENTITY_SHA256"
}

test_replace_changes_only_the_selected_identity() {
  setup_fixture
  run_account auth-login work > "$FIXTURE_ROOT/output"
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"
  printf 'personal\n' | CLAUDE_AUTH_STATUS_EMAIL=other@example.test CLAUDE_AUTH_STATUS_ORG=org-other run_account auth-login personal --replace > "$FIXTURE_ROOT/output"
  assert_contains "$FIXTURE_HOME/.config/claude-account/accounts/personal/login-profiles.json" "$OTHER_IDENTITY_SHA256"
  assert_contains "$FIXTURE_HOME/.config/claude-account/accounts/work/login-profiles.json" "$PERSONAL_IDENTITY_SHA256"
  CLAUDE_AUTH_STATUS_EMAIL=other@example.test CLAUDE_AUTH_STATUS_ORG=org-other run_account personal --resume example
  assert_line "$CLAUDE_LOG" 'args=<--resume><example>'
}

test_default_selection_routes_future_launches_and_can_be_cleared() {
  setup_fixture
  run_account run-default 'native prompt'
  assert_line "$CLAUDE_LOG" 'config_dir=<unset>'
  run_account auth-login personal > "$FIXTURE_ROOT/output"
  run_account auth-login work > "$FIXTURE_ROOT/output"
  run_account default personal > "$FIXTURE_ROOT/output"
  run_account default > "$FIXTURE_ROOT/output"
  assert_line "$FIXTURE_ROOT/output" personal
  printf 'input\n' | CLAUDE_EXPECT_STDIN=1 run_account run-default --resume 'session id' --dangerously-skip-permissions
  assert_line "$CLAUDE_LOG" "config_dir=$FIXTURE_HOME/.config/claude-account/accounts/personal"
  assert_line "$CLAUDE_LOG" 'stdin=input'
  assert_line "$CLAUDE_LOG" 'args=<--resume><session id><--dangerously-skip-permissions>'
  run_account work
  assert_line "$CLAUDE_LOG" "config_dir=$FIXTURE_HOME/.config/claude-account/accounts/work"
  run_account default work > "$FIXTURE_ROOT/output"
  run_account run-default
  assert_line "$CLAUDE_LOG" "config_dir=$FIXTURE_HOME/.config/claude-account/accounts/work"
  run_account default --clear > "$FIXTURE_ROOT/output"
  run_account run-default
  assert_line "$CLAUDE_LOG" 'config_dir=<unset>'
}

test_default_rejects_invalid_or_unregistered_selection_without_fallback() {
  setup_fixture
  run_account auth-login personal > "$FIXTURE_ROOT/output"
  run_account default personal > "$FIXTURE_ROOT/output"
  if run_account default missing > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'unregistered default accepted'
  fi
  run_account default > "$FIXTURE_ROOT/output"
  assert_line "$FIXTURE_ROOT/output" personal
  if CLAUDE_AUTH_STATUS_EMAIL=other@example.test run_account run-default > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'default identity mismatch fell back to native'
  fi
  assert_not_exists "$CLAUDE_LOG"
  printf '../bad\n' > "$FIXTURE_HOME/.config/claude-account/default-profile"
  if run_account run-default > "$FIXTURE_ROOT/output" 2>&1; then
    fail 'invalid stored default accepted'
  fi
  assert_not_exists "$CLAUDE_LOG"
}

main "$@"
