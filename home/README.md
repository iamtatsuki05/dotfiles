# Chezmoi Home Source

Japanese version: [README_JA.md](README_JA.md)

This directory is the chezmoi source state.
The repository root `.chezmoiroot` points here.

## Layout

| Path | Purpose |
|---|---|
| `dot_*` | Files rendered to `$HOME` with leading dots. |
| `.chezmoitemplates/` | Shared templates consumed by chezmoi source files and sync checks. |
| `private_dot_config/` | Source files rendered under `~/.config/`. |

## Update Rules

- Edit this directory for files that chezmoi should apply directly to `$HOME`.
- Keep generated source state aligned with the corresponding files under `config/` when both exist.
- Do not commit real secrets. Use templates or examples for secret-related files.
- Use `scripts/chezmoi_apply.sh --dry-run` before applying changes to the live home.

## Common Checks

```bash
zsh scripts/chezmoi_apply.sh --dry-run
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_chezmoi_rendered_home.sh
```
