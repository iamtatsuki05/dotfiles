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

assert_not_exists() {
  local target_path="$1"
  [[ ! -e "$target_path" ]] || fail "expected path not to exist: $target_path"
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
    "$repo/dotfiles/.agent/apps/copilot" \
    "$repo/dotfiles/.agent/apps/codex" \
    "$repo/dotfiles/.agent/apps/cursor" \
    "$repo/dotfiles/.agent/apps/devin" \
    "$repo/dotfiles/.agent/apps/gemini" \
    "$repo/dotfiles/.agent/apps/hermes-agent/agent-hooks" \
    "$repo/dotfiles/.agent/apps/opencode/plugins" \
    "$repo/dotfiles/.agent/apps/openclaw" \
    "$repo/dotfiles/.agent/hooks" \
    "$repo/dotfiles/.agent/skills" \
    "$repo/dotfiles/.agent/pets"

  cp "$SETUP_AGENT_SCRIPT" "$repo/scripts/setup_agent_files.sh"
  cp "$SYNC_SCRIPT" "$repo/dotfiles/.agent/sync.sh"
  print -r -- '# agent prompt' > "$repo/dotfiles/.agent/AGENTS.md"
  print -r -- '#!/usr/bin/env bash' > "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  print -r -- '#!/usr/bin/env bash' > "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  chmod +x "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  chmod +x "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  chmod +x "$repo/scripts/setup_agent_files.sh" "$repo/dotfiles/.agent/sync.sh"

  cat > "$repo/dotfiles/.agent/apps/claude/settings.json" <<'EOF'
{"cleanupPeriodDays":36500}
EOF
  cat > "$repo/dotfiles/.agent/apps/claude/.mcp.json" <<'EOF'
{"mcpServers":{"codex":{"command":"codex","args":["mcp-server"]}}}
EOF
  cat > "$repo/dotfiles/.agent/apps/copilot/mcp-config.json" <<'EOF'
{"mcpServers":{"playwright":{"type":"local","command":"bunx","args":["@playwright/mcp@latest"],"env":{},"tools":["*"]}}}
EOF
  cat > "$repo/dotfiles/.agent/apps/copilot/settings.json" <<'EOF'
{"autoUpdate":false,"respectGitignore":true,"allowedUrls":["github.com"],"hooks":{"sessionStart":[{"type":"command","bash":"zsh \"$HOME/.copilot/hooks/agent_context_reminder.sh\""}],"userPromptSubmitted":[{"type":"command","bash":"zsh \"$HOME/.copilot/hooks/agent_context_reminder.sh\""}],"postToolUse":[{"type":"command","bash":"zsh \"$HOME/.copilot/hooks/jupytext_sync.sh\""}]}}
EOF
  cat > "$repo/dotfiles/.agent/apps/codex/hooks.json" <<'EOF'
{"hooks":{"SessionStart":[{"matcher":".*","hooks":[{"type":"command","command":"~/.codex/hooks/agent_context_reminder.sh"}]}],"UserPromptSubmit":[{"matcher":".*","hooks":[{"type":"command","command":"~/.codex/hooks/agent_context_reminder.sh"}]}],"PostToolUse":[{"matcher":".*","hooks":[{"type":"command","command":"~/.codex/hooks/jupytext_sync.sh"}]}]}}
EOF
  cat > "$repo/dotfiles/.agent/apps/codex/config.toml" <<'EOF'
model = "gpt-5.4"
sandbox_mode = "workspace-write"

[features]
hooks = true

[sandbox_workspace_write]
network_access = true
EOF
  cat > "$repo/dotfiles/.agent/apps/devin/config.json" <<'EOF'
{"auto_update":false,"include_gitignored_files":false,"respect_gitignore":true,"permissions":{"deny":["Read(**/.env*)","Write(**/.env*)"]},"mcpServers":{"playwright":{"command":"bunx","args":["@playwright/mcp@latest"],"env":{}}},"hooks":{"SessionStart":[{"matcher":".*","hooks":[{"type":"command","command":"~/.config/devin/hooks/agent_context_reminder.sh"}]}],"UserPromptSubmit":[{"matcher":".*","hooks":[{"type":"command","command":"~/.config/devin/hooks/agent_context_reminder.sh"}]}],"PostToolUse":[{"matcher":".*","hooks":[{"type":"command","command":"~/.config/devin/hooks/jupytext_sync.sh"}]}]}}
EOF
  cat > "$repo/dotfiles/.agent/apps/gemini/settings.json" <<'EOF'
{"general":{"sessionRetention":{"enabled":false}},"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"~/.gemini/hooks/agent_context_reminder.sh"}]}],"BeforeAgent":[{"hooks":[{"type":"command","command":"~/.gemini/hooks/agent_context_reminder.sh"}]}]}}
EOF
  print -r -- 'secrets.env' > "$repo/dotfiles/.agent/apps/gemini/ignore"
  cat > "$repo/dotfiles/.agent/apps/cursor/mcp.json" <<'EOF'
{"mcpServers":{"gemini-cli":{"command":"bunx","args":["mcp-gemini-cli","--allow-npx"]}}}
EOF
  cat > "$repo/dotfiles/.agent/apps/cursor/cli-config.json" <<'EOF'
{"version":1,"editor":{"vimMode":false},"permissions":{"allow":[],"deny":[]}}
EOF
  cat > "$repo/dotfiles/.agent/apps/cursor/hooks.json" <<'EOF'
{"version":1,"hooks":{"sessionStart":[{"command":"zsh \"$HOME/.cursor/hooks/agent_context_reminder.sh\""}],"beforeSubmitPrompt":[{"command":"zsh \"$HOME/.cursor/hooks/agent_context_reminder.sh\""}],"postToolUse":[{"command":"zsh \"$HOME/.cursor/hooks/agent_context_reminder.sh\""}],"afterFileEdit":[{"command":"zsh \"$HOME/.cursor/hooks/jupytext_sync.sh\""}]}}
EOF
  cat > "$repo/dotfiles/.agent/apps/opencode/opencode.json" <<'EOF'
{"$schema":"https://opencode.ai/config.json","autoupdate":false,"instructions":["~/.config/opencode/AGENTS.md"],"permission":{"bash":"ask","webfetch":"allow","read":{"**/.env":"deny"},"edit":{"**/.env":"deny"}},"mcp":{"playwright":{"type":"local","command":["bunx","@playwright/mcp@latest"],"enabled":true}}}
EOF
  print -r -- 'export const JupytextSync = async () => ({})' > "$repo/dotfiles/.agent/apps/opencode/plugins/jupytext-sync.js"
  print -r -- 'export const SecretProtection = async () => ({})' > "$repo/dotfiles/.agent/apps/opencode/plugins/secret-protection.js"
  print -r -- 'export const AgentContextReminder = async () => ({})' > "$repo/dotfiles/.agent/apps/opencode/plugins/agent-context-reminder.js"
  cat > "$repo/dotfiles/.agent/apps/hermes-agent/config.yaml" <<'EOF'
hooks_auto_accept: true
hooks:
  pre_llm_call:
    - matcher: ".*"
      command: "~/.hermes/agent-hooks/agent_context_reminder.sh"
  pre_tool_call:
    - matcher: "read_file|write_file|patch|terminal"
      command: "~/.hermes/agent-hooks/secret-protection.sh"
  post_tool_call:
    - matcher: "write_file|patch"
      command: "~/.hermes/agent-hooks/jupytext_sync.sh"
mcp_servers:
  playwright:
    command: "bunx"
    args: ["@playwright/mcp@latest"]
EOF
  print -r -- '#!/usr/bin/env bash' > "$repo/dotfiles/.agent/apps/hermes-agent/agent-hooks/secret-protection.sh"
  chmod +x "$repo/dotfiles/.agent/apps/hermes-agent/agent-hooks/secret-protection.sh"
  cat > "$repo/dotfiles/.agent/apps/openclaw/openclaw.json" <<'EOF'
{"agents":{"defaults":{"workspace":"~/.openclaw/workspace","skipBootstrap":true}},"tools":{"profile":"coding"},"hooks":{"internal":{"enabled":true,"entries":{"bootstrap-extra-files":{"enabled":true,"paths":["AGENTS.md"]}}}},"mcp":{"servers":{"playwright":{"command":"bunx","args":["@playwright/mcp@latest"]}}}}
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
  {
    print -r -- 'export DEVIN_API_KEY=test-key'
    print -r -- 'export OPENCODE_API_KEY=opencode-test-key'
  } > "$xdg_config_home/shell/secrets.env"

  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" \
    run_with_timeout "$TEST_TIMEOUT_SECONDS" "$TEST_ZSH_BIN" "$repo/scripts/setup_agent_files.sh" --repo-root "$repo" >/dev/null

  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" \
    run_with_timeout "$TEST_TIMEOUT_SECONDS" "$TEST_ZSH_BIN" "$repo/scripts/setup_agent_files.sh" --repo-root "$repo" >/dev/null

  assert_symlink_target "$home_dir/.claude/settings.json" "$repo/dotfiles/.agent/apps/claude/settings.json"
  assert_not_exists "$repo/AGENTS.md"
  assert_symlink_target "$home_dir/.claude/.mcp.json" "$repo/dotfiles/.agent/apps/claude/.mcp.json"
  assert_symlink_target "$home_dir/.claude/CLAUDE.md" "$repo/dotfiles/.agent/AGENTS.md"
  assert_symlink_target "$home_dir/.claude/hooks/jupytext_sync.sh" "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  assert_symlink_target "$home_dir/.claude/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_symlink_target "$home_dir/.copilot/copilot-instructions.md" "$repo/dotfiles/.agent/AGENTS.md"
  assert_symlink_target "$home_dir/.copilot/skills" "$repo/dotfiles/.agent/skills"
  assert_symlink_target "$home_dir/.copilot/hooks/jupytext_sync.sh" "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  assert_symlink_target "$home_dir/.copilot/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_symlink_target "$home_dir/.copilot/settings.json" "$repo/dotfiles/.agent/apps/copilot/settings.json"
  assert_symlink_target "$home_dir/.copilot/mcp-config.json" "$repo/dotfiles/.agent/apps/copilot/mcp-config.json"
  assert_contains "$home_dir/.copilot/settings.json" '"respectGitignore"'
  assert_contains "$home_dir/.copilot/settings.json" '"allowedUrls"'
  assert_contains "$home_dir/.copilot/settings.json" '"sessionStart"'
  assert_contains "$home_dir/.copilot/settings.json" '"userPromptSubmitted"'
  assert_contains "$home_dir/.copilot/settings.json" 'agent_context_reminder.sh'
  assert_contains "$home_dir/.copilot/settings.json" '"postToolUse"'
  assert_contains "$home_dir/.copilot/mcp-config.json" '"mcpServers"'
  assert_contains "$home_dir/.copilot/mcp-config.json" '"playwright"'
  assert_symlink_target "$xdg_config_home/devin/config.json" "$repo/dotfiles/.agent/apps/devin/config.json"
  assert_symlink_target "$xdg_config_home/devin/skills" "$repo/dotfiles/.agent/skills"
  assert_symlink_target "$xdg_config_home/devin/hooks/jupytext_sync.sh" "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  assert_symlink_target "$xdg_config_home/devin/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_contains "$xdg_config_home/devin/config.json" '"mcpServers"'
  assert_contains "$xdg_config_home/devin/config.json" '"playwright"'
  assert_contains "$xdg_config_home/devin/config.json" '"respect_gitignore"'
  assert_contains "$xdg_config_home/devin/config.json" '"SessionStart"'
  assert_contains "$xdg_config_home/devin/config.json" '"UserPromptSubmit"'
  assert_contains "$xdg_config_home/devin/config.json" 'agent_context_reminder.sh'
  assert_contains "$xdg_config_home/devin/config.json" 'Read(**/.env*)'
  assert_symlink_target "$home_dir/.gemini/settings.json" "$repo/dotfiles/.agent/apps/gemini/settings.json"
  assert_symlink_target "$home_dir/.gemini/ignore" "$repo/dotfiles/.agent/apps/gemini/ignore"
  assert_symlink_target "$home_dir/.gemini/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_contains "$home_dir/.gemini/settings.json" '"SessionStart"'
  assert_contains "$home_dir/.gemini/settings.json" '"BeforeAgent"'
  assert_contains "$home_dir/.gemini/settings.json" 'agent_context_reminder.sh'
  assert_symlink_target "$home_dir/.cursor/cli-config.json" "$repo/dotfiles/.agent/apps/cursor/cli-config.json"
  assert_symlink_target "$home_dir/.cursor/hooks.json" "$repo/dotfiles/.agent/apps/cursor/hooks.json"
  assert_symlink_target "$home_dir/.cursor/mcp.json" "$repo/dotfiles/.agent/apps/cursor/mcp.json"
  assert_symlink_target "$home_dir/.cursor/hooks/jupytext_sync.sh" "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  assert_symlink_target "$home_dir/.cursor/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_contains "$home_dir/.cursor/cli-config.json" '"permissions"'
  assert_contains "$home_dir/.cursor/hooks.json" '"beforeSubmitPrompt"'
  assert_contains "$home_dir/.cursor/hooks.json" 'agent_context_reminder.sh'
  assert_symlink_target "$xdg_config_home/opencode/AGENTS.md" "$repo/dotfiles/.agent/AGENTS.md"
  assert_symlink_target "$xdg_config_home/opencode/skills" "$repo/dotfiles/.agent/skills"
  assert_symlink_target "$xdg_config_home/opencode/hooks/jupytext_sync.sh" "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  assert_symlink_target "$xdg_config_home/opencode/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_symlink_target "$xdg_config_home/opencode/opencode.json" "$repo/dotfiles/.agent/apps/opencode/opencode.json"
  assert_symlink_target "$xdg_config_home/opencode/plugins" "$repo/dotfiles/.agent/apps/opencode/plugins"
  assert_file "$xdg_config_home/opencode/plugins/agent-context-reminder.js"
  assert_contains "$xdg_config_home/opencode/opencode.json" '"mcp"'
  assert_contains "$xdg_config_home/opencode/opencode.json" '"permission"'
  assert_symlink_target "$home_dir/.hermes/AGENTS.md" "$repo/dotfiles/.agent/AGENTS.md"
  assert_symlink_target "$home_dir/.hermes/skills" "$repo/dotfiles/.agent/skills"
  assert_symlink_target "$home_dir/.hermes/config.yaml" "$repo/dotfiles/.agent/apps/hermes-agent/config.yaml"
  assert_symlink_target "$home_dir/.hermes/agent-hooks/jupytext_sync.sh" "$repo/dotfiles/.agent/hooks/jupytext_sync.sh"
  assert_symlink_target "$home_dir/.hermes/agent-hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_symlink_target "$home_dir/.hermes/agent-hooks/secret-protection.sh" "$repo/dotfiles/.agent/apps/hermes-agent/agent-hooks/secret-protection.sh"
  assert_contains "$home_dir/.hermes/config.yaml" 'mcp_servers:'
  assert_contains "$home_dir/.hermes/config.yaml" 'hooks_auto_accept: true'
  assert_contains "$home_dir/.hermes/config.yaml" 'pre_llm_call:'
  assert_contains "$home_dir/.hermes/config.yaml" 'agent_context_reminder.sh'
  assert_symlink_target "$home_dir/.openclaw/openclaw.json" "$repo/dotfiles/.agent/apps/openclaw/openclaw.json"
  assert_symlink_target "$home_dir/.openclaw/workspace/AGENTS.md" "$repo/dotfiles/.agent/AGENTS.md"
  assert_symlink_target "$home_dir/.openclaw/workspace/skills" "$repo/dotfiles/.agent/skills"
  assert_contains "$home_dir/.openclaw/openclaw.json" '"workspace"'
  assert_contains "$home_dir/.openclaw/openclaw.json" '"bootstrap-extra-files"'
  assert_contains "$home_dir/.openclaw/openclaw.json" '"mcp"'
  assert_symlink_target "$home_dir/.codex/config.toml" "$repo/dotfiles/.agent/apps/codex/config.toml"
  assert_symlink_target "$home_dir/.codex/hooks.json" "$repo/dotfiles/.agent/apps/codex/hooks.json"
  assert_symlink_target "$home_dir/.codex/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  assert_contains "$home_dir/.codex/hooks.json" '"SessionStart"'
  assert_contains "$home_dir/.codex/hooks.json" '"UserPromptSubmit"'
  assert_contains "$home_dir/.codex/hooks.json" 'agent_context_reminder.sh'
  assert_not_contains "$home_dir/.codex/config.toml" '[history]'
  assert_contains "$home_dir/.codex/config.toml" '[features]'
  assert_contains "$home_dir/.codex/config.toml" 'hooks = true'
  assert_symlink_target "$home_dir/.codex/pets" "$repo/dotfiles/.agent/pets"
  assert_not_contains "$home_dir/.codex/config.toml" '[memories]'
  assert_not_contains "$home_dir/.codex/config.toml" 'persistence = "save-all"'
  assert_not_contains "$home_dir/.codex/config.toml" 'codex_hooks = true'
  assert_not_contains "$home_dir/.codex/config.toml" 'memories = true'
  assert_not_contains "$home_dir/.codex/config.toml" 'generate_memories = true'
  assert_not_contains "$home_dir/.codex/config.toml" 'max_rollout_age_days = 90'
  assert_not_contains "$home_dir/.codex/config.toml" 'max_unused_days = 365'
  assert_file "$home_dir/.gemini/.env"
  assert_contains "$home_dir/.gemini/.env" 'DEVIN_API_KEY=test-key'
  assert_file "$home_dir/.hermes/.env"
  assert_contains "$home_dir/.hermes/.env" 'DEVIN_API_KEY=test-key'
  assert_contains "$home_dir/.hermes/.env" 'OPENCODE_API_KEY=opencode-test-key'
  assert_contains "$home_dir/.hermes/.env" 'OPENCODE_GO_API_KEY=opencode-test-key'
  assert_file "$home_dir/.openclaw/.env"
  assert_contains "$home_dir/.openclaw/.env" 'DEVIN_API_KEY=test-key'
  assert_contains "$home_dir/.openclaw/.env" 'OPENCODE_API_KEY=opencode-test-key'

  rm -rf "$repo" "$home_dir"
}

test_agent_sync_installs_missing_hermes_mcp_dependency() {
  local repo
  local home_dir
  local xdg_config_home
  local fake_bin
  local hermes_install
  local uv_log
  make_temp_dir
  repo="$REPLY"
  make_temp_dir
  home_dir="$REPLY"
  xdg_config_home="$home_dir/.config"
  fake_bin="$home_dir/fake-bin"
  hermes_install="$home_dir/.local/share/mise/installs/pipx-git-https-github-com-nous-research-hermes-agent-git/v2026.4.30"
  uv_log="$home_dir/uv.log"

  create_agent_fixture_repo "$repo"
  mkdir -p "$xdg_config_home/shell" "$fake_bin" "$hermes_install/bin" "$hermes_install/hermes-agent/bin"

  cat > "$hermes_install/bin/hermes" <<'EOF'
#!/usr/bin/env zsh
exit 0
EOF
  chmod +x "$hermes_install/bin/hermes"

  cat > "$hermes_install/hermes-agent/bin/python" <<'EOF'
#!/usr/bin/env zsh
exit 1
EOF
  chmod +x "$hermes_install/hermes-agent/bin/python"

  cat > "$fake_bin/uv" <<EOF
#!/usr/bin/env zsh
print -r -- "\$*" > "$uv_log"
exit 0
EOF
  chmod +x "$fake_bin/uv"

  HOME="$home_dir" XDG_CONFIG_HOME="$xdg_config_home" PATH="$hermes_install/bin:$fake_bin:$PATH" \
    run_with_timeout "$TEST_TIMEOUT_SECONDS" "$TEST_ZSH_BIN" "$repo/scripts/setup_agent_files.sh" --repo-root "$repo" >/dev/null

  assert_file "$uv_log"
  assert_contains "$uv_log" "pip install --python $hermes_install/hermes-agent/bin/python mcp>=1.24,<2"

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
  assert_contains "$codex_config" 'hooks = true'
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

test_agent_context_reminder_hook_outputs_valid_json_context() {
  local output

  output="$(printf '%s\n' '{"hook_event_name":"UserPromptSubmit","cwd":"'"$REPO_ROOT"'","prompt":"implement this"}' | "$REPO_ROOT/dotfiles/.agent/hooks/agent_context_reminder.sh")"

  print -r -- "$output" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
hook_output = payload["hookSpecificOutput"]
assert hook_output["hookEventName"] == "UserPromptSubmit"
assert payload["context"] == hook_output["additionalContext"]
assert payload["additionalContext"] == hook_output["additionalContext"]
assert payload["additional_context"] == hook_output["additionalContext"]
assert payload["prependContext"] == hook_output["additionalContext"]
context = hook_output["additionalContext"]
assert "リポジトリ hook リマインダー:" in context
assert "現在の状態を確認" in context
assert ".agent/changes/CHANGES.md" in context
'

  output="$(printf '%s\n' '{"hook_event_name":"pre_llm_call","cwd":"'"$REPO_ROOT"'","user_message":"implement this"}' | "$REPO_ROOT/dotfiles/.agent/hooks/agent_context_reminder.sh")"
  print -r -- "$output" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
assert payload["hookSpecificOutput"]["hookEventName"] == "BeforeModel"
assert "リポジトリ hook リマインダー:" in payload["context"]
'

  output="$(printf '%s\n' '{"hook_event_name":"beforeSubmitPrompt","workspace_roots":["'"$REPO_ROOT"'"],"prompt":"implement this"}' | "$REPO_ROOT/dotfiles/.agent/hooks/agent_context_reminder.sh")"
  print -r -- "$output" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
assert ".agent/changes/CHANGES.md" in payload["additional_context"]
'
}

test_agent_context_reminder_detects_managed_dotfiles_agent_dir() {
  local repo
  local output
  make_temp_dir
  repo="$REPLY"

  mkdir -p "$repo/.git" "$repo/dotfiles/.agent/changes" "$repo/dotfiles/.agent/hooks" "$repo/work/subdir"
  cp "$REPO_ROOT/dotfiles/.agent/hooks/agent_context_reminder.sh" "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  chmod +x "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh"
  print -r -- "# test changes" > "$repo/dotfiles/.agent/changes/CHANGES.md"

  output="$(printf '%s\n' '{"hook_event_name":"UserPromptSubmit","cwd":"'"$repo"'","prompt":"implement this"}' | "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh")"
  print -r -- "$output" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
context = payload["additional_context"]
assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
assert "/dotfiles/.agent/changes/CHANGES.md" in context
'

  output="$(printf '%s\n' '{"hook_event_name":"UserPromptSubmit","cwd":"'"$repo"'/work/subdir","prompt":"implement this"}' | "$repo/dotfiles/.agent/hooks/agent_context_reminder.sh")"
  print -r -- "$output" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
assert "/dotfiles/.agent/changes/CHANGES.md" in payload["additional_context"]
'

  rm -rf "$repo"
}

main() {
  test_agent_sync_links_managed_files_and_generates_runtime_state
  test_agent_sync_installs_missing_hermes_mcp_dependency
  test_agent_sync_replaces_existing_codex_config_with_managed_symlink
  test_agent_sync_wrapper_delegates_to_setup_script
  test_agent_context_reminder_hook_outputs_valid_json_context
  test_agent_context_reminder_detects_managed_dotfiles_agent_dir
  echo "agent sync tests passed"
}

main "$@"
