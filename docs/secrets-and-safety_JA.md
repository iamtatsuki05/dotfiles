# 秘密情報と安全な操作

[English](secrets-and-safety.md) · [ドキュメント一覧](README_JA.md)

実際の認証情報は、ignore 済みのローカルファイルか OS の credential store だけに
保存します。リポジトリ内の template は変数名を示すためのもので、利用可能な
secret value を入れてはいけません。

## ローカルの shell secret

管理対象のローカルファイルは `~/.config/shell/secrets.env` です。初回 setup では、
ファイルが存在しない場合に限り、chezmoi が
`config/shell/secrets.env.example` から生成します。そのマシンで必要な値だけを
入力し、shell を再起動してください。

主な変数名は次のとおりです。

```sh
export SLACK_WEBHOOK_URL=""
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
export GEMINI_API_KEY=""
export GITHUB_TOKEN=""
export OPENCODE_API_KEY=""
export DEVIN_API_KEY=""
```

実際の webhook URL、token、password、private key、export 済み credential を
`config/`、`home/`、文書、test fixture、session log に入れないでください。
差分を共有する前に、tracked file と untracked file の両方を確認します。

## Agent sync はローカルの環境ファイルを生成する

`dotfiles/.agent/sync.sh` はローカルの secrets file を読み、次の agent 用 env file を
mode 600 で更新します。

- `~/.gemini/antigravity-cli/.env`: `DEVIN_API_KEY`
- `~/.hermes/.env`: `DEVIN_API_KEY`、`OPENCODE_API_KEY`、OpenCode の値から作る
  `OPENCODE_GO_API_KEY`
- `~/.openclaw/.env`: `DEVIN_API_KEY` と `OPENCODE_API_KEY`

sync が更新するのは ignore 対象のローカルファイルであり、値を外部サービスへ
送信する処理ではありません。ただし認証情報の複製ではあるため、外部共有する
backup、diagnostic、support bundle には含めないでください。

## Shell 起動設定の管理場所

chezmoi は bash 起動ファイルと `~/.config/shell/dotfiles-shell-common.sh` を
生成します。Home Manager の zsh 設定も、存在する場合は同じ共通ファイルを
source します。共通の環境読込は canonical template に置き、secret value を
各 shell config に複製しないでください。

Bash/Zsh の共通 file は `~/.config/shell/secrets.env` を source できる管理経路であり、
Bash/Zsh 固有の `DOTFILES_REPO_ROOT` も持ちます。任意の Fish adapter は非 secret の
環境変数、PATH、interactive な safe alias を提供し、interactive な Fish session では
公式の `mise activate fish` hook も source します。standalone の csh/tcsh adapter は
限定した非 secret の環境変数、PATH、prompt 内の safe alias を提供しますが、mise
activation は行いません。どちらの adapter も `secrets.env` を source せず、その値を
展開・shell state へ複製せず、`DOTFILES_REPO_ROOT` も提供しません。Fish または csh/tcsh
から認証が必要な tool を使う場合は、承認済みの credential store か明示的な command
を使ってください。

## 適用前に変更内容を確認する

状態を書き換えない mode がある場合は、先に利用します。

```sh
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --verify
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/migrate_brew_to_nix.sh
zsh scripts/remove_homebrew.sh --dry-run
mise run package-cleanup
```

対応する apply operation を実行する前に、解決済みの対象と選択 profile を確認して
ください。

## 復旧手段を減らす操作を把握する

- Homebrew の削除では package manager が消えます。Nix に未登録の package が
  ないか確認が必要です。script は宣言済み fallback が残っていれば拒否しますが、
  untracked なローカル利用までは判定できません。
- package cleanup は古い Nix generation を削除できるため、rollback 履歴が
  減ります。
- chezmoi の適用は、管理対象 home file の drift を上書きできます。先に差分を
  確認し、意図したローカル変更は canonical source へ戻します。
- Nix switch は、現在の system または Home Manager generation を切り替えます。
  意味のある設定変更後は、先に `--dry-run` でビルドしてください。

nix-darwin の初回適用では、既存の `/etc/pam.d/sudo_local` を
`/etc/pam.d/sudo_local.before-nix-darwin` に保存します。sudo Touch ID の挙動を
確認するまで、この backup を残してください。

## 認証変更は排他的な操作として扱う

Claude Code の profile 切替は、macOS Keychain 内の共有 credential を1つ
切り替えます。`claude-account auth-login` の前に、すべての Claude process を
終了してください。複数アカウント運用中は、通常の Claude 起動で profile wrapper
を迂回しないでください。手順は[AI エージェント設定](ai-agents_JA.md)を参照します。
