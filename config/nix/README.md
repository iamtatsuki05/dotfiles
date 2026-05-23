# Nix Configuration

Japanese version: [README_JA.md](README_JA.md)

This directory contains the Nix package lists, nix-darwin modules, Home Manager modules, and migration reports used by this repository.

## Layout

| Path | Purpose |
|---|---|
| `darwin/` | nix-darwin modules for macOS system settings, Homebrew fallback, and auto-update timers. |
| `home-manager/` | Home Manager modules shared by macOS and Linux profiles. |
| `packages/` | Custom package definitions used by `dotfiles-packages.nix`. |
| `package-names.nix` | CLI package names. |
| `gui-*-package-names.nix` | GUI package lists split by platform. |
| `packages.nix` / `gui-packages.nix` | Package list materialization from the name lists. |
| `dotfiles-packages.nix` | Custom packages exposed by the flake, including Waza. |
| `homebrew-fallback.nix` | Generated Homebrew fallback entries that cannot yet move to Nix. |
| `mas-apps.nix` | Mac App Store app list used by `scripts/install_mas_apps.sh`. |
| `*-to-*.tsv`, `migrated-*`, `unmapped-homebrew.tsv` | Migration maps and reports from Homebrew / MAS to Nix. |
| `nix.conf` | Nix client configuration. |

## Update Rules

- Prefer Nix packages over Homebrew fallback entries.
- Keep Homebrew fallback reasons in `unmapped-homebrew.tsv` when an item cannot be moved.
- Regenerate fallback and migration reports with `scripts/migrate_brew_to_nix.sh`; avoid hand-editing generated reports unless the generator cannot express the correction.
- Keep `config/mise/config.toml` and `home/.chezmoitemplates/mise-config.toml` aligned when the change affects mise-managed tools.
- Changes under `darwin/` or `home-manager/` should be verified with the Nix tests or dry-run build.

## Common Checks

```bash
zsh scripts/nix_install.sh --cli-only --dry-run
zsh tests/test_nix_migration.sh
mise run nix-build
```
