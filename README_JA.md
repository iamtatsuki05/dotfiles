# dotfiles

[English](README.md)

macOS と Linux の環境を再現するための個人用 dotfiles です。パッケージと
宣言的なシェル設定は Nix、`$HOME` へ直接配置するファイルは chezmoi で
管理します。

## クイックスタート

macOS では、必要に応じて `main.sh` が Nix を導入します。Linux では先に Nix を
導入するか、[nix-portable の手順](docs/getting-started_JA.md#sudo-が使えない-linux)を
使ってください。

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

`main.sh` は macOS で `full`、Linux で `cli` を選びます。別のプロファイルを
使う場合や、制限のある Linux へ導入する場合は、先に
[導入ガイド](docs/getting-started_JA.md)を確認してください。

## ドキュメント

- [導入ガイド](docs/getting-started_JA.md): 前提条件、プロファイル、初回導入、
  導入後の確認。
- [設定の管理境界](docs/configuration-ownership_JA.md): Nix、chezmoi、mise、
  Homebrew、リポジトリがそれぞれ管理するもの。
- [日常のメンテナンス](docs/maintenance_JA.md): 既存 clone の更新、テスト、
  自動同期、キャッシュ整理。
- [パッケージ管理と移行](docs/package-management_JA.md): Nix のパッケージ一覧、
  Homebrew と Mac App Store の fallback、移行コマンド。
- [AI エージェント設定](docs/ai-agents_JA.md): 共通 prompt、アプリ別設定、同期、
  評価、Claude Code のプロファイル。
- [秘密情報と安全な操作](docs/secrets-and-safety_JA.md): ローカル認証情報、
  破壊的操作、バックアップ、dry-run の使い方。
- [トラブルシューティング](docs/troubleshooting_JA.md): セットアップ、drift、Nix、
  Homebrew、エージェント同期で起きやすい問題。

[ドキュメント一覧](docs/README_JA.md)からは、`config/`、`home/`、`scripts/`、
`tests/`、`dotfiles/.agent/` 内の詳細 README にも移動できます。

## よく使うコマンド

```sh
# ホームファイルの変更内容を確認
zsh scripts/chezmoi_apply.sh --dry-run

# CLI 用 Nix 構成を、切り替えずにビルド
zsh scripts/nix_install.sh --cli-only --dry-run

# リポジトリ全体を検証
zsh tests/run.sh
```

`flake.nix`、`flake.lock`、`config/nix/` を変更した場合は、Nix を明示的に
適用してください。Git pull hook は Nix の switch や mise tool の導入を
実行しません。
