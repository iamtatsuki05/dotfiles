# 日常のメンテナンス

[English](maintenance.md) · [ドキュメント一覧](README_JA.md)

最初にリポジトリを更新し、変更された設定層だけを適用します。Git pull hook は
軽量な管理ファイルを同期しますが、Nix や mise の明示的な更新は代行しません。

## 既存 clone を更新する

```sh
cd ~/src/dotfiles
git pull --ff-only

# chezmoi 管理のホームファイルを確認して適用
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default

# CLI 用 Nix profile をビルド確認して適用
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

GUI アプリも更新する desktop host では、次を実行します。

```sh
zsh scripts/nix_install.sh --with-gui-apps
```

`flake.nix`、`flake.lock`、`config/nix/` が変わった場合は、Nix の手順を実行します。
`dotfiles/.agent/` が変わった場合は、agent sync を実行します。

## 目的に合う最小の更新 task を選ぶ

```sh
# flake.lock だけ更新
mise run lock-update

# nixpkgs input だけ更新
mise run lock-update-nixpkgs

# Nix 管理 tool を更新して適用
mise run nix-update

# nixpkgs を更新してから Nix package set を適用
mise run nixpkgs-update

# mise 管理 tool を、設定済み release line 内で更新
mise run mise-update

# Nix、mise 管理 tool、Hermes Agent をまとめて更新
mise run package-update
```

`features.macos` が `true`（既定値）の macOS で Homebrew の GUI fallback entry がある場合、
`mise run package-update` は既定で CLI 用 Nix profile を使います。Nix の GUI package set
と Homebrew 管理の GUI fallback の両方を適用する場合は、`-- --with-gui-apps` を付けて
ください。
Hermes Agent だけなら `mise run hermes-update` を使います。

Macで `features.macos` が `false` の場合は、`--with-gui-apps` を指定してもGUIパッケージや
管理対象のHomebrew更新は有効になりません。CLIパッケージとmiseは維持します。
詳しくは[Mac用の追加機能をOFFにする](configuration-ownership_JA.md#mac用の追加機能をoffにする)を参照してください。

## Git pull hook が行う範囲を把握する

`main.sh` は、リポジトリ内に3つの Git hook を設定します。

- `post-merge`: 通常の `git pull` または merge 後に実行。
- `post-rewrite`: `git pull --rebase` などの後に実行。
- `post-checkout`: branch checkout 後に実行。

hook は `scripts/apply_updates.sh` を呼び、chezmoi ファイル、AI agent ファイル、
hook 自体を同期します。nix-darwin / Home Manager の switch、Homebrew の削除、
mise tool の導入は実行しません。

手動で入れ直す場合は、次を実行します。

```sh
zsh scripts/setup_git_hooks.sh
```

## リポジトリは毎日定期 pull される

`full` profile は、macOS では nix-darwin の launchd agent、Linux では Home
Manager の systemd user timer として `dotfiles-auto-update` を宣言します。
毎日 06:00 に `${HOME}/src/dotfiles` で `git pull --ff-only` を実行し、ログは
`/tmp/dotfiles-git-pull.log` に書きます。

macOS の activation では、nix-darwin module が旧 managed cron block も削除します。

この定期 pull にも Git hook と同じ境界があります。flake が変わっても、
それだけで新しい Nix generation へ切り替わることはありません。

## 変更を共有する前に検証する

```sh
# ローカルの全体テスト
zsh tests/run.sh

# 同じ mise task
mise run dotfiles-test

# 主な管理境界ごとの focused test
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_chezmoi_rendered_home.sh
zsh tests/test_nix_migration.sh
zsh tests/test_agent_sync.sh
```

test runner は shell 構文、補助スクリプト、生成済み chezmoi source の drift、
一時 HOME への render を確認します。ローカルに chezmoi がなければ、render の
integration test だけを skip します。GitHub Actions では chezmoi を導入し、
macOS と Ubuntu の両方で実行します。

## パッケージ管理領域を安全に整理する

`--apply` を付けない限り、cleanup は dry-run です。

```sh
mise run package-cleanup
mise run package-cleanup -- --apply
mise run package-cleanup -- --include-mise
mise run package-cleanup -- --include-mise --apply
```

この task は、古い Nix generation と cache を削除できます。generation を
削除すると rollback 履歴が減ります。`--include-mise` は、未使用の mise tool
version と古い mise cache も対象にします。適用前に表示された対象を確認して
ください。
