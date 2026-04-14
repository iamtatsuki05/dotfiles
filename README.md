# dotfiles

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
