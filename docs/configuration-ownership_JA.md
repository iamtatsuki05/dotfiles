# 設定の管理境界

[English](configuration-ownership.md) · [ドキュメント一覧](README_JA.md)

このリポジトリ内の canonical source を編集し、対応する方法で適用します。
`$HOME` に展開済みのファイルだけを編集すると drift になり、次回の適用で
上書きされる可能性があります。

## 管理対象と適用経路

| 対象 | 管理上の定義 | 適用経路 |
|---|---|---|
| パッケージと宣言的な shell 設定 | `flake.nix`、`config/nix/` | nix-darwin / Home Manager |
| terminal、bash、mise、ローカル template | `config/` と `home/` の生成済み source | chezmoi |
| `$HOME` へ直接展開するファイル | `home/` | chezmoi |
| AI agent の prompt、設定、hook、skill | `dotfiles/.agent/` | `dotfiles/.agent/sync.sh` |
| chezmoi 外の repo-level runtime asset | `dotfiles/` | 対象別スクリプト |
| tool version と task command | `config/mise/config.toml` | mise |
| Nix にない macOS package | 生成される `config/nix/homebrew-fallback.nix` | nix-darwin / Homebrew |
| Mac App Store アプリ | `config/nix/mas-apps.nix` | `scripts/install_mas_apps.sh` |

## パッケージと宣言的なシェル設定は Nix が管理する

macOS は nix-darwin と Home Manager、Linux は Home Manager を使い、同じ
パッケージ一覧を共有します。zsh と Neovim は `config/nix/home-manager/`、
macOS defaults と定期更新 agent は `config/nix/darwin/` で宣言します。

macOS defaults には keyboard repeat、sudo Touch ID、スクリーンショット保存先
`${HOME}/SS` が含まれます。この個人設定を別の Mac へ適用する前に、
`config/nix/darwin/defaults.nix` を確認してください。

`flake.nix`、`flake.lock`、`config/nix/` を変更した場合は、この層を適用します。

```sh
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

GUI パッケージも対象にするホストでは `--with-gui-apps` を使います。

## ホームへ展開するファイルは chezmoi が管理する

リポジトリルートの `.chezmoiroot` は `home/` を指します。`dot_*` と
`private_dot_config/` 配下のファイルは、対応する `$HOME` 配下へ展開されます。

一部の source は `config/` にあり、chezmoi template に反映されます。両者の
整合を保ってください。`tests/test_chezmoi_source_state.sh` が drift を検出します。

```sh
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default
zsh scripts/chezmoi_apply.sh --verify
```

`--mark-default` は `~/.config/dotfiles/manager` に `chezmoi` を記録し、選択した
プロファイルを `~/.config/dotfiles/profile` に保存します。

## Tool version と task alias は mise が管理する

`config/mise/config.toml` に mise 管理の tool と task alias を宣言しています。
更新するときは、目的に合う最小の task を選びます。`node@22` のように
major release line 自体を変える場合は、一括更新ではなく設定変更が必要です。

## Homebrew と MAS は限定的な fallback として使う

パッケージの優先順位は `Nix > Homebrew > MAS` です。Homebrew は、まだ Nix へ
移せない entry だけに使います。Mac App Store アプリは、1件の取得失敗で Nix
activation 全体が失敗しないよう、別のスクリプトで導入します。

fallback の一覧変更や Homebrew の削除前に、
[パッケージ管理と移行](package-management_JA.md)を確認してください。

## 共有 agent ファイルには別の同期手順がある

共通 prompt の canonical source は `dotfiles/.agent/AGENTS.md` です。アプリ別設定、
hook、skill、eval も同じ tree で管理します。リポジトリルートには、意図的に
`AGENTS.md` symlink を置いていません。

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
```

対応範囲と検証方法は[AI エージェント設定](ai-agents_JA.md)を参照してください。
