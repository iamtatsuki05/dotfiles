#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$DEFAULT_REPO_ROOT"
AGENT_DIR=""
APPS_DIR=""
readonly SECRETS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env"

usage() {
  cat <<EOF
Usage:
  zsh scripts/setup_agent_files.sh [--repo-root PATH]

Options:
  --repo-root PATH  Override repository root. Intended for tests.
  -h, --help        Show this help.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --repo-root)
        shift
        if ((! $#)); then
          echo "ERROR: --repo-root requires a value" >&2
          return 1
        fi
        REPO_ROOT="$1"
        ;;
      --repo-root=*)
        REPO_ROOT="${1#--repo-root=}"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        usage >&2
        return 1
        ;;
    esac
    shift
  done

  REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
  AGENT_DIR="$REPO_ROOT/dotfiles/.agent"
  APPS_DIR="$AGENT_DIR/apps"
}

link_symlink() {
  local src="$1"
  local dst="$2"

  if [ -L "$dst" ]; then
    local current
    current="$(readlink "$dst")"
    if [ "$current" = "$src" ]; then
      return 0
    fi
    rm -f "$dst"
  elif [ -e "$dst" ]; then
    if [ -d "$dst" ] && [ -z "$(ls -A "$dst")" ]; then
      rmdir "$dst"
    elif [ -f "$dst" ]; then
      rm -f "$dst"
    else
      echo "skip: $dst exists and is not a regular file or symlink" >&2
      return 0
    fi
  fi

  ln -s "$src" "$dst"
}

ensure_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
  fi
}

sync_shared_files() {
  ensure_dir ~/.codex
  ensure_dir ~/.claude
  ensure_dir ~/.gemini
  ensure_dir ~/.cursor

  link_symlink "$AGENT_DIR/AGENTS.md" ~/.codex/AGENTS.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.claude/CLAUDE.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.gemini/GEMINI.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.cursor/AGENT.md

  link_symlink "$AGENT_DIR/skills" ~/.codex/skills
  link_symlink "$AGENT_DIR/skills" ~/.claude/skills
  link_symlink "$AGENT_DIR/skills" ~/.gemini/skills
  link_symlink "$AGENT_DIR/skills" ~/.cursor/skills
}

sync_hooks() {
  local hooks_dir
  local hook_file

  for hooks_dir in ~/.claude/hooks ~/.gemini/hooks ~/.codex/hooks; do
    ensure_dir "$hooks_dir"
    for hook_file in "$AGENT_DIR/hooks"/*; do
      [ -f "$hook_file" ] || continue
      chmod +x "$hook_file"
      link_symlink "$hook_file" "$hooks_dir/$(basename "$hook_file")"
    done
  done
}

sync_tool_configs() {
  link_symlink "$APPS_DIR/claude/settings.json" ~/.claude/settings.json
  link_symlink "$APPS_DIR/claude/.mcp.json" ~/.claude/.mcp.json
  link_symlink "$APPS_DIR/codex/config.toml" ~/.codex/config.toml
  link_symlink "$APPS_DIR/codex/hooks.json" ~/.codex/hooks.json
  link_symlink "$APPS_DIR/gemini/settings.json" ~/.gemini/settings.json
  link_symlink "$APPS_DIR/gemini/ignore" ~/.gemini/ignore
  link_symlink "$APPS_DIR/cursor/mcp.json" ~/.cursor/mcp.json
}

sync_gemini_env() {
  local gemini_env_file="$HOME/.gemini/.env"

  if [[ ! -f "$SECRETS_FILE" ]]; then
    return 0
  fi

  ensure_dir "$(dirname "$gemini_env_file")"

  local vars=("DEVIN_API_KEY")
  local tmp
  tmp="$(mktemp)"

  local var
  for var in "${vars[@]}"; do
    local line
    line=$(grep -E "^(export )?${var}=" "$SECRETS_FILE" 2>/dev/null | tail -1 | sed 's/^export //')
    if [[ -n "$line" ]]; then
      echo "$line" >> "$tmp"
    fi
  done

  if [[ ! -s "$tmp" ]]; then
    rm -f "$tmp"
    return 0
  fi

  mv "$tmp" "$gemini_env_file"
  chmod 600 "$gemini_env_file"
}

main() {
  parse_args "$@"
  sync_shared_files
  sync_hooks
  sync_tool_configs
  sync_gemini_env
}

main "$@"
