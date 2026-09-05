# 導入ガイド

[English](getting-started.md) · [ドキュメント一覧](README_JA.md)

host に合わせて導入経路を選びます。

- Nix / Home Manager を含む環境全体を導入するときは `main.sh` を使います。
- portable な shell integration だけが必要で、Bash と chezmoi がすでにある場合は
  `bash scripts/setup_shell.sh` を使います。

shell 専用経路の範囲は意図的に限定しています。package、Nix、Homebrew、mise、
別の shell を install せず、login shell も変更しません。

## 前提を導入する

不足している tool は、OS の package manager または各 tool の公式文書から導入します。
リポジトリでは、確認していない download-and-execute pipeline を貼り付けるよう求めません。

- Git: [Pro Git の Git 導入手順](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
- Bash: [GNU Bash Reference Manual](https://www.gnu.org/software/bash/manual/)
- chezmoi: [公式の導入手順](https://www.chezmoi.io/install/)
- full 経路で使う Nix: [公式の Nix download page](https://nixos.org/download/)
- tool version 管理に使う mise: [公式の mise 導入手順](https://mise.jdx.dev/installing-mise.html)

### Nix 全体導入の前提

- `aarch64` または `x86_64` の macOS / Linux。
- 初回コマンドの前に Git と zsh が使えること。
- 初回 clone と package 取得に必要な network 接続。
- macOS で通常の Nix を導入するための管理者権限。Linux では、Nix が `PATH` から
  利用できる必要があります。なければ先に導入するか、後述の nix-portable を使います。
- `full` profile で `config/nix/mas-apps.nix` の app を入れる場合は、サインイン済みの
  Mac App Store account。

### Bash 専用導入の前提

- Bash 3.2 以降。古い macOS にある system Bash も、意図した構文範囲に含まれます。
- 実行可能な chezmoi。
- この repository を取得する Git と、`mkdir`、`mktemp`、`cmp`、`chmod` などの標準
  Unix utility。

Bash 専用 command は6つの allowlist target を render します。そのために Nix、zsh、
mise、Fish、csh、tcsh は必要ありません。導入後の adapter を使う場合だけ、
Fish/csh/tcsh runtime が任意で必要になります。

この repository は個人環境用です。すでに別の方法で管理している host へ適用する前に、
[設定の管理境界](configuration-ownership_JA.md)を確認してください。

## Bash 専用 shell の導入

repository を clone し、command の interface を確認してから、6つの target を preview
して適用します。

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
bash --version
chezmoi --version
bash scripts/setup_shell.sh --help
bash scripts/setup_shell.sh --dry-run
bash scripts/setup_shell.sh
bash scripts/setup_shell.sh --verify
```

bootstrap は `XDG_CONFIG_HOME` が別の場所を指していても、managed shell file を
`$HOME/.config` 配下に保ちます。custom XDG config directory を使う場合は、Fish が
canonical adapter を読めるよう、その directory に derived Fish loader も生成します。
`.profile`、`.cshrc`、`.tcshrc` には何も追加しません。csh/tcsh の手動 opt-in は
[Shell の役割と起動境界](configuration-ownership_JA.md#shell-の役割と起動境界)を
参照してください。

bootstrap は書き込み前に既存 target と render 結果を比較し、内容が異なる target を
拒否します。同一内容なら再利用できます。異なる target を置き換える明示的な option
は `--force` です。flags の正確な一覧は、実行する script の `--help` を確認してください。

## Nix 全体の profile を選ぶ

新しい machine に Nix / Home Manager 環境も導入する場合は、Nix の前提を満たしてから
`main.sh` を使います。profile を選び、Nix と chezmoi を適用し、任意の Mac App Store
app と repository の Git hook を設定します。macOS では必要に応じて Nix を導入しますが、
Linux では Nix が導入済みである必要があります。

`main.sh` は OS に応じて既定の profile を選びます。

- `full` は macOS の既定値です。nix-darwin、Home Manager、GUI app、macOS defaults、
  user timer、mise tool、home file を対象にします。
- `cli` は Linux の既定値です。GUI app、macOS 固有設定、user timer を省き、共通の
  CLI package set と home file を適用します。

macOS に小さい構成を入れたい場合は、`cli` を明示します。

```sh
zsh main.sh --cli-only
```

Mac App Store にサインインしていない場合や、一覧の一部 app が利用できない場合は、
macOS の full setup から MAS だけを省けます。

```sh
zsh main.sh --full --skip-mas-apps
```

Linux では、`main.sh` の前に `nix --version` が成功することを確認します。Nix が
なければ host で承認された方法で導入するか、nix-portable を使います。

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

full setup は再実行できます。ただし、エラーを確認してから再実行してください。
Nix の初回導入後は新しい shell が必要になる場合があり、fallback entry が残っていれば
Homebrew も必要です。

### sudo が使えない Linux

通常の `/nix` store を作成・mount できない host では、nix-portable を使います。

```sh
zsh scripts/nix_portable_install.sh
export PATH="$HOME/.local/bin:$PATH"
nixp --version
dotfiles-nix-shell

# リポジトリの CLI package set 内でコマンドを1つ実行
dotfiles-nix-run git --version
```

nix-portable の既定 runtime は `proot` です。mount namespace が制限された host でも
動作します。`scripts/nix_rootless_install.sh` は `nix-user-chroot` 用に残していますが、
その store を参照できるのは chroot 内だけです。これは Nix の代替経路であり、すべての
WSL kernel、BSD host、RHEL installation への互換性を保証するものではありません。

## 導入結果を確認する

Bash 専用経路では、`--verify` が6つの shell target と custom-XDG Fish loader を
書き換えずに確認します。

full 経路では、まず状態を書き換えない確認を実行します。

```sh
# chezmoi 管理 file を変更せずに照合
zsh scripts/chezmoi_apply.sh --verify

# CLI 用 Nix 構成を、切り替えずに build
zsh scripts/nix_install.sh --cli-only --dry-run

# repository の test を実行
zsh tests/run.sh
```

`chezmoi_apply.sh --verify` は、すべて一致すれば終了コード 0、drift があれば 1 を
返します。Nix の dry-run は選択した flake output を build しますが、現在の system や
Home Manager generation は切り替えません。

shell runtime の結果と platform 境界は、[クロスプラットフォーム検証の境界](configuration-ownership_JA.md#クロスプラットフォーム検証の境界)を
参照してください。CI は実 Fish/csh/tcsh runtime と、Intel macOS、Debian、Fedora の
shell-only cell を検証できる構成にします。platform を検証済みと扱う前に、対象 commit
の CI run を確認してください。WSL kernel、BSD、実際の RHEL は未検証の境界です。

## 次に管理境界を確認する

展開済みの file を編集する前に、[設定の管理境界](configuration-ownership_JA.md)を
読んでください。canonical source を Nix、`config/`、`home/`、Bash 専用 bootstrap、
共有 agent tree のどこに置くべきかを説明しています。
