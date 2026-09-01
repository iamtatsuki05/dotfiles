# Agent Team

[English](README.md)

`agent-team`は、通常の`claude`や`codex`の設定を変えずに、project単位の
Planner → Worker → ReviewerフローをOrca上で起動します。Task、message、terminal、
lifecycleはOrcaが管理し、各roleは通常のCLIを使う`direct`またはAgent Client Protocol
経由の`acp`で動く構成です。

初めて使う場合は、「managed commandを導入する」「起動前の条件を満たす」
「teamを起動する」を読んでください。実装や設定を変える場合は、詳細ドキュメントも
参照してください。

## 最初に読む場所

- [teamを起動する](#teamを起動する)は通常の利用手順です。
- [アーキテクチャ](docs/architecture_JA.md)はruntimeと安全境界を説明します。
- [設定リファレンス](docs/configuration_JA.md)はconfig version 3と、対応する
  provider/transportの組み合わせを説明します。[Version 4の設定](docs/configuration-v4_JA.md)
  では、明示的なteam選択とtopologyの確認を説明します。
- [Harness対応matrix](docs/support-matrix_JA.md)は、認識済み・利用可能・実行可能・
  拒否を区別します。
- [ACPの境界](docs/acp_JA.md)はadapter pin、認証、ACPがsandboxではない理由を説明します。
- [Direct background adapter](docs/background-adapters_JA.md)はCopilot/OpenCode向けread-only
  adapter実装、snapshot境界、復旧方法を説明します。
- [Coordination store、recovery、backup、restore](docs/coordination-store_JA.md)は
  SQLiteのschema境界、stable writer marker、WAL sidecar controller、backup artifact、
  candidate-first restoreを説明します。Issue #72のhistorical sectionには、v3 workflow
  checkpoint/CAS contractと、Storeが外部effectを呼び出さない境界を残しています。現在の
  [Issue #80 schema-4 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)では、
  12 tableのobject set、read-only image classifier、pure codec、およびledgerが空でない場合の
  fail-closed境界を定めています。
- [Task policy schema v4](docs/task-policy-v4_JA.md)は、不変な`TaskSpec`、依存順、
  保存やworkflow実行を含まないstate観測契約を説明します。
- [Serial review policy](docs/review-policy_JA.md)は、backend wiringを含めず、normal laneとIssue #50でadmit済みの
  express laneで共通する、Workerから独立Reviewerへ進むtyped serial gateを説明します。
- [Path/resource policy](docs/path-resource-policy_JA.md)は、filesystemやproviderを操作せず、
  canonical path admission、明示的なresource mode、reservation port、normal/express/researchの
  lane matrixを説明します。
- [Fixed-argv verification gate](docs/verification-gate_JA.md)は、write task を completed に
  する前提となる typed approval、固定 verification request、before/after snapshot の束縛、
  normalized receipt を説明します。
- [Policy/verification handoff](docs/policy-verification-handoff_JA.md)は、#49のreview ref、
  #50のcompletion ref、approved-only composition、Storeのexact readback、schema-4 workを分ける
  [Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80)、
  [#81](https://github.com/iamtatsuki05/dotfiles/issues/81)、
  [#82](https://github.com/iamtatsuki05/dotfiles/issues/82)、
  [#83](https://github.com/iamtatsuki05/dotfiles/issues/83)との境界を説明します。

現在の設定は次のとおりです。

| Role | Provider / transport | Model / effort | Permission |
|---|---|---|---|
| Main | Claude / `direct` | `fable` / `high` | `orchestrator` |
| Planner | Claude / `acp` | `fable` / `high` | `read-only` |
| Worker | Codex / `direct` | `gpt-5.6-sol` / `medium` | `workspace-write` |
| Reviewer | Codex / `direct` | `gpt-5.6-sol` / `high` | `read-only` |

起動直後に動くのはMainだけです。Planner、Worker、Reviewerは必要なときだけ
起動し、background roleは同時に1つしか動きません。

## checkoutから実行する、またはprojectをinstallする

このprojectはPython標準libraryだけで動きます。Python 3.11以降が必要です。checkoutからは
launcherを直接実行できます。

Orcaのライフサイクルbackendとbounded provider runnerはPOSIX専用です。現在のruntime metadataが
Unix socketとprocess groupを必要とするため、Windowsでは実行前に明示的に拒否します。
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

現在の実装でlive Orca runtimeを使ったsmoke testを行ったのはmacOSです。
Linuxの実行ファイルは実装上`orca-ide`に固定されていますが、このcheckoutではLinuxの
Orca実機smoke testを行っていません。Windowsは非対応で、実行前に明示的に拒否します。
Orcaの実行ファイルはplatformごとに固定し、PATH fallbackや環境変数overrideは行いません。

- macOS: `orca`
- Linux: `orca-ide`

起動前に次を確認してください。

1. 上記のplatform固有のOrca実行ファイル、`claude`、`codex`、Node.js、`npx`を利用できる。
2. Orcaを起動し、platform固有の`status --json`でruntimeとgraphがreadyである。
3. 利用するClaude/Codex accountへloginしている。
4. 対象repositoryをOrcaへ一度登録している。

```bash
claude auth status
codex login status
# macOS
orca status --json
orca repo add --path "$PWD"
# Linux
orca-ide status --json
orca-ide repo add --path "$PWD"
```

ACP Plannerは`acpx@0.13.2`と
`@agentclientprotocol/claude-agent-acp@0.70.0`を固定して使います。初回は
`npx`がpackageをdownloadする場合があります。global installは行いません。

config version 2で起動したteamが残っている場合は、version 3へ切り替える前に、
旧codeの`agent-team stop`で停止してください。legacy fallbackはありません。

## teamを起動する

Orcaやagentを起動せず、role metadataとdirect agentの引数を確認します。

```bash
agent-team start --dry-run
```

dry runでは、Taskごとに生成するACP commandまでは表示しません。ACP commandは、
ACP roleをDispatchするときに作ります。

Mainを起動し、Orca上のterminalへfocusします。

```bash
agent-team start
```

backgroundで起動する場合は`--no-attach`を付けます。

```bash
agent-team start --no-attach
```

Mainへ開発作業を依頼してください。MainはPlannerが必要か判断し、`agent_team`
MCP serverを通じてWorkerとReviewerを起動します。ユーザーと対話するroleはMainだけです。

## teamを確認して停止する

```bash
# Run、Main terminal、workerの状態を確認する。
agent-team status

# Mainへfocusする。
agent-team attach main

# Mainがbackground roleを起動した後だけ、そのroleへfocusできる。
agent-team attach worker

# teamが所有するterminalを停止し、runtime stateを削除する。
agent-team stop
```

`stop`後も、Orca Runは監査記録として残ります。project fileのcommit、push、
publish、削除は行いません。

管理commandでは、`start`と同じ`--config`と`--cwd`を指定してください。

```bash
agent-team start \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project

agent-team status \
  --config /absolute/path/to/config.toml \
  --cwd /absolute/path/to/project
```

## 安全境界を理解する

- 未対応のprovider、transport、permission、config version、state formatは、
  起動前に拒否します。別transportへ自動で切り替えません。
- ACPを使えるのはClaudeのread-only background roleだけです。Main ACP、
  Codex ACP、workspace-write ACPは拒否します。
- ACPのpermission制御はOS sandboxではありません。書き込みは、専用permission
  profileを持つdirect Codexに限定します。
- Agentの出力は信頼しません。Task、Dispatch、terminal、sender、Deliveryの
  identityが一致したときだけlifecycleを進めます。
- Claude ACPはambientな`claude.ai` loginを使い、API keyをchild processへ
  渡しません。ただしsubscription billing ledgerそのものは未確認です。
- 現在のStoreは`STORE_SCHEMA=4`とSQLite `user_version=4`を要求します。
  provider eventは`EVENT_SCHEMA_VERSION=2`のまま、workflow eventは別namespaceの
  `WORKFLOW_EVENT_SCHEMA_VERSION=1`を使います。schema-4 imageは既存9 tableに
  `task_policy_states`、`verification_operations`、`verification_receipts`を加えた正確な12 tableです。
- exactなschema-2とschema-3のStoreは、どちらもsource schemaとtarget `4`を持つ
  `StoreMigrationRequiredError`として停止し、read-only Doctorは`MIGRATION_REQUIRED`を報告します。
  malformed、mixed、missing、extra、future imageは別のschema/integrity errorです。Issue #48が
  明示的なmigration pathを所有し、Storeは暗黙のmigration、default補完、別backendへのfallbackを行いません。
- backupのdestinationは1つのexactなbasenameに限ります。database/manifest pairのidentityと
  contentをfinal readbackで確認できた場合だけ成功し、partialやmixedなpairは拒否します。
  restore candidate namespaceの`.coordination.sqlite3.restore-`は予約済みで、destinationには使えません。
- version-1 backup/inspectは従来の2-file、exact 10-field manifest shapeを維持します。
  schema-4 foundationの値は`store_schema=4`、`event_schema_version=2`、
  `sqlite_user_version=4`（`4/2/4`）です。production pathは新しい3 tableへrowを書かず、
  その3 tableが空のimageだけをstructural baselineにします。新しいtableが非空ならfail-closedであり、
  #80はnon-empty verification imageのinspectやbackup/restore成功を主張しません。
- established imageはrootを変更する前に、read-onlyでWAL/SHM-awareなpre-gateで分類します。
  structural WALはimageの一部としてcopyし、ephemeralなSHM cacheはSQLiteがprivateな一時copy上だけで
  再構成します。source、gate、marker、fileset、DB/WAL/SHM bytesは変わらず、checkpoint、truncate、
  delete、source sidecarの作成も行いません。
- #80のcodecは、15-fieldの`TaskPolicyStateV4`、approval-binding snapshot、body-free verification
  request、normalized receipt向けのpure version-1 codecです。argv/environmentのraw valueやraw bodyを
  保存せず、valueの内部整合性だけを検証します。owner authorityのcaptureやGate valueのhydrationは行いません。
  live capture/context、Store adapter、lifecycle transaction、logical record digest、non-empty imageの
  semantic validation、verification-aware Doctor/restoreは後続作業です。
- provider-only restoreは、history上のcontractどおりcandidate-firstかつprovider-freeです。#80が確認するのは
  新しいledgerが空のschema-4 backup/restore round tripだけであり、logの暗黙修復、external effectのretry、
  non-empty verification imageの認可は行いません。
- P0 StoreはWorkflowEngine reducerや外部effect adapterを配線せず、外部effectの
  exactly-onceも主張しません。

Issue #73では、このStoreと注入されたdurable effect backendの間にprivateな
`workflow_effect_adapter.py` seamを追加します。publicな`TeamRuntime`と`BackendPort`の
`start`/`request`/`stop` 3 method、既存のrequest/result type、CLI/MCP envelopeは変えません。
現行のpublic `BackendPort`とOrca backendは、role effectのmetadata、generation、exactな
Delivery/read lookup、provider proofを持たないため、effect実行前に
`DurabilityUnsupported`でfail-fastする。現行OrcaのSTOPもcomposite-stop proofを持たず、
adapterはCLIやMCPへまだ配線していない。durableな`StartSpec.attach=True`も、focus stageの
composite proofがないため拒否する。

private pathは、`load → authority → begin → backendを1回だけ呼ぶ → post-effect authorityと
observationを検証 → Store receipt → projector → commit`です。共通capabilityには、
effect-key idempotencyまたはpure lookup、attempt/fence enforcement、consumer generationを
要求する仕様です。WAITにはexact Delivery lookup、READにはexact read lookup、STOPには順序付き
composite proofとpure lookupをaction-specificに要求します。START/PROMPTはgenerationを含む
effect割当てのpost-effect identityを束縛し、receiptとobservationはimmutableなfield snapshotを
保持する設計です。lookupが返すのはcommittedかつdigest検証済みのevidenceだけです。
`DurableDeliveryLookup`はWAIT originだけを扱い、ACK/reply lifecycleを再構成しません。
`DurableReadLookup`のoutputはbackendのpure lookupで取得します。committed effectのreplayでは
backend executeとprojectorは0回です。WAIT/READ/RELEASE/STOPでは、digestに束縛したpure
lookupを1回実行する場合があります。`INTENT`、`UNKNOWN_EFFECT`、response loss、restart時の曖昧さは
明示的な#32 recoveryのため`RecoveryRequired`として残ります。raw bodyは1 MiBのUTF-8までに
制限し、保存せずdigestだけにしますが、低entropy inputの同値性までは隠しません。
deterministic fake authority/backend/projectorと実Storeが証明するのはadapter contractだけで、
provider側のexactly-onceや#31のcross-store atomic joinは証明しません。WorkflowEngineの
reducer wiringは#33、policy/verification handoffは#74の担当です。
schema-4 foundationは[Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80)、
task/reviewのproduction transitionは[#81](https://github.com/iamtatsuki05/dotfiles/issues/81)、
verification transactionとadapter wiringは[#82](https://github.com/iamtatsuki05/dotfiles/issues/82)、
image evidence、backup/restore、Doctorは[#83](https://github.com/iamtatsuki05/dotfiles/issues/83)が担当します。

Issue #74のhandoffは、実際の#49 `ReviewPolicyUpdate`とpolicy、および実際の#50 `route_task()`と
matchingなreservation resultを受け取ります。各owner refは、owner validationと`save_*`/exact
`read_*` readbackを通った後に発行します。composerが`ApprovalRef`を作るのは、canonicalな
`REVIEW_DECISION + APPROVED`のreview authorityだけです。比較するのはoverlap fieldに限ります。
#49専有の`Run`/`Dispatch`/`Attempt`、terminal、review round、
target、`claim_ref`は#49 provenanceとして残し、#50と比較済みとは主張しません。Gateの
`start(ApprovalRef)`、`resume(VerificationHandle)`とstate portの6操作は維持します。handoff testの
deterministic fakeは、SQLite、restart、provider exactly-onceの証拠ではありません。
[Issue #78](https://github.com/iamtatsuki05/dotfiles/issues/78)のschema-4 workは、
[Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80)のfoundationと
[#81](https://github.com/iamtatsuki05/dotfiles/issues/81)、
[#82](https://github.com/iamtatsuki05/dotfiles/issues/82)、
[#83](https://github.com/iamtatsuki05/dotfiles/issues/83)の後続実装に分かれます。full ledger、
restart/replay、`mark_unknown`、non-empty imageの主張は#80の範囲外です。raw body/action alias/payloadの
経路やretry/fallbackはありません。

詳しい境界と失敗時の流れは、[アーキテクチャ](docs/architecture_JA.md)を参照してください。

## よくある失敗を調べる

| 症状 | 確認する内容 |
|---|---|
| `workspace is not managed by Orca` | macOSでは`orca repo add --path "$PWD"`、Linuxでは`orca-ide repo add --path "$PWD"`を実行する。 |
| `agent-team state already exists` | 2つ目を起動せず、`status`、`attach`、`stop`を使う。 |
| `role has no active Orca Dispatch` | Mainがそのroleを未起動か、すでにrelease済み。 |
| authenticationを求められる | agent-team外で`claude auth status`か`codex login status`を確認する。 |
| 初回のACP起動に失敗する | 固定した`npx` packageへのnetwork accessを確認し、明示的に再実行する。 |
| roleが`escalation`を返す | 保持されたterminalとRunを調べる。完了として扱わない。 |

## 用語と問い合わせ時の情報を揃える

このガイドで解決しない場合は、repository maintainerへ次の情報を渡してください。
実行command、config path、workspace、Orca version、Run/Task/Dispatch ID、関係する
最小限のerrorです。認証token、prompt本文、無関係なterminal出力は含めません。

- **Run**: 1回のteam実行で使うOrcaのnamespaceとcoordinator inbox。
- **Task**: Planner、Worker、Reviewerへ渡す、範囲を限定した1件の作業。
- **Dispatch**: Taskとterminalを結ぶ1回の実行attempt。
- **Delivery**: Mainが内容を処理し、acknowledgeするmessage batch。
- **direct**: providerの通常のinteractive CLI。
- **ACP**: Agent Client Protocol。固定したacpx client経由で使う。

## 変更を検証する

```bash
python3.13 -m unittest tests.test_agent_team tests.test_agent_team_mcp
uvx ruff check \
  scripts/agent-team/agent_team \
  tests/test_agent_team.py \
  tests/test_agent_team_mcp.py
uvx mypy --strict scripts/agent-team/agent_team
python3.13 -m build --wheel scripts/agent-team
zsh tests/test_agent_sync.sh
```

OrcaやACPの連携を変更した場合は、実環境で範囲を限定したsmoke testも行います。
`stop`後にterminal、state、prompt file、session、adapter processが残っていないことを
確認してください。
