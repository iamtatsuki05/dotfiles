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

## I-have-ADHDによる返答形式の切り替え

[I-have-ADHD](https://github.com/ayghri/i-have-adhd)を、行動を先に示す返答形式の共有skillとして固定版で管理します。
Codexでは `$i-have-adhd`、Claude Codeでは `/i-have-adhd` と明示して呼び出します。呼び出した会話では `stop adhd mode` または `normal mode` と伝えるまで適用する指示です。
上流のメタデータで自動呼び出しは無効です。常時適用hookや利用者についての永続的な前提は追加せず、既存の文章系skillとAgentの規約は変更しません。
同期後、新しいAgentの会話で追加したskillを読み込んでください。

## UnslopによるUIの改善

[Unslop](https://github.com/heydamir/unslop)は、Damir Sokolovsky（@heydamir）による既存UIの点検・改善用skillです。固定版に補助ツールの利用条件を示す指示だけを追加し、コードは変更せず管理します。Codexでは `$unslop`、Claude Codeでは `/unslop` と指定でき、UI改善の依頼内容から選ばれる場合もあります。既存のデザイン系skillは変更しません。
CC BY 4.0のライセンス、作者表記、`CITATION.cff`を保持します。hookや画像生成サービスの設定は追加せず、有料画像生成には明示的な許可を必要とします。
任意機能のライブサイト取得には実行先プロジェクトのPlaywright、動画作成にはさらにffmpegが必要です。これらは機能を使うときに導入してください。生成したページや動画はローカルファイルで、自動公開されません。
取得したHTMLは未信頼の実行可能コンテンツとして扱い、取得結果や出所不明のHTMLを動画helperへ渡しません。動画helperは `.story-frames` を削除し、出力を上書きするため、信頼済みの生成HTMLを、機密ファイルやsymlinkのない新規の使い捨てディレクトリで処理してください。明示的な上書き依頼がなければ、新しい出力先を使います。
追加したskillは新しいAgentの会話で読み込んでください。

## Notion CLI

Notion公式CLIの `ntn` を、miseのツール（`"npm:ntn" = "latest"`）として導入します。共有skillの `notion-cli` は [makenotion/skills](https://github.com/makenotion/skills) の固定版です。上流の文面からの変更は3点です。まず、導入方法を上流のcurl installerからmiseに置き換えました。次に、存在しない `ntn pages update` を `ntn pages edit` に直し、ページ全体が置き換わることと、削除や確認省略のflagは明示的な依頼があるときだけ使うことを注記しています。最後の1点は `NOTION_API_TOKEN` の確認方法の変更で、値の表示や `ntn auth token` の実行はせず、設定の有無だけを調べる形です。
認証は `ntn login` か、個人用アクセストークンを `NOTION_API_TOKEN` に設定して行います。どちらの方法でも本人のNotion権限で動くため、Agentはあなたのアカウントで閲覧できるページを読み、編集できるページを変更できます。ゲストと制限付きメンバーは、ログインも個人用アクセストークンの作成もできません。複数のworkspaceを使う場合は、`NOTION_WORKSPACE_ID` でコマンドごとに対象を指定するか、workspaceごとのトークンを必要なコマンドにだけ `NOTION_API_TOKEN` で渡してください。shellの起動ファイルでトークンを常時exportすることは避けてください。
追加したskillは新しいAgentの会話で読み込んでください。

## Pstackのワークフロー

Claude CodeとCodexには、固定した [michael-denyer/pstack-claude](https://github.com/michael-denyer/pstack-claude) 版（`7c045e5617b5`、version `0.9.119`）を `dotfiles/.agent/apps/pstack/canonical` から配布します。Cursorは `cursor/plugins@ae5650febf43`、version `0.15.36` を `dotfiles/.agent/apps/pstack/cursor` に保持します。独立した2個の原則skillは、従来の導入元と明示呼び出しの方針を維持します。

`mise run pstack-setup` で、導入済みのClaude Code・Codex・Cursor Agentへそれぞれの版を登録します。`--agent claude|codex|cursor` で対象を限定でき、`--dry-run` では変更せず予定を表示します。通常の全体セットアップでもmiseのツール導入後に実行します。登録名は `pstack@dotfiles-pstack` のままです。両native marketplaceは統合された `canonical` を参照し、各実行環境用のmanifestを使います。Claudeはinstallに続けて対象scopeのupdateを行い、参照先とcacheの版情報を更新します。既存アカウントも対象とし、`CLAUDE_CONFIG_DIR` を指定した場合はその設定先だけに登録します。

Cursorの `~/.cursor/plugins/local/pstack` には保持した旧版だけを配置します。再実行では同じ内容を保持し、記録と内容が一致する管理済みコピーだけを更新します。非管理の同名ディレクトリやローカル変更がある場合は停止します。起動ごとに指定するパスは `cursor-agent --plugin-dir <repo>/dotfiles/.agent/apps/pstack/cursor` です。`canonical` の変更でCursorの版が切り替わることはありません。

再起動後、Codexでは `$pstack:poteto-mode`、Claudeでは `/pstack:poteto-mode` と指定してください。新しいforkは67 skillで、従来のcanonical版に対して `babysit`、`deslop`、`fix-ci`、`fix-merge-conflicts`、`get-pr-comments`、`make-pr-easy-to-review`、`thermo-nuclear-code-quality-review`、`thermo-nuclear-review`、`thermos`、`what-did-i-get-done` が追加され、`make-bot-ui` は含みません。Cursorは元の58 skillを維持します。旧Benny automationテンプレートはnative版には含まれず、保持したCursor版に残ります。セットアップは既存の稼働中automationを変更しません。名前空間付きの `pstack:unslop` と、独立したUIデザイン用の `unslop` は別のままです。

新しいClaude Code・Codex用SessionStart hookは、モデル設定シートを読み、複数ファイルの変更・設計・原因未特定の不具合調査でpstackへ誘導する文面を出力します。通信や永続ファイルへの書き込みはありません。Codexでは実行環境のhook信頼操作が必要で、セットアップはその許可を与えません。既存の `session hook: off` 設定も尊重します。導入時に `setup-pstack` の実行、モデル設定シートの変更、helperの初期セットアップ、automationの起動は行いません。commit・公開・有料サービス利用の許可も追加しません。各入口には従来のローカル統合方針を適用し、利用できないモデル・ツール・実行経路へ黙って代替しないよう制約しています。

## 選定したCursorプラグインのskill

2026-11-02に `cursor/plugins@ae5650f` の全129件を確認し、4つの提供元から指示文のみの9個のskillを選定しました。pstack全体は別途導入済みです。既存の共有skill配置を使い、新しいhook・常時ルール・MCPサービス・カスタムAgentは登録しません。

- `cursor-team-kit`：`control-cli`、`control-ui`、`deslop`、`verify-this`。CLI/UIの実証と、pstackが参照する操作・コード整理の依存を補います。
- `cli-for-agent`：`cli-for-agents`。非対話CLIの設計・レビューに使います。
- `create-plugin`：`create-plugin-scaffold`、`review-plugin-submission`。リポジトリ管理下でのプラグイン作成と、提出前の静的確認に使います。
- `teaching`：`create-learning-path`、`run-learning-retrospective`。学習計画と、実績に基づく進捗の見直しに使います。

MITライセンスとCursorの帰属情報を保持しています。宣言した指示差分で、作業範囲・既存コメント・ユーザーが起動したプロセスを保護し、非対話CLIの入力不足は勝手に対話モードへ移らずエラーにする方針を明記しました。プラグインの作成と有効化も区別します。ブラウザ・端末の操作は現在のホストの規約に従い、今回の導入では実操作や新しいプラグインの公開を行いません。

残る124件は、既存機能との重複、自動記憶・ループ方針、未完成のcanvas、または現在の用途がないホスト・サービス・SDK依存を理由に見送りました。第三者サービスの112件は有効化せず、既存の接続を保持します。4プラグインの全機能ではなく、上記skillだけの選択導入です。

## Agentごとのskill配布

PiとDeepSeekには、クライアント同期cacheを新たに取り込まず、管理対象のskillを個別リンクで配布します。同じGitリポジトリの別checkoutから明示的に移行する場合は、配布元にするcheckoutで次を実行します。

```sh
python3 -B scripts/sync_agent_skills.py --repo-root "$PWD" --agent pi --migrate-from /absolute/previous-checkout/dotfiles/.agent/skills --dry-run
python3 -B scripts/sync_agent_skills.py --repo-root "$PWD" --agent pi --migrate-from /absolute/previous-checkout/dotfiles/.agent/skills
# DeepSeekの場合は --agent deepseek を指定します。
```

旧checkoutと個人・同期skillのファイルは削除しません。Piでは元の場所への参照を保持するため、旧checkoutを移動・削除する前に、その参照も別途移す必要があります。通常の同期では、未承認の配布元変更やユーザーファイルとの衝突を拒否します。この専用helperはモデル・hook設定を変更しません。

Antigravityのdotfiles-agent manifestには、管理対象のネストしたskillを明示列挙します。既定の直下探索で見つかる既存候補は保持します。native pluginのルートは実ディレクトリのままにし、その配下の参照にsymlinkを使います。

Grokは `~/.grok/dotfiles-skills` 経由の個別パスを `[skills].paths` に登録します。既存のbundled `docx` は上書きせず、`pptx` を同期cacheから追加することもしません。他の互換経路の候補まで消える一括 `ignore` は設定しません。管理skillを追加したときは配布リストも更新し、`tests/test_skill_distribution.py` で網羅性を確認します。

## Canonical file と管理境界

- [`AGENTS.md`](../AGENTS.md): dotfiles 固有の規約。agent home には配布しません。
- `dotfiles/.agent/AGENTS.md`: 常時読む共通規約。委譲と作業記録は `agent-delegation` と `agent-work-log` を条件付きで参照します。
- `dotfiles/.agent/apps/`: アプリ別設定と hook。
- `dotfiles/.agent/skills/`: local skill と review 済みの vendored skill。
- `dotfiles/.agent/evals/`: Waza evaluation suite。
- `dotfiles/.agent/sync.sh`: 対応する agent home への同期処理。

リポジトリ直下の規約は、共通規約への symlink ではなく独立した通常ファイルです。
一部の展開先は canonical tree を指す symlink です。展開済みファイルは別の
source of truth ではありません。

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
