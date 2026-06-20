#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SUPPORT_MATRIX="$REPO_ROOT/dotfiles/.agent/AGENT_SUPPORT.md"
readonly UPSTREAM_SCRIPT="$REPO_ROOT/scripts/agent_skill_upstreams.py"
readonly WAZA_CLI_AGENT_SCRIPT="$REPO_ROOT/scripts/waza_eval_cli_agent.sh"
readonly WAZA_CLI_AGENT_IMPL="$REPO_ROOT/scripts/agent/waza_eval_cli_agent.sh"
readonly SCHEDULER_MODELS="$REPO_ROOT/dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/src/agent_job_scheduler/models.py"
readonly SCHEDULER_ADAPTERS="$REPO_ROOT/dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/src/agent_job_scheduler/adapters.py"
readonly SCHEDULER_SETTINGS="$REPO_ROOT/dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/src/agent_job_scheduler/settings.py"
readonly MISE_CONFIG="$REPO_ROOT/config/mise/config.toml"
readonly MISE_TEMPLATE="$REPO_ROOT/home/.chezmoitemplates/mise-config.toml"

source "$TEST_DIR/lib/assertions.sh"

assert_agent_documented() {
  local agent="$1"

  assert_contains "$SUPPORT_MATRIX" "| \`$agent\` |"
}

test_support_matrix_documents_managed_agents() {
  local agent

  assert_file "$SUPPORT_MATRIX"
  for agent in claude codex copilot cursor devin antigravity hermes opencode openclaw grok; do
    assert_agent_documented "$agent"
  done
  assert_contains "$SUPPORT_MATRIX" "brew cask: antigravity-cli"
  assert_contains "$SUPPORT_MATRIX" "scripts/agent_skill_upstreams.py"
  assert_contains "$SUPPORT_MATRIX" "scripts/waza_eval_cli_agent.sh"
  assert_contains "$SUPPORT_MATRIX" "scripts/agent/waza_eval_cli_agent.sh"
  assert_contains "$SUPPORT_MATRIX" "agent-job-scheduler"
}

test_review_agent_code_supports_matrix_agents() {
  local agent

  for agent in codex claude-code antigravity-cli copilot cursor-agent devin hermes opencode openclaw grok; do
    assert_contains "$UPSTREAM_SCRIPT" "\"$agent\""
  done
  assert_contains "$UPSTREAM_SCRIPT" "brew-cask:antigravity-cli"
  assert_contains "$UPSTREAM_SCRIPT" "agy"
  assert_contains "$UPSTREAM_SCRIPT" "chat"
  assert_contains "$UPSTREAM_SCRIPT" "\"openclaw\": \"npm:openclaw\""
  assert_contains "$UPSTREAM_SCRIPT" "openclaw"
  assert_contains "$UPSTREAM_SCRIPT" "--local"
  assert_not_contains "$UPSTREAM_SCRIPT" "agent-swarm"
  assert_not_contains "$UPSTREAM_SCRIPT" "@desplega.ai/agent-swarm"
}

test_waza_cli_agent_code_supports_matrix_agents() {
  local agent

  for agent in codex claude antigravity copilot devin cursor opencode hermes openclaw grok; do
    assert_contains "$WAZA_CLI_AGENT_IMPL" "$agent"
  done
  assert_contains "$WAZA_CLI_AGENT_SCRIPT" 'agent/waza_eval_cli_agent.sh'
  assert_contains "$WAZA_CLI_AGENT_IMPL" 'brew install --cask $cask'
  assert_contains "$WAZA_CLI_AGENT_IMPL" "agy chat --mode agent"
  assert_contains "$WAZA_CLI_AGENT_IMPL" "npm:openclaw"
  assert_contains "$WAZA_CLI_AGENT_IMPL" "openclaw agent"
  assert_not_contains "$WAZA_CLI_AGENT_IMPL" "agent-swarm"
  assert_not_contains "$WAZA_CLI_AGENT_IMPL" "@desplega.ai/agent-swarm"
  assert_contains "$MISE_CONFIG" "[tasks.waza-eval-model]"
  assert_contains "$MISE_TEMPLATE" "[tasks.waza-eval-model]"
  assert_not_contains "$MISE_CONFIG" "[tasks.waza-eval-openclaw]"
  assert_not_contains "$MISE_TEMPLATE" "[tasks.waza-eval-openclaw]"
}

test_agent_job_scheduler_code_supports_matrix_agents() {
  local enum_name

  for enum_name in ANTIGRAVITY CLAUDE CODEX COPILOT CURSOR DEVIN HERMES OPENCODE OPENCLAW GROK; do
    assert_contains "$SCHEDULER_MODELS" "$enum_name"
  done
  assert_contains "$SCHEDULER_ADAPTERS" "Agent.ANTIGRAVITY"
  assert_contains "$SCHEDULER_ADAPTERS" "build_antigravity_command"
  assert_contains "$SCHEDULER_SETTINGS" "Agent.ANTIGRAVITY.value"
  assert_contains "$SCHEDULER_ADAPTERS" "Agent.OPENCLAW"
  assert_contains "$SCHEDULER_ADAPTERS" "build_openclaw_command"
  assert_contains "$SCHEDULER_SETTINGS" "Agent.OPENCLAW.value"
  assert_contains "$SCHEDULER_ADAPTERS" "Agent.GROK"
  assert_contains "$SCHEDULER_ADAPTERS" "build_grok_command"
  assert_contains "$SCHEDULER_SETTINGS" "Agent.GROK.value"
  assert_not_contains "$SCHEDULER_MODELS" "AGENT_SWARM"
  assert_not_contains "$SCHEDULER_ADAPTERS" "Agent.AGENT_SWARM"
  assert_not_contains "$SCHEDULER_ADAPTERS" "build_agent_swarm_command"
  assert_not_contains "$SCHEDULER_SETTINGS" "Agent.AGENT_SWARM.value"
}

main() {
  test_support_matrix_documents_managed_agents
  test_review_agent_code_supports_matrix_agents
  test_waza_cli_agent_code_supports_matrix_agents
  test_agent_job_scheduler_code_supports_matrix_agents
  echo "agent support matrix tests passed"
}

main "$@"
