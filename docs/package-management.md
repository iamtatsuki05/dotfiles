# Package management and migration

[日本語](package-management_JA.md) · [Documentation index](README.md)

Nix is the primary package-management path. Homebrew is a declared fallback
for macOS packages not yet available through Nix, and Mac App Store apps are
installed in a separate best-effort step.

## Package records

- CLI package names: `config/nix/package-names.nix`
- Shared GUI package names: `config/nix/gui-common-package-names.nix`
- macOS GUI package names: `config/nix/gui-macos-package-names.nix`
- Linux GUI package names: `config/nix/gui-linux-package-names.nix`
- Generated Homebrew fallback: `config/nix/homebrew-fallback.nix`
- Unmapped Homebrew report: `config/nix/unmapped-homebrew.tsv`
- Mac App Store apps: `config/nix/mas-apps.nix`

Nix modules are split between `config/nix/darwin/` and
`config/nix/home-manager/`. See the [Nix directory README](../config/nix/README.md)
for the detailed file map and focused checks.

## Build or apply Nix

```sh
# Build the CLI configuration without switching.
zsh scripts/nix_install.sh --cli-only --dry-run
# or
mise run nix-build

# Apply the CLI profile.
zsh scripts/nix_install.sh --cli-only
# or
mise run nix-apply

# Apply GUI applications as well.
zsh scripts/nix_install.sh --with-gui-apps
# or
mise run nix-apply-with-gui-apps
```

The flake provides `full` and `cli` outputs for aarch64 and x86_64 on both
Darwin and Linux. On a first macOS apply, `scripts/nix_install.sh` can invoke
the flake-provided `darwin-rebuild` before it is available in `PATH`. Linux uses
the flake-provided `home-manager` in the same situation.

The first macOS apply backs up an existing `/etc/pam.d/sudo_local` as
`/etc/pam.d/sudo_local.before-nix-darwin` before nix-darwin takes ownership.

## Migrate Homebrew state

Committed `.Brewfile` files are not the source of truth. Migrate the live
Homebrew state, or pass an exported Brewfile explicitly:

```sh
# The default mode is a non-writing dry-run.
zsh scripts/migrate_brew_to_nix.sh

# Regenerate package lists and reports from live Homebrew state.
zsh scripts/migrate_brew_to_nix.sh --apply

# Migrate a Brewfile exported from another machine.
zsh scripts/migrate_brew_to_nix.sh \
  --brewfile /path/to/Brewfile \
  --apply
```

When no Brewfile is supplied, the script creates a temporary one with
`brew bundle dump`, migrates it, and removes the temporary file. Mac App Store
entries are matched against `mas-to-nix.tsv`, then `mas-to-cask.tsv`; unmatched
apps are written to `mas-apps.nix`.

## Keep fallback behavior explicit

When `homebrew-fallback.nix` contains entries, Homebrew is still required for
those formulae, casks, taps, or VS Code extensions. Formulae apply even with the
CLI profile. Casks and VS Code extensions require `--with-gui-apps`.

Mac App Store apps are not passed to nix-darwin's `homebrew.masApps`. A single
unavailable app would otherwise fail the whole `brew bundle` activation.
`scripts/install_mas_apps.sh` instead reports individual failures without
failing the complete setup. The Mac App Store account must be signed in, and
removing an app from `mas-apps.nix` does not uninstall it.

## Remove Homebrew only after the fallback is empty

Homebrew removal is destructive. Preview the exact operation first:

```sh
zsh scripts/remove_homebrew.sh --dry-run
zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready
```

The apply command refuses to continue while fallback entries remain.
`zsh scripts/nix_install.sh --uninstall-homebrew` performs the same removal only
after the selected Nix switch succeeds.
