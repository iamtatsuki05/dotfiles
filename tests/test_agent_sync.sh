#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="${0:A:h}"
readonly REPO_ROOT="${TEST_DIR:h}"
readonly SETUP_AGENT_SCRIPT="$REPO_ROOT/scripts/setup_agent_files.sh"
readonly SYNC_SCRIPT="$REPO_ROOT/dotfiles/.agent/sync.sh"
readonly TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"
readonly TEST_TIMEOUT_SECONDS="${DOTFILES_TEST_TIMEOUT_SECONDS:-10}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"

  grep -Fq -- "$expected" "$file_path" || fail "expected $file_path to contain: $expected"
}

assert_not_contains() {
  local file_path="$1"
  local unexpected="$2"

  ! grep -Fq -- "$unexpected" "$file_path" || fail "expected $file_path not to contain: $unexpected"
}

assert_symlink_target() {
  local link_path="$1"
  local expected_target="$2"

  [[ -L "$link_path" ]] || fail "expected symlink: $link_path"
  [[ "$link_path" -ef "$expected_target" ]] || fail "expected $link_path -> $expected_target"
}

run_with_timeout() {
  local timeout_seconds="$1"
  shift

  perl -e 'alarm shift @ARGV; exec @ARGV' "$timeout_seconds" "$@" || fail "command timed out: $*"
}

make_temp_dir() {
  local candidate
  local attempts=0

  while (( attempts < 10 )); do
    candidate="${TMPDIR:-/tmp}/dotfiles-test-$$-$RANDOM-$RANDOM"
    if mkdir "$candidate" 2>/dev/null; then
      REPLY="$candidate"
      return 0
    fi
    attempts=$((attempts + 1))
  done

  fail "failed to create temporary directory"
}

create_agent_fixture_repo() {
  local repo="$1"

  mkdir -p \
    "$repo/scripts" \
    "$repo/dotfiles/.agent/apps/claude" \
    "$repo/dotfiles/.agent/apps/codex" \
    "$repo/dotfiles/.agent/apps/cursor" \
    "$repo/dotfiles/.agent/apps/gemini" \
    "$repo/dotfiles/.agent/hooks" \
    "$repo/dotfiles/.agent/skills"

  cp "$SETUP_AGENT_SCRIPT" "$repo/scripts/setup_agent_files.sh"
  cp "$SYNC_SCRIPT" "$repo/dotfiles/.agent/sync.sh"
  print -r -- '# agent prompt' > "$repo/dotfiles/.agent/AGENTS.md"
  print -r -- '#!/usr/bin/env bash' > "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  chmod +x "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  chmod +x "$repo/scripts/setup_agent_files.sh" "$repo/dotfiles/.agent/sync.sh"

  cat > "$repo/dotfiles/.agent/apps/claude/settings.json" <<'EOF'
{"cleanupPeriodDays":36500}
EOF
  cat > "$repo/dotfiles/.agent/apps/claude/.mcp.json" <<'EOF'
{"mcpServers":{"codex":{"command":"codex","args":["mcp-server"]}}}
EOF
  cat > "$repo/dotfiles/.agent/apps/codex/hooks.json" <<'EOF'
{"hooks":{"PostToolUse":[{"matcher":".*","hooks":[{"type":"command","command":"~/.codex/hooks/jupytext_sync.sh"}]}]}}
EOF
  cat > "$repo/dotfiles/.agent/apps/codex/config.toml" <<'EOF'
model = "gpt-5.4"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
EOF
  cat > "$repo/dotfiles/.agent/apps/gemini/settings.json" <<'EOF'
{"general":{"sessionRetention":{"enabled":false}}}
EOF
  print -r -- 'secrets.env' > "$repo/dotfiles/.agent/apps/gemini/ignore"
  cat > "$repo/dotfiles/.agent/apps/cursor/mcp.json" <<'EOF'
{"mcpServers":{"gemini-cli":{"command":"bunx","args":["mcp-gemini-cli","--allow-npx"]}}}
EOF
}

test_agent_sync_links_managed_files_and_generates_runtime_state() {
  local repo
  local home_dir
  local xdg_config_home
  make_temp_dir
  repo="$REPLY"
  make_temp_dir
  home_dir="$REPLY"
  xdg_config_home="$home_dir/.config"

  create_agent_fixture_repo "$repo"
  mkdir -p "$xdg_config_home/shell"
  print -r -- 'export DEVIN_API_KEY=test-key' > "$xdg_config_home/shell/secrets.env"

  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" \
    run_with_timeout "$TEST_TIMEOUT_SECONDS" "$TEST_ZSH_BIN" "$repo/scripts/setup_agent_files.sh" --repo-root "$repo" >/dev/null

  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" \
    run_with_timeout "$TEST_TIMEOUT_SECONDS" "$TEST_ZSH_BIN" "$repo/scripts/setup_agent_files.sh" --repo-root "$repo" >/dev/null

  assert_symlink_target "$home_dir/.claude/settings.json" "$repo/dotfiles/.agent/apps/claude/settings.json"
  assert_symlink_target "$home_dir/.claude/.mcp.json" "$repo/dotfiles/.agent/apps/claude/.mcp.json"
  assert_symlink_target "$home_dir/.claude/CLAUDE.md" "$repo/dotfiles/.agent/AGENTS.md"
  assert_symlink_target "$home_dir/.claude/hooks/jupytext_sync.sh" "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  assert_symlink_target "$home_dir/.gemini/settings.json" "$repo/dotfiles/.agent/apps/gemini/settings.json"
  assert_symlink_target "$home_dir/.gemini/ignore" "$repo/dotfiles/.agent/apps/gemini/ignore"
  assert_symlink_target "$home_dir/.cursor/mcp.json" "$repo/dotfiles/.agent/apps/cursor/mcp.json"
  assert_symlink_target "$home_dir/.codex/config.toml" "$repo/dotfiles/.agent/apps/codex/config.toml"
  assert_symlink_target "$home_dir/.codex/hooks.json" "$repo/dotfiles/.agent/apps/codex/hooks.json"
  assert_not_contains "$home_dir/.codex/config.toml" '[history]'
  assert_not_contains "$home_dir/.codex/config.toml" '[features]'
  assert_not_contains "$home_dir/.codex/config.toml" '[memories]'
  assert_not_contains "$home_dir/.codex/config.toml" 'persistence = "save-all"'
  assert_not_contains "$home_dir/.codex/config.toml" 'codex_hooks = true'
  assert_not_contains "$home_dir/.codex/config.toml" 'memories = true'
  assert_not_contains "$home_dir/.codex/config.toml" 'generate_memories = true'
  assert_not_contains "$home_dir/.codex/config.toml" 'max_rollout_age_days = 90'
  assert_not_contains "$home_dir/.codex/config.toml" 'max_unused_days = 365'
  assert_file "$home_dir/.gemini/.env"
  assert_contains "$home_dir/.gemini/.env" 'DEVIN_API_KEY=test-key'

  rm -rf "$repo" "$home_dir"
}

test_agent_sync_replaces_existing_codex_config_with_managed_symlink() {
  local repo
  local home_dir
  local xdg_config_home
  local codex_config
  make_temp_dir
  repo="$REPLY"
  make_temp_dir
  home_dir="$REPLY"
  xdg_config_home="$home_dir/.config"
  codex_config="$home_dir/.codex/config.toml"

  create_agent_fixture_repo "$repo"
  mkdir -p "$home_dir/.codex" "$xdg_config_home/shell"
  print -r -- 'export DEVIN_API_KEY=test-key' > "$xdg_config_home/shell/secrets.env"
  cat > "$codex_config" <<'EOF'
model = "gpt-5.4"
history.max_bytes = 1024

[features]
codex_hooks = false

[memories]
use_memories = false
max_unused_days = 7

[projects."/tmp/example"]
trust_level = "trusted"
EOF

  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" \
    run_with_timeout "$TEST_TIMEOUT_SECONDS" "$TEST_ZSH_BIN" "$repo/scripts/setup_agent_files.sh" --repo-root "$repo" >/dev/null

  assert_symlink_target "$codex_config" "$repo/dotfiles/.agent/apps/codex/config.toml"
  assert_contains "$codex_config" 'sandbox_mode = "workspace-write"'
  assert_not_contains "$codex_config" '[history]'
  assert_not_contains "$codex_config" 'persistence = "save-all"'
  assert_not_contains "$codex_config" 'history.max_bytes = 1024'
  assert_not_contains "$codex_config" 'codex_hooks = false'
  assert_not_contains "$codex_config" 'memories = true'
  assert_not_contains "$codex_config" 'use_memories = false'
  assert_not_contains "$codex_config" 'max_unused_days = 7'

  rm -rf "$repo" "$home_dir"
}

test_agent_sync_wrapper_delegates_to_setup_script() {
  assert_contains "$SYNC_SCRIPT" 'scripts/setup_agent_files.sh'
  assert_contains "$SYNC_SCRIPT" '--repo-root "$REPO_ROOT"'
}

main() {
  test_agent_sync_links_managed_files_and_generates_runtime_state
  test_agent_sync_replaces_existing_codex_config_with_managed_symlink
  test_agent_sync_wrapper_delegates_to_setup_script
  echo "agent sync tests passed"
}

main "$@"
