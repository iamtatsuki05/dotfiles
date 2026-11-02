#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/home/.pi/agent" "$fixture/bin"
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
bash "$installer"
[[ "$target" -ef "$repo_root/dotfiles/.agent/apps/pi/web-search.json" ]]
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
bash "$installer"
[[ "$PI_CODING_AGENT_DIR/web-search.json" -ef "$repo_root/dotfiles/.agent/apps/pi/web-search.json" ]]
echo 'PASS: Pi extension pins, config conflict protection, repeat install and fail-fast'
