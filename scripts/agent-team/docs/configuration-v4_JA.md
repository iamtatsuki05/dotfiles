# Version 4の設定

[English](configuration-v4.md) · [Version 3の設定](configuration_JA.md)

Version 4は、1つ以上のteam topologyを不変な定義として記述します。
これは確認と選択のための契約であり、provider、Orca resource、Task、lease、
state storeを起動しません。

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

PR #20のrendererで、選択したtopologyを描画します。

```bash
agent-team graph \
  --config /absolute/path/to/config-v4.toml \
  --team build \
  --format json
```

`--format`は`json`、`ascii`、`mermaid`だけを受け付けます。rendererの出力は
topology dataだけで、shell commandやOrca payloadは含みません。

v4のdry runは、将来のruntime/store seamへtypedな選択planだけを渡します。

```bash
agent-team start \
  --config /absolute/path/to/config-v4.toml \
  --cwd /absolute/path/to/project \
  --team build \
  --dry-run
```

出力のfieldは`config_path`、`team_id`、`workspace`だけです。configの
`runtime = "orca"`は検証しますが、runtime/backendの選択はこの狭いplanへ渡しません。
workspaceはabsoluteかつcanonicalです。state path、lease、backend ownership、
provider command、role起動引数は含めません。`--dry-run`なしのv4 `start`と、v4の
`status`、`attach`、`stop`は、後続のruntime/store統合が完了するまで明示的に失敗します。

3つの確認commandとv4 dry runは、外部processを実行しません。config内のどれかのteamが
不正なら`teams`で報告し、launch planやgraphは作成できません。
`--no-attach`はversion 3の起動optionなので、v4 dry runでは無視せず拒否します。

## versionの境界

version 3のloaderとstate契約は変更しません。version 3 fileは既存の固定role schemaを
使い、v4のteam操作を受け付けない設計です。version 3 fileへtop-levelのv4 `teams`を
追加した場合、CLIのversion境界で拒否されます。version間でfieldをcopyする処理や
silent fallbackもありません。version 3の`start`、`status`、`attach`、`stop`では、
従来のpathとfile sizeの扱いを維持します。`--team`を明示した場合だけbounded v4 loaderを
選ぶため、version 3 fileの結果はversion 4 errorです。`--team`なしのversion 4 fileは
従来のversion 3経路へ進み、resource作成前に`version must be integer 3`を返して停止します。
