#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="$(mktemp -d)"
fixture="$(cd "$fixture" && pwd -P)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/repo/scripts" "$fixture/home with spaces" "$fixture/bin"
setup="$repo_root/scripts/setup_pstack.sh"
[[ -f "$setup" ]] || { echo 'FAIL: pstack setup entrypoint is missing' >&2; exit 1; }
cp "$setup" "$fixture/repo/scripts/setup_pstack.sh"
market="$fixture/repo/dotfiles/.agent/apps/pstack"
mkdir -p "$market/canonical/skills/example" "$market/claude-code" "$market/.agents/plugins" "$market/.claude-plugin"
printf '{"name":"pstack","version":"fixture"}\n' > "$market/canonical/plugin.json"
printf 'original\n' > "$market/canonical/skills/example/SKILL.md"
printf '{}\n' > "$market/.agents/plugins/marketplace.json"
printf '{}\n' > "$market/.claude-plugin/marketplace.json"
for agent in claude codex cursor-agent; do
  cat > "$fixture/bin/$agent" <<'CLI'
#!/usr/bin/env bash
set -eu
printf '%s|%s|%s\n' "$(basename "$0")" "${CLAUDE_CONFIG_DIR:-default}" "$*" >> "$EVENT_LOG"
[[ "${FAIL_AGENT:-}" != "$(basename "$0")" ]] || exit 73
CLI
  chmod +x "$fixture/bin/$agent"
done
run_setup() {
  env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME="$fixture/home with spaces" \
    XDG_CONFIG_HOME="$fixture/home with spaces/.config" PATH="$fixture/bin:$PATH" \
    EVENT_LOG="$fixture/events" bash "$fixture/repo/scripts/setup_pstack.sh" "$@" > "$fixture/output" 2>&1
}
fail() { cat "$fixture/output" >&2; echo "FAIL: $*" >&2; exit 1; }
run_setup --dry-run || fail 'dry run failed'
[[ ! -e "$fixture/events" && -z "$(ls -A "$fixture/home with spaces")" ]] || fail 'dry run wrote files'
if run_setup --agent invalid; then fail 'invalid agent accepted'; fi
if run_setup --agent; then fail 'missing agent accepted'; fi
cursor_home="$fixture/home with spaces/.cursor/plugins/local/pstack"
mkdir -p "$cursor_home"
printf 'user data\n' > "$cursor_home/keep"
if run_setup --agent cursor; then fail 'foreign Cursor plugin overwritten'; fi
[[ "$(cat "$cursor_home/keep")" == 'user data' ]] || fail 'foreign plugin changed'
rm -rf "$cursor_home"
mkdir -p "$fixture/existing-plugin"
printf 'keep\n' > "$fixture/existing-plugin/keep"
ln -s "$fixture/existing-plugin" "$cursor_home"
if run_setup --agent cursor; then fail 'Cursor plugin symlink accepted'; fi
[[ -L "$cursor_home" && "$(cat "$fixture/existing-plugin/keep")" == keep ]] || fail 'symlink target changed'
rm "$cursor_home"
run_setup --agent cursor || fail 'Cursor installation failed'
[[ -f "$cursor_home/.dotfiles-pstack.json" ]] || fail 'ownership receipt missing'
run_setup --agent cursor || fail 'repeat Cursor install failed'
[[ "$(cat "$cursor_home/skills/example/SKILL.md")" == original ]] || fail 'payload incorrect'
printf 'user edit\n' > "$cursor_home/skills/example/SKILL.md"
if run_setup --agent cursor; then fail 'modified Cursor plugin overwritten'; fi
[[ "$(cat "$cursor_home/skills/example/SKILL.md")" == 'user edit' ]] || fail 'modified plugin lost'
printf 'original\n' > "$cursor_home/skills/example/SKILL.md"
printf 'updated\n' > "$market/canonical/skills/example/SKILL.md"
run_setup --agent cursor || fail 'managed Cursor update failed'
[[ "$(cat "$cursor_home/skills/example/SKILL.md")" == updated ]] || fail 'managed update missing'
for profile in first second; do
  mkdir -p "$fixture/home with spaces/.config/claude-account/accounts/$profile"
  printf '{}\n' > "$fixture/home with spaces/.config/claude-account/accounts/$profile/login-profiles.json"
done
mkdir -p "$fixture/home with spaces/.claude"
printf '{"existing":true}\n' > "$fixture/home with spaces/.claude/settings.json"
run_setup --agent claude || fail 'Claude install failed'
[[ "$(cat "$fixture/home with spaces/.claude/settings.json")" == '{"existing":true}' ]] || fail 'existing Claude settings rewritten'
python3 - "$fixture/events" "$market" <<'PY'
import sys
rows = open(sys.argv[1]).read().splitlines()
assert len(rows) == 6, rows
assert all('plugin marketplace add ' + sys.argv[2] in r or r.endswith('plugin install pstack@dotfiles-pstack') for r in rows)
assert sum('/accounts/first|' in r for r in rows) == 2
assert sum('/accounts/second|' in r for r in rows) == 2
PY
: > "$fixture/events"
env CLAUDE_CONFIG_DIR="$fixture/specific" HOME="$fixture/home with spaces" PATH="$fixture/bin:$PATH" EVENT_LOG="$fixture/events" \
  bash "$fixture/repo/scripts/setup_pstack.sh" --agent claude > "$fixture/output" 2>&1 || fail 'explicit Claude home failed'
[[ "$(wc -l < "$fixture/events" | tr -d ' ')" == 2 ]] || fail 'explicit Claude home installed other profiles'
: > "$fixture/events"
run_setup --agent codex || fail 'Codex install failed'
[[ "$(wc -l < "$fixture/events" | tr -d ' ')" == 2 ]] || fail 'Codex registration mismatch'
if FAIL_AGENT=codex run_setup --agent codex; then fail 'native CLI failure hidden'; fi
echo 'PASS: pstack dry-run, host registration, profile scope and Cursor ownership/update protection'
