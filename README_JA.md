# dotfiles

macOS 向けの dotfiles セットアップ手順です。

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

## Cron ジョブ

cron ジョブは `config/cron/crontab` で管理できます。

- `main.sh` は `scripts/setup_cron.sh` を実行し、このリポジトリが管理するブロックだけを `crontab` に同期します。
- managed block 以外の既存 cron エントリは保持されます。
- `config/cron/crontab` に有効な cron エントリが 1 つもなければ、managed block は削除されます。
- デフォルトでは、このリポジトリに対して毎日 06:00 に `git pull --ff-only` を実行し、ログを `/tmp/dotfiles-git-pull.log` に出力します。

例:

```cron
0 6 * * * /usr/bin/git -C /Users/tatsuki/src/dotfiles pull --ff-only >> /tmp/dotfiles-git-pull.log 2>&1
```

## API キーの管理

ローカルの秘密情報は `~/.config/shell/secrets.env`（gitignore 済み）で管理します。

初回セットアップ時に `config/shell/secrets.env.example` から自動生成されます。値を入力してシェルを再起動してください。

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
```
