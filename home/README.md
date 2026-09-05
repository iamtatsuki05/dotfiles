# Chezmoi Home Source

Japanese version: [README_JA.md](README_JA.md)

This directory is the chezmoi source state.
The repository root `.chezmoiroot` points here.

## Layout

| Path | Purpose |
|---|---|
| `dot_*` | Files rendered to `$HOME` with leading dots. |
| `.chezmoitemplates/` | Canonical shared templates consumed by chezmoi source files and sync checks. |
| `private_dot_config/` | Source files rendered under `~/.config/`. |

## Update Rules

- Edit this directory for files that chezmoi should apply directly to `$HOME`.
- Keep generated source state aligned with the corresponding files under `config/` when both exist.
- `home/.chezmoidata.toml` is the single source for shell integration values;
  `config/nix/home-manager/session.nix` reads it instead of repeating values.
- `home/.chezmoitemplates/dotfiles-shell-common.sh` is the sole Bash/Zsh
  common implementation. Fish and csh/tcsh remain native adapters in their
  respective `private_dot_config/` templates.
- Do not commit real secrets. Use templates or examples for secret-related files.
- Use `scripts/chezmoi_apply.sh --dry-run` before applying changes to the live home.
- For a shell-only host, use `bash scripts/setup_shell.sh --dry-run`; it has a
  fixed six-target allowlist and does not apply unrelated home files.
- Shell ownership, fixed managed paths, custom XDG behavior, and startup
  boundaries are documented in [Configuration ownership](../docs/configuration-ownership.md).

## Common Checks

```bash
zsh scripts/chezmoi_apply.sh --dry-run
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_chezmoi_rendered_home.sh
```
