# Task policy schema version 4

[English](task-policy-v4.md)

`agent_team.task_policy`は、task policy dataの純粋でbackendに依存しない契約を定義します。
SQLiteやJSON fileを開く、lockを取得する、workspaceを調べる、providerを起動する、workflowを
遷移させるといった処理は行いません。

## TaskSpec

`TaskSpec`は1つのtaskを表す不変な定義です。すべてのfieldを明示します。

| Field | 意味 |
|---|---|
| `task_id` | 安定したtask identity |
| `title`、`context`、`goal` | taskを説明するtext |
| `acceptance` | 1件以上の受け入れ条件を持つtuple |
| `allowed_paths` | 変更範囲として宣言したpathのtuple。pathの意味は後続policyで扱う |
| `do_not_modify` | 変更禁止pathの明示的な宣言。空tupleも明示値として扱う |
| `dependencies` | このtaskより先に完了させるtask ID |
| `verification` | 明示的に指定するverification profile参照 |
| `escalation_node` | topology nodeの明示的な参照、または明示した`null` |
| `kind` | `implementation`、`small-change`、`research`のいずれか |
| `lane` | `normal`、`express`、`research`のいずれか |
| `resource_claims` | 明示的に指定する論理resource claim。各要素は`ResourceClaim` |

parserが受け付けるfieldは上記だけです。task本文からlane、argv、permission、provider、
default valueを推測しません。mappingからparseするときは、`parse_task_spec`が`acceptance`、
`allowed_paths`、`do_not_modify`、`dependencies`のlistをそれぞれtupleに揃える仕様です。
`resource_claims`はlist[str]から`tuple[ResourceClaim, ...]`への変換です。たとえば
`resource_claims = ["workspace"]`では`ResourceClaim("workspace")`を作り、tupleにします。
typedな直接構築では、これらのtuple、`TaskKind`/`TaskLane` enum、`TaskId`・`NodeId`・
`VerificationProfileRef`などのNewTypeが必要です。`tuple[str, ...]`を`resource_claims`へ渡す
ことはできません。
`TaskId`や`TeamId`などのidentity wrapperはPythonの`NewType`による型名です。Pythonのruntimeでは
元の`str`と区別できませんが、nominalなannotationによってtyped callerがidentityを混同しにくく
なります。文字列を検証してwrapperへ変換する場所はmapping parserに限定しています。

textには上限を設け、空文字、前後の空白、C0/C1/DEL control、単独のsurrogate、Unicodeの
line separatorを拒否します。未知のenum、欠落field、文字列でないmapping key、上限を超える
arrayも、runtime resourceを作る前に失敗します。

## 依存関係の検証

`validate_task_specs`は、登録済みteam ID、topology node ID、verification profile名を明示して
受け取る仕様です。unknown team・escalation node・verification profile、task IDの重複（大文字・
小文字だけが異なる場合を含む）、dependencyやresource claimの重複、自己依存、unknown dependency、
dependency cycleを拒否します。返す`ValidationResult`と`ValidationIssue`は不変で、codeとmessageの
順に並びます。

`task_dependency_order`はKahn法で順序を求めます。候補は`(task_id.casefold(), task_id)`の順に
選ぶため、入力tupleの順序に結果が左右されません。`canonical_task_json`は検証後に同じ順序で
taskを出力し、dependency orderを含むUTF-8 JSONを、sorted keyと末尾の改行付きで返します。

## TaskPolicyStateV4

`TaskPolicyStateV4`は、`version = 4`を明示した不変の観測・データrecordです。これは論理的な
state contractであり、既存runtimeのversion 3 `state.json`を置き換えません。v3からv4への
migrationも、このsliceでは行いません。envelopeは次の15 fieldで構成します。

| Field | Type | null可否 | 契約 |
|---|---|---|---|
| `version` | `int` | 不可 | 必ず`4` |
| `team_id` | `TeamId` | 不可 | 安定したteam identity |
| `workspace` | `WorkspaceIdentity` | 不可 | lexicalにcanonicalなabsolute path value。symlink・device・inode identityは証明しない |
| `sequence` | `int` | 不可 | 0以上のmonotonic sequence |
| `task_id` | `TaskId` | 不可 | 安定したtask identity |
| `attempt_id` | `AttemptId \| None` | 可 | `dispatch_id`と同時に指定 |
| `dispatch_id` | `DispatchId \| None` | 可 | `attempt_id`と同時に指定 |
| `worker_node` | `NodeId \| None` | 可 | 観測できたworker node identity |
| `reviewer_node` | `NodeId \| None` | 可 | 観測できたreviewer node identity |
| `review_round` | `int` | 不可 | 0以上のreview round |
| `target_head` | `GitObjectId \| None` | 可 | lowercase 40桁または64桁のGit object ID。`target_tree_digest`と同時に指定 |
| `target_tree_digest` | `TreeDigest \| None` | 可 | lowercase 64桁のSHA-256 digest。`target_head`と同時に指定 |
| `claim_ref` | `ClaimRef \| None` | 可 | opaqueな論理claim参照 |
| `receipt_ref` | `ReceiptRef \| None` | 可 | opaqueな論理receipt参照 |
| `phase` | `TaskPhase` | 不可 | 下記11 literalのいずれか |

`phase`のliteralは、`pending`、`assigned`、`worker_done`、`review_pending`、`approved`、
`changes_requested`、`verifying`、`completed`、`failed`、`ask_user`、`verification_failed`です。

optionalなidentity fieldも、canonical state envelopeでは必ず`null`として出力します。attemptと
dispatch、`HEAD`とtree digestは片方だけの指定を禁止します。workspaceの検査はlexicalなものに
限られ、filesystemのsymlink・device・inode identityはこのmoduleの責務外です。state valueには`complete`やtransition、
storageのmethodを持たない設計です。`completed`の観測値は、完了を許可するauthorityではありません。
将来のstoreは、別の手段でmutation authorityを確立する必要があります。

`TreeDigest` は、後続のtrusted snapshot portが作るcanonical tree manifestのSHA-256であり、
Git tree object IDではありません。本sliceは値の形式だけを固定し、manifestの計算や真正性確認は
行いません。

`parse_task_state`はv4のfield setを正確に要求します。v3 envelope、別version、欠落field、unknown
field、canonicalでないworkspaceは明示的なerrorにします。既存v3 runtimeの`state.json`は従来の
契約のままであり、この論理v4 recordによる置換、migration、書き戻しは行いません。v3からv4への
変換、欠落fieldの補完、v4からv3への書き戻し、backend fallbackはありません。

`ExpectedSequenceUpdate`は、将来のstore portへ渡すtypedな更新意図です。`apply_expected_sequence_update`
はexpected sequenceが一致すること、sequenceが1だけ増えること、team/workspace/task identityが
変わらないことを確認します。古いsequenceでは`StateConflictError`を送出します。このhelperは保存
副作用を持たず、SQLite、CAS、lock、journal、lease、transitionを実装しません。

## 後続の統合

config parserは、teamを明示的に選択した後で、この純粋なmapping parserを呼び出せます。backend
roadmapの保存実装は、SQL rowやfile formatを公開せず`TaskPolicyStatePort`へ適応させます。review
gate、pathのcanonicalization、laneの実行、provider effect、recoveryは後続のpolicy/backend
sliceの責務です。
