# Configuration Sources

Japanese version: [README_JA.md](README_JA.md)

This directory contains source configuration files used by Nix, chezmoi
templates, and setup scripts. Files here are not necessarily copied directly
into `$HOME`; many are rendered or imported by scripts.

## Layout

| Path | Purpose |
|---|---|
| `alacritty/` | Alacritty terminal config source. |
| `ghostty/` | Ghostty terminal config source. |
| `mise/` | Repo-managed mise tools, tasks, and update commands. |
| `mouse/` | Pointing device configuration exports. |
| `nix/` | Nix, nix-darwin, Home Manager, package lists, and migration reports. |
| `nvim/` | Neovim config source used by Home Manager. |
| `shell/` | Shell startup wrappers and the local secret example used by chezmoi. |
| `zellij/` | Zellij config source. |

Some empty or agent-named directories may exist as compatibility placeholders.
Check the scripts that consume a directory before removing it.

## Update Rules

- If a file is rendered into `home/`, keep the source and generated chezmoi state aligned.
- Edit `home/.chezmoidata.toml` for shell integration values (`EDITOR`, XDG
  defaults, named PATH candidates, aliases, and shell mise policy). Do not
  duplicate those values in Nix or in a second shell template.
- The Bash/Zsh common implementation lives only in
  `home/.chezmoitemplates/dotfiles-shell-common.sh`; the shell files under
  `config/shell/` are small wrappers or examples.
- Do not commit real secrets. Use `shell/secrets.env.example` as the tracked template.
- Nix package and module changes usually require checks from `nix/README.md`.
- mise tool changes should stay aligned with `home/.chezmoitemplates/mise-config.toml`.
- Shell ownership, fixed managed paths, and the Bash-only bootstrap are
  documented in [Configuration ownership](../docs/configuration-ownership.md).

## Common Checks

```bash
zsh tests/run.sh
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_nix_migration.sh
```
