# Agent Team

[English](README.md)

`agent-team`は、通常の`claude`や`codex`の設定を変えずに、選択した実行環境で
プロジェクト単位のチームを起動します。既定の`runtime = "orca"`では、
Planner → Worker → Reviewerの流れと、Task・メッセージ・端末・実行状態をOrcaが管理します。
native runtimeの`tmux`、`herdr`、`zellij`では、agent teamはdirect Claude Main、version 5の
`program`/`serial`と`program`/`parallel`はMain modelを置かないprogram coordinatorを使います。
どちらも設定したClaude ACPのPlanner、Worker、Reviewerを動かします。native Workerはconfigに宣言した
TaskSpecとscoped Claude ACP policyを使い、terminal driverだけがruntimeごとに変わります。
native `program`/`parallel`は`max_active`まで独立したassignmentを受け付け、nodeごとにDelivery stateを保存します。
Main-agentのparallelと名前付きOrcaのparallelは引き続き拒否します。実装とfocused contract testに加え、
boundedな実端末・fake providerのcoverageがあります。実モデルのparallel受入は未実施です。
native Claude ACP assignmentには、既存ACPのform elicitationとprivate question socketを使う
制限付きの`AskUserQuestion` pathもあります。同じTask/Dispatch内で動きます。契約と現在の証拠は
[アーキテクチャ](docs/architecture_JA.md)にまとめています。実モデルでの質問応答はtmuxで確認済みです。
HerdrとZellijでは、実際の端末と模擬プロバイダーを使って契約を検証しています。

初めて使う場合は、「managed commandを導入する」「起動前の条件を満たす」
「teamを起動する」を読んでください。実装や設定を変える場合は、詳細ドキュメントも
参照してください。

## 最初に読む場所

- [teamを起動する](#teamを起動する)は通常の利用手順です。
- [アーキテクチャ](docs/architecture_JA.md)はruntimeと安全境界を説明します。
- [設定リファレンス](docs/configuration_JA.md)はconfig version 3、native TaskSpec catalog、
  対応するprovider/transportの組み合わせを説明します。[Version 4の設定](docs/configuration-v4_JA.md)
  では、team名による選択、graphの確認、起動設定への参照を説明します。
  [Version 5の設定](docs/configuration-v5_JA.md)では、node ID、nodeごとの設定、
  taskの担当、nativeのserial/parallel program実行を説明します。
- [Harness対応matrix](docs/support-matrix_JA.md)は、認識済み・利用可能・実行可能・
  拒否を区別します。
- [ACPの境界](docs/acp_JA.md)はadapter pin、認証、ACPがsandboxではない理由を説明します。
- [Direct background adapter](docs/background-adapters_JA.md)はCopilot/OpenCode向けread-only
  adapter実装、snapshot境界、復旧方法を説明します。

現在の設定は次のとおりです。

| Role | Provider / transport | Model / effort | Permission |
|---|---|---|---|
| Main | Claude / `direct` | `fable` / `high` | `orchestrator` |
| Planner | Claude / `acp` | `fable` / `high` | `read-only` |
| Worker | Codex / `direct` | `gpt-6-astra` / `medium` | `workspace-write` |
| Reviewer | Codex / `direct` | `gpt-6-astra` / `high` | `read-only` |

bundled Orca configでは起動直後に動くのはMainだけです。Planner、Worker、Reviewerは
必要なときだけ起動し、background roleは同時に1つしか動きません。

bundled configは、これまでどおり4 roleのOrca構成です。version 3のcustomなnative configでは、
Mainをdirect Claude・permission `orchestrator`として定義し、verified Claude ACPの
Planner/Reviewer（`read-only`）とscoped Worker（`workspace-write`）を追加できます。
native Workerへdispatchするには、一致する`[[tasks]]` entryが必要です。その他の未対応profileは、state、
Task、Dispatch、processに影響する前に拒否します。

nativeの質問応答は、選択したClaude ACP assignment内の追加通信です。roleのpermission、
TaskSpecのfile scope、Bash・external-tool policyは変わらず、Codexの質問応答も有効にしません。
完全検証済みの`0b3e5bc` milestoneは過去の証拠です。tmuxのbounded acceptanceと協調的な検証状況は
[アーキテクチャ](docs/architecture_JA.md)に記載します。version 5のnative `program`/`serial`はMainなしで接続し、
native `program`/`parallel`にも実装、focused contract test、boundedな実端末・fake providerのcoverageがあります。
serialの実モデル試験は実装・review・検証の前にprovider利用上限で停止しました。実モデルのparallel受入、全harness、
Codex認証、Orcaとnativeに共通する進行管理は別の実証gateとして残ります。

Version 5では複数のWorkerとReviewerに名前を付け、taskごとの担当を指定できます。
nativeの`agent`/`serial`に加えて、Main roleを置かない`program`/`serial`と
`program`/`parallel`も接続しています。実モデルのtmux試験では、4件の独立したassignmentで
2つのTaskSpecを処理し、質問応答、レビュー、同じ統合revisionでの固定argv検証、公開コマンドによる停止まで確認しました。
既定profileは上表のままで、この試験では5nodeすべてにClaude Fableを明示指定しています。この実モデルrunはnativeのagent/serial受入です。
native parallelにはfocused contract testとboundedな端末coverageがありますが、実モデルのparallel受入は未実施です。
Main-agentのparallelと名前付きOrca構成は実行時に利用できません。
名前付きnativeのReviewer相談はopaqueなIDで回答できます。再開には元のwriterと上限内の再reviewが必要です。
回数上限に達している場合は、回答を保存してもtaskは未解決のままです。

## checkoutから実行する、またはprojectをinstallする

このprojectはPython標準libraryだけで動きます。Python 3.11以降が必要です。checkoutからは
launcherを直接実行できます。

Orcaのライフサイクルbackend、実験的なnative terminal backend、bounded provider runnerはPOSIX専用です。
runtime metadataがUnix socketまたはprivateなprocess groupを必要とするため、Windowsでは
実行前に明示的に拒否します。
CLI名はplatformごとに固定し、macOSでは`orca`、Linuxでは`orca-ide`を使います。PATH fallbackや環境変数overrideは行いません。

```bash
scripts/agent-team/agent-team harnesses
scripts/agent-team/agent-team start --dry-run
```

隔離した環境へinstallする場合は、任意のPython環境でprojectをbuild/installします。
console scriptと`python -m agent_team`は同じpackageとbundled defaultを使います。team起動時は
同じPython環境のconsole scriptを解決し、別のinstallへfallbackしません。

```bash
python3.13 -m venv /tmp/agent-team-venv
/tmp/agent-team-venv/bin/python -m pip install scripts/agent-team
/tmp/agent-team-venv/bin/agent-team harnesses --json
```

## managed commandを導入する

このdotfiles repositoryで、通常のagent file syncを実行します。

```bash
zsh dotfiles/.agent/sync.sh
command -v agent-team
```

syncはproject launcherを`~/.local/bin/agent-team`へmanaged linkとして配置し、dotfiles側の
config/promptsを`$XDG_CONFIG_HOME/agent-team`へlinkします。Python packageのinstallやteamの
起動は行いません。config directoryに空でない既存directoryがある場合は触らず、bundled default
を利用できる状態を保ちます。

## 起動前の条件を満たす

Orcaの実機確認はmacOSで行っています。tmuxでは、OrcaとCodexがない環境、空白を含む
作業パス、削除済み設定ファイルを使い、CLIの起動・状態確認・停止に成功しました。
さらに実tmuxと模擬providerを使い、MCPでのread→release→ack、別CLIからの実行中処理の
中断・停止、所有資源の回収を確認しました。これらは実モデルを使った検証ではありません。

2026-09-06に、実際のClaude Code 2.1.261を使った`fable`/`high`のMainが、MCPで
Claude ACP Plannerを呼び出し、prompt→wait→read→release→ackを完了しました。
Plannerは指定ファイルを読み取り、正常な結果を返しました。公開stopで所有プロセス、state、
socket、prompt、一時directoryの消滅も確認しています。隔離したPython環境にはこのpackageだけを
導入し、Orca、Codex、OpenCode、Zellij、HerdrをPATHから除外しました。
確認できたのは当時の読み取り専用経路です。後述するnative TaskSpecの変更作成・レビュー
workflowとは別の検証です。

以前の拒否は、Nix側の古いClaude Code 2.1.112を選んだことが原因でした。
正式IDの`claude-fable-5-1`を指定すると`claude_code_version_too_old`が返り、既に導入済みの
2.1.261では同じ`fable`が成功しました。PATHが選ぶ実行ファイルと版を確認してください。
この失敗を解消するためのモデル変更は不要です。版の条件は公式の
[モデル設定](https://code.claude.com/docs/en/model-config)を参照してください。

LinuxのOrca実行ファイルは`orca-ide`に固定していますが、Linuxでの実機確認は未実施です。
Windowsは非対応で、実行前に拒否します。Orcaの実行ファイルはOSごとに固定し、
別名のPATH探索や環境変数による置き換えは行いません。

- macOS: `orca`
- Linux: `orca-ide`

Codexも版の確認が必要です。WorkerとReviewerを別々に起動したdirect経路の試験では、
0.152.1が`gpt-6-astra`に
新しいclientを要求し、既に導入済みの0.153.4では設定どおりのAstra WorkerとReviewerが動きました。
Workerによる指定ファイルの作成とReviewerによる読み取りを確認し、別の`:read-only` sandbox試験で
書き込みの拒否を確認しました。これは未実装のチーム内レビュー・検証制御やOrcaの後始末の証拠ではありません。

2026-09-07には、隔離したPython 3.13.15のwheel-only環境で、Claude Code 2.1.261の
`fable`/`high` Mainからnative Claude ACPのPlanner/Worker/Reviewerを呼び出しました。
Planner 1回、plan Reviewer 1回、Worker 2回、implementation Reviewer 2回の計6 assignmentです。
意図的な`a-b`実装は差し戻され、`a+b`が同じworkspace revisionで承認され、trustedな
fixed-argv verificationも成功しました。元のconfigとpromptを削除した後のpublic `stop`で、
所有processとartifactは0件でした。この再試験は、起動時のTaskSpec catalog、PID/PGID/argvの
gate、4 fileの依存bindingを含む実装で行いました。専用npm環境には選択したClaude ACPと
その依存だけを導入し、acpxや他のharness packageは含めていません。完了後も起動時のcatalogは変わらず、
Mainが`NATIVE_WORKFLOW_OK`を報告しました。
これは`308b1ba`時点の以前のtmux generation proofです。

別のlive SDK probeでは、許可pathの編集と禁止pathの拒否を確認しました。`persistSession=false`、
`autoMemoryEnabled=false`で、選択したSDK processとClaude project directoryは残りませんでした。
別のactive cancel probeでは、同じ最終runtimeで実行中のnative Workerを停止し、所有processとartifactが
0件でした。この過去のprobeが確認したのは、所有するOS process groupとpathのcleanupだけです。明示的なACP
session closeは確認しておらず、以前のPython stop経路はprocess groupの終了をsession cleanupへ昇格していました。
いずれも限定されたnative checkであり、すべてのruntime、harness、recoveryの証拠ではありません。

新しいterminal driverには、modelを呼び出さないfake Main/Nodeのpublic CLI evidenceがあります。
最新コードでPython 3.11と3.13の両方を使い、tmux、Herdr、Zellijそれぞれ3 case（MCP
read/release/ack、active cancel、Main自然終了後のconfig/prompt削除とcold status/stop）を実行し、
PID、socket、state、config、private rootの消失を独立確認しました。これはfake providerのterminal
contractだけを証明します。

別の実Claude Code 2.1.261 workflowでは、HerdrとZellijの両方を、Python 3.13.15のisolated
wheel-only環境、Node 22.23.2、Claude ACP 0.70.0、SDK 1.3.0、Claude SDK 0.3.232で実行しました。
各runはClaude Max header付きのFable 5.1、effort `high`のdirect Claude Mainを使い、Planner、
Reviewer、Workerの6 assignment（plan approve、implementation request_changes、implementation
approve）を自律的に完了しました。承認済みworkspace revisionのtrusted fixed-argv verificationは
`FIXED_ARGV_OK`を返し、TaskSpec catalog、Worker scope、protected file、kernel identityを確認しました。
元のconfigとpromptを削除してからpublic `stop`を実行し、所有PID/PGID、state、socket、private pathが
残っていないことを独立確認しました。通常のinteractive Main historyは残し、自動SDK callには
`persistSession=false`を使いました。
runtime package 51 fileはbuilt wheelとbyte-levelで一致しました
（`655c3bc3c24a278c366cd6282bb2870d10129806f6f312d765329463b47afb7b`）。
選択したACP依存のpackage metadataを122件確認しました。未選択packageの不在は依存一覧で照合し、
未選択CLIと`npm`、`npx`、`uv`の不在は実行時の`PATH`で別に検査しました。

Herdrの初回はtextとEnterを同時にpasteしたもののtextが貼付欄に残ったため、同じ初回messageを別のEnterで送信しました。
追加の指示は送らず、6 assignmentは自律的に進みました。typed verificationは完了しましたが、停止前に最終画面の
`NATIVE_WORKFLOW_OK` markerは観測していません。Zellijでは初回submissionからworkflow全体が自動で進み、
最終markerを観測しました。従来の実モデルworkflow proofであるtmuxのrunは`308b1ba`時点のものです。
今回のHerdr/Zellijのbounded runも、全harnessやrecovery経路の証拠ではありません。

別の実Herdr active-cancel probeでは、public MCPからWorkerをdispatchし、`CANCEL_STARTED`を観測したうえで、
同じWorkerのlive kernel PID/PGIDとnative resultの不在をpublic stop直前に再確認しました。独立readbackで
所有PID/PGID、process reference、pathが残っていないことを確認しました。これはHerdrのClaude ACP
cancelに関する代表的な証拠であり、すべてのharnessの証拠ではありません。確認したのはOS group/pathのcleanupだけで、
明示的なACP session closeは確認していません。古いprocess groupベースのcleanup判定にも同じ限界があります。

実Claudeを使ったtmuxの質問応答試験では、run `dc101afd-87bf-4697-9bbb-0d1339d381a8`を完了しました。
Claude Code 2.1.263、Node 22.23.2、Claude ACP 0.70.0、SDK 1.3.0、Claude SDK 0.3.232を使い、
Main、Worker、Reviewerは`fable`/`high`、Plannerは省略しました。Mainが質問2件に回答して受領確認した後、同じWorkerの
ACPセッションを再開し、Reviewer承認と同一リビジョンの固定コマンド検証を経てTaskが完了しました。
別の質問待ち試験では、協調的な停止と型付きACP終了記録を確認しました。両試験とも、所有するリソースはすべて回収済みです。
[アーキテクチャ](docs/architecture_JA.md)に確認範囲、保持している過去の失敗、他の実行環境の制約を記載しています。

以前のtmux proofである`308b1ba`では、wheel-only環境からnative必須command（`node`、
`claude-agent-acp`、`tmux`、`claude`）を1つずつ欠落させると、state作成前に拒否しました。
これは旧tmux generationのpreflight evidenceであり、Herdr/Zellij workflow runでこれらを欠落させたことを示しません。

起動前に次を確認してください。

1. 選択したruntimeとharnessのcommandを利用できる。既定のteamは上記のOrca実行ファイル、
   `claude`、`codex`と、後述のACP toolを使います。native configでは選択した`tmux`、
   `herdr`、または`zellij`も必要です。
2. `runtime = "orca"`ではOrcaを起動し、platform固有の`status --json`でruntimeとgraphが
   readyであることを確認します。`runtime = "tmux"`、`"herdr"`、`"zellij"`では選択した
   terminalが利用できることを確認し、
   Orcaは必要ありません。
3. 選択したproviderを使うaccountへloginしている。bundled Orca roleではClaudeとCodexの両方、
   native runtimeではClaudeが必要です。
4. `runtime = "orca"`では対象repositoryをOrcaへ一度登録している。

現在のnative runtimeは、通常のホームディレクトリにある標準のClaudeログインを使います。
Claudeの実行ファイルを直接起動するため、`claude-account`の既定プロファイルを選択せず、
`CLAUDE_CONFIG_DIR`も渡しません。native teamを起動する前に、
`env -u CLAUDE_CONFIG_DIR claude auth status`で標準のログインが意図したアカウントであることを
確認してください。名前付きアカウントプロファイルにはまだ対応していません。

```bash
# bundled Orca configのprovider
command -v claude
claude --version
claude auth status
command -v codex
codex --version
codex login status
# macOSのOrca runtime
orca status --json
orca repo add --path "$PWD"
# LinuxのOrca runtime
orca-ide status --json
orca-ide repo add --path "$PWD"
# native tmux runtime
tmux -V
# native Herdr runtime
herdr --version
# native Zellij runtime
zellij --version
```

OrcaのACP roleを選択するconfigにはNode.js 22.13以降と、`acpx@0.13.2`、
`@agentclientprotocol/claude-agent-acp@0.70.0`のcommandが必要です。利用するtoolは、
例えば次のように指定したdirectoryへ事前に導入してください。

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

起動時に実行ファイルのpathとfingerprintを保存し、実行時はそのファイルを直接使います。
`npm`や`npx`は呼び出しません。依存が不足した場合やファイルが変わった場合はエラーにします。
direct transportだけのteamにはACP toolは不要です。

native tmux、Herdr、ZellijのACP roleにはNode.js 22.0.0以降、`@agentclientprotocol/claude-agent-acp@0.70.0`、
その依存である`@agentclientprotocol/sdk@1.3.0`が必要です。nativeはNode、Claude ACP entrypoint、
`dist/lib.js`、SDKのabsolute pathとSHA-256 fingerprintを保存し、assignmentごとにpublic SDK接続を1本だけ
使います。nativeでは`acpx`を選択せず、通常SDK persistenceを`persistSession=false`、
`autoMemoryEnabled=false`に固定します。これはpublic SDKへの直接接続であり、providerの
direct/model transportではありません。interactive Mainの通常Claude historyは残ります。

```bash
npm install --prefix /path/to/agent-team-native \
  @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-native/node_modules/.bin:$PATH"
```

config version 2で起動したteamが残っている場合は、version 3へ切り替える前に、
旧codeの`agent-team stop`で停止してください。legacy fallbackはありません。

## teamを起動する

Orcaやagentを起動せず、role metadataとdirect agentの引数を確認します。

```bash
agent-team start --dry-run
```

dry runでは、Taskごとに生成するACP commandまでは表示しません。ACP commandは、
ACP roleをDispatchするときに作ります。

Mainを起動し、管理対象のterminalへfocusします。

```bash
agent-team start
```

backgroundで起動する場合は`--no-attach`を付けます。

```bash
agent-team start --no-attach
```

Mainへ開発作業を依頼してください。bundled Orca configでは、MainがPlannerの要否を判断し、
`agent_team` MCP serverを通じてWorkerとReviewerを起動します。native runtimeでは、configに
含まれるClaude ACPのPlanner、Worker、Reviewerだけを依頼できます。native Workerは選択した
configの`[[tasks]]`に完全一致するTaskSpec付き`task_dispatch`で起動します。
ユーザーと対話するroleはMainだけです。

team名で選ぶ場合は、同梱の一覧、またはsync後の`teams.toml`を指定します。

```bash
agent-team start --config ~/.config/agent-team/teams.toml --team agent-team --dry-run
```

teamの追加、graphの確認、選択したteamの起動は、[Version 4の設定](docs/configuration-v4_JA.md#名前を指定してteamを起動する)
を参照してください。

## teamを確認して停止する

```bash
# 選択したruntime、Main terminal、active roleの状態を確認する。
agent-team status

# Mainへfocusする。
agent-team attach main

# Orcaの場合だけ: Mainがbackground roleを起動した後に、そのroleへfocusする。
agent-team attach worker

# teamが所有するterminalを停止し、runtime stateを削除する。
agent-team stop
```

`stop`は選択したruntimeのbackendを使い、そのruntimeが所有するresourceだけを削除します。
Orcaの場合はRunを監査記録として残します。project fileのcommit、push、publish、削除は
行いません。

管理コマンドは、起動時に保存した設定を使います。元の設定やpromptファイルを
変更・削除しても管理できます。対象の実行を選ぶには、`start`と同じ`--cwd`と、
指定した場合は同じ`--config`・`--team`を使ってください。`--config`はversion 4の
カタログも含めた元の入力パスと照合し、ファイルを読み直しません。選択条件の省略は、
一致する保存済みの実行が1件だけの場合に限ります。

```bash
agent-team start \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project

agent-team status \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project
```

`status`・`attach`・`stop`では、`--state /absolute/path/to/state.json`で
現在のディレクトリに関係なく保存済みの実行を選べます。`--team`とは併用できません。
`--config`も指定する場合は、保存したパスとの一致が必要です。

## native runtimeのTaskSpec workflowを使う

native runtimeのTaskSpecは、選択したversion 3 configでuserが宣言します。各`[[tasks]]`
entryはそのrunで変更できず、`[[tasks.verification]]`はimplementation approval後に使う
fixed argvを宣言します。

```toml
[[tasks]]
task_id = "addition-workflow"
objective = "許可されたsource fileにadd(a, b)を実装する"
acceptance_criteria = ["整数、負数、小数の加算が通る"]
allowed_paths = ["workflow-fixture/calc.py"]
forbidden_paths = [
  "workflow-fixture/protected.txt",
  "workflow-fixture/verify_calc.py",
]
dependencies = []
evidence_requirements = ["変更pathとcommand結果"]
consultation_conditions = []

[[tasks.verification]]
name = "check-addition"
argv = ["python", "-B", "workflow-fixture/verify_calc.py"]
timeout_seconds = 30
```

Mainが呼べる`task_dispatch`は、宣言済みentryと完全一致するTaskSpecだけです。dispatch時に
新しいtask ID、path、dependency、verification argvを発明できません。起動前にIDの重複、
未宣言dependency、dependency cycleを拒否します。native configに`[[tasks]]`がなければ、
read-onlyの`role_prompt`は使えますが、structured task dispatchは拒否します。Orcaは`tasks`
fieldを拒否します。

実際の順序は次のとおりです。

```text
task_dispatch（PlannerまたはWorker）
  -> role_wait -> role_read -> role_release -> delivery_ack
  -> task_get
  -> task_dispatch（planまたはimplementation reviewのReviewer）
  -> request_changes後はPlannerまたはWorkerへ戻る
  -> implementation approve後にtask_verify
```

`role_wait`が`question`を返した場合は、各eventの`message_id`へ`message_reply`を送り、
その後に`delivery_ack`を呼びます。回答を先に保存し、assignmentのprivateな`q.sock`へ送り、Nodeの
`received`、Pythonのdurableな消費記録、`recorded`の順に確認してから同じACP sessionを再開します。
同じIDと同じ本文の再送はidempotentですが、異なる本文は拒否します。1 batchは1〜4問、各question/answerは
20,000文字以内、frameは512 KiB以内、1 assignmentは最大64 batchです。question Deliveryが保留中は、
そのassignmentの`role_read`、`role_release`、別dispatchを拒否し、そのassignmentのsuccessful completionも拒否します。
version 3/4のnative serial stateでは、questionが消費されるまでrun全体の次のdispatchも止まります。
version 5のnative `program`/`parallel`では、`max_active`内でWorkerのwrite scopeが重ならない独立assignmentを
継続できますが、`task_verify`は全active assignmentとDeliveryのdrainが終わるまでrun全体で拒否します。
その間にstopした場合は明示的なcancellationとして扱い、provider、process group、socket、private cleanupが
不明ならstateを保持します。名前付きnativeの`agent`または`program`でReviewerが`consult`を返すと、`status`にopaqueな相談IDを表示し、
`answer --consultation-id ID --body ...`でboundedな回答を保存します。review roundが残っていれば、元のwriterを再実行してからreviewをやり直します。
上限到達後は回答を保存しても再dispatchを許可しません。回答だけで承認にはならず、回数もリセットしません。

nativeの完了・停止証拠はfail-closedです。typed client-result artifact、clientのexit status、取得時のstdout parity、
session identity、process groupの証明をすべてそろえる必要があります。cancellation Eventはcleanupを要求するための
制御情報であり、cleanupの証拠ではありません。public stopはsignal前に`native.phase=stopping`を保存し、同時に
providerが成功してもstopを優先してReviewer evidenceを破棄し、failed outcomeを保存します。証拠が欠ける場合や
判定できない場合はassignmentとstateを保持して調査します。

planとimplementationのreviewは`max_review_rounds`を別々に数えます。Reviewerの出力は
`task_id`、`stage`、`revision`、`decision`、`findings`だけを持つexact JSONです。
implementation reviewと`task_verify`は同じworkspace revisionに束縛されます。宣言済みの
fixed argvが全件成功し、cleanupが確認できた場合だけ`completed`として報告します。
[設定リファレンス](docs/configuration_JA.md#taskspec-catalog-is-optional-required-for-native-task-dispatch)に全fieldと10 toolをまとめています。
verificationがevidence付きで失敗してもcleanupが確認できれば、implementationのreview round上限内で
Workerへretryできます。cleanupが不明な場合はユーザー判断が必要で、stateを保持します。

## 安全境界を理解する

- 未対応のruntime、provider、transport、permission、config version、state formatは、起動前に
  拒否します。別backendや別transportへ自動で切り替えません。
- Orcaは4 role固定です。version 3のnative runtimeはMainを必須とし、verified Claude ACPのread-only
  Planner/Reviewerと、scoped Claude ACPのworkspace-write Workerを任意に追加できます。version 5の
  native `agent`/`serial`はMainを使い、`program`/`serial`は保存済みcoordinatorを使います。
  native Workerの`task_dispatch`にはconfigの`[[tasks]]` entryとの一致が必要で、その他の未対応profileは起動処理の効果が発生する前に拒否します。
- nativeの`start`、`status`、`attach`、`stop`は選択した`TmuxBackend`、`HerdrBackend`、
  `ZellijBackend`を共通`NativeBackend`から使います。`attach`はagent teamのMain、または`--coordinator`を指定した
  program coordinatorへ接続します。`native_main`は所有するMain process groupまたは固定`_program-run` coordinator childを監督します。
- native ACPの完了はterminal paneの文字列ではなく`publish_completion`で通知します。lifecycleの
  順序は`role_read` → `role_release` → `delivery_ack`です。nativeの`last_ack`は1つのreceipt
  markerを記録するだけで、Taskやユーザーのgoal全体の完了を意味しません。
- version 5のnative `program`/`parallel` stateは、activeなnodeごとにresult、question、
  pending Deliveryのcontainerを持ちます。`max_active`、正確なnode identity、重ならないWorker write scopeで
  admissionを判定します。pending questionは自分のassignmentだけを止め、条件を満たす独立peerは継続できます。
  public `stop`は`native.phase=stopping`を保存し、安全なpeerをRead → Release → Ackの順でprivateにdrainします。
  identity不明、typed result不足、cleanup未確認のnodeは保持したまま、安全なpeerの処理を続けます。
- native Claudeのquestionは、pinned ACP 0.70.0 / SDK 1.3.0の既存`AskUserQuestion` form elicitationを
  使います。消費済みreceiptにはidentityとhashだけを残します。protected outboxには、公開失敗から
  復旧できるよう、次のquestionまたはterminal completionまでquestion/answer本文を保持する場合があります。
  Codexのquestion socketとcapabilityは無効のままです。
- native WorkerのRead/Glob/Grepは、保護pathやlink・file typeの検査を除き、workspace内を
  読めます。TaskSpecの`allowed_paths`と`forbidden_paths`はWrite/Editだけを制限し、書き込みは
  禁止pathを優先します。Bash、terminal、その他の
  RPCは拒否します。これはin-bandのmodel/tool境界であり、同じuserのhostile processがfileを
  同時に差し替える攻撃は防ぎません。
- ACPのpermission制御はOS sandboxではありません。bundled Orcaの書き込みroleは、専用permission
  profileを持つdirect Codexです。nativeの書き込みは上記scoped Workerに限定します。
- Agentの出力は信頼しません。Task、Dispatch、terminal、sender、Deliveryの
  identityが一致したときだけlifecycleを進めます。
- native taskは`task_get`が`completed`を返した場合だけ完了です。Reviewerの承認や
  `native.last_ack`だけでは完了しません。implementation reviewと`task_verify`は同じ
  workspace revisionに束縛されます。
- workspace revisionはsymlinkとspecial fileを拒否し、5,000 file、1 file 10 MB、合計100 MBに
  制限します。任意repository全体のcoverageは主張しません。
- verification中断またはcleanup不確認では`verifying`などのstateを保持し、必要に応じてstop、
  新しいrole、再verificationをblockします。自動recoveryは主張しません。
- Herdr 0.8.2はprivate headless serverと通常shell bootstrapを使い、`HERDR_ENV`を偽装しません。
  Main自然終了でpane/workspaceが消える場合も、server/socketのownershipとMain cleanupを確認するまで
  stop成功とは扱いません。
- Zellijは0.44.1で互換性を確認しており、persistent clientなしのdetached sessionを使い、`--max-panes 1`を使いません。
  Main paneと既知のsuppressed `zellij:link` pluginをJSONとprocess identityで確認し、未知のpane/pluginは保持します。
- frozenな`supervisor_argv`や完全なMain process receiptがない古いnative stateは自動再構成・migrationせず、
  一致するexecutable/versionで停止してからupgradeします。
- Claude ACPはambientな`claude.ai` loginを使い、API keyをchild processへ
  渡しません。ただしsubscription billing ledgerそのものは未確認です。
詳しい境界と失敗時の流れは、[アーキテクチャ](docs/architecture_JA.md)を参照してください。

## よくある失敗を調べる

Orca 1.4.190では、非表示の検出済みworktreeに作ったterminalの終了が
`runtime_error: tab_not_found`で失敗する場合があります。Mainだけでなく、
単純な`sleep`でも再現しました。この場合は停止失敗を報告し、`state.json`と
`.cleanup.json`を保持します。一覧からterminalが消えたことだけでは、processの停止を
確認済みにはしません。teamを再利用する前にOrca側の終了処理を解決してください。
再起動を通すためにstateを削除しないでください。残件は[#11](https://github.com/iamtatsuki05/dotfiles/issues/11)
で追跡します。

| 症状 | 確認する内容 |
|---|---|
| `workspace is not managed by Orca` | macOSでは`orca repo add --path "$PWD"`、Linuxでは`orca-ide repo add --path "$PWD"`を実行する。 |
| `agent-team state already exists` | 2つ目を起動せず、`status`、`attach`、`stop`を使う。 |
| `role has no active Orca Dispatch` | Orca runtimeで、Mainがそのbackground roleを未起動か、すでにrelease済み。 |
| `native Worker requires task_dispatch with a TaskSpec` | 選択したnative configの`[[tasks]]` entryと完全一致するTaskSpecで`task_dispatch`を呼ぶ。 |
| `native role is not a Claude ACP role` | 選択したnative runtimeでは、configにあるClaude ACPのPlanner/Worker/Reviewerだけを使う。 |
| authenticationを求められる | agent-team外で`claude auth status`か`codex login status`を確認する。 |
| ACPの依存検査に失敗する | Orcaはacpx package、nativeはClaude ACP 0.70.0、`dist/lib.js`、SDK 1.3.0を使う。選んだ`node_modules/.bin`とNode >=22.0.0を`PATH`に含める。 |
| `approved workspace revision changed` | Workerを再dispatchして新しいrevisionをreviewする。gateを迂回しない。 |
| `verification cleanup is unconfirmed` | 保存stateとprocess/cleanup evidenceを残す。restartのためにstateを削除しない。 |
| roleが`escalation`を返す | 保持されたterminalとRunを調べる。完了として扱わない。 |

## 用語と問い合わせ時の情報を揃える

このガイドで解決しない場合は、repository maintainerへ実行command、config path、workspace、
選択したruntime、関係する最小限のerrorを渡してください。Orca runtimeではOrca versionと
Run/Task/Dispatch IDを、native runtimeではnativeのrun/assignment IDを添えます。認証token、
prompt本文、無関係なterminal出力は含めません。

- **Run**: 1回のteam実行を識別するruntime identity。Orcaではnamespaceとcoordinator inboxも含む。
- **Task**: Planner、Worker、Reviewerへ渡す、範囲を限定した1件の作業。
- **TaskSpec**: path scope、dependency、evidence、fixed verification argvを含む、userが宣言する変更不可のtask policy。
- **Dispatch**: Taskとterminalを結ぶ1回の実行attempt。
- **Delivery**: Mainが内容を処理し、acknowledgeするmessage batch。
- **direct**: providerの通常のinteractive CLI。
- **ACP**: Agent Client Protocol。Orcaは固定したacpx client、nativeは選択したpublic ACP SDK経由で使う。

## 変更を検証する

開発用toolは`pyproject.toml`に宣言し、解決したバージョンとhashを`uv.lock`へ保存します。
実行時の依存は増やしません。次のcommandはリポジトリのrootから実行してください。

```bash
uv sync --locked --project scripts/agent-team --python 3.13
uv run --locked --project scripts/agent-team python -m unittest discover -s scripts/agent-team/tests
uv run --locked --project scripts/agent-team ruff check \
  scripts/agent-team/agent_team \
  scripts/agent-team/tests \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uv run --locked --project scripts/agent-team ruff format --check \
  scripts/agent-team/agent_team \
  scripts/agent-team/tests \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uv run --locked --project scripts/agent-team mypy --strict --python-version 3.11 scripts/agent-team/agent_team
uv run --locked --project scripts/agent-team python -m build --no-isolation scripts/agent-team
DOTFILES_TEST_PYTHON=python uv run --locked --project scripts/agent-team /bin/zsh tests/run.sh
```

CIもPython 3.11と3.13で同じlockを使います。buildではlockから導入したsetuptoolsを使い、
別のbuild環境は作りません。通常の隔離installでも版が変わらないよう、build-system側の
要求も固定しています。ソース配布物にも`uv.lock`を含めます。

開発依存を変更するときは、`uv lock --project scripts/agent-team`を実行し、両ファイルを
commitしてください。`--locked`は不整合のあるlockを自動更新せず、エラーにします。
詳しくは[uvのlock管理ドキュメント](https://docs.astral.sh/uv/concepts/projects/sync/)を参照してください。

Orca、native terminal、ACPの連携を変更した場合は、実環境で範囲を限定したsmoke testも行います。
`stop`後に選択したruntimeのterminal、state、prompt file、session、adapter processが残って
いないことを確認してください。

tmux端末driverの実機確認は、tmuxを導入した環境で、リポジトリのrootから明示的に実行します。

```bash
uv run --locked --project scripts/agent-team python scripts/agent-team/tests/live_tmux.py
```

専用tmux serverを作り、引数の保持とprocessの終了状態を検証して、自分の資源を回収します。
このtestではtmux未導入をエラーとします。通常のsuiteはtmuxを要求せずdriverの契約を検証します。
実機testの対象は端末操作であり、チーム全工程の動作を証明するものではありません。

模擬providerによるnative CLI/MCPの一巡と中断処理は、次のコマンドで検証できます。

```bash
AGENT_TEAM_RUN_LIVE_NATIVE=1 uv run --locked --project scripts/agent-team python -m unittest scripts/agent-team/tests/live_native_contract.py -v
```

選択したnative terminalと、試験用のClaude・Node・ACP adapter commandを使います。他のbackendと
harnessをPATHから除外し、read→release→ack、active cancel、Main自然終了後のcold status/stop、
process・socket・設定・prompt・state・private rootの回収を確認します。モデルへの問い合わせや、
実モデルworkflowは対象外です。

runtimeを明示して実行します。

```bash
for runtime in tmux herdr zellij; do
  AGENT_TEAM_RUN_LIVE_NATIVE=1 AGENT_TEAM_LIVE_RUNTIME="$runtime" \
    uv run --locked --project scripts/agent-team python -m unittest \
    scripts/agent-team/tests/live_native_contract.py -v
done
```

driver単体のcontract test:

```bash
AGENT_TEAM_RUN_LIVE_HERDR=1 uv run --locked --project scripts/agent-team \
  python -m unittest scripts/agent-team/tests/live_herdr.py -v
AGENT_TEAM_RUN_LIVE_ZELLIJ=1 uv run --locked --project scripts/agent-team \
  python -m unittest scripts/agent-team/tests/live_zellij.py -v
```

これはfake providerのterminal evidenceです。実モデルのHerdr/Zellij workflowとHerdrのcancel evidenceは
上記のとおりです。paneやsessionの消失だけをcleanup成功とは扱いません。

公開SDK clientの契約テストは、偽のagentを使います。全ケースの実行には、
Claude用のSDK `1.3.0`とCodex用のSDK `1.4.0`を明示してください。
次のテスト専用の導入例は、CIと同じpackage aliasを使います。

```bash
npm install --prefix /path/to/agent-team-sdk-test --ignore-scripts --no-audit --no-fund \
  @agentclientprotocol/sdk@1.3.0 codex-acp-sdk@npm:@agentclientprotocol/sdk@1.4.0
AGENT_TEAM_SDK_ENTRY=/path/to/agent-team-sdk-test/node_modules/@agentclientprotocol/sdk/dist/acp.js \
AGENT_TEAM_CODEX_SDK_ENTRY=/path/to/agent-team-sdk-test/node_modules/codex-acp-sdk/dist/acp.js \
  uv run --locked --project scripts/agent-team python -m unittest \
  scripts/agent-team/tests/test_scoped_acp_client.py -v
```

個人固有のpathは既定値にしません。NodeまたはClaude用SDKの指定がなければsuiteをskipし、
実行が有効な状態でCodex用SDKの指定が欠けていれば失敗します。
この試験は、公開設定で無効にしているCodex ACPの実モデル対応を実証するものではありません。
