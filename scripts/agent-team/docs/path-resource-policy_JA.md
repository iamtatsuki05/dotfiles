# canonical path/resource claim と lane routing

[English](path-resource-policy.md)

`agent_team.path_resource_policy` は Issue #50 の pure な admission 境界です。
v4 `TaskSpec` が明示した path/resource 宣言と、trusted snapshot が渡す workspace 観測を
検査し、決定的な routing 結果を返します。`TaskSpec`、topology、review state、config、
store は変更しません。このseamを使うowner-issued completion handoffは
[Policy/verification handoff](policy-verification-handoff_JA.md)にまとめています。

## Path claim

`PathClaim` には次の3項目をすべて明示します。

- `relative_path`: 観測済み workspace からの正規化した POSIX relative path
- `kind`: 1つの entry を表す `PathKind.EXACT`、または自身と descendant を表す
  `PathKind.DIRECTORY`
- `access`: `PathAccess.READ` または `PathAccess.WRITE`

absolute path、`..` component、control文字、末尾のslash、未対応のglob文字は拒否します。
`.` は workspace root を明示する claim に限って使えます。拡張子、Task本文、欠落値から claim を推測しない方針です。
`PathClaimPolicy.from_task_spec` は `allowed_paths` と `do_not_modify` の各文字列に対して、
typed claim がちょうど1つずつ対応する形に限定します。受理した claim は canonical order
に揃え、allow同士の重複・ancestor/descendant交差を拒否する設計です。deny claim と明示された
`reserved_roots` は allow より先に検査するルールです。

`PathObservation` は trusted snapshot producer が渡す typed value です。canonical path、
entry kind、device/inode、link数、parent identity、ancestor symlink flag を含む構成です。
admission は `Path.resolve()` を呼ばず、directory を走査せず、fileを開きません。missing、
workspace外、symlink、special file、device不一致、hardlink、不完全な観測、case collision
は拒否対象です。すべての操作で `.` → lexical ancestor → target の完全な observation chain
を要求し、ancestorが既存directoryで、parentのdevice/inodeが次の観測へつながることを
検査対象にします。root directoryにも正のlink数が必要です。異なるpathが同じnon-null
`(device, inode)` identityを共有する場合は、snapshotが
`nlink=1`と報告しても拒否対象となります。この後に発生する time-of-check/time-of-use race は
producerと後段 backendの責務です。

操作は `PathMutation` で明示します。

- `READ`: 既存の1 entryを検査
- `CREATE`: targetがmissingであることを検査
- `MODIFY`: 既存のregular fileであることを検査
- `DELETE`: sourceとparentを検査
- `RENAME`: source、destination、双方のparentを検査

触れるすべてのpathに、必要なaccessのallowが必要です。exact matchingは単純な文字列prefix
ではありません。`src/a` は `src/ab` に一致しない仕組みです。path admissionに失敗した場合は
`PathAdmission(candidate=False, reason_code=...)` を返し、resource portを呼ばない設計です。

## Resource claim

`ResourceClaimPolicy` は既存の `ResourceClaim` を包み、`ResourceKey` と `ResourceMode` を
明示的に要求します。`adapt_resource_claims` は caller が渡す既知keyの明示的な
`frozenset` と一対一のbindingを検査します。keyをlowercaseにしたり、claim名から導いたり、
modeの既定値を補ったりしません。同じkeyは、両方が `ResourceMode.SHARED` の場合だけ共存
できます。

resource claimを持つtaskには、opaqueで空でない `owner_id`、0以上の `lease_epoch`、1以上の
`fencing_token` を持つ `ResourceReservationAuthority` も明示します。このauthorityは
`ResourceReservationRequest` に入り、resultから完全一致で返る必要があります。owner、epoch、
tokenの欠落・外部値はcandidateにしません。resource claimがないtaskではauthorityを作らず、
reservation portも呼びません。

`route_task` はimmutableな `known_keys` を受け取り、public boundaryで同じ
`adapt_resource_claims` の一対一検査を再実行します。そのため、unknown key、TaskSpecの
duplicate claim、forgeされたmodeを、portがechoしただけで既知扱いにはしません。

`ResourceReservationPort` はconsumer側の境界だけを定義します。

```python
def reserve(
    request: ResourceReservationRequest,
) -> ResourceReservationResult: ...
```

callerがopaqueなreservation identityとauthorityを明示します。requestにはtask/reservation ID、
claimのname/key/mode、authorityから作るcanonical SHA-256 digestも含まれます。resultはtask ID、
digest、authority、sorted claim-key tupleを完全一致でechoしなければなりません。routeはpureな
path/profile/lane検査を通過した後に限りportを1回だけ呼ぶ仕組みです。一致する `RESERVED` だけが
candidateになります。同じrequestの明示的なidempotent再実行は後段authorityが同じidentityを返す
場合だけ許可し、禁止された二重reservationは `CONFLICT` または `STALE` を返します。
`CONFLICT`、`UNKNOWN`、`STALE`、呼び出し失敗、replay、identity不一致はcandidateになりません。
SQLite、lock、lease、owner/epoch/fencing authority、release、raceの解決は #31/#11 の後段実装の
責務です。このmoduleはlocal reservation stateや別providerへのfallbackを持ちません。

public boundaryでは、opaqueなauthorityとstring-backedなkey/digestにexact runtime typeを要求する
設計です。authorityとrequest identityはsubclassの `__eq__` や暗黙のstring変換を呼ばず、fieldごとの
比較とします。topology/profile bindingの `TeamDefinition`、`AgentNode`、`ProfileRef`、`Edge`、
`EdgeKind` もexact typed valueに限定し、built-in fieldのcanonical tupleでidentityを比較する形です。
serial policyはvalidatedなTaskSpec、topology、pair、worker、dependency、roundからcanonicalに再構成し、
各fieldとfingerprintをbuilt-in valueだけで比較します。そのため、equalityを上書きしたdataclassや
string subclassをauthorityとして扱いません。malformedな
observation、binding、known-key、path claim、reserved root、TaskSpec path collectionの診断には固定precedence
（`invalid-type`、`invalid-task`、`empty-value`、`unsafe-text`、path error、
`unknown-resource-mode`、`unknown-resource-key`、duplicate/missing/extra claimの順）を使うため、
tuple順やhash seedで拒否理由は変わりません。

## Lane matrix

`route_task` は `TaskSpec` に明示されたlaneだけを受け付け、常に
`parallel_candidate=False` を返します。

| Lane | 必須条件 | Decision |
| --- | --- | --- |
| `normal` | 一致する `SerialReviewPolicy`、workspace-write Worker、read-only Reviewer、admit済みwrite path、必要なreservation | `SERIAL`。review/completion gateを維持 |
| `express` | normalの条件に加え、`kind=small-change`、依存なし、既存regular fileを変更する単一exact write claim、exclusive resourceなし | normalと同じserial review/completion gate |
| `research` | `kind=research`、同じ `TeamDefinition` にある worker node の `read-only` profile、read-only path claim、READ操作、resource claimなし | `READ_ONLY`。workspace-write permission/completion authorityなし |

serial policy は同じ `TaskSpec`、固定されたWorker/Reviewer pair、同じ `TeamDefinition`、
再計算したpolicy fingerprintへの一致が必須です。profile bindingの `TeamDefinition`にある
node profileからWorker/Reviewerのpermissionを再計算し、callerのbooleanをauthorityにしない
構造です。researchでは同じtopologyにworker nodeが存在し、そのprofile permissionが
`read-only` であることを確認します。reviewerとserial policyは受け付けない設計です。
profile、policy fingerprint、path observation、resource key、lane/kindの不一致はfail closed
です。Task本文、Agentの自己申告、provider状態、別backendでlaneを変更しません。

## 責務の境界

このmoduleはvalueとpure checkだけを提供します。composition rootがvalidated topology/profile
bindingとtrusted observationを渡します。Worker → Reviewer のtransitionはreview policy、
atomic ownership/fencingは将来のreservation/store、durable completionはworkflow engineの
責務です。provider process、terminal、filesystem scan、database、lock file、workspace-write
completionはここでは作りません。

## completion admissionへのhandoff

Issue #74の`PolicyVerificationHandoff.issue_completion_admission(...)`は、実際のtyped task、
path、resource、profile、reservation入力で`route_task()`を1回呼びます。routeのpostconditionと、
必要なreservation portのresultを検証した後だけ、#50 ownerの`CompletionAdmissionRef`を発行します。
callerが`LaneRoutingDecision`、`PathAdmission`、`ResourceReservationResult`、routing digest、
reservation digestをauthorityとして渡す経路はありません。

handoffが受け付けるのは、normalまたはexpressのserial write candidateで、review/completion gate、
workspace-write permissionが有効、parallel flagがなく、rejection reasonもない場合だけです。
resourceを持つtaskには、同じrequest identityを持つmatchingな`RESERVED` resultも必要です。
claimなしのtaskではreservation authorityを持たせず、reservation portも未実行のままにします。
research/read-only route、conflict、unknown/stale result、port failure、identity mismatchではrefを
発行しません。handoffはretryせず、別lane、provider、backendへのfallbackも選びません。

返すrecordに残すのは、path/resource observationとroutingのcanonical primitive identityおよび
domain-separated digestです。raw route/reservation object、mutable observation、Task本文、provider
outputは保持しません。`Run`、`Dispatch`、`Attempt`、terminal、review round、target、`claim_ref`は
#49 review ownerのfieldです。このmoduleはそれらを補ったり、cross-checkしたりしません。full runtime
correlationは[Issue #78](https://github.com/iamtatsuki05/dotfiles/issues/78)へ渡します。
