# dotfiles

macOS / Linux 向けの dotfiles セットアップ手順です。

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

## リポジトリ構成

| Path | 用途 |
|---|---|
| [`.github/`](.github/README_JA.md) | GitHub Actions と repository automation。 |
| [`config/`](config/README_JA.md) | Nix、terminal、shell template、mise などの設定 source。 |
| [`config/nix/`](config/nix/README_JA.md) | Nix package list、nix-darwin module、Home Manager module、migration report。 |
| [`home/`](home/README_JA.md) | `$HOME` に render される chezmoi source state。 |
| [`scripts/`](scripts/README_JA.md) | setup、migration、update、sync、eval helper script。 |
| [`tests/`](tests/README_JA.md) | local / CI check。 |
| [`dotfiles/`](dotfiles/README_JA.md) | 通常の chezmoi source tree 外で管理する dotfile と runtime asset。 |
| [`dotfiles/.agent/`](dotfiles/.agent/README_JA.md) | 共有 AI agent prompt、config、hook、skill、eval、pet asset。 |

## セットアッププロファイル

`main.sh` は OS に応じて既定プロファイルを切り替えます。

- macOS: `full`
- Linux: `cli`

`full` は macOS 向けの全体セットアップです。nix-darwin、Home Manager、GUI アプリ、macOS defaults、launchd / systemd user timer、設定ファイル、mise、Neovim をセットアップします。
この profile で Homebrew fallback entry が残っている場合、`main.sh` は Nix 適用前に Homebrew も自動で導入します。

`cli` は Ubuntu などでも使いやすい CLI 中心のセットアップです。GUI アプリ、macOS 専用ツール、macOS defaults、launchd / systemd user timer は実行せず、Nix の CLI package set だけを適用します。

```sh
# Ubuntu / Linux や CLI だけを入れたい macOS
zsh main.sh --cli-only

# Nix/Home Manager の CLI パッケージだけを入れたい場合
zsh scripts/nix_install.sh --cli-only

# Homebrew だけ先に入れたい場合
zsh scripts/install_homebrew.sh --profile full
```

## 既存 clone の追尾

すでにこのリポジトリを clone 済みの別 PC では、まずリポジトリを更新し、その後に chezmoi と Nix を明示的に適用します。

```sh
cd ~/src/dotfiles
git pull --ff-only

# chezmoi 管理のホームファイルを確認して適用
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default

# Nix / Home Manager / nix-darwin の CLI 構成を確認して適用
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

GUI app も Nix で更新したい macOS / Linux desktop では次を明示します。

```sh
zsh scripts/nix_install.sh --with-gui-apps
```

`flake.nix`、`flake.lock`、`config/nix/` 配下が変わったときは、`scripts/nix_install.sh` を実行してください。Git pull hook は `chezmoi apply`、AI ツール設定の同期、hook の更新だけを行い、Nix の switch や `mise` tool install は実行しません。

Nix + chezmoi への移行後は、zsh と Neovim は Home Manager 管理、ターミナル設定・bash 起動ファイル・`mise` config・ローカル secret 雛形は chezmoi 管理です。そのため、追尾時は `chezmoi_apply.sh` と `nix_install.sh` の両方を流すのが基本です。

## chezmoi のホームファイル

このリポジトリには chezmoi の source state として `home/` を置いています。`.chezmoiroot` は `home` を指します。ターミナル設定、bash 起動ファイル、`mise` config、ローカル secret の雛形など、直接コピーまたはテンプレート展開するホーム配下のファイルをここで管理します。zsh や Neovim など Home Manager で宣言した方が自然なものは `config/nix/home-manager/` で管理します。

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

`--mark-default` は `~/.config/dotfiles/manager` に `chezmoi` を書き込み、選択した profile を `~/.config/dotfiles/profile` に保存します。このリポジトリの Git pull hook は `chezmoi apply` を実行します。従来のコピー方式へのフォールバックはありません。

## dotfiles のテスト

設定の検証は [tests/run.sh](tests/run.sh) にまとめています。[scripts/test_dotfiles.sh](scripts/test_dotfiles.sh) は互換用 wrapper として残しています。

```sh
zsh tests/run.sh
# または
mise run test-dotfiles
```

この runner は zsh の構文、補助スクリプト、生成済み chezmoi source state の drift、chezmoi による一時 HOME への展開を確認します。ローカルに `chezmoi` が無い場合は、展開テストだけ skip します。

GitHub Actions では `ubuntu-latest` と `macos-latest` の両方で同じ検証を実行します。CI では `chezmoi` もインストールし、両 OS で source state が一時 HOME に適用できることを確認します。

## Nix package migration

Homebrew は主経路から外し、macOS は nix-darwin + Home Manager、Linux は Home Manager で同じ package set を適用します。CLI package は [config/nix/package-names.nix](config/nix/package-names.nix)、GUI app は common / macOS / Linux に分けた [config/nix/gui-common-package-names.nix](config/nix/gui-common-package-names.nix)、[config/nix/gui-macos-package-names.nix](config/nix/gui-macos-package-names.nix)、[config/nix/gui-linux-package-names.nix](config/nix/gui-linux-package-names.nix) で管理します。Mac App Store app は [config/nix/mas-apps.nix](config/nix/mas-apps.nix) に一覧化しますが、個別の App Store 失敗で setup 全体が落ちないよう nix-darwin の Homebrew activation からは分離して導入します。Nix に移せない Homebrew entry は [config/nix/unmapped-homebrew.tsv](config/nix/unmapped-homebrew.tsv) に残し、macOS で nix-darwin が管理する fallback は [config/nix/homebrew-fallback.nix](config/nix/homebrew-fallback.nix) に生成します。

Nix module は [config/nix/darwin](config/nix/darwin) と [config/nix/home-manager](config/nix/home-manager) に分け、`default.nix` から責務別の小さな module を import します。macOS の keyboard repeat、スクリーンショット保存先、sudo の Touch ID は [config/nix/darwin/defaults.nix](config/nix/darwin/defaults.nix) で管理します。スクリーンショットは `${HOME}/SS` に保存します。初回の macOS 適用では、[scripts/nix_install.sh](scripts/nix_install.sh) が既存の `/etc/pam.d/sudo_local` を `/etc/pam.d/sudo_local.before-nix-darwin` へ自動退避してから activation を進めます。`defaults write` のような場当たり的な直接変更には依存しません。

app の登録優先度は `Nix > Homebrew > MAS` です。Brewfile 移行時、Mac App Store entry はまず [config/nix/mas-to-nix.tsv](config/nix/mas-to-nix.tsv)、次に [config/nix/mas-to-cask.tsv](config/nix/mas-to-cask.tsv) に照合し、どちらにも無いものだけ [config/nix/mas-apps.nix](config/nix/mas-apps.nix) に書き込みます。

```sh
# 現在の Homebrew 状態から Nix package list と未移行レポートを再生成
zsh scripts/migrate_brew_to_nix.sh --apply
# 他 PC で export した Brewfile を指定して移行
zsh scripts/migrate_brew_to_nix.sh --brewfile /path/to/Brewfile --apply
# または
mise run nix-migrate-brew

# CLI 構成の build dry-run
zsh scripts/nix_install.sh --cli-only --dry-run
# または
mise run nix-build

# CLI 構成を macOS では nix-darwin、Linux では Home Manager で適用
zsh scripts/nix_install.sh --cli-only
# または
mise run nix-apply

# GUI app も入れる
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

更新は重さに応じて分けて使えます。

```bash
# flake.lock だけ更新
mise run lock-update

# flake.lock の nixpkgs だけ更新
mise run lock-update-nixpkgs

# Nix 管理の tool だけ更新して適用
mise run nix-update

# nixpkgs を更新して Nix package set を適用
mise run nixpkgs-update

# mise 管理の tool だけ現在の release line 内で更新
mise run mise-update

# 全部まとめて更新
mise run package-update

# helper script を bash で動かしたい場合
mise run package-update -- --shell bash

# Homebrew 管理の GUI fallback app も更新したい場合
mise run package-update -- --with-gui-apps
```

`mise run package-update` は `nix flake update`、`scripts/nix_install.sh`、`mise` config 同期、`mise upgrade` をまとめて実行します。macOS で Homebrew 管理の GUI fallback app が定義されている場合、既定では CLI Nix profile を適用し、GUI fallback app の更新は行いません。GUI fallback app も更新したいときだけ `--with-gui-apps` を明示してください。`--with-gui-apps` 付きでは `brew update` も実行し、[config/nix/homebrew-fallback.nix](config/nix/homebrew-fallback.nix) に宣言された Homebrew formula / cask を upgrade します。重いので、通常は Nix 管理の tool だけなら `mise run nix-update`、`nixpkgs` だけ触りたいなら `mise run nixpkgs-update`、`mise` 管理の tool だけなら `mise run mise-update` を使ってください。旧名の `nix-mise-upgrade`、`nix-upgrade`、`nixpkgs-upgrade`、`mise-upgrade` などは alias として残しています。`codex`、`claude-code`、`copilot`、`cursor-agent`、`hermes`、`opencode`、`devin` などの AI CLI は `mise` 管理です。Antigravity CLI は Homebrew Cask `antigravity` として GUI package set で管理し、`agy` binary もそこから提供されます。`node@22` のように major line 自体を上げたい場合は、先に `config/mise/config.toml` を明示的に変更してください。
この script は記事の `nix flake lock --update-input ...` 方式に寄せており、`nixpkgs` / `home-manager` / `nix-darwin` を個別更新できます。実行中は段階ベースの progress bar を出すので、今どのフェーズか分かります。
macOS で Homebrew が未導入でも、GUI fallback entry だけが残っている場合は、この task は CLI Nix profile にフォールバックして Nix 管理の CLI tool を更新します。

[config/nix/homebrew-fallback.nix](config/nix/homebrew-fallback.nix) に entry がある間は、macOS の fallback formula、cask、tap、VS Code extension のために Homebrew が必要です。formula は CLI profile でも適用し、cask と VS Code extension は `--with-gui-apps` の時だけ適用します。Mac App Store app は `scripts/install_mas_apps.sh` で別に扱います。Homebrew fallback entry が空で、Nix 適用後に問題なければ Homebrew は明示的に削除できます。これは破壊的操作なので dry-run でコマンドを確認してから実行します。

Mac App Store app は nix-darwin の `homebrew.masApps` には渡しません。現在の App Store アカウントで download できない app が 1 つでもあると `brew bundle` が activation 全体を失敗させるためです。代わりに `main.sh` が Nix activation 後に [scripts/install_mas_apps.sh](scripts/install_mas_apps.sh) を実行し、各 app を best-effort で導入します。個別の失敗は警告として表示しますが、setup 全体は失敗させません。key は app 名、value は App Store の ADAM ID です。

```nix
{
  "Xcode" = 497799835;
}
```

macOS GUI package は入れつつ Mac App Store app だけを skip したい場合は、次を使います。

```sh
zsh main.sh --full --skip-mas-apps
```

Mac App Store にサインインしている必要があります。また Homebrew Bundle の制限により、`mas-apps.nix` から削除しても app は自動 uninstall されません。

```sh
zsh scripts/remove_homebrew.sh --dry-run
zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready
```

`zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready` は fallback entry が残っている場合は削除を拒否します。`zsh scripts/nix_install.sh --uninstall-homebrew` を使うと、Nix 適用が成功した後に同じ削除処理を実行します。

古い Nix generation や Homebrew cache をまとめて掃除したい場合:

```sh
mise run package-cleanup
mise run package-cleanup -- --apply
mise run package-cleanup -- --include-mise
mise run package-cleanup -- --include-mise --apply
```

この task は `nix profile wipe-history --profile <user-profile> --older-than 30d`、`nix store gc`、`nix store optimise`、`brew cleanup --prune=all --scrub` を順に実行します。既定は dry-run です。古い generation を消すと rollback の履歴が減るためです。`--include-mise` を付けた場合だけ、未使用の mise tool version を消す `mise prune --tools` と stale cache を消す `mise cache prune` も実行します。旧名の `mise run nix-brew-cleanup` も alias として残しています。

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

hook は [scripts/apply_updates.sh](scripts/apply_updates.sh) を呼び、chezmoi source state、AI ツール設定、hook 自体を同期します。nix-darwin / Home Manager の switch、Homebrew の uninstall、mise tool install は自動実行しません。

手動で再インストールする場合:

```sh
zsh scripts/setup_git_hooks.sh
```

## AI agent 設定

AI agent 関連ファイルは [dotfiles/.agent](dotfiles/.agent) にまとめています。共通 prompt は `dotfiles/.agent/AGENTS.md` で管理し、リポジトリルートには `AGENTS.md` symlink を置きません。

管理対象の CLI agent は `codex`、`claude-code`、`copilot`、`cursor-agent`、`devin`、`antigravity-cli`、`hermes`、`opencode`、`openclaw`、`grok`、`agent-swarm` です。CLI 本体は可能な範囲で `mise` から導入し、agent 別設定・MCP・hooks・skills・Waza eval suite は `dotfiles/.agent/` で管理します。Antigravity CLI は Homebrew Cask `antigravity-cli` として管理し、`agy` binary もそこから提供されます。Agent Swarm の localhost MCP は常時有効化せず、`dotfiles/.agent/apps/agent-swarm/` のテンプレートから必要な project/client にだけ入れます。

```bash
zsh dotfiles/.agent/sync.sh
mise run waza-eval-model -- --agent all --dry-run
```

ファイル対応表、同期内容、ignore、hooks、Waza 評価コマンドは [dotfiles/.agent/README_JA.md](dotfiles/.agent/README_JA.md) を参照してください。

## API キーの管理

ローカルの秘密情報は `~/.config/shell/secrets.env`（gitignore 済み）で管理します。

初回セットアップ時に chezmoi が `config/shell/secrets.env.example` から自動生成します。値を入力してシェルを再起動してください。

chezmoi は `config/shell/bashrc.tmpl` と `config/shell/bash_profile.tmpl` を `~/.bashrc` と `~/.bash_profile` に生成し、`config/shell/dotfiles-shell-common.tmpl` を `~/.config/shell/dotfiles-shell-common.sh` に生成します。Home Manager の zsh 設定も、この共通 file が存在する場合は source します。

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
export GEMINI_API_KEY=""
export GITHUB_TOKEN=""
export DEVIN_API_KEY=""
```
