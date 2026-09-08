# Version 5 設定リファレンス

[English](configuration-v5.md) · [設定](configuration_JA.md) · [アーキテクチャ](architecture_JA.md)

Version 5では、各nodeにID、役割の種類、個別の設定を持たせ、TaskSpecの工程ごとに担当者を指定します。
nativeの`agent`/`serial`、`program`/`serial`、`program`/`parallel`構成を実行できます。
program構成にはMain roleを置きません。選択したnative terminal上で既存の`native_main` supervisorが、
固定argvの`_program-run` coordinatorを監督します。専用のMain role spec、model、CLIは作りません。
native `program`/`parallel`はversion 5のstateとnodeごとのDelivery recordを使います。
Main-agentのparallelと名前付きOrca構成は引き続き拒否します。実装にはfocused contract testとboundedな
実端末・fake providerのcoverageがあります。実モデルのparallel受入は未実施です。

同梱のversion 3設定は、Main/PlannerにClaude `fable`、Worker/Reviewerにdirect Codex
`gpt-6-astra`を使うままです。このページの例では、全nodeにClaudeを明示指定します。
実端末・模擬providerの検証と実モデルの検証それぞれの範囲と限界は、
[アーキテクチャ](architecture_JA.md)を参照してください。

## nodeとgraphのfieldを指定する

| 対象 | 必須fieldと上限 |
|---|---|
| top level | `version = 5`、`runtime`、`teams`。runtimeは`orca`、`tmux`、`zellij`、`herdr`。teamは1〜64個。 |
| team | `name`、`max_review_rounds`、`nodes`、`edges`、`coordination`、`tasks`、`routes`。nodeは1〜128個、edgeは256個以下。 |
| node | `id`、`label`、`kind`、`role_spec`。kindは`main`、`planner`、`worker`、`reviewer`。 |
| role_spec | `provider`、`transport`、`model`、`effort`、`prompt`、`permission`。選択したprofileとkindに照らして検証する。 |
| coordination | `mode`、`entry_nodes`、`dispatch_mode`、`max_active`。modeは`agent`または`program`、dispatchは`serial`または`parallel`。 |
| edge | `source`、`target`、`kind`。kindは`delegates-to`、`reviewed-by`、`consults-to`。 |

team IDは`[a-z][a-z0-9-]{0,23}`、node IDは`[a-z][a-z0-9-]{0,63}`に一致させます。
nameとlabelは空でない表示可能な文字列で、128文字以内です。`max_review_rounds`と`max_active`は
正の整数で、順次実行では`max_active = 1`にします。parallelのprogram実行では正の`max_active`を上限にします。
booleanを整数としては受け付けません。
未知のfieldも拒否します。promptのpathはconfigのあるdirectoryから解決し、その内側の既存ファイルを指定します。

agent構成にはMainを1つだけ置き、Mainだけを開始点にします。program構成にはMainを置かず、
開始点を明示します。全nodeに到達できる必要があり、委譲とレビューの経路には有向閉路を認めません。
レビューはPlannerまたはWorkerからReviewerへ接続し、Mainへの委譲は拒否します。
相談先は1段だけ到達可能とみなし、その先の経路をたどりません。

native programの保存stateでは、coordinatorを`coordinator_terminal`、`coordinator_argv`、
`coordinator_process`、`coordinator_pid`で保持します。これはMainのterminal、argv、processの別名ではありません。
記録済みcoordinatorだけがprogram waveを進め、`status`、`stop`、ユーザーの回答は明示した外部操作として残します。

taskには[TaskSpecのfield](configuration_JA.md#taskspec-catalog-is-optional-required-for-native-task-dispatch)を使います。
routeには`task_id`と、次の少なくとも一組を指定します。
計画担当の`plan_writer`/`plan_reviewer`、または実装担当の`implementation_writer`/`implementation_reviewer`です。
片方だけの指定はできず、各組に対応するレビューedgeが必要です。両組を指定する場合は、計画担当から実装担当への
委譲edgeも必要です。routeと宣言済みtaskのID集合は一致させます。省略した組はgraph JSONでは`null`になり、
計画担当を宣言した場合は、そのレビュー承認後に実装へ進みます。

## WorkerとReviewerを2体ずつ定義する

この5nodeの例ではPlannerを省略します。検証前に、参照するpromptファイルをconfigと同じdirectory内に用意してください。
同梱の[prompts directory](../agent_team/defaults/prompts)をコピーしても使えます。実行前にはworkspaceに対象のsourceと検証ファイルを用意し、
`/workspace/example`をその絶対pathに置き換えます。検証では宣言したargvをそのまま実行します。
選択するnative runtimeとACPの依存関係は、[設定リファレンス](configuration_JA.md#実験的なnative-terminal-runtimeを明示的に選ぶ)に記載しています。

```toml
version = 5
runtime = "tmux"
[teams.all-claude]
name = "All Claude Serial"
max_review_rounds = 2
[[teams.all-claude.nodes]]
id = "main"
label = "Main"
kind = "main"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "direct"
model = "fable"
effort = "high"
prompt = "prompts/orchestrator.md"
permission = "orchestrator"
[[teams.all-claude.nodes]]
id = "worker-a"
label = "Worker A"
kind = "worker"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/worker.md"
permission = "workspace-write"
[[teams.all-claude.nodes]]
id = "worker-b"
label = "Worker B"
kind = "worker"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/worker.md"
permission = "workspace-write"
[[teams.all-claude.nodes]]
id = "reviewer-a"
label = "Reviewer A"
kind = "reviewer"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"
[[teams.all-claude.nodes]]
id = "reviewer-b"
label = "Reviewer B"
kind = "reviewer"
[teams.all-claude.nodes.role_spec]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"
[[teams.all-claude.edges]]
source = "main"
target = "worker-a"
kind = "delegates-to"
[[teams.all-claude.edges]]
source = "main"
target = "worker-b"
kind = "delegates-to"
[[teams.all-claude.edges]]
source = "worker-a"
target = "reviewer-a"
kind = "reviewed-by"
[[teams.all-claude.edges]]
source = "worker-b"
target = "reviewer-b"
kind = "reviewed-by"
[teams.all-claude.coordination]
mode = "agent"
entry_nodes = ["main"]
dispatch_mode = "serial"
max_active = 1
[[teams.all-claude.tasks]]
task_id = "write-sum"
objective = "Implement the sum operation in the user's workspace."
acceptance_criteria = ["The sum verifier passes."]
allowed_paths = ["src/calc_sum.py"]
forbidden_paths = ["src/verify_sum.py"]
dependencies = []
evidence_requirements = ["Changed paths and verifier output."]
consultation_conditions = []
[[teams.all-claude.tasks.verification]]
name = "verify-sum"
argv = ["python", "-B", "/workspace/example/src/verify_sum.py"]
timeout_seconds = 30
[[teams.all-claude.tasks]]
task_id = "write-product"
objective = "Implement the product operation in the user's workspace."
acceptance_criteria = ["The product verifier passes."]
allowed_paths = ["src/calc_product.py"]
forbidden_paths = ["src/verify_product.py"]
dependencies = []
evidence_requirements = ["Changed paths and verifier output."]
consultation_conditions = []
[[teams.all-claude.tasks.verification]]
name = "verify-product"
argv = ["python", "-B", "/workspace/example/src/verify_product.py"]
timeout_seconds = 30
[[teams.all-claude.routes]]
task_id = "write-sum"
implementation_writer = "worker-a"
implementation_reviewer = "reviewer-a"
[[teams.all-claude.routes]]
task_id = "write-product"
implementation_writer = "worker-b"
implementation_reviewer = "reviewer-b"
```

## Mainなしのserial programを実行する

上の設定を次のように変更すると、実行可能なMainなしのserial program構成を作れます。

1. `main` nodeとその`role_spec`、Mainからの委譲edge 2件を削除します。
2. coordinationのtableを次の内容に置き換えます。
3. WorkerからReviewerへのedge、TaskSpec、routeは残します。

```toml
[teams.all-claude.coordination]
mode = "program"
entry_nodes = ["worker-a", "worker-b"]
dispatch_mode = "serial"
max_active = 1
```

この構成はgraphの検証を通り、前提条件を満たしたnative terminalで起動できます。coordinatorは宣言したTaskSpecの
順序と依存関係からserialのintegration waveを作ります。wave内の全writerが終了し、同じrevisionのReviewerがすべて承認し、
宣言済みfixed argvの検証が完了してから次のwaveへ進みます。parallelのprogram構成は、次のcoordination tableを使います。

## Mainなしのparallel programを実行する

上のserial例で、WorkerからReviewerへのedge、TaskSpec、routeを残したまま、coordinationを次のようにします。

```toml
[teams.all-claude.coordination]
mode = "program"
entry_nodes = ["worker-a", "worker-b"]
dispatch_mode = "parallel"
max_active = 2
```

native coordinatorは`max_active`まで独立したassignmentを受け付けます。nodeが使用中、上限到達中、
またはWorkerの`allowed_paths`がactive Workerのscopeと重なる場合はadmissionを拒否します。
pending user questionは自分のassignmentだけを止め、条件を満たす独立candidateは継続できます。
`task_verify`は全active assignmentとDeliveryのdrainが終わるまでrun全体で拒否します。
version 5 stateはassignmentごとにresult、question、Delivery stageを保持します。canonical waveは全writer完了後にsealし、
同じ統合revisionをreviewし、宣言したfixed argvを検証してから次のwaveへ進みます。focused contract testとboundedな
実端末・fake providerのcaseでこの経路を確認していますが、実モデルのparallel受入は実施していません。

coordinatorは既存のnative supervisorを使い、Main modelを追加しません。確認が必要な場合は`--coordinator`でterminalへattachします。

```bash
agent-team attach --config /path/to/config-v5.toml \
  --cwd /workspace/example --team all-claude --coordinator
agent-team status --state /path/to/state.json
```

native ACP questionは既存の`message_id`経路を使います。batch内の全questionへ回答してからcoordinatorがDeliveryをackします。

```bash
agent-team answer --state /path/to/state.json \
  --message-id ID --body "回答"
```

Reviewerの相談は、名前付きnativeの`agent`と`program`で使える別の操作です。`status`にはopaqueな相談ID、task/stage、
review findings、回答済みかどうかを表示します。IDはrun、TaskSpec digest、review stage、正確なreview Dispatchに束縛されます。
回答本文は16,000文字以内で、同じIDと同じ本文の再送だけがidempotentです。異なる本文や古いIDは拒否します。
回答だけで承認・完了にはせず、元のwriterを再dispatchし、boundedなreviewをもう一度行います。review roundの上限は維持し、
上限到達後の回答は保存できても再dispatchを許可しません。

```bash
agent-team answer --state /path/to/state.json \
  --consultation-id ID --body "人間の判断"
```

plan-only route（`plan_writer`と`plan_reviewer`だけを持つroute）では、plan本文のdigestを`record.revision`に、
Reviewerが確認したコードのsnapshotを`workspace_revision`に保存します。両者を置き換えません。mixed waveでは、実装writerを
進める計画reviewはwriters phaseで行い、finalのplan-only reviewは全writer完了後、implementation reviewと同じsealed workspace
revisionに束縛します。fixed argvの検証も同じrevisionを使います。計画変更は元のPlannerへ戻り、review roundを維持します。
focused contract testは通っていますが、実モデルのplan-only runは完了していません。

## teamを明示して選ぶ

```bash
agent-team teams --config /path/to/config-v5.toml
agent-team validate --config /path/to/config-v5.toml --team all-claude
agent-team graph --config /path/to/config-v5.toml \
  --team all-claude --format json
agent-team start --config /path/to/config-v5.toml \
  --cwd /workspace/example --team all-claude --dry-run
```

`teams`は読み込んだ全teamを表示します。`validate`は`--team`を省略すると全teamを検証します。
`graph`とversion 5の`start`には`--team`が必須で、別名や大文字・小文字の変換をせず完全一致で選びます。
graphの形式は`json`、`ascii`、`mermaid`です。上のnative agent/serial、program/serial、
program/parallel構成は、起動条件を満たした後で`--dry-run`を外すと起動します。agent/parallelと名前付きOrca構成の起動は、
依存関係の確認や資源作成より前に拒否します。
検査コマンドはproviderを起動しません。

## 保存済みの識別情報で管理する

`NodeRef(node_id, kind)`は、nodeの識別子と役割の種類を分けます。同じWorkerでも、
model、effort、promptを個別に変えられます。kindの名前や設定内の順序からnodeを選ぶことはありません。

runtimeのteam名は`<team-id>-<workspace-slug>-<sha256(absolute-workspace)[:8]>`です。
stateは`$XDG_STATE_HOME/agent-team/<derived-team-id>/state.json`に保存し、
既定の保存先は`~/.local/state/agent-team/`です。`status`、`attach`、`stop`に`--state`を渡すと、
configを再読込せず、その保存済みrunを管理できます。

configはversion 5、名前付きnativeのserial stateはversion 4、program/parallel stateはversion 5です。
`role_specs`は全nodeを、`roles`は実行中のassignmentとnodeごとのDelivery stateを保持します。
従来の固定role stateは変換しません。
識別子の照合、質問、完了通知、レビュー、検証、後始末の契約は、[アーキテクチャ](architecture_JA.md)を参照してください。
