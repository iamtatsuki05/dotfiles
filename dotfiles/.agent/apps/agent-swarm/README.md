# Agent Swarm Config

Japanese version: [README_JA.md](README_JA.md)

This directory stores helper templates for Desplega Agent Swarm. The `agent-swarm` CLI itself is managed by `mise`.

## CLI

```bash
mise exec 'npm:@desplega.ai/agent-swarm' -- agent-swarm version
mise exec 'npm:@desplega.ai/agent-swarm' -- agent-swarm onboard --dry-run
```

`onboard` and `connect` may write `.env`, `docker-compose.yml`, `.mcp.json`, Claude local settings, and related files. Start with `--dry-run`.

## MCP

`agent-swarm api` exposes an HTTP MCP server at `http://localhost:3013/mcp`.

```bash
agent-swarm api --port 3013 --key "$AGENT_SWARM_API_KEY"
```

MCP clients use `Authorization: Bearer $AGENT_SWARM_API_KEY`. During real swarm worker sessions, `X-Agent-ID` and `X-Source-Task-Id` are also required and are normally injected by Agent Swarm adapters.

This repo does not enable the localhost MCP server globally by default. That avoids startup failures and timeouts when no local swarm API is running. Copy `mcp-http.example.json` or `mcp-codex.example.toml` into the project/client that should connect, then fill in the values.
