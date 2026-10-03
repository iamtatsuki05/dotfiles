#!/usr/bin/env zsh
set -euo pipefail
readonly REPO_ROOT="${0:A:h:h}"
source "$REPO_ROOT/tests/lib/assertions.sh"
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
mkdir -p "$fixture/repo/scripts/lib" "$fixture/repo/home" "$fixture/home" "$fixture/bin"
cp "$REPO_ROOT/main.sh" "$fixture/repo/"
cp "$REPO_ROOT/scripts/lib/"*.sh "$fixture/repo/scripts/lib/"
cp "$REPO_ROOT/home/.chezmoidata.toml" "$fixture/repo/home/"
for script in chezmoi_apply setup_agent_files; do
  print -r -- "print -r -- \"$script \$*\" >> \"\$EVENT_LOG\"" > "$fixture/repo/scripts/$script.sh"
done
for executable in sudo nix brew mise curl softwareupdate; do
  cat > "$fixture/bin/$executable" <<'GUARD'
#!/bin/sh
printf 'unexpected external command: %s\n' "$0" >> "$EVENT_LOG"
exit 99
GUARD
  chmod +x "$fixture/bin/$executable"
done
run_main() {
  : > "$fixture/events"
  HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/.config" EVENT_LOG="$fixture/events" \
    __ETC_ZSHENV_SOURCED=1 PATH="$fixture/bin:/usr/bin:/bin:/usr/sbin:/sbin" /bin/zsh "$fixture/repo/main.sh" "$@" > "$fixture/output" 2>&1
}
run_main --only config --profile cli || { cat "$fixture/output"; fail 'config selection failed'; }
assert_contains "$fixture/events" 'chezmoi_apply --profile cli --mark-default --no-install'
assert_not_contains "$fixture/events" 'setup_agent_files'
run_main --only agent --profile cli
assert_contains "$fixture/events" 'setup_agent_files'
assert_not_contains "$fixture/events" 'chezmoi_apply'
assert_not_contains "$fixture/events" '--install-deps'
run_main --only=config,agent --profile cli --dry-run
assert_contains "$fixture/events" 'chezmoi_apply --profile cli --mark-default --no-install --dry-run'
assert_contains "$fixture/events" 'setup_agent_files --dry-run'
for selection in '' bogus config, ,agent config,config; do
  if run_main --only "$selection"; then fail "accepted invalid selection: $selection"; fi
  [[ ! -s "$fixture/events" ]] || fail 'invalid arguments caused actions'
done
if run_main --dry-run; then fail 'unscoped dry-run accepted'; fi
if run_main --only agent --skip-mas-apps; then fail 'irrelevant install option accepted'; fi

# A missing chezmoi must not trigger mise exec/install in settings-only mode.
cp "$REPO_ROOT/scripts/chezmoi_apply.sh" "$fixture/repo/scripts/"
print home > "$fixture/repo/.chezmoiroot"
cat > "$fixture/bin/mise" <<'MISE'
#!/bin/sh
printf '%s\n' "$*" >> "$EVENT_LOG"
exit 1
MISE
chmod +x "$fixture/bin/mise"
: > "$fixture/events"
if HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/.config" EVENT_LOG="$fixture/events" \
  __ETC_ZSHENV_SOURCED=1 PATH="$fixture/bin:/usr/bin:/bin" /bin/zsh "$fixture/repo/main.sh" --only config --profile cli > "$fixture/output" 2>&1; then
  fail 'missing chezmoi was accepted'
fi
assert_contains "$fixture/events" 'where chezmoi@latest'
assert_not_contains "$fixture/events" 'exec '
assert_not_contains "$fixture/events" 'install '
assert_contains "$fixture/output" 'chezmoi is not installed'
[[ ! -e "$fixture/home/.config/dotfiles/manager" ]] || fail 'failed config apply wrote marker'

# Exercise real sync with copied sources so chmod cannot affect the checkout.
mkdir -p "$fixture/repo/dotfiles/.agent/skills"
cp -R "$REPO_ROOT/dotfiles/.agent/apps" "$REPO_ROOT/dotfiles/.agent/hooks" "$fixture/repo/dotfiles/.agent/"
cp "$REPO_ROOT/dotfiles/.agent/AGENTS.md" "$REPO_ROOT/dotfiles/.agent/sync.sh" "$fixture/repo/dotfiles/.agent/"
cp "$REPO_ROOT/scripts/setup_agent_files.sh" "$REPO_ROOT/scripts/agent-run-compact" "$fixture/repo/scripts/"
chmod -x "$fixture/repo/dotfiles/.agent/hooks/jupytext_sync.sh"

mkdir -p "$fixture/home/.config/shell"
print -r -- 'DEVIN_API_KEY=fixture-secret' > "$fixture/home/.config/shell/secrets.env"
HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/.config" /bin/zsh "$fixture/repo/dotfiles/.agent/sync.sh" --dry-run > "$fixture/plan"
[[ ! -x "$fixture/repo/dotfiles/.agent/hooks/jupytext_sync.sh" ]] || fail "dry-run changed source permissions"
[[ ! -e "$fixture/home/.codex" ]] || fail 'dry-run created agent home'
assert_not_contains "$fixture/plan" 'fixture-secret'
assert_contains "$fixture/plan" '.codex/config.toml'
HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/.config" /bin/zsh "$fixture/repo/dotfiles/.agent/sync.sh" > "$fixture/apply"
[[ "$fixture/home/.codex/config.toml" -ef "$fixture/repo/dotfiles/.agent/apps/codex/config.toml" ]] || fail 'agent config not linked'
[[ -L "$fixture/home/.codex/skills" ]] || fail 'skills not linked'
[[ -L "$fixture/home/.codex/hooks/jupytext_sync.sh" ]] || fail 'hooks not linked'
assert_contains "$fixture/home/.hermes/.env" 'DEVIN_API_KEY=fixture-secret'
cp "$fixture/home/.hermes/.env" "$fixture/env-before"
HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/.config" /bin/zsh "$fixture/repo/dotfiles/.agent/sync.sh" --dry-run > "$fixture/plan"
cmp "$fixture/env-before" "$fixture/home/.hermes/.env" || fail 'dry-run rewrote env'
[[ -L "$fixture/home/.codex/config.toml" ]] || fail 'dry-run removed existing link'
print 'selective setup tests passed'
