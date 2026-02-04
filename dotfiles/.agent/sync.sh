# Synv system prompt and skills

## functions
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
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
    else
      echo "skip: $dst exists and is not symlink" >&2
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

## creat dir
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
