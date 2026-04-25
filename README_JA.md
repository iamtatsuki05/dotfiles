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

`full` は macOS 向けの全体セットアップです。nix-darwin、Home Manager、GUI アプリ、macOS defaults、launchd / systemd user timer、設定ファイル、mise、Neovim をセットアップします。

`cli` は Ubuntu などでも使いやすい CLI 中心のセットアップです。GUI アプリ、macOS 専用ツール、macOS defaults、launchd / systemd user timer は実行せず、Nix の CLI package set だけを適用します。

```sh
# Ubuntu / Linux や CLI だけを入れたい macOS
zsh main.sh --cli-only

# Nix/Home Manager の CLI パッケージだけを入れたい場合
zsh scripts/nix_install.sh --cli-only
```

## chezmoi への移行

このリポジトリには chezmoi の source state として `home/` を追加しています。`.chezmoiroot` は `home` を指します。既存の `dotfiles/` と `config/` は今の setup scripts の source of truth として残しているので、段階的に移行できます。

chezmoi source state を生成・更新します。

```sh
zsh scripts/migrate_to_chezmoi.sh --dry-run
zsh scripts/migrate_to_chezmoi.sh --apply
# または
mise run chezmoi-migrate
```

`chezmoi` 本体を任意の方法でインストールしてから、適用内容を確認して反映します。

```sh
sh -c "$(curl -fsLS get.chezmoi.io)" -- -b "$HOME/.local/bin"
# または: nix run nixpkgs#chezmoi -- init
# または: mise use --global chezmoi@latest

zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default
# macOS で CLI-only にしたい場合:
zsh scripts/chezmoi_apply.sh --cli-only --mark-default
# または
mise run chezmoi-diff
mise run chezmoi-apply
```

`--mark-default` は `~/.config/dotfiles/manager` に `chezmoi` を書き込み、選択した profile を `~/.config/dotfiles/profile` に保存します。その後、このリポジトリの Git pull hook は `chezmoi` が使える場合に `chezmoi apply` を実行し、使えない場合は従来のコピー方式へフォールバックします。

## dotfiles のテスト

設定の検証は [scripts/test_dotfiles.sh](scripts/test_dotfiles.sh) にまとめています。

```sh
zsh scripts/test_dotfiles.sh
# または
mise run test-dotfiles
```

この runner は zsh の構文、移行ヘルパー、生成済み chezmoi source state の drift、chezmoi による一時 HOME への展開を確認します。ローカルに `chezmoi` が無い場合は、展開テストだけ skip します。

GitHub Actions では `ubuntu-latest` と `macos-latest` の両方で同じ検証を実行します。CI では `chezmoi` もインストールし、両 OS で source state が一時 HOME に適用できることを確認します。

## Nix package migration

Homebrew は主経路から外し、macOS は nix-darwin + Home Manager、Linux は Home Manager で同じ package set を適用します。CLI package は [config/nix/package-names.nix](config/nix/package-names.nix)、GUI app は common / macOS / Linux に分けた [config/nix/gui-common-package-names.nix](config/nix/gui-common-package-names.nix)、[config/nix/gui-macos-package-names.nix](config/nix/gui-macos-package-names.nix)、[config/nix/gui-linux-package-names.nix](config/nix/gui-linux-package-names.nix) で管理します。Mac App Store app は macOS だけ [config/nix/mas-apps.nix](config/nix/mas-apps.nix) で管理します。Nix に移せない Homebrew entry は [config/nix/unmapped-homebrew.tsv](config/nix/unmapped-homebrew.tsv) に残し、macOS で nix-darwin が管理する fallback は [config/nix/homebrew-fallback.nix](config/nix/homebrew-fallback.nix) に生成します。

Nix module は [config/nix/darwin](config/nix/darwin) と [config/nix/home-manager](config/nix/home-manager) に分け、`default.nix` から責務別の小さな module を import します。macOS の keyboard repeat、スクリーンショット保存先、sudo の Touch ID は [config/nix/darwin/defaults.nix](config/nix/darwin/defaults.nix) で管理します。スクリーンショットは `${HOME}/SS` に保存します。`/etc/pam.d/sudo_local` や `defaults write` の直接変更は行いません。

app の登録優先度は `Nix > Homebrew > MAS` です。Brewfile 移行時、Mac App Store entry はまず [config/nix/mas-to-nix.tsv](config/nix/mas-to-nix.tsv)、次に [config/nix/mas-to-cask.tsv](config/nix/mas-to-cask.tsv) に照合し、どちらにも無いものだけ [config/nix/mas-apps.nix](config/nix/mas-apps.nix) に書き込みます。

```sh
# 現在の Homebrew 状態から Nix package list と未移行レポートを再生成
zsh scripts/migrate_brew_to_nix.sh --apply
# 他 PC で export した Brewfile を指定して移行
zsh scripts/migrate_brew_to_nix.sh --brewfile /path/to/Brewfile --apply
# または
mise run nix-migrate-brew

# Nix 構成の build dry-run
zsh scripts/nix_install.sh --dry-run
# または
mise run nix-build

# macOS では nix-darwin、Linux では Home Manager で適用
zsh scripts/nix_install.sh
# または
mise run nix-apply

# CLI だけを適用
zsh scripts/nix_install.sh --cli-only
# または
mise run nix-apply-cli

# Ubuntu など GUI が使える Linux でも Slack 等の GUI app を入れる
zsh scripts/nix_install.sh --with-gui-apps
# または
mise run nix-apply-with-gui-apps
```

macOS の初回適用では `darwin-rebuild` がまだ PATH に無いことがあります。その場合も [scripts/nix_install.sh](scripts/nix_install.sh) が flake 内の `darwin-rebuild` を `nix run` で呼びます。Linux では `home-manager` が無ければ flake 内の `home-manager` を使います。

sudo が使えない Linux では、Homebrew ではなく `nix-portable` を主経路にします。`nix-portable` は `${HOME}/.nix-portable/store` を仮想的な `/nix/store` として扱うため、Nix 由来の package は `nixp`、`dotfiles-nix-shell`、`dotfiles-nix-run` 経由で使います。`pine11` のように mount namespace が制限された環境でも動くよう、既定 runtime は `proot` です。

```sh
zsh scripts/nix_portable_install.sh
nixp --version
dotfiles-nix-shell

# dotfiles の CLI package set 内でコマンドを実行
dotfiles-nix-run git --version
```

`nix-user-chroot` を使う [scripts/nix_rootless_install.sh](scripts/nix_rootless_install.sh) も残していますが、通常のログインシェルから `/nix/store` を直接参照できないため、sudo なし Linux の第一候補は [scripts/nix_portable_install.sh](scripts/nix_portable_install.sh) です。

`mise` と `Nix` の両方で管理しているものをまとめて最新化して適用するには、次を使います。

```bash
mise run nix-mise-upgrade

# helper script を bash で動かしたい場合
mise run nix-mise-upgrade -- --shell bash
```

この task は `config/mise/config.toml` を `mise upgrade --bump` で更新し、`home/.chezmoitemplates/mise-config.toml` と `~/.config/mise/config.toml` を同期したあと、`nix flake update` と `scripts/nix_install.sh` を順に実行します。

[config/nix/homebrew-fallback.nix](config/nix/homebrew-fallback.nix) または [config/nix/mas-apps.nix](config/nix/mas-apps.nix) に entry がある間は、macOS の fallback formula、cask、tap、VS Code extension、Mac App Store app のために Homebrew が必要です。formula は CLI profile でも適用し、cask、VS Code extension、Mac App Store app は `--with-gui-apps` の時だけ適用します。これらが空で、Nix 適用後に問題なければ Homebrew は明示的に削除できます。これは破壊的操作なので dry-run でコマンドを確認してから実行します。

Mac App Store app は nix-darwin の `homebrew.masApps` で管理します。key は app 名、value は App Store の ADAM ID です。

```nix
{
  "Xcode" = 497799835;
}
```

Mac App Store にサインインしている必要があります。また Homebrew Bundle の制限により、`mas-apps.nix` から削除しても app は自動 uninstall されません。

```sh
zsh scripts/remove_homebrew.sh --dry-run
zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready
```

`zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready` は fallback entry が残っている場合は削除を拒否します。`zsh scripts/nix_install.sh --uninstall-homebrew` を使うと、Nix 適用が成功した後に同じ削除処理を実行します。

## 他の Homebrew マシンからの移行

commit 済みの `.Brewfile` は使いません。まだ Homebrew が残っている古い macOS では、現在の Homebrew 状態から直接移行するか、export した Brewfile を明示的に渡します。

```sh
zsh scripts/migrate_brew_to_nix.sh --apply
zsh scripts/migrate_brew_to_nix.sh --brewfile /path/to/Brewfile --apply
```

`--brewfile` を省略した場合、script は `brew bundle dump` で一時 Brewfile を作り、Nix package list に移行した後、その一時ファイルを削除します。

## 定期更新

`full` profile では、macOS は nix-darwin の launchd agent、Linux は Home Manager の systemd user timer で `dotfiles-auto-update` を管理します。毎日 06:00 に `${HOME}/src/dotfiles` で `git pull --ff-only` を実行し、ログを `/tmp/dotfiles-git-pull.log` に出力します。macOS では、旧 `setup_cron.sh` が入れた managed cron block を nix-darwin activation 時に削除します。

## Git pull 後の自動同期

`main.sh` は Git hook を `.git/hooks/` にインストールします。

- `post-merge`: `git pull` / merge 後に実行
- `post-rewrite`: `git pull --rebase` 後に実行
- `post-checkout`: branch checkout 後に実行

hook は [scripts/apply_updates.sh](scripts/apply_updates.sh) を呼び、dotfiles、AI ツール設定、アプリ設定、hook 自体を同期します。nix-darwin / Home Manager の switch、Homebrew の uninstall、mise tool install は自動実行しません。

手動で再インストールする場合:

```sh
zsh scripts/setup_git_hooks.sh
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

同じ `scripts/setup_config.sh` で `config/shell/bashrc.tmpl` と `config/shell/bash_profile.tmpl` を `~/.bashrc` と `~/.bash_profile` に生成し、`config/shell/dotfiles-shell-common.tmpl` を `~/.config/shell/dotfiles-shell-common.sh` に生成します。`~/.bashrc` と `.zshrc` はこの共通 file を source し、`__DOTFILES_REPO_ROOT__` はそこで現在の clone path に合わせて置換します。

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
```
