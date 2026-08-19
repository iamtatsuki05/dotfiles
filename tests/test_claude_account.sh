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
printf '<%s>' "$@" >> "$CLAUDE_CALL_LOG"
printf '\n' >> "$CLAUDE_CALL_LOG"

if [ "${1:-}" = auth ] && [ "${2:-}" = login ]; then
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

  mkdir -p "$FIXTURE_HOME/.config/claude-account"
  cat > "$FIXTURE_HOME/.config/claude-account/login-profiles.json" <<EOF
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
  chmod 600 "$FIXTURE_HOME/.config/claude-account/login-profiles.json"
}

test_default_run_requires_matching_full_login_and_forwards_arguments() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  ANTHROPIC_API_KEY=api-secret \
    ANTHROPIC_AUTH_TOKEN=auth-secret \
    ANTHROPIC_BASE_URL=https://gateway.example.test \
    run_account personal --resume session-123 --model fable --dangerously-skip-permissions

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

  assert_contains "$FIXTURE_ROOT/output" "shared Claude login does not match profile: personal"
  assert_contains "$FIXTURE_ROOT/output" "claude-account auth-login personal"
  assert_not_exists "$CLAUDE_LOG"
}

test_default_run_rejects_same_email_in_a_different_organization() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  if CLAUDE_AUTH_STATUS_ORG=org-other run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "different organization with the same email unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "shared Claude login does not match profile: personal"
  assert_not_exists "$CLAUDE_LOG"
}

test_auth_login_registers_full_login_identity_without_storing_email() {
  setup_fixture

  run_account auth-login personal > "$FIXTURE_ROOT/output"

  assert_contains "$CLAUDE_CALL_LOG" "<auth><login>"
  assert_contains "$FIXTURE_ROOT/output" "Registered full-login profile: personal"
  assert_contains "$FIXTURE_HOME/.config/claude-account/login-profiles.json" "$PERSONAL_IDENTITY_SHA256"
  assert_not_contains "$FIXTURE_HOME/.config/claude-account/login-profiles.json" "personal@example.test"
  [[ "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$FIXTURE_HOME/.config/claude-account/login-profiles.json")" == 600 ]] || fail "login registry must be mode 600"
}

test_auth_login_refuses_to_switch_while_any_claude_process_is_running() {
  setup_fixture

  if CLAUDE_RUNNING_PROCESSES=2 run_account auth-login personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "auth login unexpectedly switched while Claude processes were running"
  fi

  assert_contains "$FIXTURE_ROOT/output" "2 Claude processes are still running"
  assert_not_exists "$CLAUDE_CALL_LOG"
}

test_auth_login_refuses_while_a_managed_session_holds_the_shared_login_lock() {
  setup_fixture
  local lock_dir="$FIXTURE_HOME/.config/claude-account"
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

  kill "$holder_pid" 2>/dev/null || true
  wait "$holder_pid" 2>/dev/null || true
  assert_contains "$FIXTURE_ROOT/output" "shared login lock is busy"
  assert_not_exists "$CLAUDE_CALL_LOG"
}

test_auth_login_rejects_accidental_remap_of_existing_profile() {
  setup_fixture
  write_login_registry personal "$PERSONAL_IDENTITY_SHA256"

  if CLAUDE_AUTH_STATUS_EMAIL=other@example.test run_account auth-login personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "existing login profile was unexpectedly remapped"
  fi

  assert_contains "$FIXTURE_ROOT/output" "login identity does not match the registered profile: personal"
  assert_contains "$FIXTURE_HOME/.config/claude-account/login-profiles.json" "$PERSONAL_IDENTITY_SHA256"
  assert_not_contains "$FIXTURE_HOME/.config/claude-account/login-profiles.json" "$OTHER_IDENTITY_SHA256"
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

  assert_line "$FIXTURE_ROOT/output" $'personal\tcurrent login\tmax'
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

main() {
  test_default_run_requires_matching_full_login_and_forwards_arguments
  test_default_run_rejects_unregistered_login_profile_without_token_fallback
  test_default_run_preserves_terminal_stdin_through_the_lock_holder
  test_default_run_rejects_shared_login_identity_mismatch
  test_default_run_rejects_same_email_in_a_different_organization
  test_auth_login_registers_full_login_identity_without_storing_email
  test_auth_login_refuses_to_switch_while_any_claude_process_is_running
  test_auth_login_refuses_while_a_managed_session_holds_the_shared_login_lock
  test_auth_login_rejects_accidental_remap_of_existing_profile
  test_removed_setup_token_commands_fail_without_legacy_aliases
  test_list_marks_the_matching_shared_login_without_exposing_identity
  test_authentication_settings_are_rejected_before_full_login_launch
  echo "claude account tests passed"
}

main "$@"
