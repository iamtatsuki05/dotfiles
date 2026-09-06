# AI エージェント設定

[English](ai-agents.md) · [ドキュメント一覧](README_JA.md)

共有 AI agent ファイルは `dotfiles/.agent/` で管理します。変更は canonical tree に
加え、同期を実行してから、管理ソースと代表的な展開先の両方を検証します。

## Canonical file と管理境界

- `dotfiles/.agent/AGENTS.md`: 共通の agent policy。
- `dotfiles/.agent/apps/`: アプリ別設定と hook。
- `dotfiles/.agent/skills/`: local skill と review 済みの vendored skill。
- `dotfiles/.agent/evals/`: Waza evaluation suite。
- `dotfiles/.agent/sync.sh`: 対応する agent home への同期処理。

リポジトリルートには、意図的に `AGENTS.md` symlink を置いていません。一部の
展開先は canonical tree を指す symlink です。展開済みファイルは別の source of
truth ではありません。

support matrix、ファイル対応、ignore、hook の挙動は、このリポジトリ全体の
説明より頻繁に変わります。編集前に
[AI agent ディレクトリの README](../dotfiles/.agent/README_JA.md)を確認してください。

## 同期して検証する

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
zsh tests/test_agent_support_matrix.sh
```

ファイルの同期が成功しても、起動中の agent process が設定を再読込したとは
限りません。対象 client の仕様に応じて再起動または reload し、live config は
別に確認してください。

## Skill と prompt を評価する

Waza の command routing を確認するときは、先に dry-run を使います。

```sh
mise run waza-eval-model -- --agent all --dry-run
```

focused command と suite の構成は `dotfiles/.agent/README_JA.md` に記載しています。
評価結果が示すのは、選択した suite と agent の結果です。sync test や live client
の確認を代替するものではありません。

## 外部 skill の由来と review を保つ

外部 skill は `dotfiles/.agent/skills/upstreams.json` に登録し、
`scripts/agent_skill_upstreams.py` で管理します。upstream の pinned commit、license、
attribution、local overlay、security review、focused validation を残してください。
review 済み tree にファイルを直接上書きして更新しないでください。

```sh
python3 scripts/agent_skill_upstreams.py check
```

## Claude Codeをアカウント別に起動する

Claude Code 2.1.261以降が必要です。アカウントごとの固定ディレクトリを `CLAUDE_CONFIG_DIR` に指定し、通常の `auth login` を保存します。[公式認証仕様](https://code.claude.com/docs/en/authentication)はmacOS Keychainもこのディレクトリごとに分離すると説明しており、2.1.261で異なるKeychain項目を参照することを資格情報なしで確認しています。

初回だけ、各コマンドで開くブラウザのアカウントと組織を確認してログインします。profile名には小文字の英字・数字・ドット・ハイフン・アンダースコアを使います。旧版の共有loginやsetup-tokenは自動移行しないため、以前登録したアカウントも一度再登録します。tokenのコピーは不要です。

```sh
claude-account auth-login personal
claude-account auth-login work
claude-account list
```

普段は名前で選択します。別アカウントのsessionを終了する必要はありません。

```sh
claude-account personal --model fable
claude-account work --model fable --dangerously-skip-permissions
```

上限に達したsessionを終了し、そのプロジェクトのディレクトリから、利用枠のある別アカウントで再開します。他のsessionは継続できます。

```sh
claude-account work --resume SESSION_ID --model fable
```

元sessionを開いたまま履歴を分岐する場合は `--fork-session` も指定します。同じsession IDを二つのprocessで同時に更新しないでください。別組織のアカウントでresumeすると、過去の会話やコードもその組織の認証で送信されます。業務データを送ってよいアカウントを選んでください。

認証の更新が必要なときだけ `claude-account auth-login work` を再実行します。そのprofileのsessionが動いている間は再loginを拒否しますが、他profileには影響しません。切替のたびのブラウザloginは不要です。

ログイン成功時は対話画面の初期設定完了も保存します。以前登録したprofileで、認証済みなのに初回ログイン画面が出る場合は、そのprofileのsessionを終了して `claude-account repair work` を実行してください。現在の本人情報を照合してから初期設定完了フラグだけを補い、再ログインや認証情報・プロジェクトの信頼設定のコピーは行いません。

初回登録でアカウントを間違えた場合は、`claude-account auth-login personal --replace` で紐付けを置き換えます。確認欄に `personal` と入力し、ブラウザで正しいアカウントと組織を選んでください。通常の再loginは別の本人情報への変更を拒否します。`--replace` も、そのprofileのsessionが動いている間は実行できません。キャンセルやlogin失敗では登録情報を維持しますが、Claude本体が保存した認証情報は変わっている場合があります。その場合は再loginしてください。

通常の `claude` で使うアカウントは、次のように選びます。

```sh
claude-account default personal
claude --model fable
claude --resume SESSION_ID --model fable --dangerously-skip-permissions
claude-account default
```

設定は `~/.config/claude-account/default-profile`（`XDG_CONFIG_HOME` 設定時はその配下）に保存します。更新済みのdotfilesシェル設定を読み込んだBash・Zshで有効です。新しいターミナルにも引き継ぎますが、実行中sessionのアカウントは変えません。`claude-account work ...` はデフォルトに関係なく `work` を使います。デフォルト先の認証が無効な場合は停止し、別の認証へ自動切替しません。デフォルトに選んだprofileを `--replace` すると、次回起動から置換後のアカウントを使います。

`claude-account default --clear` でデフォルト指定を解除します。未設定時は従来のClaude本体の認証と引数をそのまま使います。`command claude ...`、実行ファイルの直接起動、シェル関数を読み込まないスクリプトはデフォルト指定の対象外です。デフォルト設定後の認証操作は `claude auth login` ではなく `claude-account auth-login PROFILE` を使ってください。

保存先は `~/.config/claude-account/accounts/PROFILE/`（`XDG_CONFIG_HOME` 設定時はその配下）です。資格情報、アカウント情報、plugins、常駐processの状態を分離します。既存の `~/.claude` にある `settings.json`、`.mcp.json`、`CLAUDE.md`、skills、hooks、commands、agents、rules、projectsはリンクで共有します。projectsの共有により従来の会話もresumeできます。profileディレクトリは権限700、本人照合用のemailとorganization IDのハッシュは権限600で保存します。旧共有login・登録ファイル・setup-tokenは削除しません。

引数は原則そのまま渡しますが、認証を上書きする `--settings`、`--setting-sources`、`--managed-settings` と `--bare` は拒否します。Agent Viewは無効にし、background・cloud・Remote Control起動は対象外です。session内の `/login`・`/logout` を隠し、認証変更は `auth-login` に集約します。

subscription認証の確認は、Fableの利用資格や無料枠の残量の確認とは別です。wrapperはモデルを変更せず、usage creditsも購入・有効化しませんが、アカウント側で有効な追加課金までは禁止しません。追加料金を避ける場合は、Claudeの利用設定でusage creditsを無効にし、Fable専用枠を確認してください。credits要求が出た場合は同意せず、契約・残量・認証状態を確認します。実アカウントでのFable課金・再開は導入後の確認事項です。

CodexBarの監視用登録はwrapperから独立しています。複数アカウントの同時表示には、CodexBar側にアカウントごとのWebセッションを登録し、次で確認します。

```sh
codexbar usage --provider claude --all-accounts --format json --pretty
```

この一覧は `CLAUDE_CONFIG_DIR` を自動探索しません。setup-tokenや短命なOAuth tokenをコピーして同期する仕組みも追加していません。Webセッションの期限切れ時はCodexBar側で更新します。cookieはログイン資格情報なので、Gitやチャットへ貼り付けないでください。[CodexBarの認証・複数アカウント仕様](https://github.com/steipete/CodexBar/blob/v0.56.3/docs/claude.md)も参照してください。`claude-account work /usage` では選択profileの利用状況を直接確認できます。
