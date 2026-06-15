---
name: agent-swarm
description: "Use when installing, configuring, operating, or troubleshooting Desplega Agent Swarm (`agent-swarm`, `@desplega.ai/agent-swarm`), including local API/MCP server setup, onboarding, connect, lead/worker runs, Composio routes, Agent Swarm skills, or one-shot CLI-agent evaluation through Agent Swarm."
---

# Agent Swarm

Use this for Desplega Agent Swarm work, not for generic multi-agent planning inside the current Codex session.

## Managed CLI

The CLI is managed by mise:

```bash
mise exec 'npm:@desplega.ai/agent-swarm' -- agent-swarm version
mise exec 'npm:@desplega.ai/agent-swarm' -- agent-swarm help
```

The upstream package is `@desplega.ai/agent-swarm`; the binary is `agent-swarm`.

## Safe Defaults

- Use `AGENT_SWARM_API_KEY` instead of generic `API_KEY` in shell profiles and docs.
- Run `agent-swarm onboard --dry-run` or `agent-swarm connect --dry-run` before allowing writes.
- Do not run `onboard`, `connect`, `api`, `lead`, `worker`, `e2b`, `claude-managed-setup`, or `x composio ...` without checking the target directory, credentials, Docker/API impact, external writes, and possible paid quota.
- Do not commit generated `.env`, `docker-compose.yml`, `.mcp.json`, `.claude/settings.local.json`, logs, DB files, or task artifacts unless the user explicitly wants them tracked.

## Common Commands

```bash
agent-swarm docs
agent-swarm onboard --dry-run
agent-swarm connect --dry-run
agent-swarm api --port 3013 --key "$AGENT_SWARM_API_KEY"
agent-swarm claude --headless -m "Summarize this repository"
```

`agent-swarm claude --headless -m` is the repo-managed one-shot path used by Waza and `agent-job-scheduler`. `lead` and `worker` are long-running swarm roles and should be started only when the user explicitly asks for a running swarm.

## MCP

`agent-swarm api` exposes the HTTP MCP server at:

```text
http://localhost:3013/mcp
```

Use `Authorization: Bearer $AGENT_SWARM_API_KEY`. In real swarm worker sessions, also include `X-Agent-ID` and `X-Source-Task-Id`; Agent Swarm adapters normally inject those headers. Outside a swarm task, avoid enabling this MCP globally unless the API is running and you know which identity/task should own tool calls.

Templates live under:

```text
dotfiles/.agent/apps/agent-swarm/
```

Copy or adapt them into a project/client-specific config only after `agent-swarm api` is reachable. Keeping this MCP disabled by default prevents normal agent startup from timing out when no local swarm is running.

## Agent Swarm Skills And Tools

Agent Swarm has its own MCP tools for skills and MCP servers (`skill-*`, `mcp-server-*`) and upstream plugin skills for Composio, pages, KV, artifacts, and workflow-specific commands. Treat those as Agent Swarm runtime assets. Do not blindly vendor broad upstream skills into the global local skill tree if they would override existing local connectors such as Gmail, Google Calendar, Google Docs, Slack, or Drive.
