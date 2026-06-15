# Agent Swarm 設定

English version: [README.md](README.md)

このディレクトリは Desplega Agent Swarm の補助テンプレート置き場です。`agent-swarm` CLI 本体は `mise` で管理します。

## CLI

```bash
mise exec 'npm:@desplega.ai/agent-swarm' -- agent-swarm version
mise exec 'npm:@desplega.ai/agent-swarm' -- agent-swarm onboard --dry-run
```

`onboard` と `connect` は `.env`、`docker-compose.yml`、`.mcp.json`、Claude local settings などを書き得るため、最初は `--dry-run` で確認します。

## MCP

`agent-swarm api` は HTTP MCP server を `http://localhost:3013/mcp` に公開します。

```bash
agent-swarm api --port 3013 --key "$AGENT_SWARM_API_KEY"
```

MCP client からは `Authorization: Bearer $AGENT_SWARM_API_KEY` を使います。Swarm worker の実行中は `X-Agent-ID` と `X-Source-Task-Id` も必要で、Agent Swarm の adapter が通常は注入します。

この repo では localhost MCP を常時有効化しません。API が起動していない普段の agent 起動時に失敗や timeout を増やさないためです。必要な project/client にだけ `mcp-http.example.json` または `mcp-codex.example.toml` をコピーして値を埋めてください。
