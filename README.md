# dotfiles

Japanese version: [README_JA.md](README_JA.md)

Run:

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

## Setup profiles

`main.sh` picks a default profile by OS:

- macOS: `full`
- Linux: `cli`

`full` is the complete macOS setup. It applies nix-darwin, Home Manager, GUI apps, macOS defaults, cron, configs, mise tools, and Neovim.

`cli` is a portable CLI-focused setup for Ubuntu and other Linux hosts. It skips GUI apps, macOS-only tools, macOS defaults, and cron, then applies only the Nix CLI package set.

```sh
# Ubuntu / Linux, or CLI-only setup on macOS
zsh main.sh --cli-only

# Apply only Nix/Home Manager CLI packages
zsh scripts/nix_install.sh --cli-only
```

## Chezmoi migration

The repository now includes a chezmoi source state under `home/`, with `.chezmoiroot` pointing to that directory. The existing `dotfiles/` and `config/` layout is still kept as the source of truth for the current setup scripts, so migration can be done gradually.

Generate or refresh the chezmoi source state:

```sh
zsh scripts/migrate_to_chezmoi.sh --dry-run
zsh scripts/migrate_to_chezmoi.sh --apply
# or
mise run chezmoi-migrate
```

Install `chezmoi` with any supported package manager, then preview and apply:

```sh
sh -c "$(curl -fsLS get.chezmoi.io)" -- -b "$HOME/.local/bin"
# or: nix run nixpkgs#chezmoi -- init
# or: mise use --global chezmoi@latest

zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default
# On macOS CLI-only machines:
zsh scripts/chezmoi_apply.sh --cli-only --mark-default
# or
mise run chezmoi-diff
mise run chezmoi-apply
```

`--mark-default` writes `~/.config/dotfiles/manager` with `chezmoi` and stores the selected profile in `~/.config/dotfiles/profile`. After that, the git pull hooks installed by this repo use `chezmoi apply` when `chezmoi` is available, and fall back to the legacy copy flow otherwise.

## Testing dotfiles

Configuration checks are centralized in [scripts/test_dotfiles.sh](scripts/test_dotfiles.sh).

```sh
zsh scripts/test_dotfiles.sh
# or
mise run test-dotfiles
```

The test runner checks zsh syntax, migration helpers, generated chezmoi source state drift, and chezmoi rendering into a temporary home directory. If `chezmoi` is not installed locally, only the rendered-home integration check is skipped.

GitHub Actions runs the same checks on `ubuntu-latest` and `macos-latest`, installs `chezmoi`, and verifies that the source state can be applied to a temporary home on both platforms.

## Nix package migration

Homebrew is no longer the primary setup path. macOS uses nix-darwin plus Home Manager, and Linux uses standalone Home Manager with the same package sets. CLI packages live in [config/nix/package-names.nix](config/nix/package-names.nix). GUI apps are split by platform in [config/nix/gui-common-package-names.nix](config/nix/gui-common-package-names.nix), [config/nix/gui-macos-package-names.nix](config/nix/gui-macos-package-names.nix), and [config/nix/gui-linux-package-names.nix](config/nix/gui-linux-package-names.nix). Homebrew entries that cannot be moved to Nix are recorded in [config/nix/unmapped-homebrew.tsv](config/nix/unmapped-homebrew.tsv), and the macOS fallback managed by nix-darwin is generated as [config/nix/homebrew-fallback.nix](config/nix/homebrew-fallback.nix).

```sh
# Regenerate Nix package lists and the unmapped Homebrew report from current Homebrew state
zsh scripts/migrate_brew_to_nix.sh --apply
# or migrate an exported Brewfile from another machine
zsh scripts/migrate_brew_to_nix.sh --brewfile /path/to/Brewfile --apply
# or
mise run nix-migrate-brew

# Build the selected Nix configuration without switching
zsh scripts/nix_install.sh --dry-run
# or
mise run nix-build

# Apply with nix-darwin on macOS or Home Manager on Linux
zsh scripts/nix_install.sh
# or
mise run nix-apply

# Apply CLI packages only
zsh scripts/nix_install.sh --cli-only
# or
mise run nix-apply-cli

# Install GUI apps too on hosts that support GUI apps, including Ubuntu desktop
zsh scripts/nix_install.sh --with-gui-apps
# or
mise run nix-apply-with-gui-apps
```

On first macOS setup, `darwin-rebuild` may not be available in `PATH` yet. [scripts/nix_install.sh](scripts/nix_install.sh) handles that by running the flake-provided `darwin-rebuild`. On Linux, it similarly uses the flake-provided `home-manager` when the command is not installed yet.

If [config/nix/homebrew-fallback.nix](config/nix/homebrew-fallback.nix) has entries, Homebrew is still required on macOS for fallback formulae, casks, taps, and VS Code extensions. Formulae are applied even in the CLI profile. Casks and VS Code extensions are applied only with `--with-gui-apps`. If the fallback is empty and Nix is applied successfully, Homebrew can be removed explicitly. This is destructive, so check the dry-run first.

```sh
zsh scripts/remove_homebrew.sh --dry-run
zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready
```

`zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready` refuses to remove Homebrew while fallback entries exist. `zsh scripts/nix_install.sh --uninstall-homebrew` runs the same removal only after the selected Nix switch succeeds.

## Migrating Another Homebrew Machine

Committed `.Brewfile` files are no longer used. On an old macOS machine that still has Homebrew, run the migration directly from the live Homebrew state, or pass an exported Brewfile explicitly.

```sh
zsh scripts/migrate_brew_to_nix.sh --apply
zsh scripts/migrate_brew_to_nix.sh --brewfile /path/to/Brewfile --apply
```

When `--brewfile` is omitted, the script uses `brew bundle dump` to create a temporary Brewfile, migrates it to Nix package lists, and then removes the temporary file.

## Cron jobs

You can manage cron jobs from `config/cron/crontab`.

- `main.sh` runs `scripts/setup_cron.sh` and syncs only the block managed by this repository.
- Existing cron entries outside the managed block are preserved.
- If `config/cron/crontab` does not contain any active cron entries, the managed block is removed.
- The default managed job runs `git pull --ff-only` for this repository once per day at 06:00 and writes logs to `/tmp/dotfiles-git-pull.log`.

Example:
```cron
0 6 * * * /usr/bin/git -C /Users/tatsuki/src/dotfiles pull --ff-only >> /tmp/dotfiles-git-pull.log 2>&1
```

## Auto-sync after git pull

`main.sh` installs Git hooks into `.git/hooks/`.

- `post-merge`: runs after `git pull` / merge
- `post-rewrite`: runs after `git pull --rebase`
- `post-checkout`: runs after branch checkout

The hooks call [scripts/apply_updates.sh](scripts/apply_updates.sh), which syncs dotfiles, AI tool files, app configs, cron, and the hooks themselves. nix-darwin / Home Manager switch, Homebrew uninstall, and mise tool install are not run automatically.

To reinstall hooks manually:

```sh
zsh scripts/setup_git_hooks.sh
```

## AI tool configuration (Claude Code / Codex / Gemini CLI)

Settings files for each AI tool are managed in `config/` and symlinked to the appropriate locations by `sync.sh`:

| Repository path | Symlinked to |
|---|---|
| `config/claude/settings.json` | `~/.claude/settings.json` |
| `config/codex/hooks.json` | `~/.codex/hooks.json` |
| `config/gemini/settings.json` | `~/.gemini/settings.json` |

Hook scripts in `dotfiles/.agent/hooks/` are symlinked to `~/.claude/hooks/`, `~/.codex/hooks/`, and `~/.gemini/hooks/`.

### Jupyter Notebook (jupytext)

To reduce token consumption, AI tools are configured to edit `.py` files only. `jupytext --sync` runs automatically after each file edit via hooks, keeping the paired `.ipynb` up to date.

To pair a new notebook:
```bash
jupytext --set-formats ipynb,py:percent notebook.py
```

## API keys and secrets

Local secrets are managed in `~/.config/shell/secrets.env` (gitignored).

On first setup, `scripts/setup_config.sh` copies `config/shell/secrets.env.example` to `~/.config/shell/secrets.env`. Fill in the values and restart the shell.

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
```
