#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="$(mktemp -d)"
fixture="$(cd "$fixture" && pwd -P)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/repo/scripts" "$fixture/repo/dotfiles/.agent" "$fixture/bin"
[[ -f "$repo_root/scripts/setup_hallmark.sh" ]] || { echo 'FAIL: Hallmark setup is missing' >&2; exit 1; }
cp "$repo_root/scripts/setup_hallmark.sh" "$fixture/repo/scripts/"
printf '{}\n' > "$fixture/repo/dotfiles/.agent/package.json"
printf '{}\n' > "$fixture/repo/dotfiles/.agent/package-lock.json"
cat > "$fixture/bin/npm" <<'NPM'
#!/usr/bin/env bash
set -eu
[[ "$*" == "ci --ignore-scripts --engine-strict --no-audit --no-fund --prefix $FIXTURE_ROOT/repo/dotfiles/.agent" ]] || exit 91
[[ "${FIXTURE_NPM_FAIL:-0}" == 0 ]] || exit 42
mkdir -p "$FIXTURE_ROOT/repo/dotfiles/.agent/node_modules"
touch "$FIXTURE_ROOT/repo/dotfiles/.agent/node_modules/installed"
NPM
chmod +x "$fixture/bin/npm"
run_setup() {
  FIXTURE_ROOT="$fixture" PATH="$fixture/bin:$PATH" bash "$fixture/repo/scripts/setup_hallmark.sh" "$@" > "$fixture/output" 2>&1
}
fail() { cat "$fixture/output" >&2; echo "FAIL: $*" >&2; exit 1; }
run_setup --dry-run || fail 'dry-run failed'
[[ ! -e "$fixture/repo/dotfiles/.agent/node_modules" ]] || fail 'dry-run installed dependencies'
if run_setup --invalid; then fail 'invalid option accepted'; fi
run_setup || fail 'installation failed'
[[ -f "$fixture/repo/dotfiles/.agent/node_modules/installed" ]] || fail 'dependency destination is wrong'
if FIXTURE_NPM_FAIL=1 run_setup; then fail 'npm failure hidden'; fi
rm "$fixture/repo/dotfiles/.agent/package-lock.json"
if run_setup; then fail 'missing lockfile accepted'; fi
echo 'Hallmark dependency setup tests passed'
