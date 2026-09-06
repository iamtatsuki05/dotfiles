# 設定リファレンス

[English](configuration.md) · [README](../README_JA.md) ·
[アーキテクチャ](architecture_JA.md)

`agent-team`は既存のversion 3固定role設定を維持しながら、明示的なversion 4の
topology設定も受け付けます。version 3の`runtime = "orca"`は4 role固定のcontractを
使い、version 3の`runtime = "tmux"`はMainと任意のClaude ACP read-only background
roleだけを持つ実験的なnative subsetです。必須値の欠落や未対応の組み合わせは、roleを
起動する前に拒否します。topology schemaとresourceを起動しない確認commandは
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

## 実験的なnative tmux subsetを明示的に選ぶ

`runtime = "tmux"`はcustomなversion 3 configでだけ指定します。Mainは必須で、direct
Claude・permission `orchestrator`でなければなりません。PlannerとReviewerは省略するか、
それぞれverified Claude ACP・permission `read-only`・pinned `claude-acp-0.70.0` adapterで
定義できます。Workerとその他のnative profileは、state、Task、Dispatch、processに影響する
前に拒否します。
nativeの起動には`tmux` commandが必要で、OrcaやCodexは必要ありません。ACP roleを選ぶconfig
では、後述するpinned Node.jsとACP dependencyも必要です。

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

[roles]
```

この例は既定の`fable`を維持しています。起動前に`command -v claude`と`claude --version`を
確認してください。実Main/Plannerの経路はClaude Code 2.1.261で成功し、2.1.112はFableに
対応する版を満たしませんでした。nativeの`start`、`status`、`attach`、`stop`は
`TmuxBackend`へ送られ、`attach`できるのはMainだけです。native ACPの完了はrunnerが
publishし、tmux paneの文字列には依存しません。lifecycleには`role_read` → `role_release`
→ `delivery_ack`の順序が必要です。`native.last_ack`は1つのreceipt markerであり、Taskや
goal全体の完了を示しません。

modelなしのnative tmux CLI start/status/stopは、OrcaとCodexがない環境、空白を含むworkspace
path、削除済みconfigで成功しました。実際のClaude Code 2.1.261を使った`fable`/`high`の
Mainも、ログイン済みの`claude.ai` accountでClaude ACP Plannerを呼び出し、MCPの
read/release/ackと公開stopを完了しました。所有資源の消滅を独立に確認しています。
Python環境には`dotfiles-agent-team`だけを導入し、Orca、Codex、OpenCode、Zellij、Herdrを
PATHから除外しました。これは読み取り専用のMain/Plannerの一巡であり、未実装の変更作成・
レビューの全工程ではありません。以前の2.1.112での拒否はCLIの版が古いことによるもので、
`fable`の利用不可を意味しません。

## top-level fieldで1つのteam contractを定義する

| Field | Contract |
|---|---|
| `version` | 整数`3`だけを受け付ける。自動migrationは行わない。 |
| `runtime` | `"orca"`または`"tmux"`だけを受け付ける。`orca`は4 role、`tmux`は実験的なnative subset。HerdrやZellijへのfallbackはない。 |
| `team_prefix` | `[a-z][a-z0-9-]{0,23}`に一致する値。runtime team IDの一部になる。 |
| `max_review_rounds` | 正の整数。各段階の初回判定と再判定を数える。 |
| `main` | 必須のMain role table。 |
| `roles` | `orca`は`planner`、`worker`、`reviewer`を過不足なく含める。`tmux`は任意の`planner`と`reviewer`だけを含められます。Mainは別に宣言し、常に必須です。 |

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

native tmuxの対応matrixはさらに小さくなります。

| Role | 対応するprovider / transport | 必須permission |
|---|---|---|
| Main | Claude / `direct` | `orchestrator` |
| Planner | Claude / `acp`（任意） | `read-only` |
| Reviewer | Claude / `acp`（任意） | `read-only` |

native Worker、direct Reviewer、Codex ACP、Main ACP、workspace-write ACP、その他のnative
provider profileは、起動処理の効果が発生する前に拒否します。

## ACP依存関係は明示し、選択したroleだけで解決する

Claudeの`acp`を選ぶconfigには、Node.js `22.13.0`以降と、exact packageの
`acpx@0.13.2`、`@agentclientprotocol/claude-agent-acp@0.70.0`が必要です。`agent-team`の外で、
たとえば次のように導入してください。

```bash
npm install --prefix /path/to/agent-team-acp acpx@0.13.2 @agentclientprotocol/claude-agent-acp@0.70.0
export PATH="/path/to/agent-team-acp/node_modules/.bin:$PATH"
```

ACP roleを選択した場合だけ、起動時に`node`、`acpx`、`claude-agent-acp`を解決し、exact package
manifestを確認したうえで、absoluteなfile pathとSHA-256 fingerprintをlaunch snapshotへ保存します。
runnerは保存したbindingを検証して使います。fileの不足や変更はfail-closedで停止します。実行時の
commandは`npm`や`npx`を呼び出さず、directだけのconfigではACP依存関係を解決しません。

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
- Workerは`workspace-write`かつdirect Codex。

direct Codexでは、隔離した`CODEX_HOME`を作り、`:read-only`または`:workspace`から
permission profileを派生させます。Claude ACPではtoolを`Read`、`Grep`、`Glob`へ
限定し、readを許可します。non-interactive permissionを解決できない場合は失敗します。
native tmuxにはdirect Workerやdirect Reviewerはありません。direct Mainは`native_main`が
監督し、任意のACP roleはlauncher所有のbackground processとして動きます。

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
4つのcommandは保存した`runtime`が選んだbackendへ送られ、native tmuxで`attach`できるのは
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
version間でfieldをcopyしたりしないでください。
