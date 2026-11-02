#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
marketplace="$repo_root/dotfiles/.agent/apps/pstack"
agent=all
dry_run=0
while (($#)); do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --agent)
      shift
      case "${1:-}" in
        all|claude|codex|cursor) agent="$1" ;;
        *) echo 'ERROR: --agent requires all, claude, codex or cursor' >&2; exit 1 ;;
      esac
      ;;
    -h|--help) echo 'Usage: bash scripts/setup_pstack.sh [--dry-run] [--agent all|claude|codex|cursor]'; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
  esac
  shift
done
agents=()
for host in claude codex cursor; do
  [[ "$agent" == all || "$agent" == "$host" ]] || continue
  executable="$host"
  [[ "$host" != cursor ]] || executable=cursor-agent
  if command -v "$executable" >/dev/null 2>&1; then
    agents+=("$host")
  elif [[ "$agent" != all ]]; then
    echo "ERROR: $executable is not installed" >&2
    exit 1
  else
    echo "SKIP: $executable is not installed"
  fi
done
((${#agents[@]})) || { echo 'ERROR: no supported agents are installed' >&2; exit 1; }
if ((dry_run)); then
  echo "Pstack marketplace: $marketplace"
  printf 'Register pstack: %s\n' "${agents[@]}"
  echo 'Keep model rules, automations, hook trust and existing skills unchanged.'
  exit 0
fi
for file in .agents/plugins/marketplace.json .claude-plugin/marketplace.json canonical/plugin.json; do
  [[ -f "$marketplace/$file" ]] || { echo "ERROR: missing pstack file: $file" >&2; exit 1; }
done
[[ -d "$marketplace/claude-code" ]] || { echo 'ERROR: missing pstack Claude adapter tree' >&2; exit 1; }
for host in "${agents[@]}"; do
  case "$host" in
    claude)
      claude_homes=("${CLAUDE_CONFIG_DIR:-$HOME/.claude}")
      if [[ -z "${CLAUDE_CONFIG_DIR:-}" ]]; then
        for profile in "${XDG_CONFIG_HOME:-$HOME/.config}"/claude-account/accounts/*; do
          [[ -d "$profile" && ! -L "$profile" && -f "$profile/login-profiles.json" ]] || continue
          claude_homes+=("$profile")
        done
      fi
      for claude_home in "${claude_homes[@]}"; do
        CLAUDE_CONFIG_DIR="$claude_home" claude plugin marketplace add "$marketplace"
        CLAUDE_CONFIG_DIR="$claude_home" claude plugin install pstack@dotfiles-pstack
      done
      ;;
    codex)
      codex plugin marketplace add "$marketplace"
      codex plugin add pstack@dotfiles-pstack
      ;;
    cursor)
      python3 - "$marketplace/canonical" "$HOME/.cursor/plugins/local/pstack" <<'PY'
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

source, target = map(Path, sys.argv[1:])
receipt_name = ".dotfiles-pstack.json"
owner = "dotfiles-pstack"

def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path == root / receipt_name:
            continue
        if path.is_symlink():
            raise SystemExit(f"ERROR: refusing symlink in pstack tree: {path}")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(str(path.stat().st_mode & 0o777).encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()

if (source / receipt_name).exists():
    raise SystemExit("ERROR: pstack source contains a reserved ownership receipt")
source_hash = tree_hash(source)
if target.is_symlink():
    raise SystemExit(f"ERROR: refusing Cursor plugin symlink: {target}")
if target.exists():
    receipt = target / receipt_name
    if not target.is_dir() or not receipt.is_file() or receipt.is_symlink():
        raise SystemExit(f"ERROR: refusing unmanaged Cursor plugin: {target}")
    try:
        previous = json.loads(receipt.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: invalid pstack ownership receipt: {exc}")
    if previous.get("owner") != owner or previous.get("sha256") != tree_hash(target):
        raise SystemExit(f"ERROR: refusing locally modified Cursor plugin: {target}")
    if previous["sha256"] == source_hash:
        print("Cursor pstack is already current")
        sys.exit(0)
target.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix=".pstack-install-", dir=target.parent) as temporary:
    staging = Path(temporary) / "new"
    backup = Path(temporary) / "previous"
    shutil.copytree(source, staging)
    (staging / receipt_name).write_text(json.dumps({"owner": owner, "sha256": source_hash}) + "\n")
    if target.exists():
        target.rename(backup)
    try:
        staging.rename(target)
    except OSError:
        if backup.exists():
            backup.rename(target)
        raise
print(f"Installed Cursor pstack: {target}")
PY
      ;;
  esac
  echo "REGISTERED: pstack for $host"
done
echo 'Restart agents. No model rules, automations or hook trust were configured.'
