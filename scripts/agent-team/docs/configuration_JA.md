# 設定リファレンス

[English](configuration.md) · [README](../README_JA.md) ·
[アーキテクチャ](architecture_JA.md)

`agent-team`は既存のversion 3固定role設定を維持しながら、明示的なversion 4の
topology設定も受け付けます。version 3の`runtime = "orca"`は4 role固定のcontractを
使い、version 3の`runtime = "tmux"`、`"herdr"`、`"zellij"`はdirect Claude Mainと任意のClaude ACP Planner、
Worker、Reviewerを持つ実験的なnative subsetです。native Workerのassignmentにはconfigの
`[[tasks]]` catalogにあるTaskSpecとの完全一致が必要です。必須値の欠落や未対応の組み合わせは、
roleを起動する前に拒否します。topology schemaとresourceを起動しない確認commandは
[Version 4の設定](configuration-v4_JA.md)を参照してください。

## canonical configから始める

```toml
version = 3
runtime = "orca"
team_prefix = "agent-team"
max_review_rounds = 2

[main]
provider = "claude"
transport = "direct"
model = "fable"
effort = "high"
prompt = "prompts/orchestrator.md"
permission = "orchestrator"

[roles.planner]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/planner.md"
permission = "read-only"

[roles.worker]
provider = "codex"
transport = "direct"
model = "gpt-6-astra"
effort = "medium"
prompt = "prompts/worker.md"
permission = "workspace-write"

[roles.reviewer]
provider = "codex"
transport = "direct"
model = "gpt-6-astra"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"
```

bundled configでは、MainとPlannerに`fable`、WorkerとReviewerに`gpt-6-astra`を使います。
canonical PlannerはClaudeのread-only ACP roleで、canonical WorkerとReviewerはdirect Codexのままです。

## 実験的なnative terminal runtimeを明示的に選ぶ

native runtimeはcustomなversion 3 configでだけ指定します。Mainは必須で、direct
Claude・permission `orchestrator`でなければなりません。PlannerとReviewerは省略するか、
それぞれverified Claude ACP・permission `read-only`・pinned `claude-acp-0.70.0` adapterで
定義できます。Workerはscoped Claude ACPの`workspace-write` profileとして選択できますが、
選択にはReviewerも必要です。Workerへ作業をdispatchするときは、一致する`[[tasks]]` entryが
必要です。direct Worker/Reviewerとその他のnative
profileは、state、Task、Dispatch、processに影響する前に拒否します。
nativeの起動には選択したterminalのcommandが必要で、OrcaやCodexは必要ありません。ACP roleを選ぶconfig
では、後述するNode.jsの最低版とACP dependencyも必要です。

```toml
version = 3
runtime = "tmux"
team_prefix = "native-experiment"
max_review_rounds = 2

[main]
provider = "claude"
transport = "direct"
model = "fable"
effort = "high"
prompt = "prompts/orchestrator.md"
permission = "orchestrator"

[roles.planner]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/planner.md"
permission = "read-only"

[roles.worker]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/worker.md"
permission = "workspace-write"

[roles.reviewer]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"

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

この例は既定の`fable`を維持しています。起動前に`command -v claude`と`claude --version`を
確認してください。実Main/Plannerの経路はClaude Code 2.1.261で成功し、2.1.112はFableに
対応する版を満たしませんでした。nativeの`start`、`status`、`attach`、`stop`は
選択したnative backendへ送られ、`attach`できるのはMainだけです。native ACPの完了はrunnerが
publishし、terminal paneの文字列には依存しません。lifecycleには`role_read` → `role_release`
→ `delivery_ack`の順序が必要です。`native.last_ack`は1つのreceipt markerであり、Taskや
goal全体の完了を示しません。

上のroleと`[[tasks]]`宣言はすべてのnative runtimeで共通です。`runtime`だけを`"herdr"`または
`"zellij"`へ変えると対応するterminal backendを選びます。Main、ACP roleのpermission、TaskSpec
catalog、review gateは変わりません。

以前のモデルを使わないtmux CLI start/status/stop試験は、OrcaとCodexがない環境、空白を含むworkspace
path、削除済みconfigで成功しました。実際のClaude Code 2.1.261を使った`fable`/`high`の
Mainも、ログイン済みの`claude.ai` accountでClaude ACP Plannerを呼び出し、MCPの
read/release/ackと公開stopを完了しました。所有資源の消滅を独立に確認しています。
Python環境には`dotfiles-agent-team`だけを導入し、Orca、Codex、OpenCode、Zellij、Herdrを
PATHから除外しました。これは読み取り専用のMain/Plannerの一巡です。2026-09-07には、
Python 3.13.15のwheel-only環境でnativeのPlanner、Worker、Reviewerを6 assignment実行し、
意図的な`a-b`実装を差し戻し、`a+b`を同じworkspace revisionで承認して、trusted fixed-argv
verificationまで完了しました。元のconfigとpromptを削除した後のpublic stopで、所有processと
artifactは0件でした。再試験は、起動時のTaskSpec catalog、PID/PGID/argvのgate、
4 fileの依存bindingを含む実装で行い、catalogが最後まで変わらないことを確認しました。これは`308b1ba`時点の
以前のtmux generation proofです。以前の2.1.112
での拒否はCLIの版が古いことによるもので、`fable`の利用不可を意味しません。

新しいterminal driverには、modelを呼び出さないfake Main/Nodeのpublic CLI evidenceがあります。
Python 3.11と3.13でtmux、Herdr、ZellijそれぞれがMCP read/release/ack、active cancel、Main自然終了後の
元config/prompt削除とcold status/stopに成功し、PID、socket、state、config、private rootの消失を独立確認しました。
Herdr 0.8.2は通常shell bootstrapを使い、HERDR_ENVを偽装しません。0.44.1で互換性を確認したZellijはdetached mode、
persistent clientなし、`--max-panes 1`なし、Main pane保持です。これはfake providerの証拠だけです。

別の実Claude Code 2.1.261 workflowでは、HerdrとZellijをPython 3.13.15のisolated wheel-only環境で実行し、
Node 22.23.2、Claude ACP 0.70.0、SDK 1.3.0、Claude SDK 0.3.232だけを選択しました。各direct Claude Mainは
Claude Max header付きのFable 5.1、effort `high`で動き、Planner、Reviewer、Workerの6 assignmentを、plan approve、
implementation request_changes、implementation approveの順に自律完了しました。trusted fixed-argv verificationは
`FIXED_ARGV_OK`を返し、catalog、Worker scope、protected file、kernel identityを確認しました。元のconfigとpromptを
削除してからpublic stopを実行し、所有PID/PGID、state、socket、private pathが残っていないことを確認しました。
通常のinteractive Main historyは残り、自動SDK callには`persistSession=false`を使いました。runtime package 51 fileはbuilt wheelとbyte-levelで一致しました
（`655c3bc3c24a278c366cd6282bb2870d10129806f6f312d765329463b47afb7b`）。
選択したACP依存のpackage metadataを122件確認しました。未選択packageの不在は依存一覧で照合し、
未選択CLIと`npm`、`npx`、`uv`の不在は実行時の`PATH`で別に検査しました。Herdrの初回はtextとEnterを同時に
pasteしたもののtextが貼付欄に残ったため、同じ初回messageを別のEnterで送信しました。追加の指示は送らず、typed verificationは完了しました。
停止前に最終画面の`NATIVE_WORKFLOW_OK` markerは観測していません。Zellijは初回submissionからworkflow全体が自動で進み、
最終markerを観測しました。従来の実モデルtmux proofは`308b1ba`時点のrunです。

別の実Herdr active-cancel probeでは、public MCPからWorkerをdispatchし、`CANCEL_STARTED`を観測したうえで、live kernel
PID/PGIDとnative resultの不在をstop直前に再確認しました。独立readbackで所有PID/PGID、process reference、pathが残っていない
ことを確認しました。これはHerdrのClaude ACP cancelに関する代表的な証拠であり、すべてのharnessの証拠ではありません。

## top-level fieldで1つのteam contractを定義する

| Field | Contract |
|---|---|
| `version` | 整数`3`だけを受け付ける。自動migrationは行わない。 |
| `runtime` | `"orca"`、`"tmux"`、`"herdr"`、`"zellij"`を受け付ける。`orca`は4 role、nativeは共通NativeBackendと選択terminal driverを使う。 |
| `team_prefix` | `[a-z][a-z0-9-]{0,23}`に一致する値。runtime team IDの一部になる。 |
| `max_review_rounds` | 正の整数。各段階の初回判定と再判定を数える。 |
| `main` | 必須のMain role table。 |
| `roles` | `orca`は`planner`、`worker`、`reviewer`を過不足なく含める。各native runtimeは任意の`planner`、`worker`、`reviewer`を含められますが、WorkerにはReviewerが必要です。Mainは別に宣言し、常に必須です。 |
| `tasks` | native runtimeだけで使う任意の`[[tasks]]` TaskSpec catalogです。各taskの固定検証は`[[tasks.verification]]`で宣言します。Orcaはこのfieldを拒否します。未指定ならread-onlyの`role_prompt`は使えますが、structured `task_dispatch`は拒否します。 |

runtime team IDは、`team_prefix`、workspace名、workspaceのabsolute pathのhashから
作ります。config pathはIDに含みません。同じprefixとworkspaceを使う2つのconfigは、
同じteam stateを参照します。prefixを分ければstateも分かれますが、team間のfile編集は
agent-teamが調整しません。

`team_prefix`を変えるとstateの場所も変わります。変更前に既存teamを停止してください。

## すべてのroleで同じfieldを宣言する

| Field | 意味 |
|---|---|
| `provider` | 認識している10個のharness IDのいずれか。実行できるのは[対応matrix](support-matrix_JA.md)にあるprofileだけ。 |
| `transport` | `direct`か`acp`。必ず明示する。 |
| `model` | 選択したruntimeへ渡すprovider model ID。 |
| `effort` | provider固有のreasoning/effort level。 |
| `prompt` | agent-team config directoryからの相対Markdown path。 |
| `permission` | roleごとに固定したpermission。任意値は拒否する。 |

prompt pathはconfig directoryの内側にあり、実在するfileを指定する必要があります。
absolute pathや`..`で外へ出る指定は拒否します。

## Orcaの対応matrixを小さく保つ

| Role | 対応するprovider / transport | 必須permission |
|---|---|---|
| Main | ClaudeまたはCodex / `direct` | `orchestrator` |
| Planner | ClaudeまたはCodex / `direct`、Claude / `acp`、Copilot / `direct` | `read-only` |
| Worker | Codex / `direct` | `workspace-write` |
| Reviewer | ClaudeまたはCodex / `direct`、Claude / `acp`、Copilot / `direct` | `read-only` |

canonical Reviewerはdirect Codexです。Claude ACPはread-only background roleで利用
できますが、configを明示的に変更する必要があります。Copilotは厳密なCLI `1.0.81`を使う
direct backgroundのread-only Planner/Reviewerに限定します。Main ACP、Codex ACP、
workspace-write Claude、すべてのworkspace-write ACPはfail-fastで拒否します。

新しいproviderやACP adapterの追加は、configだけでは完了しません。code変更、
capability/permission test、exact version policy、実lifecycle/cleanup smokeが必要です。

native terminalの対応matrixはさらに小さくなります。

| Role | 対応するprovider / transport | 必須permission |
|---|---|---|
| Main | Claude / `direct` | `orchestrator` |
| Planner | Claude / `acp`（任意） | `read-only` |
| Worker | Claude / `acp`（任意、scoped） | `workspace-write` |
| Reviewer | Claude / `acp`（任意） | `read-only` |

direct Worker/Reviewer、Codex ACP、Main ACP、scoped profile以外のworkspace-write ACP、
その他のnative provider profileは、起動処理の効果が発生する前に拒否します。

## ACP依存関係は明示し、選択したroleだけで解決する

OrcaのClaude `acp`を選ぶconfigには、Node.js `22.13.0`以降と、exact packageの
`acpx@0.13.2`、`@agentclientprotocol/claude-agent-acp@0.70.0`が必要です。`agent-team`の外で、
たとえば次のように導入してください。

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

Orcaでは起動時に`node`、`acpx`、`claude-agent-acp`を解決し、exact package manifestを確認したうえで、
absoluteなfile pathとSHA-256 fingerprintをlaunch snapshotへ保存します。
runnerは保存したbindingを検証して使います。fileの不足や変更はfail-closedで停止します。実行時の
commandは`npm`や`npx`を呼び出さず、directだけのconfigではACP依存関係を解決しません。

native runtimeは別のbindingを使います。Node.js `22.0.0`以降、導入済みの
`@agentclientprotocol/claude-agent-acp@0.70.0` command、その依存の
`@agentclientprotocol/sdk@1.3.0`と実際にimportする`dist/lib.js`を解決し、4 fileのabsolute pathとSHA-256 fingerprintを保存します。
assignmentごとにpublic ACP SDK connectionを1本だけ使い、nativeでは`acpx`を選択せず、`npm`や
`npx`をruntimeから呼びません。通常のSDK persistenceは`persistSession=false`、
`autoMemoryEnabled=false`に固定し、interactive Mainの通常Claude historyは残します。これは
public SDKへの直接接続であり、providerのdirect/model transportではありません。

terminalの挙動はHerdr `0.8.2`とZellij `0.44.1`で確認しています。Herdrはversion `0.8.2`と
protocol 20のhandshakeを厳密に確認します。Zellijのpreflightはexecutableと既知のinventoryを確認し、
CLIのexact versionは要求しません。確認したcompatibility versionではpersistent clientなしのdetached
sessionを使い、`--max-panes 1`を使いません。

## effortはproviderごとの値を使う

| Provider | 受け付ける値 |
|---|---|
| Claude | `low`、`medium`、`high`、`xhigh`、`max` |
| Codex | `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max` |
| Copilot | modelが`auto`なら`none`。明示modelなら`low`、`medium`、`high`、`xhigh`、`max` |

agent-teamはmodel IDを別名へ変換しません。設定したproviderまたはACP sessionが、その
modelを受け付ける必要があります。不一致の場合は、別modelへ切り替えず失敗します。

## permissionはroleごとに固定する

configのpermission文字列だけではroleを昇格できません。

- Mainは`orchestrator`。
- PlannerとReviewerは`read-only`。
- OrcaのWorkerは`workspace-write`かつdirect Codex。
- native Workerはscoped Claude ACPの`workspace-write`です。

direct Codexでは、隔離した`CODEX_HOME`を作り、`:read-only`または`:workspace`から
permission profileを派生させます。read-only Claude ACPではtoolを`Read`、`Grep`、`Glob`へ
限定し、readを許可します。non-interactive permissionを解決できない場合は失敗します。
native runtimeのscoped Workerは、保護pathやlink・file typeの検査を除き、Read/Grep/Globで
workspace内を読めます。TaskSpecの`allowed_paths`と`forbidden_paths`はWrite/Editだけを制限し、
書き込みでは禁止pathを優先します。Bash、
terminal、その他のRPCを拒否します。native Planner/Reviewerはread-onlyで、全native ACP roleは
launcher所有のbackground processとして動きます。

## TaskSpec catalog is optional; required for native task dispatch

native task dispatchはcatalog方式です。taskごとに`[[tasks]]` tableを1つ宣言し、
fixed argvごとに`[[tasks.verification]]` tableを1つ以上記述します。

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

TaskSpec parserは、`task_id`、`objective`、`acceptance_criteria`、`allowed_paths`、
`forbidden_paths`、`dependencies`、`verification`、`evidence_requirements`、
`consultation_conditions`だけを受け付けます。verificationには`name`、`argv`、
1〜900秒の`timeout_seconds`が必要です。pathはworkspace相対POSIX pathで、重なる場合は
`forbidden_paths`が優先されます。起動前にtask ID重複、未宣言dependency、dependency cycleを
拒否し、stateやprovider effectを作りません。

native Mainにはcatalogをstartup instructionとして渡します。`task_dispatch`は宣言済み
TaskSpecの1件と完全一致しなければならず、dispatch時にtask ID、path scope、dependency、
evidence、fixed verification argvを追加・変更できません。native configに`[[tasks]]`が
なければ、read-onlyの`role_prompt`は使えますが、structured task dispatchは拒否します。
Orca configでは`tasks` fieldを拒否します。

native task lifecycleの順序は次のとおりです。

1. 宣言済みTaskSpecをPlanner、Worker、Reviewerへ渡すため`task_dispatch`を呼びます。
2. `role_wait`、`role_read`、`role_release`、`delivery_ack`の順にresultを処理します。
3. `task_get`で保存済みstage、review evidence、verification evidenceを確認します。
4. Planner/Workerの成功は`awaiting_plan_review`または`awaiting_implementation_review`になります。
5. Reviewerは`task_id`、`stage`、`revision`、`decision`、`findings`だけのexact JSONを返します。
6. `approve`は次のstageへ進み、`request_changes`は元のwriterへ戻り、`consult`はuser判断で停止します。
7. implementation approval後に`task_verify`が宣言済みfixed argvを実行します。

planとimplementationのreview roundは別々に数え、どちらも`max_review_rounds`に従います。
implementation reviewはReviewer assignment準備時のworkspace revisionに束縛されます。
`task_verify`は同じrevision、active role/Deliveryなし、cleanup確認を要求します。`shell=False`
でcommandを実行し、revisionを前後で確認し、stdout/stderr hashとbounded errorを保存します。
全command成功とcleanup確認がそろった場合だけ`completed`になります。Reviewer approvalだけ
では完了しません。

verification中断またはcleanup不確認では、Taskは`verifying`または`verification_failed`に
evidenceを残し、必要に応じて次のrole、verification、stopをblockします。自動recoveryは主張しません。
`verification_failed`でも、実行済みcommandの証拠とcleanupを確認できれば、implementationのreview round
上限内でWorkerへ戻せます。cleanupが不明な場合はユーザー相談が必要です。
revisionの変化や中断で検証が途中までしか進まなかった場合も、実行済みの結果が宣言順に
一致し、cleanupを確認できれば修正を再試行できます。実行前の中断なら結果は0件です。
この扱いは修正の再試行に限り、成功判定には全commandの完了が必要です。

## promptはroleの振る舞いだけを定義する

| File | 用途 |
|---|---|
| `prompts/orchestrator.md` | Mainのrouting、handoff、review、user gate。 |
| `prompts/planner.md` | read-only計画の出力形式とscope。 |
| `prompts/worker.md` | 最小実装、検証、禁止操作。 |
| `prompts/reviewer.md` | 独立reviewと`APPROVED` / `CHANGES_REQUESTED` / `ASK_USER`。 |

process authorityはlauncher、共通MCP allowlist、選択したbackend、Dispatchまたはnative
assignment、provider permission profileが管理します。promptの文章を変えても、新しいtool、
transport、permissionは付与されません。

## defaultとcustom configの優先順位

`--config`を省略した場合、launcherは次の順で最初に存在するconfigを使います。

1. `$XDG_CONFIG_HOME/agent-team/config.toml`（未設定なら`~/.config/agent-team/config.toml`）
2. このprojectまたはinstall済みwheelのbundled `agent_team/defaults/config.toml`

user configが存在するのに不正な場合、bundled defaultへ黙ってfallbackしません。dotfilesのsyncは
`dotfiles/.agent/apps/agent-team/`をXDG user directoryへlinkします。dotfiles側のconfigとpromptは
user override、bundled fileはstandalone distributionのdefaultです。repository testで両者がbyte単位で
一致することを確認します。

## custom configは全commandで同じ値を使う

```bash
agent-team start \
  --config /absolute/path/to/team/config.toml \
  --cwd /absolute/path/to/project
```

`status`、`attach`、`stop`でも同じ値を使います。`--cwd`の既定値は現在のdirectoryです。
4つのcommandは保存した`runtime`が選んだbackendへ送られ、native runtimeで`attach`できるのは
Mainだけです。

configを有効にする前にdry runを実行します。

```bash
agent-team start \
  --config /absolute/path/to/team/config.toml \
  --cwd /absolute/path/to/project \
  --dry-run
```

dry runはconfigを検証し、role metadataとdirect agentの引数を表示します。ACP commandは
Task固有のidentityを含むため、Dispatch時にだけ生成します。

## fallbackなしでupgradeする

現在のcodeはconfig version 2を拒否します。version 2のteamは、version 3へ切り替える
前に、旧launcherの`agent-team stop`で停止してください。live stateを編集したり、state
version間でfieldをcopyしたりしないでください。古いnative terminal contractのstateで、固定した
`supervisor_argv`やMain process receiptが欠ける場合は保持したままfail-closedにし、自動再構成や
migrationは行いません。upgrade前に一致するexecutable/versionで停止してください。
