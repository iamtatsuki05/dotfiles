# Managed Dotfiles

Japanese version: [README_JA.md](README_JA.md)

This directory contains files that are managed as repository-level dotfiles or runtime assets rather than normal chezmoi home source state.

## Layout

| Path | Purpose |
|---|---|
| `.agent/` | Shared AI agent prompts, app configs, hooks, skills, evals, and pet assets. |
| `.tmux.conf` | tmux configuration source. |

`home/` is the chezmoi source state for most home files.
Use this directory when a file is intentionally managed outside the chezmoi source tree or belongs to the shared AI agent runtime.

## Update Rules

- Keep `.agent/` documentation and sync behavior aligned with `dotfiles/.agent/README.md`.
- If a file should be rendered by chezmoi, put it under `home/` instead.
- Do not put local secrets or generated caches here.

## Common Checks

```bash
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
git diff --check -- dotfiles
```
