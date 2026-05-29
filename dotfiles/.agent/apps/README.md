# Agent App Configs

Japanese version: [README_JA.md](README_JA.md)

This directory contains per-agent configuration files that are synced into each local AI CLI home by `dotfiles/.agent/sync.sh`.

The shared behavior prompt lives in `../AGENTS.md`.
This directory is for tool-specific settings, MCP configuration, hooks, permissions, plugins, and runtime integration.

## Layout

| Path | Agent | Contents |
|---|---|---|
| `antigravity-cli/` | Antigravity CLI | Settings and the `dotfiles-agent` plugin files for Gemini / Antigravity. |
| `claude/` | Claude Code | Claude settings and MCP configuration. |
| `codex/` | Codex | Codex config and hook registration. |
| `copilot/` | GitHub Copilot CLI | Copilot settings and MCP configuration. |
| `cursor/` | Cursor Agent | Cursor CLI config, MCP config, hook registration, and `.cursorignore`. |
| `devin/` | Devin CLI | Devin local config and permissions. |
| `hermes-agent/` | Hermes Agent | Hermes config plus extra agent hooks. |
| `opencode/` | opencode | opencode config and JavaScript plugins. |
| `openclaw/` | OpenClaw | OpenClaw workspace and MCP configuration. |
| `grok/` | Grok CLI (xAI) | Grok CLI config and settings. |

## File Types

- `settings.*`, `config.*`, `*.toml`, `*.yaml`, `*.json`: agent-specific runtime configuration.
- `mcp*`: MCP server definitions for agents that support them.
- `hooks.json`: hook registration for agents with hook support.
- completion notification hooks: register `agent_turn_done_notify.sh` on each compatible agent's end-of-turn event (`Stop`, `agentStop`, `stop`, `post_llm_call`, or opencode `session.idle`). Claude Code also keeps a `Notification` hook for its idle/permission notifications, but completion sound depends on `Stop`.
- `plugins/`: plugin code or plugin manifests for agents that expose plugin APIs.
- `agent-hooks/`: shell hooks consumed by agents that use hook directories instead of JSON hook maps.

## Update Rules

- Keep the canonical agent list aligned with `../AGENT_SUPPORT.md`.
- When adding a new supported agent, update this directory, `../AGENT_SUPPORT.md`, `../../scripts/setup_agent_files.sh`, and relevant tests in the same change.
- Do not put secrets in this directory. Runtime env files are generated from `~/.config/shell/secrets.env`.
- Validate structured config after edits. Typical checks include `jq empty` for JSON, `bash -n` for shell hooks, and agent-specific config validators when available.

## Common Checks

```bash
jq empty dotfiles/.agent/apps/claude/settings.json dotfiles/.agent/apps/copilot/settings.json dotfiles/.agent/apps/devin/config.json dotfiles/.agent/apps/codex/hooks.json dotfiles/.agent/apps/cursor/hooks.json dotfiles/.agent/apps/antigravity-cli/plugins/dotfiles-agent/hooks.json dotfiles/.agent/apps/opencode/opencode.json
bash -n dotfiles/.agent/hooks/agent_turn_done_notify.sh
bash -n dotfiles/.agent/apps/hermes-agent/agent-hooks/secret-protection.sh
zsh tests/test_agent_sync.sh
```
