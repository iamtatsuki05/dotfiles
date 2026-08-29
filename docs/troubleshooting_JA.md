# トラブルシューティング

[English](troubleshooting.md) · [ドキュメント一覧](README_JA.md)

最初に、失敗したコマンドのエラー全体と `--help` を確認してください。この
ページには、read-only の診断と、package、file、credential を変更する復旧コマンドが
混在します。変更を伴う手順には明記しているため、実行前に内容を確認してください。

## `main.sh` で Nix 導入後に処理を継続できない

初回導入では、現在の shell の `PATH` がまだ更新されていない場合があります。
terminal を再起動するか、エラーに表示された Nix daemon profile を source して、
`zsh main.sh` を再実行してください。Nix 自体が使える状態なら、script は flake 内の
`darwin-rebuild` または `home-manager` を利用できます。

## Linux で sudo が使えない、または `/nix` を mount できない

通常の installer ではなく nix-portable を使います。

```sh
zsh scripts/nix_portable_install.sh
export PATH="$HOME/.local/bin:$PATH"
nixp --version
dotfiles-nix-shell
```

既定の `proot` runtime は、mount namespace が制限された host を想定しています。
Linux の package path を暗黙に Homebrew へ切り替えないでください。

## Linux で `nix_install.sh --with-gui-apps` が失敗する

Linux の GUI application setup には `DISPLAY` または `WAYLAND_DISPLAY` が必要です。
headless host では `--cli-only` を使います。

```sh
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

## Nix から Homebrew が必要だと表示される

`config/nix/homebrew-fallback.nix` を確認します。formula fallback は CLI profile にも
影響します。cask と VS Code extension は GUI application の適用時に影響します。
entry が意図したものなら、選択 profile 用の Homebrew を導入します。

1つ目のコマンドは installer の preview です。2つ目は Homebrew を導入し、マシンの
状態を変更します。

```sh
zsh scripts/install_homebrew.sh --profile full --dry-run
zsh scripts/install_homebrew.sh --profile full
```

不要な entry であれば、check を迂回せず、canonical package config から移行または
削除してください。

## Chezmoi の verify で drift が見つかる

適用前に変更内容を確認します。

```sh
zsh scripts/chezmoi_apply.sh --dry-run
```

展開済みファイルに残すべきローカル変更がある場合は、先に `home/` または
`config/` 内の source へ戻します。その後、source-state test と適用を実行します。

```sh
zsh tests/test_chezmoi_source_state.sh
zsh scripts/chezmoi_apply.sh --mark-default
zsh scripts/chezmoi_apply.sh --verify
```

## Git pull 後に新しいパッケージが適用されない

想定された挙動です。pull hook が行うのは chezmoi file の適用、agent file の同期、
hook の更新です。Nix switch や mise tool の導入は実行しません。flake または Nix
設定が変わった場合は、次を実行します。

```sh
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

## Mac App Store アプリが skip される、または個別に失敗する

Mac App Store にサインインしているか、現在のアカウントで対象アプリを取得できるか
確認します。installer は意図的に各アプリを best-effort で処理するため、1件の
失敗で setup 全体は失敗しません。アカウント状態を直した後、対象スクリプトを
再実行します。

次のコマンドはアプリをインストールし、マシンの状態を変更します。

```sh
zsh scripts/install_mas_apps.sh --profile full
```

## Agent sync は成功したが、client の挙動が古い

最初に canonical file と展開済み file を確認します。

`sync.sh` は managed symlink、setting、ローカルの agent env file を更新します。
read-only の test を実行する前に、ローカルの agent 設定を変更します。

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
```

その後、必要に応じて対象 client を再起動または reload します。file sync の成功は、
起動中 process が prompt、hook、MCP setting、skill を再読込した証拠ではありません。

## Claude の profile 切替が拒否される

共有 Keychain credential を変更する前に、Claude process を一覧し、すべての
session を終了します。

`claude-account auth-login` は、browser 認証が成功した後に共有 macOS Keychain
login を変更します。

```sh
pgrep -fl claude
claude-account auth-login <profile>
```

関連のない process を自動で kill しないでください。active session が状態を保存
できるよう、通常の手順で閉じます。共有 login と既存 profile mapping が一致しない
場合は、その profile を最初に登録したアカウントで認証してください。command は
既存 mapping を上書きせず保持します。

## 全体テストで1件だけ skip される

chezmoi が利用できない場合、ローカル runner は rendered-home integration test だけを
skip することがあります。cross-platform の apply path を完全に検証したと判断する
前に、chezmoi を導入するか CI で実行してください。それ以外の失敗は想定された
skip ではありません。最初のエラーから原因を調べます。
