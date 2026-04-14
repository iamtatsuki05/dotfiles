# dotfiles

Japanese version: [README_JA.md](README_JA.md)

Please this command(support for mac)
```
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
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
