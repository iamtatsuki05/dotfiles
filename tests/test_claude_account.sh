#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SCRIPT="$REPO_ROOT/scripts/claude_account.sh"

source "$TEST_DIR/lib/assertions.sh"

setup_fixture() {
  make_temp_dir claude-account-test
  FIXTURE_ROOT="$REPLY"
  FIXTURE_HOME="$FIXTURE_ROOT/home"
  FIXTURE_BIN="$FIXTURE_ROOT/bin"
  SECURITY_LOG="$FIXTURE_ROOT/security.log"
  CLAUDE_LOG="$FIXTURE_ROOT/claude.log"
  mkdir -p "$FIXTURE_HOME" "$FIXTURE_BIN"

  cat > "$FIXTURE_BIN/security" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$SECURITY_LOG"
case "$1" in
  add-generic-password)
    exit 0
    ;;
  find-generic-password)
    if [ "${SECURITY_TOKEN_MISSING:-0}" = 1 ]; then
      exit 44
    fi
    printf '%s\n' 'setup-token-from-keychain'
    ;;
esac
EOF

  cat > "$FIXTURE_BIN/claude" <<'EOF'
#!/bin/sh
if [ "${1:-}" = auth ] && [ "${2:-}" = status ]; then
  printf '{"loggedIn":true,"authMethod":"%s","apiProvider":"%s","apiKeySource":%s}\n' \
    "${CLAUDE_AUTH_STATUS_METHOD:-oauth_token}" \
    "${CLAUDE_AUTH_STATUS_PROVIDER:-firstParty}" \
    "${CLAUDE_AUTH_STATUS_API_KEY_SOURCE:-null}"
  exit 0
fi
{
  printf 'token=%s\n' "${CLAUDE_CODE_OAUTH_TOKEN:-<unset>}"
  printf 'api_key=%s\n' "${ANTHROPIC_API_KEY:-<unset>}"
  printf 'auth_token=%s\n' "${ANTHROPIC_AUTH_TOKEN:-<unset>}"
  printf 'base_url=%s\n' "${ANTHROPIC_BASE_URL:-<unset>}"
  printf 'bedrock=%s\n' "${CLAUDE_CODE_USE_BEDROCK:-<unset>}"
  printf 'vertex=%s\n' "${CLAUDE_CODE_USE_VERTEX:-<unset>}"
  printf 'foundry=%s\n' "${CLAUDE_CODE_USE_FOUNDRY:-<unset>}"
  printf 'anthropic_aws=%s\n' "${CLAUDE_CODE_USE_ANTHROPIC_AWS:-<unset>}"
  printf 'anthropic_google=%s\n' "${CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD:-<unset>}"
  printf 'mantle=%s\n' "${CLAUDE_CODE_USE_MANTLE:-<unset>}"
  printf 'gateway=%s\n' "${CLAUDE_CODE_USE_GATEWAY:-<unset>}"
  printf 'aws_api_key=%s\n' "${ANTHROPIC_AWS_API_KEY:-<unset>}"
  printf 'foundry_api_key=%s\n' "${ANTHROPIC_FOUNDRY_API_KEY:-<unset>}"
  printf 'bedrock_token=%s\n' "${AWS_BEARER_TOKEN_BEDROCK:-<unset>}"
  printf 'custom_headers=%s\n' "${ANTHROPIC_CUSTOM_HEADERS:-<unset>}"
  printf 'oauth_token_fd=%s\n' "${CLAUDE_CODE_OAUTH_TOKEN_FD:-<unset>}"
  printf 'model=%s\n' "${ANTHROPIC_MODEL:-<unset>}"
  printf 'native_search=%s\n' "${CLAUDE_CODE_USE_NATIVE_FILE_SEARCH:-<unset>}"
  printf 'login_command=%s\n' "${DISABLE_LOGIN_COMMAND:-<unset>}"
  printf 'logout_command=%s\n' "${DISABLE_LOGOUT_COMMAND:-<unset>}"
  printf 'api_key_fd=%s\n' "${CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR:-<unset>}"
  printf 'oauth_token_file=%s\n' "${CCR_OAUTH_TOKEN_FILE:-<unset>}"
  printf 'host_auth=%s\n' "${CLAUDE_CODE_HOST_AUTH_ENV_VAR:-<unset>}"
  printf 'unix_socket=%s\n' "${ANTHROPIC_UNIX_SOCKET:-<unset>}"
  printf 'google_cloud_key=%s\n' "${ANTHROPIC_GOOGLE_CLOUD_API_KEY:-<unset>}"
  printf 'managed_settings=%s\n' "${CLAUDE_CODE_MANAGED_SETTINGS_PATH:-<unset>}"
  printf 'args=' 
  printf '<%s>' "$@"
  printf '\n'
} > "$CLAUDE_LOG"
EOF

  chmod +x "$FIXTURE_BIN/security" "$FIXTURE_BIN/claude"
}

run_account() {
  HOME="$FIXTURE_HOME" \
    XDG_CONFIG_HOME="$FIXTURE_HOME/.config" \
    PATH="$FIXTURE_BIN:/bin:/usr/bin" \
    SECURITY_LOG="$SECURITY_LOG" \
    CLAUDE_LOG="$CLAUDE_LOG" \
    "$SCRIPT" "$@"
}

test_add_stores_token_in_keychain_without_command_line_secret() {
  setup_fixture

  run_account add personal > "$FIXTURE_ROOT/output"

  assert_contains "$SECURITY_LOG" "add-generic-password -U -a personal -s dotfiles.claude-account.setup-token"
  assert_contains "$SECURITY_LOG" "-w"
  assert_not_contains "$SECURITY_LOG" "setup-token-from-keychain"
  assert_file_content "$FIXTURE_HOME/.config/claude-account/profiles" "personal"
  [[ "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$FIXTURE_HOME/.config/claude-account/profiles")" == 600 ]] || fail "profiles registry must be mode 600"
}

test_run_uses_selected_token_and_removes_higher_priority_credentials() {
  setup_fixture
  print -r -- personal > "$FIXTURE_ROOT/profile"
  mkdir -p "$FIXTURE_HOME/.config/claude-account"
  cp "$FIXTURE_ROOT/profile" "$FIXTURE_HOME/.config/claude-account/profiles"

  ANTHROPIC_API_KEY=api-secret \
    ANTHROPIC_AUTH_TOKEN=auth-secret \
    ANTHROPIC_BASE_URL=https://gateway.example.test \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_USE_VERTEX=1 \
    CLAUDE_CODE_USE_FOUNDRY=1 \
    CLAUDE_CODE_USE_ANTHROPIC_AWS=1 \
    CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD=1 \
    CLAUDE_CODE_USE_MANTLE=1 \
    CLAUDE_CODE_USE_GATEWAY=1 \
    ANTHROPIC_AWS_API_KEY=aws-secret \
    ANTHROPIC_FOUNDRY_API_KEY=foundry-secret \
    AWS_BEARER_TOKEN_BEDROCK=bedrock-secret \
    ANTHROPIC_CUSTOM_HEADERS='X-Secret: value' \
    CLAUDE_CODE_OAUTH_TOKEN_FD=9 \
    ANTHROPIC_MODEL=claude-test-model \
    CLAUDE_CODE_USE_NATIVE_FILE_SEARCH=1 \
    CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR=9 \
    CCR_OAUTH_TOKEN_FILE=/tmp/oauth-token \
    CLAUDE_CODE_HOST_AUTH_ENV_VAR=HOST_TOKEN \
    ANTHROPIC_UNIX_SOCKET=/tmp/anthropic.sock \
    ANTHROPIC_GOOGLE_CLOUD_API_KEY=google-secret \
    CLAUDE_CODE_MANAGED_SETTINGS_PATH=/tmp/managed-settings.json \
    run_account personal --model opus "prompt with spaces"

  assert_contains "$CLAUDE_LOG" "token=setup-token-from-keychain"
  assert_contains "$CLAUDE_LOG" "api_key=<unset>"
  assert_contains "$CLAUDE_LOG" "auth_token=<unset>"
  assert_contains "$CLAUDE_LOG" "base_url=<unset>"
  assert_contains "$CLAUDE_LOG" "bedrock=<unset>"
  assert_contains "$CLAUDE_LOG" "vertex=<unset>"
  assert_contains "$CLAUDE_LOG" "foundry=<unset>"
  assert_contains "$CLAUDE_LOG" "anthropic_aws=<unset>"
  assert_contains "$CLAUDE_LOG" "anthropic_google=<unset>"
  assert_contains "$CLAUDE_LOG" "mantle=<unset>"
  assert_contains "$CLAUDE_LOG" "gateway=<unset>"
  assert_contains "$CLAUDE_LOG" "aws_api_key=<unset>"
  assert_contains "$CLAUDE_LOG" "foundry_api_key=<unset>"
  assert_contains "$CLAUDE_LOG" "bedrock_token=<unset>"
  assert_contains "$CLAUDE_LOG" "custom_headers=<unset>"
  assert_contains "$CLAUDE_LOG" "oauth_token_fd=<unset>"
  assert_contains "$CLAUDE_LOG" "model=claude-test-model"
  assert_contains "$CLAUDE_LOG" "native_search=1"
  assert_contains "$CLAUDE_LOG" "login_command=1"
  assert_contains "$CLAUDE_LOG" "logout_command=1"
  assert_contains "$CLAUDE_LOG" "api_key_fd=<unset>"
  assert_contains "$CLAUDE_LOG" "oauth_token_file=<unset>"
  assert_contains "$CLAUDE_LOG" "host_auth=<unset>"
  assert_contains "$CLAUDE_LOG" "unix_socket=<unset>"
  assert_contains "$CLAUDE_LOG" "google_cloud_key=<unset>"
  assert_contains "$CLAUDE_LOG" "managed_settings=<unset>"
  assert_contains "$CLAUDE_LOG" "args=<--model><opus><prompt with spaces>"
}

test_run_fails_closed_when_api_key_source_is_present() {
  setup_fixture

  if CLAUDE_AUTH_STATUS_API_KEY_SOURCE='"ANTHROPIC_API_KEY"' run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "API key source unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "unexpected authentication route"
  assert_not_exists "$CLAUDE_LOG"
}

test_authentication_settings_are_rejected_before_keychain_access() {
  setup_fixture
  mkdir -p "$FIXTURE_HOME/.claude"
  print -r -- '{"env":{"ANTHROPIC_API_KEY":"must-not-be-read"}}' > "$FIXTURE_HOME/.claude/settings.json"

  if run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "authentication setting unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "authentication override in Claude settings"
  assert_contains "$FIXTURE_ROOT/output" "ANTHROPIC_API_KEY"
  assert_not_contains "$FIXTURE_ROOT/output" "must-not-be-read"
  assert_not_exists "$SECURITY_LOG"
}

test_per_invocation_settings_flags_are_rejected() {
  setup_fixture

  if run_account personal --settings '{"apiKeyHelper":"unsafe"}' > "$FIXTURE_ROOT/output" 2>&1; then
    fail "--settings unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "--settings is not supported by claude-account"
  assert_not_exists "$SECURITY_LOG"
}

test_hidden_managed_settings_flag_is_rejected() {
  setup_fixture

  if run_account personal --managed-settings /tmp/unsafe.json > "$FIXTURE_ROOT/output" 2>&1; then
    fail "--managed-settings unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "managed settings flags are not supported"
  assert_not_exists "$SECURITY_LOG"
}

test_run_fails_closed_when_claude_selects_another_provider() {
  setup_fixture

  if CLAUDE_AUTH_STATUS_PROVIDER=anthropicAws run_account personal > "$FIXTURE_ROOT/output" 2>&1; then
    fail "non-first-party provider unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "Claude Code selected an unexpected authentication route"
  assert_not_exists "$CLAUDE_LOG"
}

test_xtrace_does_not_print_setup_token() {
  setup_fixture

  HOME="$FIXTURE_HOME" \
    XDG_CONFIG_HOME="$FIXTURE_HOME/.config" \
    PATH="$FIXTURE_BIN:/bin:/usr/bin" \
    SECURITY_LOG="$SECURITY_LOG" \
    CLAUDE_LOG="$CLAUDE_LOG" \
    bash -x "$SCRIPT" personal --version > "$FIXTURE_ROOT/output" 2> "$FIXTURE_ROOT/xtrace"

  assert_not_contains "$FIXTURE_ROOT/xtrace" "setup-token-from-keychain"
}

test_bare_mode_is_rejected_because_it_ignores_setup_tokens() {
  setup_fixture

  if run_account personal --bare -p test > "$FIXTURE_ROOT/output" 2>&1; then
    fail "bare mode unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "--bare does not support setup-token profiles"
  assert_not_exists "$SECURITY_LOG"
  assert_not_exists "$CLAUDE_LOG"
}

test_invalid_profile_is_rejected_before_keychain_access() {
  setup_fixture

  if run_account '../work' > "$FIXTURE_ROOT/output" 2>&1; then
    fail "invalid profile unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "invalid profile name"
  assert_not_exists "$SECURITY_LOG"
}

test_reserved_command_name_cannot_be_registered_as_profile() {
  setup_fixture

  if run_account add list > "$FIXTURE_ROOT/output" 2>&1; then
    fail "reserved profile unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "reserved profile name: list"
  assert_not_exists "$SECURITY_LOG"
}

test_missing_profile_token_fails_without_starting_claude() {
  setup_fixture

  if SECURITY_TOKEN_MISSING=1 run_account work > "$FIXTURE_ROOT/output" 2>&1; then
    fail "missing token unexpectedly succeeded"
  fi

  assert_contains "$FIXTURE_ROOT/output" "profile not found in macOS Keychain: work"
  assert_not_exists "$CLAUDE_LOG"
}

main() {
  test_add_stores_token_in_keychain_without_command_line_secret
  test_run_uses_selected_token_and_removes_higher_priority_credentials
  test_invalid_profile_is_rejected_before_keychain_access
  test_reserved_command_name_cannot_be_registered_as_profile
  test_missing_profile_token_fails_without_starting_claude
  test_bare_mode_is_rejected_because_it_ignores_setup_tokens
  test_run_fails_closed_when_claude_selects_another_provider
  test_xtrace_does_not_print_setup_token
  test_authentication_settings_are_rejected_before_keychain_access
  test_per_invocation_settings_flags_are_rejected
  test_run_fails_closed_when_api_key_source_is_present
  test_hidden_managed_settings_flag_is_rejected
  echo "claude account tests passed"
}

main "$@"
