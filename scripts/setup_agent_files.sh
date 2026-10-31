#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
DEFAULT_REPO_ROOT="${SCRIPT_DIR:h}"
REPO_ROOT="$DEFAULT_REPO_ROOT"
DRY_RUN=0
INSTALL_DEPS=0
ONLY_AGENT=""
AGENT_DIR=""
APPS_DIR=""
readonly SECRETS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/shell/secrets.env"

usage() {
  cat <<EOF
Usage:
  zsh scripts/setup_agent_files.sh [--repo-root PATH]

Options:
  --dry-run        Preview changes without writing files or installing dependencies.
  --install-deps   Also install missing Hermes MCP dependencies (may access network).
  --only AGENT     Sync only deepseek or pi, preserving all other agent homes.
  --repo-root PATH  Override repository root. Intended for tests.
  -h, --help        Show this help.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --install-deps)
        INSTALL_DEPS=1
        ;;
      --only)
        shift
        if ((! $#)) || [[ "$1" != "deepseek" && "$1" != "pi" ]]; then
          echo "ERROR: --only requires deepseek or pi" >&2
          return 1
        fi
        ONLY_AGENT="$1"
        ;;
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

  if [[ -n "$ONLY_AGENT" ]] && (( INSTALL_DEPS )); then
    echo "ERROR: --install-deps installs Hermes dependencies and cannot be combined with --only" >&2
    return 1
  fi

  REPO_ROOT="${REPO_ROOT:A}"
  AGENT_DIR="$REPO_ROOT/dotfiles/.agent"
  APPS_DIR="$AGENT_DIR/apps"
}

link_symlink() {
  local src="$1"
  local dst="$2"

  if (( DRY_RUN )); then
    print -r -- "link: $dst -> $src (existing files/symlinks may be replaced)"
    return 0
  fi

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
      if (( DRY_RUN )); then
        print -r -- "remove managed link: $dst"
      else
        rm -f "$dst"
      fi
      return 0
    fi
  done
}

ensure_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    if (( DRY_RUN )); then
      print -r -- "mkdir: $dir"
    else
      mkdir -p "$dir"
    fi
  fi
}

sync_link_specs() {
  local spec_function="$1"
  shift
  local src
  local dst

  while IFS=$'\t' read -r src dst; do
    [[ -n "$src" && -n "$dst" ]] || continue
    ensure_dir "${dst:h}"
    link_symlink "$src" "$dst"
  done < <("$spec_function" "$@")
}

require_shared_skill_link() {
  local dst="$1"
  (( DRY_RUN )) && return 0

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

  if [[ "$ONLY_AGENT" == "deepseek" ]]; then
    print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.dsh/AGENTS.md"
    return
  elif [[ "$ONLY_AGENT" == "pi" ]]; then
    print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.pi/agent/AGENTS.md"
    print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.pi/agent/skills"
    return
  fi

  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.codex/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.claude/CLAUDE.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.copilot/copilot-instructions.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.gemini/antigravity-cli/plugins/dotfiles-agent/rules/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.cursor/AGENT.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$xdg_config_home/opencode/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.hermes/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.openclaw/workspace/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.grok/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.dsh/AGENTS.md"
  print -r -- "$AGENT_DIR/AGENTS.md"$'\t'"$HOME/.pi/agent/AGENTS.md"

  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.codex/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.claude/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.copilot/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$xdg_config_home/devin/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.gemini/antigravity-cli/plugins/dotfiles-agent/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.cursor/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$xdg_config_home/opencode/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.hermes/dotfiles-skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.openclaw/workspace/skills"
  print -r -- "$AGENT_DIR/skills"$'\t'"$HOME/.pi/agent/skills"

  print -r -- "$AGENT_DIR/pets"$'\t'"$HOME/.codex/pets"
  print -r -- "$REPO_ROOT/scripts/agent-run-compact"$'\t'"$HOME/.local/bin/agent-run-compact"
}

# Hermes writes its bundled skills, sync manifest, and curator state into
# ~/.hermes/skills, so that path must stay a real directory owned by Hermes.
# The shared tree is exposed to Hermes read-only through skills.external_dirs.
ensure_hermes_local_skills_dir() {
  local dst="$HOME/.hermes/skills"
  if [[ -L "$dst" ]]; then
    if [[ ! -e "$dst" || "$dst" -ef "$AGENT_DIR/skills" ]]; then
      if (( DRY_RUN )); then
        print -r -- "replace managed link with Hermes-owned directory: $dst"
        return 0
      fi
      rm -f "$dst"
    else
      echo "ERROR: $dst must be a real directory owned by Hermes, but it is a symlink to an unmanaged target: $(readlink "$dst")" >&2
      return 1
    fi
  fi
  ensure_dir "$dst"
}

sync_deepseek_skills() {
  local dst_dir="$HOME/.dsh/skills"
  local skill_file relative_dir link_name dst target
  local -A sources

  if [[ -L "$dst_dir" || ( -e "$dst_dir" && ! -d "$dst_dir" ) ]]; then
    echo "ERROR: $dst_dir must be a real directory; existing path was preserved" >&2
    return 1
  fi

  # DSH discovers only direct children, while our shared skills include categories.
  for skill_file in "$AGENT_DIR"/skills/**/SKILL.md(N.); do
    relative_dir="${skill_file:h}"
    relative_dir="${relative_dir#$AGENT_DIR/skills/}"
    link_name="dotfiles-${relative_dir//\//--}"
    if [[ -n "${sources[$link_name]-}" ]]; then
      echo "ERROR: DeepSeek skill paths collide at $link_name" >&2
      return 1
    fi
    sources[$link_name]="${skill_file:h}"
    dst="$dst_dir/$link_name"
    if [[ -L "$dst" ]]; then
      target="$(readlink "$dst")"
      [[ "$target" == "$AGENT_DIR/skills/"* ]] && continue
    elif [[ ! -e "$dst" ]]; then
      continue
    fi
    echo "ERROR: managed DeepSeek skill link is blocked by existing path: $dst" >&2
    return 1
  done

  ensure_dir "$dst_dir"
  for dst in "$dst_dir"/dotfiles-*(N@); do
    link_name="${dst:t}"
    if [[ -z "${sources[$link_name]-}" ]]; then
      remove_managed_symlink "$dst" "$AGENT_DIR/skills"
    fi
  done
  for link_name in ${(ok)sources}; do
    link_symlink "${sources[$link_name]}" "$dst_dir/$link_name"
  done
}

sync_shared_files() {
  local pi_skills="$HOME/.pi/agent/skills"

  if [[ "$ONLY_AGENT" == "deepseek" ]]; then
    sync_deepseek_skills
    sync_link_specs shared_link_specs
    return
  fi
  if [[ ( -L "$pi_skills" || -f "$pi_skills" ) && ! "$pi_skills" -ef "$AGENT_DIR/skills" ]]; then
    echo "ERROR: Pi skills path is not managed by this repository; existing path was preserved: $pi_skills" >&2
    return 1
  fi
  preflight_required_shared_skill_link "$pi_skills"
  if [[ "$ONLY_AGENT" == "pi" ]]; then
    sync_link_specs shared_link_specs
    require_shared_skill_link "$pi_skills"
    return
  fi
  preflight_required_shared_skill_link "$HOME/.codex/skills"
  preflight_required_shared_skill_link "$HOME/.claude/skills"
  preflight_required_shared_skill_link "$HOME/.pi/agent/skills"
  sync_deepseek_skills
  ensure_hermes_local_skills_dir
  sync_link_specs shared_link_specs
  require_shared_skill_link "$HOME/.codex/skills"
  require_shared_skill_link "$HOME/.claude/skills"
  require_shared_skill_link "$HOME/.pi/agent/skills"
}

new_agent_link_specs() {
  local agent="$1"
  local target_dir hook profile
  case "$agent" in
    pi) target_dir="$HOME/.pi/agent" ;;
    deepseek) target_dir="$HOME/.dsh" ;;
  esac
  for hook in agent_context_reminder.sh agent_turn_done_notify.sh jupytext_sync.sh japanese_prose_lint.sh; do
    print -r -- "$AGENT_DIR/hooks/$hook"$'\t'"$target_dir/hooks/$hook"
  done
  if [[ "$agent" == "pi" ]]; then
    print -r -- "$APPS_DIR/pi/extensions/dotfiles.ts"$'\t'"$target_dir/extensions/dotfiles.ts"
    print -r -- "$APPS_DIR/pi/extensions/rtk.ts"$'\t'"$target_dir/extensions/rtk.ts"
  else
    print -r -- "$APPS_DIR/deepseek/cordis.patch.yml"$'\t'"$target_dir/cordis.patch.yml"
    for profile in web headless; do
      print -r -- "$APPS_DIR/deepseek"$'\t'"$target_dir/profiles/$profile/node_modules/@dotfiles/deepseek-native"
    done
  fi
}

preflight_new_agent_integrations() {
  local agent="$1"
  local src dst
  while IFS=$'\t' read -r src dst; do
    if [[ ( -e "$dst" || -L "$dst" ) && ! ( -L "$dst" && "$dst" -ef "$src" ) ]]; then
      echo "ERROR: managed $agent integration is blocked by existing path: $dst" >&2
      return 1
    fi
  done < <(new_agent_link_specs "$agent")
  if [[ "$agent" == "pi" ]]; then
    sync_pi_runtime_configs check
  fi
}

sync_pi_runtime_configs() {
  python3 -B - "$APPS_DIR/pi" "$HOME/.pi/agent" "$1" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

source, destination = map(Path, sys.argv[1:3])
mode = sys.argv[3]
updates = []
for name in ("settings.json", "mcp.json"):
    target = destination / name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SystemExit(f"ERROR: Pi runtime config must be a regular file: {target}")
    current = json.loads(target.read_text()) if target.exists() else {}
    managed = json.loads((source / name).read_text())
    if not isinstance(current, dict) or not isinstance(managed, dict):
        raise SystemExit(f"ERROR: Pi runtime config must contain an object: {target}")
    if name == "mcp.json":
        servers = current.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise SystemExit(f"ERROR: Pi mcpServers must contain an object: {target}")
        current["mcpServers"] = {**servers, **managed["mcpServers"]}
    else:
        current.update(managed)
    updates.append((target, json.dumps(current, ensure_ascii=False, indent=2) + "\n"))
if mode == "check":
    sys.exit(0)
for target, text in updates:
    if mode == "preview":
        print(f"merge managed Pi settings, preserving other fields: {target}")
        continue
    if target.exists() and target.read_text() == text:
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dotfiles-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
PY
}

sync_new_agent_integrations() {
  sync_link_specs new_agent_link_specs "$1"
  if [[ "$1" == "pi" ]]; then
    if (( DRY_RUN )); then
      sync_pi_runtime_configs preview
    else
      sync_pi_runtime_configs write
    fi
  fi
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
    if (( DRY_RUN )); then
      print -r -- "chmod +x: $hook_file"
    else
      chmod +x "$hook_file"
    fi
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
  print -r -- "$HOME/.openclaw/hooks"
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
  print -r -- "$APPS_DIR/copilot/hooks/rtk-rewrite.json"$'\t'"$HOME/.copilot/hooks/rtk-rewrite.json"
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

agent_plugin_link_specs() {
  print -r -- "$APPS_DIR/antigravity-cli/plugins/rtk"$'\t'"$HOME/.gemini/config/plugins/rtk"
  print -r -- "$APPS_DIR/openclaw/extensions/rtk-rewrite"$'\t'"$HOME/.openclaw/extensions/rtk-rewrite"
  print -r -- "$APPS_DIR/hermes-agent/plugins/rtk-rewrite"$'\t'"$HOME/.hermes/plugins/rtk-rewrite"
  print -r -- "$APPS_DIR/hermes-agent/plugins/japanese-prose-lint"$'\t'"$HOME/.hermes/plugins/japanese-prose-lint"
  print -r -- "$APPS_DIR/openclaw/extensions/japanese-prose-lint"$'\t'"$HOME/.openclaw/extensions/japanese-prose-lint"
}

sync_tool_configs() {
  sync_link_specs tool_config_link_specs
  sync_link_specs agent_plugin_link_specs
}

enable_rtk_antigravity_plugin() {
  if (( DRY_RUN )); then
    print -r -- "enable RTK plugin: $HOME/.gemini/config/config.json"
    return 0
  fi

  python3 - "$HOME/.gemini/config/config.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
content = path.read_text() if path.exists() else "{}"
settings = json.loads(content)
settings.setdefault("plugins", {}).setdefault("rtk", {})["enabled"] = True
updated = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
if updated != content:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated)
PY
}

write_env_file_from_secrets() {
  local env_file="$1"
  shift
  if [[ ! -f "$SECRETS_FILE" ]]; then
    return 0
  fi

  if (( DRY_RUN )); then
    print -r -- "sync env (values hidden, mode 600): $env_file"
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
  local -a new_agents
  local agent
  if [[ -n "$ONLY_AGENT" ]]; then
    new_agents=("$ONLY_AGENT")
  else
    new_agents=(deepseek pi)
  fi
  for agent in "${new_agents[@]}"; do
    preflight_new_agent_integrations "$agent"
  done
  sync_shared_files
  for agent in "${new_agents[@]}"; do
    sync_new_agent_integrations "$agent"
  done
  [[ -n "$ONLY_AGENT" ]] && return 0
  sync_hooks
  sync_tool_configs
  enable_rtk_antigravity_plugin
  sync_agent_env_files
  if (( INSTALL_DEPS )); then
    if (( DRY_RUN )); then
      print -r -- "check/install missing Hermes MCP dependencies"
    else
      sync_hermes_mcp_dependency
    fi
  fi
}

main "$@"
