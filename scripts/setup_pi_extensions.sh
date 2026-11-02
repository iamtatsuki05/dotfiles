#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_dir="$repo_root/dotfiles/.agent/apps/pi"
bili_target="${BILI_CONFIG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/billion-context/billion-context.json}"
agent_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
mcp_source="$agent_dir/mcp.json"
mcp_target="$agent_dir/mcp-adapter.json"
if [[ -n "${PI_CODING_AGENT_DIR:-}" ]]; then
  target="$PI_CODING_AGENT_DIR/web-search.json"
elif [[ -n "${XDG_CONFIG_HOME:-}" ]]; then
  target="$XDG_CONFIG_HOME/pi/web-search.json"
else
  target="$HOME/.pi/agent/web-search.json"
fi

if [[ $# != 0 ]]; then
  printf 'Usage: bash scripts/setup_pi_extensions.sh\n' >&2
  exit 1
fi
command -v pi >/dev/null || { printf 'ERROR: install the mise-managed Pi CLI first\n' >&2; exit 1; }
if [[ ! -f "$mcp_source" ]]; then
  printf 'ERROR: run agent-sync to create the Pi MCP config first: %s\n' "$mcp_source" >&2
  exit 1
fi
node - "$mcp_source" <<'JS'
const { readFileSync } = require("node:fs");
const config = JSON.parse(readFileSync(process.argv[2], "utf8"));
const sharedKeys = new Set(["type", "command", "args", "env", "cwd", "url", "headers", "description"]);
if (!config.mcpServers || typeof config.mcpServers !== "object" || Array.isArray(config.mcpServers)) {
  throw new Error("Pi MCP config must contain an mcpServers object");
}
for (const [name, server] of Object.entries(config.mcpServers)) {
  if (!server || typeof server !== "object" || Array.isArray(server)) {
    throw new Error(`Pi MCP server must be an object: ${name}`);
  }
  const unsupported = Object.keys(server).filter(key => !sharedKeys.has(key));
  if (unsupported.length || (server.type !== undefined && !["stdio", "http", "streamable-http"].includes(server.type))) {
    throw new Error(`Cannot share Pi MCP server ${name} with the older GUI adapter: ${unsupported.join(", ") || "type"}. Reconcile the two schemas explicitly.`);
  }
}
JS
if [[ -e "$mcp_target" || -L "$mcp_target" ]]; then
  if [[ ! "$mcp_target" -ef "$mcp_source" ]]; then
    printf 'ERROR: preserve and reconcile the existing MCP Adapter config: %s\n' "$mcp_target" >&2
    exit 1
  fi
fi
if [[ -e "$bili_target" || -L "$bili_target" ]]; then
  if [[ ! "$bili_target" -ef "$source_dir/billion-context.json" ]]; then
    printf 'ERROR: preserve and reconcile the existing billion-context config: %s\n' "$bili_target" >&2
    exit 1
  fi
fi
if [[ ! -e "$target" && ! -L "$target" && -e "$HOME/.pi/web-search.json" && -z "${PI_CODING_AGENT_DIR:-}" ]]; then
  printf 'ERROR: reconcile the existing legacy Pi Web Access config: %s/.pi/web-search.json\n' "$HOME" >&2
  exit 1
fi
if [[ -e "$target" || -L "$target" ]]; then
  if [[ ! "$target" -ef "$source_dir/web-search.json" ]]; then
    printf 'ERROR: preserve and reconcile the existing Pi Web Access config: %s\n' "$target" >&2
    exit 1
  fi
else
  mkdir -p "$(dirname "$target")"
  ln -s "$source_dir/web-search.json" "$target"
fi
if [[ ! -e "$bili_target" ]]; then
  mkdir -p "$(dirname "$bili_target")"
  ln -s "$source_dir/billion-context.json" "$bili_target"
fi
if [[ ! -e "$mcp_target" ]]; then
  ln -s "$mcp_source" "$mcp_target"
fi

while IFS= read -r source || [[ -n "$source" ]]; do
  pi install "$source"
done < "$source_dir/packages.txt"
