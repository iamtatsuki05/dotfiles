# Version 4の設定

[English](configuration-v4.md) · [Version 3の設定](configuration_JA.md)

Version 4は、名前付きteamの構成を管理する一覧です。各teamからversion 3の起動設定を
明示参照すると、既存のOrca runtimeで起動できます。起動設定のないteamは、graphの確認と
選択内容のdry runに使えます。

topologyが保持するのはprovider、transport、permissionです。参照先のversion 3起動設定が
modelとeffortを供給し、ACP依存関係の起動前検査も、その設定で選択したACP roleに対してだけ行います。
Claude ACPでは、この検査にNode.js `22.13.0`以降と、exactな`acpx@0.13.2`、
`@agentclientprotocol/claude-agent-acp@0.70.0` packageが必要です。absoluteな実行ファイルpathと
SHA-256 fingerprintを保存し、`npm`や`npx`は呼び出しません。

## 最小のschema

top-levelの`version` keyには整数の`4`を指定し、`runtime`には`"orca"`を明示します。
`teams` tableは空にできません。各teamは、tableのmap keyを正確なIDとして使い、
表示名とnode/edgeのarrayを持ちます。

```toml
version = 4
runtime = "orca"

[teams.build]
name = "Build Team"

[[teams.build.nodes]]
id = "main"
label = "Main"
main = true
[teams.build.nodes.profile]
provider = "claude"
transport = "direct"
permission = "orchestrator"

[[teams.build.nodes]]
id = "worker"
label = "Worker"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "workspace-write"

[[teams.build.nodes]]
id = "reviewer"
label = "Reviewer"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "read-only"

[[teams.build.edges]]
source = "main"
target = "worker"
kind = "delegates-to"

[[teams.build.edges]]
source = "worker"
target = "reviewer"
kind = "reviewed-by"
```

`version` keyがない場合やtable内に置いた場合は、v4設定として扱わず拒否します。

edgeがないteamは`edges = []`と明記してください。受け付けるfieldは意図的に
限定しています。

- config: `version`、`runtime`、`teams`
- team: `name`、`nodes`、`edges`
- node: `id`、`label`、`main`、`profile`
- profile: `provider`、`transport`、`permission`
- edge: `source`、`target`、`kind`

parserはtopologyを検証する前に、入力と診断結果へ次の上限を適用します。

| 対象 | 上限 |
|---|---:|
| config file | 1,048,576 bytes |
| team数 | 64 |
| 1 teamあたりのnode数 | 128 |
| 1 teamあたりのedge数 | 256 |
| team/node/edge/profileのidentifier | 64文字 |
| teamの表示名 | 128文字 |
| node label | 128文字 |
| config全体のvalidation error数 | 64 |
| 1件のvalidation error message | 512文字 |
| validation診断の合計 | 16,384文字 |

いずれも上限を超えた時点で失敗します。parserは上限超過を判定するために
file limitの1 byte分だけ余分に読みますが、上限のない診断文字列は生成しません。

未知のfield、空または安全でないtext、booleanでない`main`、未対応のpermissionは、
runtime resourceを検討する前に失敗させます。profileの解決先は検証済みharness registry
です。続くtopology validatorがunknown profile、ID/labelの重複、Mainの数、edgeの誤り、
relationshipごとのcycle、Mainから到達できないnodeを拒否します。

team IDは大文字・小文字を区別するmap keyです。選択時にtrim、case変換、alias、
先頭teamの採用、default補完は行いません。大文字・小文字だけが異なるIDも曖昧な
定義として拒否します。

## resourceを起動しない確認command

teamをID順に決定的に列挙します。JSONには表示名、validationの成否、安定した
validation error recordが含まれます。

```bash
agent-team teams --config /absolute/path/to/config-v4.toml
```

このcommandの出力は常にJSONです。`--json`の互換optionは用意していません。

選択したtopologyを描画します。

```bash
agent-team graph \
  --config /absolute/path/to/config-v4.toml \
  --team build \
  --format json
```

`--format`は`json`、`ascii`、`mermaid`だけを受け付けます。rendererの出力は
topology dataだけで、shell commandやOrca payloadは含みません。

`launch_config`がない場合、dry runは選択した`config_path`、`team_id`、
正規化した`workspace`だけを返します。

```bash
agent-team start --config /absolute/path/to/teams.toml --team build --dry-run
```

`teams`、`graph`、すべてのdry runは外部processを起動しません。一覧内に不正なtopologyが
ある場合は、graph描画と起動planの生成を拒否します。起動設定のないteamでは、実際の起動や
管理commandも拒否します。

## 名前を指定してteamを起動する

同梱の[teams.toml](../agent_team/defaults/teams.toml)は、そのまま使える一覧の例です。
隣の`config.toml`を参照し、モデル、effort、prompt、permission、review回数の上限は
既存の起動設定から読みます。リポジトリ内では、次のcommandで確認できます。

同梱の起動設定では、MainとPlannerに`fable`、WorkerとReviewerに`gpt-6-astra`を使います。
dry runではACP依存関係を解決せず、参照先のversion 3設定にACP roleがある場合の実起動時だけ検査します。

```bash
scripts/agent-team/agent-team start \
  --config scripts/agent-team/agent_team/defaults/teams.toml \
  --team agent-team --dry-run
```

通常のdotfiles sync後は、同じ一覧を`~/.config/agent-team/teams.toml`から使えます。
`XDG_CONFIG_HOME`を変更している場合は、その配下を指定してください。

```bash
agent-team start --config ~/.config/agent-team/teams.toml --team agent-team --no-attach
agent-team status --config ~/.config/agent-team/teams.toml --team agent-team
agent-team attach main --config ~/.config/agent-team/teams.toml --team agent-team
agent-team stop --config ~/.config/agent-team/teams.toml --team agent-team
```

起動可能なteamを増やすには、一覧と同じdirectory配下に起動設定をcopyし、固有の
`team_prefix`を付けます。このprefixを一覧のteam IDにも使い、相対pathを`launch_config`に
指定してください。モデル、effort、promptは参照先の起動設定で変更します。同じworkspaceで
別のteamへ切り替えるときは、動いているteamを先に停止してください。

起動可能な項目は、実装済みの逐次workflowと一致する必要があります。

- node IDは`main`、`planner`、`worker`、`reviewer`の4つに限定し、`main`だけを
  `main = true`にします。
- 各nodeのprovider、transport、permissionは、起動設定の対応roleと一致させます。
- edgeは8本です。Mainから3つのroleへ`delegates-to`、PlannerとWorkerからReviewerへ
  `reviewed-by`、3つのroleからMainへ`escalates-to`を定義します。
- `launch_config`は一覧のdirectory配下にある通常fileへの相対pathです。path内にsymlinkは
  使えません。参照先はversion 3の
  通常の検証にも通る必要があります。
- 選択するteam IDと参照先の`team_prefix`を一致させます。異なるteam IDが、暗黙に同じ
  runtime stateを選ぶことを防ぐためです。

これらの項目では、`start --dry-run`がroleのcommandとstate pathを含む実際の起動planを
表示し、`--no-attach`も使えます。実際の`start`、`status`、`attach`、`stop`は既存の
version-3 lifecycleを使います。planの`config_path`は参照先の起動設定を指すため、
子roleも同じ設定を読みます。未対応のgraphは確認用に保持できますが、runtime resourceを
作る前に起動を拒否します。任意graphの実行とroleの並列実行は未実装です。

## versionの境界

version 3のloaderとstate契約は変更しません。version 3 fileは既存の固定role schemaを
使い、v4のteam操作を受け付けない設計です。version 3 fileへtop-levelのv4 `teams`を
追加した場合、CLIのversion境界で拒否されます。version間でfieldをcopyする処理や
silent fallbackもありません。version 3の`start`、`status`、`attach`、`stop`では、
従来のpathとfile sizeの扱いを維持します。`--team`を明示した場合だけbounded v4 loaderを
選ぶため、version 3 fileの結果はversion 4 errorです。`--team`なしのversion 4 fileは
従来のversion 3経路へ進み、resource作成前に`version must be integer 3`を返して停止します。
