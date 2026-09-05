# 設定リファレンス

[English](configuration.md) · [README](../README_JA.md) ·
[アーキテクチャ](architecture_JA.md)

`agent-team`は既存のversion 3固定role設定を維持しながら、明示的なversion 4の
topology設定も受け付けます。必須値の欠落や未対応の組み合わせは、roleを起動する
前に拒否します。topology schemaとresourceを起動しない確認commandは
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

## top-level fieldで1つのteam contractを定義する

| Field | Contract |
|---|---|
| `version` | 整数`3`だけを受け付ける。自動migrationは行わない。 |
| `runtime` | `"orca"`だけを受け付ける。Herdr fallbackはない。 |
| `team_prefix` | `[a-z][a-z0-9-]{0,23}`に一致する値。runtime team IDの一部になる。 |
| `max_review_rounds` | 正の整数。各段階の初回判定と再判定を数える。 |
| `main` | 必須のMain role table。 |
| `roles` | `planner`、`worker`、`reviewer`を過不足なく含める。 |

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

## 対応matrixを小さく保つ

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

## promptはroleの振る舞いだけを定義する

| File | 用途 |
|---|---|
| `prompts/orchestrator.md` | Mainのrouting、handoff、review、user gate。 |
| `prompts/planner.md` | read-only計画の出力形式とscope。 |
| `prompts/worker.md` | 最小実装、検証、禁止操作。 |
| `prompts/reviewer.md` | 独立reviewと`APPROVED` / `CHANGES_REQUESTED` / `ASK_USER`。 |

process authorityはlauncher、MCP allowlist、Orca Dispatch、provider permission profileが
管理します。promptの文章を変えても、新しいtool、transport、permissionは付与されません。

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
