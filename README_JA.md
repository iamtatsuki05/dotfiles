# dotfiles

macOS / Linux 向けの dotfiles セットアップ手順です。

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

## セットアッププロファイル

`main.sh` は OS に応じて既定プロファイルを切り替えます。

- macOS: `full`
- Linux: `cli`

`full` は macOS 向けの全体セットアップです。Homebrew cask、VS Code 拡張、macOS defaults、cron、設定ファイル、mise、Neovim をセットアップします。

`cli` は Ubuntu などでも使いやすい CLI 中心のセットアップです。cask、VS Code 拡張、macOS 専用ツール、macOS defaults、cron は実行せず、`dotfiles/.Brewfile.cli` から CLI ツールをインストールします。CLI プロファイルでは `~/.Brewfile` も CLI 版に差し替えます。

```sh
# Ubuntu / Linux や CLI だけを入れたい macOS
zsh main.sh --cli-only

# Homebrew の CLI パッケージだけを入れたい場合
zsh scripts/brew_install.sh --cli-only
```

## Brewfile の更新

macOS で現在の Homebrew 状態から `dotfiles/.Brewfile` と CLI 版の `dotfiles/.Brewfile.cli` を更新できます。

```sh
zsh scripts/brew_dump.sh
# または
mise run brew-dump
```

CLI 版は Homebrew Bundle の `--tap` / `--formula` / `--uv` で生成します。
`setup_config.sh` でインストールされた mise config では、`mise run brew-dump` をどのディレクトリから実行しても、このリポジトリの `scripts/brew_dump.sh` が実行されます。

```sh
# 現在の Homebrew 状態から CLI 版だけを再生成
zsh scripts/brew_dump.sh --generate-cli-only
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

## AI ツール設定（Claude Code / Codex / Gemini CLI）

各 AI ツールの設定ファイルは `config/` で管理し、`sync.sh` がシンボリックリンクで配置します。

| リポジトリのパス | リンク先 |
|---|---|
| `config/claude/settings.json` | `~/.claude/settings.json` |
| `config/codex/hooks.json` | `~/.codex/hooks.json` |
| `config/gemini/settings.json` | `~/.gemini/settings.json` |

`dotfiles/.agent/hooks/` のフックスクリプトは `~/.claude/hooks/`・`~/.codex/hooks/`・`~/.gemini/hooks/` にシンボリックリンクされます。

### Jupyter Notebook（jupytext）

トークン消費を抑えるため、AI ツールは `.py` ファイルのみを編集する構成にしています。ファイル編集のたびにフックで `jupytext --sync` が自動実行され、ペアリングされた `.ipynb` に反映されます。

新規ノートブックをペアリングする場合:

```bash
jupytext --set-formats ipynb,py:percent notebook.py
```

## API キーの管理

ローカルの秘密情報は `~/.config/shell/secrets.env`（gitignore 済み）で管理します。

初回セットアップ時に `config/shell/secrets.env.example` から自動生成されます。値を入力してシェルを再起動してください。

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
```
