#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/home/.pi/agent" "$fixture/bin"
export BILI_CONFIG_FILE="$fixture/billion-context.json"
export XDG_CONFIG_HOME="$fixture/home/.config"
unset PI_CODING_AGENT_DIR
export HOME="$fixture/home" PATH="$fixture/bin:$PATH" PI_INSTALL_LOG="$fixture/install.log"
cat > "$fixture/bin/pi" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$PI_INSTALL_LOG"
[[ ${PI_INSTALL_FAIL:-0} != 1 ]] || exit 7
STUB
chmod +x "$fixture/bin/pi"
installer="$repo_root/scripts/setup_pi_extensions.sh"
printf '{"mcpServers":{}}\n' > "$HOME/.pi/agent/mcp.json"
for key in enabled exposure toolExposure timeout oauth; do
  printf '{"mcpServers":{"blocked":{"command":"never-run","%s":false}}}\n' "$key" > "$HOME/.pi/agent/mcp.json"
  if bash "$installer" > "$fixture/mcp-schema-$key.log" 2>&1; then
    echo "FAIL: accepted incompatible Pi MCP key: $key" >&2; exit 1
  fi
  [[ ! -e "$PI_INSTALL_LOG" ]]
done
printf '{"mcpServers":{}}\n' > "$HOME/.pi/agent/mcp.json"
target="$XDG_CONFIG_HOME/pi/web-search.json"
mkdir -p "$(dirname "$target")"
# A conflicting personal config must be preserved before any package install.
printf '{"provider":"brave"}\n' > "$target"
if bash "$installer" > "$fixture/conflict.log" 2>&1; then
  echo 'FAIL: accepted a conflicting personal config' >&2; exit 1
fi
[[ ! -e "$PI_INSTALL_LOG" ]]
[[ $(cat "$target") == '{"provider":"brave"}' ]]
rm "$target"
printf '{"provider":"brave"}\n' > "$HOME/.pi/web-search.json"
if bash "$installer" > "$fixture/legacy.log" 2>&1; then
  echo 'FAIL: bypassed a personal legacy config' >&2; exit 1
fi
[[ ! -e "$PI_INSTALL_LOG" ]]
rm "$HOME/.pi/web-search.json"
printf '{"autoUpdate":true}\n' > "$BILI_CONFIG_FILE"
if bash "$installer" > "$fixture/bili-conflict.log" 2>&1; then
  echo 'FAIL: accepted a conflicting billion-context config' >&2; exit 1
fi
[[ ! -e "$PI_INSTALL_LOG" ]]
[[ $(cat "$BILI_CONFIG_FILE") == '{"autoUpdate":true}' ]]
rm "$BILI_CONFIG_FILE"
bash "$installer"
[[ "$BILI_CONFIG_FILE" -ef "$repo_root/dotfiles/.agent/apps/pi/billion-context.json" ]]
[[ "$target" -ef "$repo_root/dotfiles/.agent/apps/pi/web-search.json" ]]
[[ "$HOME/.pi/agent/mcp-adapter.json" -ef "$HOME/.pi/agent/mcp.json" ]]
expected="$(sed 's/^/install /' "$repo_root/dotfiles/.agent/apps/pi/packages.txt")"
[[ $(cat "$PI_INSTALL_LOG") == "$expected" ]]
# Re-running converges to the same declarations through Pi's native installer.
: > "$PI_INSTALL_LOG"
bash "$installer"
[[ $(cat "$PI_INSTALL_LOG") == "$expected" ]]
: > "$PI_INSTALL_LOG"
export PI_INSTALL_FAIL=1
if bash "$installer" > "$fixture/failure.log" 2>&1; then
  echo 'FAIL: ignored a failed Pi install' >&2; exit 1
fi
[[ $(wc -l < "$PI_INSTALL_LOG" | tr -d ' ') == 1 ]]
unset PI_INSTALL_FAIL XDG_CONFIG_HOME
bash "$installer"
[[ "$HOME/.pi/agent/web-search.json" -ef "$repo_root/dotfiles/.agent/apps/pi/web-search.json" ]]
export PI_CODING_AGENT_DIR="$fixture/custom-agent"
mkdir -p "$PI_CODING_AGENT_DIR"
printf '{"mcpServers":{}}\n' > "$PI_CODING_AGENT_DIR/mcp.json"
bash "$installer"
[[ "$PI_CODING_AGENT_DIR/web-search.json" -ef "$repo_root/dotfiles/.agent/apps/pi/web-search.json" ]]
rm "$PI_CODING_AGENT_DIR/mcp-adapter.json"
printf '{"mcpServers":{"personal":{}}}\n' > "$PI_CODING_AGENT_DIR/mcp-adapter.json"
: > "$PI_INSTALL_LOG"
if bash "$installer" > "$fixture/mcp-conflict.log" 2>&1; then
  echo 'FAIL: accepted a personal MCP Adapter config' >&2; exit 1
fi
[[ ! -s "$PI_INSTALL_LOG" ]]
[[ $(cat "$PI_CODING_AGENT_DIR/mcp-adapter.json") == '{"mcpServers":{"personal":{}}}' ]]
rm "$PI_CODING_AGENT_DIR/mcp.json"
if bash "$installer" > "$fixture/mcp-missing.log" 2>&1; then
  echo 'FAIL: accepted missing Pi MCP settings' >&2; exit 1
fi
[[ ! -s "$PI_INSTALL_LOG" ]]
echo 'PASS: Pi extension pins, config conflict protection, repeat install and fail-fast'
