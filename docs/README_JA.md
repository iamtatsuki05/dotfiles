# ドキュメント一覧

[English](README.md) · [リポジトリ README](../README_JA.md)

新しいマシンへ導入するときは、まず[導入ガイド](getting-started_JA.md)を
読んでください。それ以外の文書は、運用や設定変更の際に参照します。

## 最初に読む文書

- [導入ガイド](getting-started_JA.md): プロファイル、初回導入、検証方法。
- [設定の管理境界](configuration-ownership_JA.md): 変更する前に、どのファイルを
  編集すべきか判断するための説明。
- [トラブルシューティング](troubleshooting_JA.md): 症状から復旧手順を探すための
  ガイド。

## 環境を運用・拡張するための文書

- [日常のメンテナンス](maintenance_JA.md): 更新、テスト、hook、定期 pull、
  キャッシュ整理。
- [パッケージ管理と移行](package-management_JA.md): Nix、Homebrew fallback、
  Mac App Store、Brewfile 移行。
- [AI エージェント設定](ai-agents_JA.md): 管理対象ファイル、同期、Waza 評価、
  Claude Code のログインプロファイル。
- [秘密情報と安全な操作](secrets-and-safety_JA.md): ローカル認証情報、dry-run、
  バックアップ、破壊的操作。

## ディレクトリ別リファレンス

各ディレクトリの詳細は、次の README に記載しています。

- [設定ソース](../config/README_JA.md)
- [Nix 設定](../config/nix/README_JA.md)
- [chezmoi のホームソース](../home/README_JA.md)
- [補助スクリプト](../scripts/README_JA.md)
- [テスト](../tests/README_JA.md)
- [管理対象 dotfiles](../dotfiles/README_JA.md)
- [AI エージェントファイル](../dotfiles/.agent/README_JA.md)

コマンドの挙動と文書が一致しない場合は、現在の `--help`、設定ファイル、
テストを正としてください。同じ変更の中で、該当する文書も更新します。
