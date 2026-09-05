# 設定の管理境界

[English](configuration-ownership.md) · [ドキュメント一覧](README_JA.md)

shell と環境設定をどこで管理するかは、このページを基準に判断します。
リポジトリ内の canonical source を編集し、対応する適用経路を使ってください。
`$HOME` に展開済みのファイルだけを編集すると drift になり、次回の適用で
上書きされる可能性があります。

shell integration では、値の source は `home/.chezmoidata.toml` に一本化し、
Bash/Zsh 共通実装は `home/.chezmoitemplates/dotfiles-shell-common.sh` だけに
置きます。`config/nix/home-manager/session.nix` は同じ TOML を読み、値を重複して
持ちません。

## 管理対象と適用経路

| 対象 | 管理上の定義 | 適用先・利用者 |
|---|---|---|
| shell integration の値と policy | `home/.chezmoidata.toml` | chezmoi template と Nix projection |
| Bash/Zsh 共通の環境変数と alias | `home/.chezmoitemplates/dotfiles-shell-common.sh` | chezmoi |
| shell 起動 wrapper と native adapter | `config/shell/bash*.tmpl`、`home/.chezmoitemplates/bash*`、`home/private_dot_config/` の shell template | chezmoi |
| Nix の session variable と宣言的 shell UI | `config/nix/home-manager/` | Home Manager / nix-darwin |
| `$HOME` に直接 render する file | `home/` | chezmoi |
| Bash 専用 shell bootstrap | `scripts/setup_shell.sh` | 明示した `bash` 実行 |
| AI agent の prompt、設定、hook、skill | `dotfiles/.agent/` | `dotfiles/.agent/sync.sh` |
| chezmoi 外の repo-level runtime asset | `dotfiles/` | 対象別 script |
| tool version と task alias | `config/mise/config.toml` | mise |
| Nix にない macOS package | 生成される `config/nix/homebrew-fallback.nix` | nix-darwin / Homebrew |
| Mac App Store アプリ | `config/nix/mas-apps.nix` | `scripts/install_mas_apps.sh` |

## Shell の役割と起動境界

対話 shell の役割は、意図的に限定しています。

- ローカルの対話作業は zsh を使います。
- server の対話 session は Bash を使います。
- 新しく portable script を追加するときは Bash で実装し、Bash に対応する
  shebang を付けます。既存 script は宣言済みの shebang と shell 固有の挙動を
  保ちます。これは全 script を Bash へ移行する方針でも、shell 間の完全な
  同等性を保証する方針でもありません。

### shell の canonical data

shell integration の値は `home/.chezmoidata.toml` を編集します。`EDITOR` と4つの
XDG default が対象です。名前付きの PATH 候補、safe alias、shell ごとの mise policy
もここで管理します。PATH 候補は `home_local_bin`、
`darwin_arm64_homebrew_bin`、`darwin_x86_64_homebrew_bin` のような named scalar
であり、配列の index に意味を持たせません。Darwin では Apple Silicon の
`/opt/homebrew` と Intel の `/usr/local` を別の候補として扱います。

3つの native shell template は共通の
`home/.chezmoitemplates/shell-data-validate` で schema を検証します。不正または
安全でない値は render 時に失敗させます。旧 schema 用の key、alias、silent
fallback は追加しません。

`config/nix/home-manager/session.nix` は `builtins.fromTOML` で同じ TOML を読みます。
default 値を変えるときは TOML だけを変更し、Nix module に値をコピーしないでください。

### Shell ごとの挙動

`home/.chezmoitemplates/dotfiles-shell-common.sh` は、非 secret の共通環境変数、
PATH、safe alias、Bash/Zsh 固有の `DOTFILES_REPO_ROOT`、Bash/Zsh の interactive
mise activation を一度だけ担当します。Zsh の prompt、completion、option、
oh-my-zsh は引き続き `config/nix/home-manager/zsh.nix` が担当します。この module
に共通環境や mise activation を重複して書きません。

Fish には `~/.config/fish/conf.d/zz-dotfiles.fish` の任意の native adapter があり、
共通する非 secret の環境変数、PATH、interactive な safe alias を提供します。
公式の `mise activate fish` hook は interactive な session でだけ使います。generator
または source の失敗は、隠さず failure として扱います。

csh/tcsh には `~/.config/shell/dotfiles-shell-common.csh` の standalone adapter が
あります。限定した非 secret の環境変数、PATH、prompt 内の safe alias だけを提供し、
activation は行いません。mise shim、`mise exec`、`mise run` を使ってください。
どちらの optional adapter も `DOTFILES_REPO_ROOT` を提供せず、`secrets.env` を読みません。

Bash/Zsh 共通 file、Fish adapter、csh/tcsh adapter は、`MISE_GLOBAL_CONFIG_FILE` が
unset または空の場合に限り、readable な managed file
`$HOME/.config/mise/config.toml` を設定します。明示された non-empty の値は保持します。
そのため custom `XDG_CONFIG_HOME` を使っても、managed な mise の tool と task 設定を
同時に利用できます。

### 固定 managed path と custom XDG

managed shell file は `$HOME/.config` 配下に固定します。

- `~/.config/shell/dotfiles-shell-common.sh`
- `~/.config/shell/dotfiles-shell-common.csh`
- `~/.config/fish/conf.d/zz-dotfiles.fish`
- `~/.config/mise/config.toml`

`XDG_CONFIG_HOME` は application の設定 default または override であり、managed
file の配置先を変えません。Bash と Zsh の起動 wrapper は、常に固定 path の common
file を探します。custom `XDG_CONFIG_HOME` が有効な場合、shell bootstrap は
`$XDG_CONFIG_HOME/fish/conf.d/zz-dotfiles-canonical.fish` に derived Fish loader
も生成します。この loader は `$HOME/.config` 配下の canonical Fish adapter を
source するもので、chezmoi の別の source file ではありません。custom XDG path を
変えたときは bootstrap を再実行してください。

csh/tcsh の adapter は既存の起動 file へ自動追加しません。既存の `.cshrc` または
`.tcshrc` から、次のように手動で opt-in します。

```csh
if (-r "$HOME/.config/shell/dotfiles-shell-common.csh") source "$HOME/.config/shell/dotfiles-shell-common.csh"
```

## Bash 専用の shell bootstrap

Nix profile 全体ではなく shell integration だけを導入したい host では、
`scripts/setup_shell.sh` を使います。Bash 3.2 以降で実行してください。

```sh
bash scripts/setup_shell.sh --dry-run
bash scripts/setup_shell.sh
bash scripts/setup_shell.sh --verify
```

必要なのは Bash、chezmoi、標準 Unix utility です。package、Nix、Homebrew、mise、
別の shell を install せず、login shell を変更したり `chsh` を実行したりしません。
`.profile`、`.cshrc`、`.tcshrc`、`~/.config/fish/config.fish`、`secrets.env`、
対象外の application file も変更しません。

通常の apply 対象は、次の6つに固定しています。

| 対象 | 役割 |
|---|---|
| `~/.bashrc` | Bash startup wrapper |
| `~/.bash_profile` | Bash login wrapper |
| `~/.config/shell/dotfiles-shell-common.sh` | Bash/Zsh 共通実装 |
| `~/.config/shell/dotfiles-shell-common.csh` | csh/tcsh standalone adapter |
| `~/.config/fish/conf.d/zz-dotfiles.fish` | Fish adapter |
| `~/.config/mise/config.toml` | repo の mise 設定の render 先 |

custom `XDG_CONFIG_HOME` を使う場合は、前述の Fish loader が derived file として
追加されます。これは custom path 専用の生成物であり、managed source target の数には
含めません。

bootstrap は書き込み前に既存 target と比較し、foreign または内容の異なる file が
あれば拒否します。custom-XDG Fish loader も同じです。同一内容の既存 file は再利用
できますが、異なる file を置き換えるには `--force` が必要です。`--dry-run` は
書き込まず、`--verify` は allowlist の target が一致するかを報告し、`--help` は
現在の interface を表示します。この文書と実際の script が異なる場合は、script の
`--help` を正としてください。

## クロスプラットフォーム検証の境界

shell check は、optional runtime がない場合と、存在する runtime が失敗した場合を
区別します。Fish は required check が成功すれば `PASS`、binary があるのに check
または activation が失敗すれば `FAIL`、binary がない場合だけ not-applicable の
`SKIP` です。csh/tcsh は runtime の identity を記録し、同じ binary を2つの名前で
呼んでいるだけの場合は、genuine csh の別検証を示す `PASS` としません。

CI は実 Fish/csh/tcsh runtime と、Intel macOS、Debian、Fedora の shell-only cell を
検証できる構成にします。CI の結果は、その実行で使った runner と commit に対する
証跡です。この文書は全 distribution、kernel、shell implementation の成功を保証
しません。WSL kernel、BSD host、実 RHEL host は未検証の境界です。対象が検証済み
cell の外にある場合は、その host で focused check を実行してから利用してください。

## パッケージと宣言的なシェル設定は Nix が管理する

macOS は nix-darwin と Home Manager、Linux は standalone Home Manager を使い、同じ
package list を共有します。zsh と Neovim は `config/nix/home-manager/`、macOS
defaults と定期更新 agent は `config/nix/darwin/` で宣言します。

macOS defaults には keyboard repeat、sudo Touch ID、スクリーンショット保存先
`${HOME}/SS` が含まれます。この個人設定を別の Mac へ適用する前に、
`config/nix/darwin/defaults.nix` を確認してください。

`flake.nix`、`flake.lock`、`config/nix/` を変更した場合は、この層を適用します。

```sh
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

GUI package も対象にする host では `--with-gui-apps` を使います。

## ホームへ展開するファイルは chezmoi が管理する

リポジトリルートの `.chezmoiroot` は `home/` を指します。`dot_*` と
`private_dot_config/` 配下の file は、対応する `$HOME` 配下へ render されます。

`config/shell/` と `home/.chezmoitemplates/bash*` の小さな wrapper は同期を保ちます。
Bash/Zsh 共通実装を管理する場所は
`home/.chezmoitemplates/dotfiles-shell-common.sh` だけです。削除した大きな mirror を
2つ目の編集先として扱いません。

通常の chezmoi 経路では、適用前に preview と verify を実行します。

```sh
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default
zsh scripts/chezmoi_apply.sh --verify
```

`--mark-default` は `~/.config/dotfiles/manager` に `chezmoi` を記録し、選択した
profile を `~/.config/dotfiles/profile` に保存します。Bash 専用 bootstrap はこの
marker を書きません。

## Tool version と task alias は mise が管理する

`config/mise/config.toml` に mise 管理の tool と repo task alias を宣言しています。
更新するときは、目的に合う最小の task を選びます。`node@22` のように major release
line 自体を変える場合は、一括更新ではなく設定変更が必要です。

shell integration policy は `home/.chezmoidata.toml` に分けて宣言します。Fish は
`interactive-only`、csh/tcsh は `unsupported-activation` です。csh/tcsh adapter の
shim 優先順位は `MISE_DATA_DIR`、`XDG_DATA_HOME`、`HOME` の順で、Fish は公式
activation hook に委譲します。この policy は shell integration 用のデータであり、
csh/tcsh の activation を追加したり、`config/mise/config.toml` の tool version と
task alias を置き換えたりするものではありません。

## Homebrew と MAS は限定的な fallback として使う

パッケージの優先順位は `Nix > Homebrew > MAS` です。Homebrew は、まだ Nix へ移せない
entry だけに使います。Mac App Store アプリは、1件の取得失敗で Nix activation 全体が
失敗しないよう、別の script で導入します。

fallback の一覧変更や Homebrew の削除前に、[パッケージ管理と移行](package-management_JA.md)
を確認してください。

## 共有 agent ファイルには別の同期手順がある

共通 prompt の canonical source は `dotfiles/.agent/AGENTS.md` です。アプリ別設定、
hook、skill、eval も同じ tree で管理します。リポジトリルートには、意図的に
`AGENTS.md` symlink を置いていません。

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
```

sync test は canonical prompt 全体を managed link 経由で検証します。shell policy の
詳細はこの管理境界に集約し、shared prompt には役割の短いメモだけを置きます。

対応範囲と検証方法は[AI エージェント設定](ai-agents_JA.md)を参照してください。
