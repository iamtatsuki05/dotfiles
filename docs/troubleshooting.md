# Troubleshooting

[日本語](troubleshooting_JA.md) · [Documentation index](README.md)

Start with the failing command's complete error and `--help`. This page mixes
read-only diagnosis with recovery commands that change packages, files, or
credentials. Each changing step is labeled; review it before execution.

## `main.sh` installed Nix but cannot continue

The first installation may not update the current shell's `PATH`. Restart the
terminal, or source the Nix daemon profile named in the error, then rerun
`zsh main.sh`. The script can use the flake-provided `darwin-rebuild` or
`home-manager` once Nix itself is available.

## A Linux host has no sudo or cannot mount `/nix`

Use the nix-portable path instead of the normal installer:

```sh
zsh scripts/nix_portable_install.sh
export PATH="$HOME/.local/bin:$PATH"
nixp --version
dotfiles-nix-shell
```

Its default `proot` runtime is intended for hosts where mount namespaces are
restricted. Do not silently substitute Homebrew as the Linux package path.

## `nix_install.sh --with-gui-apps` fails on Linux

Linux GUI application setup requires `DISPLAY` or `WAYLAND_DISPLAY`. Use
`--cli-only` on a headless host:

```sh
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

## Nix reports that Homebrew is required

Inspect `config/nix/homebrew-fallback.nix`. Formula fallback entries can affect
the CLI profile; casks and VS Code extensions affect GUI application runs.
Install Homebrew for the selected profile only if those entries are intended:

The first command previews the installer. The second command installs
Homebrew and changes the machine.

```sh
zsh scripts/install_homebrew.sh --profile full --dry-run
zsh scripts/install_homebrew.sh --profile full
```

If the entries should no longer exist, migrate or remove them from the
canonical package configuration rather than bypassing the check.

## Chezmoi verification reports drift

View the proposed change before applying it:

```sh
zsh scripts/chezmoi_apply.sh --dry-run
```

If the deployed file contains an intended local change, move that change into
`home/` or its source under `config/` first. Then run the source-state test and
apply:

```sh
zsh tests/test_chezmoi_source_state.sh
zsh scripts/chezmoi_apply.sh --mark-default
zsh scripts/chezmoi_apply.sh --verify
```

## A Git pull did not apply new packages

This is expected. Pull hooks apply chezmoi files, sync agent files, and refresh
hooks. They do not run a Nix switch or install mise tools. After a flake or Nix
configuration change, run:

```sh
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

## Mac App Store apps were skipped or failed individually

Confirm that the Mac App Store is signed in and that the listed app is
available to the current account. The installer intentionally treats each app
as best-effort, so one failure does not fail the complete setup. Re-run the
focused script after correcting the account state:

The following command installs applications and changes the machine.

```sh
zsh scripts/install_mas_apps.sh --profile full
```

## Agent sync passed but the client still uses old behavior

First verify the canonical and deployed files:

`sync.sh` updates managed symlinks, settings, and local agent environment files.
It changes the local agent configuration before the read-only test runs.

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
```

Then restart or reload the affected client if required. A successful file sync
does not prove that an existing process reloaded prompts, hooks, MCP settings,
or skills.

## Claude profile switching is blocked

List all Claude processes and exit every session before changing the shared
Keychain credential:

`claude-account auth-login` changes the shared macOS Keychain login after the
browser authentication succeeds.

```sh
pgrep -fl claude
claude-account auth-login <profile>
```

Do not kill unrelated processes automatically. Close them normally so active
sessions can save their state. If the shared login does not match an existing
profile mapping, authenticate with the account originally registered for that
profile; the command preserves the old mapping rather than overwriting it.

## The full test suite skips one check

If chezmoi is unavailable, the local runner may skip only the rendered-home
integration check. Install chezmoi or run the test in CI before treating the
cross-platform apply path as fully verified. Other failures are not expected
skips and should be investigated from their first error.
