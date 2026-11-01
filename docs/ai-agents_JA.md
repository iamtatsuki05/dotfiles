# AI エージェント設定

[English](ai-agents.md) · [ドキュメント一覧](README_JA.md)

共有 AI agent ファイルは `dotfiles/.agent/` で管理します。変更は canonical tree に
加え、同期を実行してから、管理ソースと代表的な展開先の両方を検証します。

## RTKによるコマンド出力の圧縮

通常のセットアップで[RTK](https://github.com/rtk-ai/rtk)をmise経由で導入します。
Agent同期で、Claude Code、Codex、Copilot、Cursor、Antigravity CLI、Hermes、
OpenCode、OpenClaw、Piの公式hook・pluginを有効にします。固定したバージョンでは、
Devin、Grokの公式連携はありません。DeepSeek HarnessにはRTK利用指示を追加し、自動変換とは区別します。

```sh
mise install github:rtk-ai/rtk
zsh scripts/setup_agent_files.sh
rtk --version
rtk gain
```

RTKは対応するシェルコマンドを書き換えます。組み込みのファイル読み取り・検索
ツールは対象外です。CopilotのCLI固有hook形式ではRTK経由の再実行を促し、
今回採用したPascalCase形式では`updatedInput`による書き換えに対応します。Codexでは
`/hooks`から`rtk hook codex`の定義を確認し、信頼を承認してください。
[Codexのhook仕様](https://learn.chatgpt.com/docs/hooks)により、新規hookは承認まで実行されません。
同期後はAgentを再起動してください。OpenClawではGatewayの再読み込みも必要です。
起動済みのセッションへの反映は別途確認してください。無加工の出力が必要な場合は`rtk proxy <command>`を使います。

権限チェックは書き換え後の`rtk ...`を評価する場合があり、コマンド単位の許可・
拒否ルールによっては確認の挙動が変わります。既存の権限設定は緩和しません。

本体と公式pluginをRTKのリリース0.51.0に揃えて固定しています。
[配布元・更新手順](../dotfiles/.agent/apps/rtk/README.md)を参照してください。
管理対象のHOMEに直接`rtk init`を実行すると、共有プロンプトの変更やsymlinkの
置換が起きる可能性があります。設定の反映にはAgent同期を使ってください。
既存の`opencode/plugins`が非空の実ディレクトリの場合、同期は保護のため
置換をスキップします。その場合はRTKも配置されないため、警告を確認してください。

## TokenEconomizerによる繰り返し作業の削減

通常の全体セットアップでは、miseのツール導入後に[TokenEconomizer](https://github.com/microsoft/TokenEconomizer)を登録します。
対象はClaude Code、Codex、Copilot CLI、Pi、OpenCodeです。OpenCodeは上流で実験的対応です。
既存環境への導入と再適用には、次のコマンドを使います。

```sh
mise run tokeneconomizer-setup -- --dry-run
mise run tokeneconomizer-setup
# 1つだけ導入する場合
bash scripts/setup_tokeneconomizer.sh --agent pi
```

単独のAgent同期と `main.sh --only agent` は設定だけを反映します。
初回は上の導入コマンドも実行してください。未導入のCLIは `SKIP` と表示します。
`--agent` で指定したCLIがなければエラーになります。

上流のコミット `e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9` を
`~/.local/share/dotfiles/tokeneconomizer` に保持し、上流コードは変更しません。
ライセンスはApache-2.0で、LICENSEと帰属情報を含むcheckoutをそのまま使います。
Claude Code・Copilotはローカルmarketplace、Piはローカルpackageとして登録します。
Codexは同じコミットに固定したGit marketplaceを使い、OpenCodeは設定の `plugin` から読み込みます。
CLIの登録情報とPiのpackage一覧は各クライアントが管理し、共有プロンプトは変更しません。
Claude Codeは標準の設定先に加え、`claude-account` の登録済みアカウントにも導入します。
`CLAUDE_CONFIG_DIR` を明示した場合は、その設定先だけが対象です。認証情報は変更しません。
更新するときは導入スクリプトとCodex設定のコミットを揃え、検証後に導入コマンドを再実行してください。
checkout内に変更がある場合は上書きせず停止します。

機能は上流既定のままです。Micro-skillsは無効、未使用ツールの除外とLoop guardは有効です。
Loop guardの判定には利用中のAgent経由で追加のモデル呼び出しが発生します。
学習状態は `~/.tokeneconomizer` に保存され、ツール除外はプロジェクトのローカル設定を変更します。
Pi・OpenCodeにはツール除外機能がありません。既存のRTK連携は保持します。

導入後は各Agentを再起動してください。Codexでは `/hooks` からTokenEconomizerの定義を
確認して信頼を承認し、もう一度起動します。インストール処理は信頼・権限設定を変更しません。
Claude CodeとCopilotでは、初回とCLI更新後にツール一覧を取得します。
次のコマンドは上流の仕組みで一覧を取得し、有料モデルの応答を必要としません。

```sh
~/.local/share/dotfiles/tokeneconomizer/plugin/scripts/te --host claude toolprune refresh-catalog
~/.local/share/dotfiles/tokeneconomizer/plugin/scripts/te --host copilot toolprune refresh-catalog
~/.local/share/dotfiles/tokeneconomizer/plugin/scripts/te --host codex status
```

一時停止は起動時に `TE_DISABLE=1` を付けます。解除する場合は、まずツールを除外した
各プロジェクトで `te --host <agent> toolprune restore` を実行し、各CLIのplugin/packageを
アンインストールします。再同期で復活しないよう管理元のTokenEconomizer設定も取り除きます。
詳しい解除手順は[上流の導入ガイド](https://github.com/microsoft/TokenEconomizer/blob/main/plugin/README.md)を参照してください。

## HallmarkによるWebデザイン

[Hallmark](https://github.com/Nutlope/hallmark)を固定コミットの共有skillとして管理します。
Claude Code、Codex、Cursor、OpenCode、Copilotの共有skill配置から利用できます。
新規ページの作成に使い、既存デザインの変更は `hallmark redesign <path>`、
確認だけなら `hallmark audit <path>` と明示します。

通常の全体セットアップは、miseのツール導入後にブラウザ検証用の依存を導入します。
既存環境では `mise run hallmark-setup` を実行してください。Nodeはmise管理の24を使い、
固定した `puppeteer-core` を `dotfiles/.agent/node_modules` に配置します。
インストール時のpackage scriptは実行せず、Chrome自体は追加導入しません。
render検証にはChromeまたはChromiumが必要です。標準位置にない場合は `CHROME_PATH` を指定します。

上流コード、MIT LICENSE、写真の帰属情報は保持しています。上流との差分は
`upstreams.json` の5件の指示変更です。有料Together API、既存ブラウザへの接続、
追加hookには明示的な依頼・許可を必要とし、toolの権限を広げる提案を除いています。
APIキーがあるだけでは画像生成を行いません。Claude Codeでは上流の編集時hookを有効にし、Hallmark印のあるHTML/CSSと `.hallmark` のあるプロジェクトのコンポーネントを検査します。指摘は通知だけで処理を止めません。CodexにはHallmark hookを登録しません。
Agentを再起動するか、新しい会話で `/hallmark`、Codexでは `$hallmark` を指定してください。

## Yomiyasuによる日本語の推敲

[Yomiyasu](https://github.com/nanaism/yomiyasu)を、上流から変更せず固定版の共有skillとして管理します。
使うときは `yomiyasuで推敲して` と明示してください。既存の文章系skillと適用条件は変更しません。
Python標準ライブラリの検査で表現と原文との差分を確認し、提出する本文が検査済みの最終稿と一致するか照合します。意味の正しさを保証する検査ではありません。
既定では原文と最終稿の写しがローカルの `.yomiyasu/` に残ります。保存が不要な場合は記録を残さないよう指定してください。その場合、記録とのハッシュ照合はできません。
Claude CodeとCodexの管理設定で上流hookを有効にしています。明示的に登録された原稿について、完了や投稿の前に最終稿の検査記録を確認し、未検査なら処理を止める場合があります。未登録の文書は対象外です。一般的なEdit/Write後の検査を行う既存の日本語文章lint hookも保持します。
同期後はAgentを再起動してください。Codexの新しいhook定義は `/hooks` での承認が別途必要です。セットアップは信頼設定を変更しません。追加したskillは新しい会話で読み込んでください。

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

Agent 関連だけを反映する入口は `zsh main.sh --only agent` です。
`--dry-run` を付けると、秘密値を表示せず配置予定を確認できます。
`zsh dotfiles/.agent/sync.sh --dry-run` も同じ同期処理を使います。

単独の同期コマンドでは、依存パッケージを自動導入しません。
従来の Hermes MCP 依存導入も必要なら `zsh dotfiles/.agent/sync.sh --install-deps` を使います。
この指定は環境を変更し、ネットワークにアクセスする場合があります。
`main.sh` の全体セットアップはこの指定を明示して従来の動作を維持します。
設定・秘密情報の反映範囲は[導入ガイド](getting-started_JA.md)を参照してください。

ファイルの同期が成功しても、起動中の agent process が設定を再読込したとは
限りません。対象 client の仕様に応じて再起動または reload し、live config は
別に確認してください。

## クライアント同期skillの管理範囲

`dotfiles/.agent/skills/synced/` はクライアントがアカウント別に生成・更新するデータです。
ローカルには保持しますが、Gitで配布するskillには含めません。CIでは、追跡済みファイルと
ignore対象ではない新規skillを検証し、同期データが再び追跡された場合はエラーにします。
管理対象skillの名前の重複と参照先の欠落は、引き続き検出します。

新しいcloneでは、同期skillとその付属スクリプトはクライアントによる再同期で取得します。
これは既存のlocal skillや、`upstreams.json` で固定した外部skillとは別の配布経路です。
既存cloneが追跡解除のコミットを取り込むと、以前追跡していた同期ファイルが削除される
場合があります。独自に編集したものは先に退避し、必要に応じてクライアントから再同期してください。
この変更は、クライアントが実行時に表示するskillの一覧や優先順位を変更するものではありません。

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

ログイン成功時は対話画面の初期設定完了も保存します。以前登録したprofileで、認証済みなのに初回ログイン画面が出る場合は、そのprofileのsessionを終了して `claude-account repair work` を実行してください。通常起動と同じ共有設定リンクの確認・整備と、現在の本人情報の照合を行います。その後、profileの状態JSONに初期設定完了フラグだけを補います。再ログインや認証情報・プロジェクトの信頼設定のコピーは行いません。

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
