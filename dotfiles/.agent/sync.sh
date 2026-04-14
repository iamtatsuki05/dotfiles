# Sync system prompt, skills, hooks, and config files

## functions
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG_DIR="$REPO_ROOT/config"
echo "$SCRIPT_DIR"

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
      # 通常ファイルはシンボリックリンクに置き換える
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

## create dirs
ensure_dir ~/.codex
ensure_dir ~/.claude
ensure_dir ~/.gemini
ensure_dir ~/.cursor

## sync system prompt
link_symlink "$SCRIPT_DIR/AGENTS.md" ~/.codex/AGENTS.md
link_symlink "$SCRIPT_DIR/AGENTS.md" ~/.claude/CLAUDE.md
link_symlink "$SCRIPT_DIR/AGENTS.md" ~/.gemini/GEMINI.md
link_symlink "$SCRIPT_DIR/AGENTS.md" ~/.cursor/AGENT.md

## sync skills
link_symlink "$SCRIPT_DIR/skills" ~/.codex/skills
link_symlink "$SCRIPT_DIR/skills" ~/.claude/skills
link_symlink "$SCRIPT_DIR/skills" ~/.gemini/skills
link_symlink "$SCRIPT_DIR/skills" ~/.cursor/skills

## sync hooks (Claude Code / Gemini CLI / Codex CLI)
for hooks_dir in ~/.claude/hooks ~/.gemini/hooks ~/.codex/hooks; do
  ensure_dir "$hooks_dir"
  for hook_file in "$SCRIPT_DIR/hooks"/*; do
    [ -f "$hook_file" ] || continue
    chmod +x "$hook_file"
    link_symlink "$hook_file" "$hooks_dir/$(basename "$hook_file")"
  done
done

## sync config files (Claude Code / Gemini CLI / Codex CLI)
link_symlink "$CONFIG_DIR/claude/settings.json" ~/.claude/settings.json
link_symlink "$CONFIG_DIR/codex/hooks.json"     ~/.codex/hooks.json
link_symlink "$CONFIG_DIR/gemini/settings.json" ~/.gemini/settings.json
