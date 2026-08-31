# 設定の管理境界

[English](configuration-ownership.md) · [ドキュメント一覧](README_JA.md)

このリポジトリ内の canonical source を編集し、対応する方法で適用します。
`$HOME` に展開済みのファイルだけを編集すると drift になり、次回の適用で
上書きされる可能性があります。

## 管理対象と適用経路

| 対象 | 管理上の定義 | 適用経路 |
|---|---|---|
| パッケージと宣言的な shell 設定 | `flake.nix`、`config/nix/` | nix-darwin / Home Manager |
| terminal、shell adapter、bash、mise、ローカル template | `config/` と `home/` の生成済み source | chezmoi |
| `$HOME` へ直接展開するファイル | `home/` | chezmoi |
| AI agent の prompt、設定、hook、skill | `dotfiles/.agent/` | `dotfiles/.agent/sync.sh` |
| chezmoi 外の repo-level runtime asset | `dotfiles/` | 対象別スクリプト |
| tool version と task command | `config/mise/config.toml` | mise |
| Nix にない macOS package | 生成される `config/nix/homebrew-fallback.nix` | nix-darwin / Homebrew |
| Mac App Store アプリ | `config/nix/mas-apps.nix` | `scripts/install_mas_apps.sh` |

## Shell の役割と起動境界

対話 shell の役割は、意図的に限定しています。

- ローカルの対話作業は zsh を使います。
- server の対話 session は Bash を使います。
- 新しく portable script を追加するときは Bash で実装し、Bash に対応する
  shebang を付けます。既存 script は、宣言済みの shebang と shell 固有の挙動を
  保ちます。これは全 script を Bash へ移行する方針でも、shell 間の完全な同等性を
  保証する方針でもありません。

chezmoi は canonical shell template から、Bash/Zsh 共通の環境変数、PATH、safe alias、
`DOTFILES_REPO_ROOT` を render します。Fish には
`~/.config/fish/conf.d/zz-dotfiles.fish` の任意の adapter があり、共通する非 secret の
環境変数と PATH、interactive な safe alias を提供します。interactive な Fish session
では、公式の `mise activate fish` hook も source します。csh/tcsh には
`~/.config/shell/dotfiles-shell-common.csh` の standalone adapter があり、限定した
非 secret の環境変数、PATH、prompt 内の safe alias を提供しますが、activation は
提供しません。どちらの adapter も `DOTFILES_REPO_ROOT`、zsh の UI 全体、
`secrets.env` は提供しません。

Fish の公式 `mise activate fish` hook は interactive な Fish session でだけ実行します。
generator または source が失敗した場合は、隠さず明示的な failure として扱います。
暗黙の fallback にはしません。csh/tcsh の activation は未対応です。これらの shell
では mise shim、`mise exec`、`mise run` を使ってください。

csh/tcsh の adapter は standalone で、既存の起動 file には追加しません。既存の
`.cshrc` または `.tcshrc` から、次のように手動で opt-in します。

```csh
if (-r "$HOME/.config/shell/dotfiles-shell-common.csh") source "$HOME/.config/shell/dotfiles-shell-common.csh"
```

verification matrix は、PATH 上または cached Nix runtime に Fish binary がある場合に、
Fish runtime の required check を実行します。check が成功すれば `PASS` です。binary が
ない場合だけ `not-applicable` の `SKIP` とし、存在する Fish runtime の check または
activation が失敗した場合は `FAIL` とします。genuine csh の実装は別の未検証境界であり、
`PASS` として報告しません。`not-applicable` の `SKIP` を runtime の `PASS` とみなさないで
ください。

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

chezmoi の shell adapter policy は `home/.chezmoidata.toml` に分けて宣言します。
Fish の activation は `interactive-only`、csh/tcsh は
`unsupported-activation` です。shim の優先順位
`MISE_DATA_DIR`、`XDG_DATA_HOME`、`HOME` は csh/tcsh の standalone adapter だけに
適用します。Fish は公式 activation hook に委譲し、この list を使いません。この
policy は shell integration 用のデータであり、csh/tcsh の activation を追加したり、
`config/mise/config.toml` の tool version と task alias を置き換えたりするものでは
ありません。

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

sync test は managed link を通じて canonical prompt 全体を検証します。個別の shell
policy 文言を test に重複して固定せず、`AGENTS.md` を更新してください。

対応範囲と検証方法は[AI エージェント設定](ai-agents_JA.md)を参照してください。
