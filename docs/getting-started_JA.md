# 導入ガイド

[English](getting-started.md) · [ドキュメント一覧](README_JA.md)

新しいマシンでは、後述の Nix 前提を満たしてから `main.sh` を使います。この
スクリプトはプロファイルを選び、Nix と chezmoi を適用します。さらに、任意の
Mac App Store アプリとリポジトリの Git hook を設定します。macOS では必要に
応じて Nix を導入しますが、Linux では Nix が導入済みである必要があります。

## 導入前に確認するもの

- `aarch64` または `x86_64` の macOS / Linux。
- 初回コマンドを実行できる Git と zsh。
- clone とパッケージ取得に必要なネットワーク接続。
- macOS に通常の Nix を導入するための管理者権限。Linux では、Nix が `PATH` から
  利用できる必要があります。未導入なら、先に Nix を導入するか、後述の
  nix-portable を使います。
- `full` プロファイルで `config/nix/mas-apps.nix` のアプリを入れる場合は、
  サインイン済みの Mac App Store アカウント。

このリポジトリは個人環境用です。すでに別の方法で管理しているマシンへ
適用する場合は、事前に設定ファイルとスクリプトを確認してください。

## 用途に合うプロファイルを選ぶ

`main.sh` は OS に応じて既定のプロファイルを選びます。

- `full` は macOS の既定値です。nix-darwin、Home Manager、GUI アプリ、
  macOS defaults、ユーザー timer、mise tool、ホームファイルを対象にします。
- `cli` は Linux の既定値です。GUI アプリ、macOS 固有設定、ユーザー timer を
  省き、共通の CLI パッケージとホームファイルを適用します。

macOS に最小構成を入れたい場合は、`cli` を明示します。

```sh
zsh main.sh --cli-only
```

Mac App Store へのサインインが済んでいない場合や、一覧に取得できないアプリが
ある場合は、Mac App Store だけを省いて macOS の full setup を実行できます。

```sh
zsh main.sh --full --skip-mas-apps
```

## 新しいマシンへ導入する

Linux では、`main.sh` の前に `nix --version` が成功することを確認します。Nix が
なければ、その host で承認された方法で導入するか、nix-portable を使います。

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

セットアップは再実行できます。ただし、エラーを確認せずに繰り返さないで
ください。Nix の初回導入後は新しいシェルが必要になる場合があります。
また fallback entry が残っていれば、Homebrew も必要です。

### sudo が使えない Linux

通常の `/nix` store を作成・mount できないホストでは、nix-portable を使います。

```sh
zsh scripts/nix_portable_install.sh
export PATH="$HOME/.local/bin:$PATH"
nixp --version
dotfiles-nix-shell

# リポジトリの CLI package set 内でコマンドを1つ実行
dotfiles-nix-run git --version
```

nix-portable の既定 runtime は `proot` です。mount namespace が制限された
ホストでも動作します。`nix-user-chroot` 用の
`scripts/nix_rootless_install.sh` も残していますが、その store を参照できるのは
chroot 内だけです。

## 導入結果を確認する

まず、状態を書き換えない確認を実行します。

```sh
# chezmoi 管理ファイルを変更せずに照合
zsh scripts/chezmoi_apply.sh --verify

# CLI 用 Nix 構成を、切り替えずにビルド
zsh scripts/nix_install.sh --cli-only --dry-run

# リポジトリのテストを実行
zsh tests/run.sh
```

`chezmoi_apply.sh --verify` は、すべて一致すれば終了コード 0、drift があれば 1 を
返します。Nix の dry-run は選択した flake output をビルドしますが、現在の
system や Home Manager generation は切り替えません。

## 次に管理境界を確認する

展開済みのファイルを編集する前に、[設定の管理境界](configuration-ownership_JA.md)を
読んでください。canonical source を Nix、`config/`、`home/`、共有 agent tree の
どこに置くべきかを説明しています。
