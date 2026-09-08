# Version 5 設定リファレンス

[English](configuration-v5.md) · [設定](configuration_JA.md) · [アーキテクチャ](architecture_JA.md)

Version 5では、各nodeにID、役割の種類、個別の設定を持たせ、TaskSpecの工程ごとに担当者を指定します。
現在実行できるのはnativeの`agent`/`serial`構成です。programによる進行管理、並列実行、
名前付きOrca構成は未対応ですが、graphとして検証・描画できます。

同梱のversion 3設定は、Main/PlannerにClaude `fable`、Worker/Reviewerにdirect Codex
`gpt-6-astra`を使うままです。このページの例では、全nodeにClaudeを明示指定します。
実モデルで確認した範囲と限界は、[アーキテクチャ](architecture_JA.md)を参照してください。

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
正の整数で、順次実行では`max_active = 1`にします。booleanを整数としては受け付けません。
未知のfieldも拒否します。promptのpathはconfigのあるdirectoryから解決し、その内側の既存ファイルを指定します。

agent構成にはMainを1つだけ置き、Mainだけを開始点にします。program構成にはMainを置かず、
開始点を明示します。全nodeに到達できる必要があり、委譲とレビューの経路には有向閉路を認めません。
レビューはPlannerまたはWorkerからReviewerへ接続し、Mainへの委譲は拒否します。
相談先は1段だけ到達可能とみなし、その先の経路をたどりません。agent間で相談する実行操作は未実装です。

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

## Mainなしの構成を検査する

上の設定を次のように変更すると、graphの検査用にMainなしの構成を作れます。

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

この構成はgraphの検証を通りますが、実際の起動は`program` modeのため拒否します。
`dispatch_mode`を`parallel`にし、正の`max_active`を指定したgraphも検証できますが、並列起動は拒否します。

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
graphの形式は`json`、`ascii`、`mermaid`です。上のnative agent/serial構成は、起動条件を満たした後で
`--dry-run`を外すと起動します。program・並列・名前付きOrca構成の起動は、依存関係の確認や資源作成より前に拒否します。
検査コマンドはproviderを起動しません。

## 保存済みの識別情報で管理する

`NodeRef(node_id, kind)`は、nodeの識別子と役割の種類を分けます。同じWorkerでも、
model、effort、promptを個別に変えられます。kindの名前や設定内の順序からnodeを選ぶことはありません。

runtimeのteam名は`<team-id>-<workspace-slug>-<sha256(absolute-workspace)[:8]>`です。
stateは`$XDG_STATE_HOME/agent-team/<derived-team-id>/state.json`に保存し、
既定の保存先は`~/.local/state/agent-team/`です。`status`、`attach`、`stop`に`--state`を渡すと、
configを再読込せず、その保存済みrunを管理できます。

configはversion 5、名前付きnative stateはversion 4です。`role_specs`は全nodeを、
`roles`は実行中のassignmentだけを保持します。従来の固定role stateは変換しません。
識別子の照合、質問、完了通知、レビュー、検証、後始末の契約は、[アーキテクチャ](architecture_JA.md)を参照してください。
