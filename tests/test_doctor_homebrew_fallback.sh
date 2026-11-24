#!/usr/bin/env bash
set -euo pipefail

TEST_ZSH_BIN="${DOTFILES_TEST_ZSH_BIN:-/bin/zsh}"
script="$(cd "$(dirname "$0")/../scripts" && pwd)/doctor_homebrew_fallback.sh"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/stubs"
export PATH="$fixture/stubs:/usr/bin:/bin"
unset HOMEBREW_PREFIX
export DOTFILES_HOMEBREW_FALLBACK_CONFIG="$fixture/homebrew-fallback.nix"
export BREW_CALL_LOG="$fixture/brew-calls.log"

cat > "$fixture/stubs/brew" <<'STUB'
#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$BREW_CALL_LOG"
case "$*" in
  "update --quiet") exit 0 ;;
  formulae) printf '%s\n' good-formula new-formula ;;
  casks) printf '%s\n' good-cask new-cask ;;
  tap) printf '%s\n' trusted/tap untrusted/tap ;;
  "info --formula -- old-formula") printf '%s\n' "==> new-formula: stable 1.0" ;;
  "info --cask -- renamed-cask") printf '%s\n' "==> new-cask (New Cask): 1.0" ;;
  "info --cask -- missing-cask")
    printf '%s\n' "Error: Cask 'missing-cask' is unavailable: No Cask with this name exists." >&2
    exit 1
    ;;
  "info --cask -- trusted/tap/qualified-cask") printf '%s\n' "==> qualified-cask (Qualified): 2.0" ;;
  "info --cask -- trusted/tap/old-qualified") printf '%s\n' "==> moved-cask (Moved): 1.0" ;;
  "info --cask -- trusted/tap/moved-cask") printf '%s\n' "==> moved-cask (Moved): 1.0" ;;
  "info --cask -- untrusted/tap/blocked-cask")
    printf '%s\n' "Error: Refusing to load cask untrusted/tap/blocked-cask from untrusted tap untrusted/tap." >&2
    exit 1
    ;;
  "search --cask -- missing-cask") printf '%s\n' "==> Casks" missing-cask-pro ;;
  *) exit 2 ;;
esac
STUB
chmod +x "$fixture/stubs/brew"

write_config() {
  cat > "$DOTFILES_HOMEBREW_FALLBACK_CONFIG" <<'NIX'
{
  taps = [
    "trusted/tap"
  ];

  brews = [
    "good-formula"
    "old-formula"
  ];

  casks = [
    "good-cask"
    "renamed-cask"
    "missing-cask"
    "trusted/tap/qualified-cask"
    "trusted/tap/old-qualified"
    "untrusted/tap/blocked-cask"
    "missing/tap/ghost-cask"
  ];
}
NIX
}

write_config
chmod 644 "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
perm_before="$(ls -l "$DOTFILES_HOMEBREW_FALLBACK_CONFIG" | cut -c1-10)"
if output="$("$TEST_ZSH_BIN" "$script" --no-update 2>&1)"; then exit 1; fi
[[ "$output" == *"RENAMED: old-formula -> new-formula"* ]]
[[ "$output" == *"RENAMED: renamed-cask -> new-cask"* ]]
[[ "$output" == *"RENAMED: trusted/tap/old-qualified -> trusted/tap/moved-cask"* ]]
[[ "$output" == *"MISSING (cask): missing-cask"* ]]
[[ "$output" == *"candidates: missing-cask-pro"* ]]
[[ "$output" == *"SKIPPED: untrusted/tap/blocked-cask"* ]]
[[ "$output" == *"SKIPPED: missing/tap/ghost-cask (tap missing/tap is not tapped locally)"* ]]
[[ "$output" == *"3 ok, 3 renamed, 1 missing, 2 skipped"* ]]
! grep -q '^update' "$BREW_CALL_LOG"

if output="$("$TEST_ZSH_BIN" "$script" --no-update --fix 2>&1)"; then exit 1; fi
[[ "$output" == *"FIXED: renamed old-formula -> new-formula"* ]]
[[ "$output" == *"FIXED: renamed renamed-cask -> new-cask"* ]]
[[ "$output" == *"FIXED: renamed trusted/tap/old-qualified -> trusted/tap/moved-cask"* ]]
grep -q '"new-formula"' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
grep -q '"new-cask"' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
grep -q '"trusted/tap/moved-cask"' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
! grep -q '"old-formula"' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
! grep -q '"renamed-cask"' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
! grep -q '"trusted/tap/old-qualified"' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
grep -q '"missing-cask"' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
[[ "$(ls -l "$DOTFILES_HOMEBREW_FALLBACK_CONFIG" | cut -c1-10)" == "$perm_before" ]]

sed -i.bak '/"missing-cask"/d' "$DOTFILES_HOMEBREW_FALLBACK_CONFIG"
output="$("$TEST_ZSH_BIN" "$script" 2>&1)"
[[ "$output" == *"6 ok, 0 renamed, 0 missing, 2 skipped"* ]]
grep -q '^update --quiet$' "$BREW_CALL_LOG"

printf '%s\n' "unit:doctor-homebrew-fallback"
