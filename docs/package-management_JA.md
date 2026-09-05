# パッケージ管理と移行

[English](package-management.md) · [ドキュメント一覧](README_JA.md)

パッケージ管理の主経路は Nix です。Nix にまだ移せない macOS パッケージだけを
Homebrew の宣言的な fallback として残します。Mac App Store アプリは、別の
best-effort 手順で導入します。

Macで `features.macos` を `false` にすると、`full` や `--with-gui-apps` より優先し、
GUIパッケージやHomebrew・Mac App Storeの導入、Touch ID設定のバックアップを停止します。
CLIパッケージは維持します。詳しくは[Mac用の追加機能をOFFにする](configuration-ownership_JA.md#mac用の追加機能をoffにする)を参照してください。

## パッケージを記録するファイル

- CLI package 名: `config/nix/package-names.nix`
- 共通 GUI package 名: `config/nix/gui-common-package-names.nix`
- macOS GUI package 名: `config/nix/gui-macos-package-names.nix`
- Linux GUI package 名: `config/nix/gui-linux-package-names.nix`
- 生成済み Homebrew fallback: `config/nix/homebrew-fallback.nix`
- 未移行 Homebrew report: `config/nix/unmapped-homebrew.tsv`
- Mac App Store アプリ: `config/nix/mas-apps.nix`

Nix module は `config/nix/darwin/` と `config/nix/home-manager/` に分けています。
詳しいファイル対応と focused test は、
[Nix ディレクトリの README](../config/nix/README_JA.md)を参照してください。

## Nix をビルド・適用する

```sh
# CLI 構成を切り替えずにビルド
zsh scripts/nix_install.sh --cli-only --dry-run
# または
mise run nix-build

# CLI profile を適用
zsh scripts/nix_install.sh --cli-only
# または
mise run nix-apply

# GUI アプリも適用
zsh scripts/nix_install.sh --with-gui-apps
# または
mise run nix-apply-with-gui-apps
```

flake は Darwin / Linux の aarch64 と x86_64 に対し、`full` と `cli` の output を
用意しています。macOS の初回適用で `darwin-rebuild` が `PATH` にない場合は、
`scripts/nix_install.sh` が flake 内のコマンドを使います。Linux の
`home-manager` も同様です。

`features.macos` が `true`（既定値）の macOS 初回適用では、nix-darwin が管理を引き継ぐ前に、既存の
`/etc/pam.d/sudo_local` を `/etc/pam.d/sudo_local.before-nix-darwin` へ
バックアップします。

## Homebrew の状態を移行する

commit 済みの `.Brewfile` は source of truth ではありません。現在の Homebrew
状態を直接移行するか、別マシンから export した Brewfile を明示します。

```sh
# 既定は書き込まない dry-run
zsh scripts/migrate_brew_to_nix.sh

# 現在の Homebrew 状態から package list と report を再生成
zsh scripts/migrate_brew_to_nix.sh --apply

# 別マシンから export した Brewfile を移行
zsh scripts/migrate_brew_to_nix.sh \
  --brewfile /path/to/Brewfile \
  --apply
```

Brewfile を省略すると、スクリプトは `brew bundle dump` で一時ファイルを作り、
移行後に削除します。Mac App Store entry は `mas-to-nix.tsv`、次に
`mas-to-cask.tsv` と照合し、どちらにもないアプリを `mas-apps.nix` に書きます。

## Fallback の挙動を明示したまま保つ

`features.macos` が `true`（既定値）で `homebrew-fallback.nix` に entry がある間は、
その formula、cask、tap、VS Code extension のために Homebrew が必要です。formula は
CLI profile でも適用します。cask と VS Code extension は `--with-gui-apps` の場合だけ
対象になります。

Mac App Store アプリは nix-darwin の `homebrew.masApps` に渡しません。取得できない
アプリが1件あるだけで、`brew bundle` activation 全体が失敗するためです。代わりに
`scripts/install_mas_apps.sh` が個別の失敗を報告し、setup 全体は継続します。
`features.macos` が `true` の場合は Mac App Store へのサインインが必要です。また、
`mas-apps.nix` からアプリを削除しても uninstall はされません。

## Fallback が空になってから Homebrew を削除する

Homebrew の削除は破壊的操作です。先に実行内容を確認してください。

```sh
zsh scripts/remove_homebrew.sh --dry-run
zsh scripts/remove_homebrew.sh --apply --confirm-nix-ready
```

fallback entry が残っている場合、apply command は処理を拒否します。
`zsh scripts/nix_install.sh --uninstall-homebrew` は、選択した Nix switch が成功した
後に限り、同じ削除処理を実行します。
