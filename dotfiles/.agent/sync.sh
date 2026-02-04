# Synv system prompt and skills

## functions
link_if_missing() {
  local src="$1"
  local dst="$2"
  if [ -L "$dst" ] || [ -e "$dst" ]; then
    return 0
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
link_if_missing ../.agent/AGENTS.md ~/.codex/AGENTS.md
link_if_missing ../.agent/AGENTS.md ~/.claude/CLAUDE.md
link_if_missing ../.agent/AGENTS.md ~/.gemini/GEMINI.md
link_if_missing ../.agent/AGENTS.md ~/.cursor/AGENT.md

## sync skills
link_if_missing ../.agent/skills ~/.codex/skills
link_if_missing ../.agent/skills ~/.claude/skills
link_if_missing ../.agent/skills ~/.gemini/skills
link_if_missing ../.agent/skills ~/.cursor/skills
