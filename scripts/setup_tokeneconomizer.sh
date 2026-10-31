#!/usr/bin/env bash
set -euo pipefail

readonly repository='https://github.com/microsoft/TokenEconomizer.git'
readonly revision='e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9'
readonly checkout="$HOME/.local/share/dotfiles/tokeneconomizer"
agent=all
dry_run=0
while (($#)); do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --agent)
      shift
      case "${1:-}" in claude|codex|copilot|pi|opencode) agent="$1" ;;
        *) echo 'ERROR: --agent requires claude, codex, copilot, pi or opencode' >&2; exit 1 ;;
      esac ;;
    -h|--help)
      echo 'Usage: bash scripts/setup_tokeneconomizer.sh [--dry-run] [--agent claude|codex|copilot|pi|opencode]'
      exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
  esac
  shift
done

agents=()
for name in claude codex copilot pi opencode; do
  [[ "$agent" == all || "$agent" == "$name" ]] || continue
  if command -v "$name" >/dev/null 2>&1; then
    agents+=("$name")
  elif [[ "$agent" != all ]]; then
    echo "ERROR: $name is not installed; install the mise tool first" >&2
    exit 1
  else
    echo "SKIP: $name is not installed"
  fi
done
((${#agents[@]})) || { echo 'ERROR: no supported agents are installed' >&2; exit 1; }
if ((dry_run)); then
  echo "Checkout: $repository @ $revision -> $checkout"
  printf 'Register TokenEconomizer: %s\n' "${agents[@]}"
  echo 'Keep upstream defaults; do not change hook trust or permission settings.'
  exit 0
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
if [[ -e "$checkout" || -L "$checkout" ]]; then
  [[ ! -L "$checkout" && -d "$checkout/.git" ]] || {
    echo "ERROR: refusing to replace non-checkout: $checkout" >&2; exit 1;
  }
  [[ "$(git -C "$checkout" config --get remote.origin.url)" == "$repository" ]] || {
    echo "ERROR: unexpected origin: $checkout" >&2; exit 1;
  }
  [[ -z "$(git -C "$checkout" status --porcelain)" ]] || {
    echo "ERROR: checkout has local changes: $checkout" >&2; exit 1;
  }
else
  mkdir -p "$(dirname "$checkout")"
  git init -q "$checkout"
  git -C "$checkout" remote add origin "$repository"
fi
if [[ "$(git -C "$checkout" rev-parse HEAD 2>/dev/null || true)" != "$revision" ]]; then
  if ! git -C "$checkout" cat-file -e "$revision^{commit}" 2>/dev/null; then
    git -C "$checkout" fetch --depth 1 origin "$revision"
  fi
  git -C "$checkout" checkout --detach "$revision"
fi

normalize_marketplace_path() {
  # Native registration writes an absolute path, including through managed symlinks.
  python3 - "$1" "$checkout" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
config = json.loads(path.read_text())
source = config['extraKnownMarketplaces']['tokeneconomizer']['source']
if source['path'] == sys.argv[2]:
    source['path'] = '~/.local/share/dotfiles/tokeneconomizer'
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
}

for name in "${agents[@]}"; do
  echo "Installing TokenEconomizer for $name"
  case "$name" in
    claude)
      claude_homes=("${CLAUDE_CONFIG_DIR:-$HOME/.claude}")
      if [[ -z "${CLAUDE_CONFIG_DIR:-}" ]]; then
        for profile_home in "${XDG_CONFIG_HOME:-$HOME/.config}"/claude-account/accounts/*; do
          [[ -d "$profile_home" && ! -L "$profile_home" && -f "$profile_home/login-profiles.json" ]] || continue
          claude_homes+=("$profile_home")
        done
      fi
      for claude_home in "${claude_homes[@]}"; do
        CLAUDE_CONFIG_DIR="$claude_home" claude plugin marketplace add "$checkout"
        CLAUDE_CONFIG_DIR="$claude_home" claude plugin install tokeneconomizer@tokeneconomizer
        normalize_marketplace_path "$claude_home/settings.json"
      done
      ;;
    codex)
      # A Git source keeps the canonical config portable across machines.
      mkdir -p "${CODEX_HOME:-$HOME/.codex}"
      codex plugin marketplace add "$repository" --ref "$revision"
      codex plugin add tokeneconomizer@tokeneconomizer
      ;;
    copilot)
      marketplace_source=$(copilot plugin marketplace list --json | python3 -c '
import json, sys
entries = [x for x in json.load(sys.stdin) if x["name"] == "tokeneconomizer"]
print(entries[0]["source"] if entries else "")')
      if [[ -z "$marketplace_source" ]]; then
        copilot plugin marketplace add "$checkout"
      elif [[ "$marketplace_source" != "Local: $checkout" ]]; then
        echo "ERROR: tokeneconomizer marketplace already uses $marketplace_source" >&2
        exit 1
      fi
      copilot plugin install tokeneconomizer@tokeneconomizer
      ;;
    pi) pi install "$checkout" ;;
    opencode)
      python3 - "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
config = json.loads(path.read_text()) if path.exists() else {}
plugins = config.setdefault('plugin', [])
if not isinstance(plugins, list):
    raise SystemExit('ERROR: opencode plugin must be an array')
entry = 'file://{env:HOME}/.local/share/dotfiles/tokeneconomizer/plugin/opencode/tokeneconomizer.ts'
if entry not in plugins:
    plugins.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
      ;;
  esac
  if [[ "$name" == copilot ]]; then
    normalize_marketplace_path "${COPILOT_HOME:-$HOME/.copilot}/settings.json"
  fi
  echo "REGISTERED: $name"
done
echo 'Restart agents. Review new Codex hooks in /hooks; installation does not grant hook trust.'
echo "Tool catalog setup (Claude/Copilot): $checkout/plugin/scripts/te --host <claude|copilot> toolprune refresh-catalog"
