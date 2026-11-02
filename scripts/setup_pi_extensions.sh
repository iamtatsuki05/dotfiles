#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_dir="$repo_root/dotfiles/.agent/apps/pi"
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

while IFS= read -r source || [[ -n "$source" ]]; do
  pi install "$source"
done < "$source_dir/packages.txt"
