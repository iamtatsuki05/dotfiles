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

remove_managed_symlink() {
  local dst="$1"
  shift

  if [ ! -L "$dst" ]; then
    return 0
  fi

  local target
  target="$(readlink "$dst")"
  local target_abs=""
  if [ -e "$dst" ]; then
    target_abs="${dst:A}"
  fi
  local allowed
  local allowed_abs
  for allowed in "$@"; do
    allowed_abs="${allowed:A}"
    if [[ "$target" == "$allowed" || "$target" == "$allowed/"* || "$target_abs" == "$allowed_abs" || "$target_abs" == "$allowed_abs/"* ]]; then
      rm -f "$dst"
      return 0
    fi
  done
}

ensure_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
  fi
}

sync_link_specs() {
  local spec_function="$1"
  local src
  local dst

  while IFS=$'\t' read -r src dst; do
    [[ -n "$src" && -n "$dst" ]] || continue
    ensure_dir "${dst:h}"
    link_symlink "$src" "$dst"
  done < <("$spec_function")
}

require_shared_skill_link() {
  local dst="$1"

  if [[ -L "$dst" && "$dst" -ef "$AGENT_DIR/skills" ]]; then
    return 0
  fi

  echo "ERROR: required shared skill link was not created: $dst -> $AGENT_DIR/skills" >&2
  return 1
}

preflight_required_shared_skill_link() {
  local dst="$1"
  local -a entries

  if [[ -L "$dst" || ! -e "$dst" || -f "$dst" ]]; then
    return 0
  fi
  if [[ -d "$dst" ]]; then
    entries=("$dst"/*(DN))
    if (( ${#entries} == 0 )); then
      return 0
    fi
  fi

  echo "ERROR: required shared skill link is blocked by existing path: $dst" >&2
  return 1
}

shared_link_specs() {
  local xdg_config_home="${XDG_CONFIG_HOME:-$HOME/.config}"

  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.codex/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.claude/CLAUDE.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.copilot/copilot-instructions.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.gemini/antigravity-cli/plugins/dotfiles-agent/rules/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.cursor/AGENT.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$xdg_config_home/opencode/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.hermes/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.openclaw/workspace/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.grok/AGENTS.md"

  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.codex/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.claude/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.copilot/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$xdg_config_home/devin/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.gemini/antigravity-cli/plugins/dotfiles-agent/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.cursor/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$xdg_config_home/opencode/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.hermes/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.openclaw/workspace/skills"

  print -r -- "$AGENT_DIR/pets"$'\t'"$HOME/.codex/pets"
}

sync_shared_files() {
  preflight_required_shared_skill_link "$HOME/.codex/skills"
  preflight_required_shared_skill_link "$HOME/.claude/skills"
  sync_link_specs shared_link_specs
  require_shared_skill_link "$HOME/.codex/skills"
  require_shared_skill_link "$HOME/.claude/skills"
}

sync_hook_files_from_dir() {
  local source_dir="$1"
  local hooks_dir="$2"
  local hook_file
  local hook_name

  ensure_dir "$hooks_dir"
  for hook_file in "$source_dir"/*.sh; do
    [ -f "$hook_file" ] || continue
    hook_name="${hook_file:t}"
    chmod +x "$hook_file"
    link_symlink "$hook_file" "$hooks_dir/$hook_name"
  done
}

common_hook_target_dirs() {
  local xdg_config_home="${XDG_CONFIG_HOME:-$HOME/.config}"

  print -r -- "$HOME/.claude/hooks"
  print -r -- "$HOME/.gemini/antigravity-cli/hooks"
  print -r -- "$HOME/.codex/hooks"
  print -r -- "$HOME/.copilot/hooks"
  print -r -- "$HOME/.cursor/hooks"
  print -r -- "$xdg_config_home/devin/hooks"
  print -r -- "$xdg_config_home/opencode/hooks"
  print -r -- "$HOME/.hermes/agent-hooks"
  print -r -- "$HOME/.grok/hooks"
}

app_hook_dir_specs() {
  print -r -- "$APPS_DIR/hermes-agent/agent-hooks"$'\t'"$HOME/.hermes/agent-hooks"
}

sync_common_hooks() {
  local hooks_dir

  while IFS= read -r hooks_dir; do
    [[ -n "$hooks_dir" ]] || continue
    remove_managed_symlink "$hooks_dir/README.md" "$AGENT_DIR/hooks"
    remove_managed_symlink "$hooks_dir/README_JA.md" "$AGENT_DIR/hooks"
    sync_hook_files_from_dir "$AGENT_DIR/hooks" "$hooks_dir"
  done < <(common_hook_target_dirs)
}

sync_app_hooks() {
  local source_dir
  local hooks_dir

  while IFS=$'\t' read -r source_dir hooks_dir; do
    [[ -n "$source_dir" && -n "$hooks_dir" ]] || continue
    sync_hook_files_from_dir "$source_dir" "$hooks_dir"
  done < <(app_hook_dir_specs)
}

sync_hooks() {
  sync_common_hooks
  sync_app_hooks
}

tool_config_link_specs() {
  local xdg_config_home="${XDG_CONFIG_HOME:-$HOME/.config}"

  print -r -- "$APPS_DIR/claude/settings.json"$'\t'"$HOME/.claude/settings.json"
  print -r -- "$APPS_DIR/claude/.mcp.json"$'\t'"$HOME/.claude/.mcp.json"
  print -r -- "$APPS_DIR/copilot/settings.json"$'\t'"$HOME/.copilot/settings.json"
  print -r -- "$APPS_DIR/copilot/mcp-config.json"$'\t'"$HOME/.copilot/mcp-config.json"
  print -r -- "$APPS_DIR/codex/config.toml"$'\t'"$HOME/.codex/config.toml"
  print -r -- "$APPS_DIR/codex/hooks.json"$'\t'"$HOME/.codex/hooks.json"
  print -r -- "$APPS_DIR/devin/config.json"$'\t'"$xdg_config_home/devin/config.json"
  print -r -- "$APPS_DIR/antigravity-cli/settings.json"$'\t'"$HOME/.gemini/antigravity-cli/settings.json"
  print -r -- "$APPS_DIR/antigravity-cli/plugins/dotfiles-agent/plugin.json"$'\t'"$HOME/.gemini/antigravity-cli/plugins/dotfiles-agent/plugin.json"
  print -r -- "$APPS_DIR/antigravity-cli/plugins/dotfiles-agent/mcp_config.json"$'\t'"$HOME/.gemini/antigravity-cli/plugins/dotfiles-agent/mcp_config.json"
  print -r -- "$APPS_DIR/antigravity-cli/plugins/dotfiles-agent/hooks.json"$'\t'"$HOME/.gemini/antigravity-cli/plugins/dotfiles-agent/hooks.json"
  print -r -- "$APPS_DIR/cursor/cli-config.json"$'\t'"$HOME/.cursor/cli-config.json"
  print -r -- "$APPS_DIR/cursor/hooks.json"$'\t'"$HOME/.cursor/hooks.json"
  print -r -- "$APPS_DIR/cursor/mcp.json"$'\t'"$HOME/.cursor/mcp.json"
  print -r -- "$APPS_DIR/opencode/opencode.json"$'\t'"$xdg_config_home/opencode/opencode.json"
  print -r -- "$APPS_DIR/opencode/plugins"$'\t'"$xdg_config_home/opencode/plugins"
  print -r -- "$APPS_DIR/hermes-agent/config.yaml"$'\t'"$HOME/.hermes/config.yaml"
  print -r -- "$APPS_DIR/openclaw/openclaw.json"$'\t'"$HOME/.openclaw/openclaw.json"
  print -r -- "$APPS_DIR/grok/config.toml"$'\t'"$HOME/.grok/config.toml"
}

sync_tool_configs() {
  sync_link_specs tool_config_link_specs
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

agent_env_file_specs() {
  print -r -- "$HOME/.gemini/antigravity-cli/.env"$'\t'"DEVIN_API_KEY"
  print -r -- "$HOME/.hermes/.env"$'\t'"DEVIN_API_KEY OPENCODE_API_KEY OPENCODE_API_KEY:OPENCODE_GO_API_KEY"
  print -r -- "$HOME/.openclaw/.env"$'\t'"DEVIN_API_KEY OPENCODE_API_KEY"
}

sync_agent_env_files() {
  local env_file
  local var_specs
  local -a vars

  while IFS=$'\t' read -r env_file var_specs; do
    [[ -n "$env_file" && -n "$var_specs" ]] || continue
    vars=("${(@s: :)var_specs}")
    write_env_file_from_secrets "$env_file" "${vars[@]}"
  done < <(agent_env_file_specs)
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
