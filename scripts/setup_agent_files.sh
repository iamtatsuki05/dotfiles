#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
DEFAULT_REPO_ROOT="${SCRIPT_DIR:h}"
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

  REPO_ROOT="${REPO_ROOT:A}"
  AGENT_DIR="$REPO_ROOT/dotfiles/.agent"
  APPS_DIR="$AGENT_DIR/apps"
}

link_symlink() {
  local src="$1"
  local dst="$2"

  if [ -L "$dst" ]; then
    if [[ "$dst" -ef "$src" ]]; then
      return 0
    fi
    rm -f "$dst"
  elif [ -e "$dst" ]; then
    if [ -d "$dst" ]; then
      if rmdir "$dst" 2>/dev/null; then
        :
      else
        echo "skip: $dst exists and is not an empty directory, regular file, or symlink" >&2
        return 0
      fi
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
  ensure_dir ~/.copilot
  ensure_dir "${XDG_CONFIG_HOME:-$HOME/.config}/devin"
  ensure_dir ~/.gemini
  ensure_dir ~/.cursor
  ensure_dir "${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
  ensure_dir ~/.hermes
  ensure_dir ~/.openclaw
  ensure_dir ~/.openclaw/workspace

  link_symlink "$AGENT_DIR/AGENTS.md" ~/.codex/AGENTS.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.claude/CLAUDE.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.copilot/copilot-instructions.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.gemini/GEMINI.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.cursor/AGENT.md
  link_symlink "$AGENT_DIR/AGENTS.md" "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/AGENTS.md"
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.hermes/AGENTS.md
  link_symlink "$AGENT_DIR/AGENTS.md" ~/.openclaw/workspace/AGENTS.md

  link_symlink "$AGENT_DIR/skills" ~/.codex/skills
  link_symlink "$AGENT_DIR/skills" ~/.claude/skills
  link_symlink "$AGENT_DIR/skills" ~/.copilot/skills
  link_symlink "$AGENT_DIR/skills" "${XDG_CONFIG_HOME:-$HOME/.config}/devin/skills"
  link_symlink "$AGENT_DIR/skills" ~/.gemini/skills
  link_symlink "$AGENT_DIR/skills" ~/.cursor/skills
  link_symlink "$AGENT_DIR/skills" "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills"
  link_symlink "$AGENT_DIR/skills" ~/.hermes/skills
  link_symlink "$AGENT_DIR/skills" ~/.openclaw/workspace/skills

  link_symlink "$AGENT_DIR/pets" ~/.codex/pets
}

sync_hooks() {
  local hooks_dir
  local hook_file
  local hook_name

  for hooks_dir in ~/.claude/hooks ~/.gemini/hooks ~/.codex/hooks ~/.copilot/hooks "${XDG_CONFIG_HOME:-$HOME/.config}/devin/hooks" "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/hooks" ~/.hermes/agent-hooks; do
    ensure_dir "$hooks_dir"
    for hook_file in "$AGENT_DIR/hooks"/*; do
      [ -f "$hook_file" ] || continue
      hook_name="${hook_file:t}"
      chmod +x "$hook_file"
      link_symlink "$hook_file" "$hooks_dir/$hook_name"
    done
  done

  ensure_dir ~/.hermes/agent-hooks
  for hook_file in "$APPS_DIR/hermes-agent/agent-hooks"/*; do
    [ -f "$hook_file" ] || continue
    hook_name="${hook_file:t}"
    chmod +x "$hook_file"
    link_symlink "$hook_file" "$HOME/.hermes/agent-hooks/$hook_name"
  done
}

sync_tool_configs() {
  link_symlink "$APPS_DIR/claude/settings.json" ~/.claude/settings.json
  link_symlink "$APPS_DIR/claude/.mcp.json" ~/.claude/.mcp.json
  link_symlink "$APPS_DIR/copilot/settings.json" ~/.copilot/settings.json
  link_symlink "$APPS_DIR/copilot/mcp-config.json" ~/.copilot/mcp-config.json
  link_symlink "$APPS_DIR/codex/config.toml" ~/.codex/config.toml
  link_symlink "$APPS_DIR/codex/hooks.json" ~/.codex/hooks.json
  link_symlink "$APPS_DIR/devin/config.json" "${XDG_CONFIG_HOME:-$HOME/.config}/devin/config.json"
  link_symlink "$APPS_DIR/gemini/settings.json" ~/.gemini/settings.json
  link_symlink "$APPS_DIR/gemini/ignore" ~/.gemini/ignore
  link_symlink "$APPS_DIR/cursor/cli-config.json" ~/.cursor/cli-config.json
  link_symlink "$APPS_DIR/cursor/mcp.json" ~/.cursor/mcp.json
  link_symlink "$APPS_DIR/opencode/opencode.json" "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
  link_symlink "$APPS_DIR/opencode/plugins" "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins"
  link_symlink "$APPS_DIR/hermes-agent/config.yaml" ~/.hermes/config.yaml
  link_symlink "$APPS_DIR/openclaw/openclaw.json" ~/.openclaw/openclaw.json
}

write_env_file_from_secrets() {
  local env_file="$1"
  shift
  if [[ ! -f "$SECRETS_FILE" ]]; then
    return 0
  fi

  ensure_dir "${env_file:h}"

  local tmp
  tmp="${env_file}.tmp.$$"
  rm -f "$tmp"
  : > "$tmp"

  local var
  local source_var
  local target_var
  for var in "$@"; do
    source_var="${var%%:*}"
    target_var="${var##*:}"
    local line=""
    local secret_line
    while IFS= read -r secret_line; do
      case "$secret_line" in
        (${source_var}=*|export\ ${source_var}=*)
          line="${secret_line#export }"
          ;;
      esac
    done < "$SECRETS_FILE"
    if [[ -n "$line" ]]; then
      if [[ "$source_var" != "$target_var" ]]; then
        line="${target_var}=${line#*=}"
      fi
      print -r -- "$line" >> "$tmp"
    fi
  done

  if [[ ! -s "$tmp" ]]; then
    rm -f "$tmp"
    return 0
  fi

  mv "$tmp" "$env_file"
  chmod 600 "$env_file"
}

sync_agent_env_files() {
  write_env_file_from_secrets "$HOME/.gemini/.env" DEVIN_API_KEY
  write_env_file_from_secrets "$HOME/.hermes/.env" DEVIN_API_KEY OPENCODE_API_KEY OPENCODE_API_KEY:OPENCODE_GO_API_KEY
  write_env_file_from_secrets "$HOME/.openclaw/.env" DEVIN_API_KEY OPENCODE_API_KEY
}

sync_hermes_mcp_dependency() {
  local hermes_bin
  local hermes_install_dir
  local hermes_python

  if ! command -v hermes >/dev/null 2>&1; then
    return 0
  fi

  hermes_bin="$(command -v hermes)"
  hermes_install_dir="${hermes_bin:h:h}"
  case "$hermes_install_dir" in
    "$HOME/.local/share/mise/installs/pipx-git-https-github-com-nous-research-hermes-agent-git"/*)
      ;;
    *)
      return 0
      ;;
  esac

  hermes_python="$hermes_install_dir/hermes-agent/bin/python"
  if [[ ! -x "$hermes_python" ]]; then
    return 0
  fi

  if "$hermes_python" - <<'PY' >/dev/null 2>&1
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
PY
  then
    return 0
  fi

  if ! command -v uv >/dev/null 2>&1; then
    echo "warning: Hermes MCP dependency is missing, but uv is not available to install it" >&2
    return 0
  fi

  uv pip install --python "$hermes_python" 'mcp>=1.24,<2'
}

main() {
  parse_args "$@"
  sync_shared_files
  sync_hooks
  sync_tool_configs
  sync_agent_env_files
  sync_hermes_mcp_dependency
}

main "$@"
