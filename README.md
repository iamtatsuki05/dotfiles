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

`full` is the complete macOS setup. It installs Homebrew casks, VS Code extensions, macOS defaults, cron, configs, mise tools, and Neovim.

`cli` is a portable CLI-focused setup for Ubuntu and other Linux hosts. It skips casks, VS Code extensions, macOS-only tools, macOS defaults, and cron, then installs CLI tools from `dotfiles/.Brewfile.cli`. The cli profile also activates the CLI bundle as `~/.Brewfile`.

```sh
# Ubuntu / Linux, or CLI-only setup on macOS
zsh main.sh --cli-only

# Install only CLI Homebrew packages
zsh scripts/brew_install.sh --cli-only
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
# or: brew install chezmoi
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

## Updating Brewfiles

On macOS, you can dump the current Homebrew state to `dotfiles/.Brewfile` and the portable CLI bundle at `dotfiles/.Brewfile.cli`.

```sh
zsh scripts/brew_dump.sh
# or
mise run brew-dump
```

The CLI bundle is generated with Homebrew Bundle's `--tap`, `--formula`, and `--uv` filters.
When installed by `setup_config.sh`, the mise config runs this repository's `scripts/brew_dump.sh` regardless of the directory where `mise run brew-dump` is invoked.

```sh
# Regenerate only dotfiles/.Brewfile.cli from the current Homebrew state
zsh scripts/brew_dump.sh --generate-cli-only
```

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

The hooks call [scripts/apply_updates.sh](scripts/apply_updates.sh), which lightly syncs dotfiles, AI tool files, app configs, cron, and the hooks themselves. Heavy operations such as Homebrew install and mise tool install are not run automatically.

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
