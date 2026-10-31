#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/home" "$fixture/bin"
printf '#!/bin/sh\nexit 0\n' > "$fixture/bin/opencode"
chmod +x "$fixture/bin/opencode"
setup="$repo_root/scripts/setup_tokeneconomizer.sh"
[[ -f "$setup" ]] || { echo 'FAIL: TokenEconomizer setup entrypoint is missing' >&2; exit 1; }
run_setup() {
  env -u CLAUDE_CONFIG_DIR HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/.config" \
    PATH="$fixture/bin:$PATH" bash "$setup" "$@" > "$fixture/output" 2>&1
}
fail() { cat "$fixture/output" >&2; echo "FAIL: $*" >&2; exit 1; }
run_setup --dry-run || fail 'dry run failed'
[[ -z "$(ls -A "$fixture/home")" ]] || fail 'dry run wrote home files'
if run_setup --agent invalid; then fail 'invalid agent accepted'; fi
[[ -z "$(ls -A "$fixture/home")" ]] || fail 'invalid arguments wrote home files'
# Existing non-checkouts and modified checkouts must never be overwritten.
mkdir -p "$fixture/home/.local/share/dotfiles/tokeneconomizer"
printf 'user data\n' > "$fixture/home/.local/share/dotfiles/tokeneconomizer/keep"
if run_setup --agent opencode; then fail 'non-checkout overwritten'; fi
[[ "$(cat "$fixture/home/.local/share/dotfiles/tokeneconomizer/keep")" == 'user data' ]] || fail 'user data lost'
# A fake git transport models downloads; the setup's validation and config merge stay real.
cat > "$fixture/bin/git" <<'GIT'
#!/usr/bin/env bash
set -eu
if [[ "$1" == '-C' ]]; then cd "$2"; shift 2; fi
case "$*" in
  'config --get remote.origin.url') echo https://github.com/microsoft/TokenEconomizer.git ;;
  'status --porcelain') [[ ! -f dirty ]] || echo ' M keep' ;;
  'rev-parse HEAD') cat revision ;;
  'cat-file -e '*) exit 0 ;;
  'checkout --detach '*) printf '%s\n' "${@: -1}" > revision ;;
  *) echo "unexpected git call: $*" >&2; exit 91 ;;
esac
GIT
chmod +x "$fixture/bin/git"
target="$fixture/home/.local/share/dotfiles/tokeneconomizer"
mkdir -p "$target/.git" "$target/plugin/opencode"
touch "$target/plugin/opencode/tokeneconomizer.ts" "$target/dirty"
printf 'old revision\n' > "$target/revision"
if run_setup --agent opencode; then fail 'dirty checkout accepted'; fi
[[ "$(cat "$target/revision")" == 'old revision' ]] || fail 'dirty checkout changed'
rm "$target/dirty"
mkdir -p "$fixture/home/.config/opencode"
printf '{"model":"existing-model","plugin":["existing-plugin"]}\n' > "$fixture/source.json"
ln -s "$fixture/source.json" "$fixture/home/.config/opencode/opencode.json"
run_setup --agent opencode || fail 'install failed'
run_setup --agent opencode || fail 'repeat install failed'
[[ -L "$fixture/home/.config/opencode/opencode.json" ]] || fail 'config symlink replaced'
python3 - "$fixture/source.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
assert value['model'] == 'existing-model'
assert value['plugin'] == ['existing-plugin', 'file://{env:HOME}/.local/share/dotfiles/tokeneconomizer/plugin/opencode/tokeneconomizer.ts']
PY
# Claude's normal entrypoint uses isolated account profiles with separate plugin registries.
cat > "$fixture/bin/claude" <<'CLAUDE'
#!/usr/bin/env bash
set -eu
config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$config_dir"
case "$*" in
  'plugin marketplace add '*)
    python3 - "$config_dir/settings.json" "${@: -1}" <<'PYCONFIG'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
v = json.loads(p.read_text()) if p.exists() else {'existingSetting': True}
v.setdefault('extraKnownMarketplaces', {})['tokeneconomizer'] = {'source': {'source': 'directory', 'path': sys.argv[2]}}
p.write_text(json.dumps(v))
PYCONFIG
    ;;
  'plugin install tokeneconomizer@tokeneconomizer') touch "$config_dir/installed" ;;
  *) exit 91 ;;
esac
CLAUDE
chmod +x "$fixture/bin/claude"
for profile in first second; do
  mkdir -p "$fixture/home/.config/claude-account/accounts/$profile"
  printf "{}\n" > "$fixture/home/.config/claude-account/accounts/$profile/login-profiles.json"
done
run_setup --agent claude || fail 'Claude profile install failed'
for profile_home in "$fixture/home/.claude" "$fixture/home/.config/claude-account/accounts/first" "$fixture/home/.config/claude-account/accounts/second"; do
  [[ -f "$profile_home/installed" ]] || fail 'Claude account registry was not installed'
  python3 - "$profile_home/settings.json" <<'PYCONFIG'
import json, sys
v = json.load(open(sys.argv[1]))
assert v['existingSetting'] is True
assert v['extraKnownMarketplaces']['tokeneconomizer']['source']['path'] == '~/.local/share/dotfiles/tokeneconomizer'
PYCONFIG
done
printf '{invalid json\n' > "$fixture/source.json"
if run_setup --agent opencode; then fail 'invalid config overwritten'; fi
[[ "$(cat "$fixture/source.json")" == '{invalid json' ]] || fail 'invalid config changed'
echo 'PASS: TokenEconomizer dry-run, checkout protection, config preservation and repeat install'
