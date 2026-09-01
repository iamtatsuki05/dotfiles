# durable fixed-argv verification gate

[English](verification-gate.md)

`agent_team.verification_gate` は、normal lane と admission 済み express lane の
write task に対する pure な completion 境界です。公開する処理の流れは次だけです。

```python
gate = VerificationGate(admission, profiles, snapshots, runner, state)
handle = gate.start(approval_ref)
terminal = gate.resume(handle)
```

caller が渡すのは opaque な approval reference と、返された opaque handle だけです。
review update、routing object、request、runner result、receipt、evidence、state は渡せません。
この module は subprocess、shell、filesystem、registry、database、receipt file、lease、
persistence を実装しません。Gateへapprovalを渡すowner-issued compositionは
[Policy/verification handoff](policy-verification-handoff_JA.md)にまとめています。

## authority の admission

`ApprovalAdmissionPort` は opaque な `ApprovalRef` を、#49/#50 の private な bound authority
へ変換する trusted composition-root adapter です。#74のcompositionでは
`PolicyVerificationHandoff`がこのadapterを担当します。#49の実際のupdateと、#50のowner seamを
通ったroute/reservationを受け取り、保存済みでbindingされたapprovalだけをGateへ解決します。
composerが比較するのは、両方のowner recordに存在するfieldだけです。#49専有のruntime fieldは
#49 provenanceとして残し、#50と照合済みとは主張しません。verification gateはraw update、
projection、route、reservation、caller-provided digestを受け取らず、validatorも再実装しません。

`ApprovedReview` は return-only で、Run、Team、workspace、Task、Dispatch、Attempt、
Worker/Reviewer node と terminal、review round、target `HEAD`、allowed claim の
tree/manifest digest、claim reference、review fingerprint、profile reference、approval
sequence、routing digest、reservation digest を束ねます。approval と routing の digest は
後続の request/receipt digest に必ず含まれます。

## 固定 profile と request

trusted な `VerificationProfileResolver` だけが verification の実行条件を返します。
`VerificationProfileIdentity` と `VerificationExecutableIdentity` は #19/#35 とは別の
verification 専用型です。composition root は trusted registry をこの protocol の adapter
として渡します。この module は registry を定義せず、profile を追加・選択・昇格しません。

profile が固定する値は次のとおりです。

- absolute canonical executable path、exact version、lowercase SHA-256
- typed argv template と digest、最大1個の exact な `{workspace}` element
- literal な `canonical-workspace` cwd policy
- sorted な有限 safe environment name/value set
- bounded timeout/output limit
- normalized result-schema identity と profile-binding digest

safe environment set は `CI`、`LANG`、`LC_ALL`、`LC_CTYPE`、`NO_COLOR`、`TERM`、`TZ` に
限定します。`ORCA_*`、proxy/endpoint/config/home、`LD_*`、`DYLD_*`、`PYTHON*`、
loader/interpreter override、provider secret は拒否対象です。request には safe value set
を渡せますが、receipt には name と profile-binding digest だけを残し、value は保存しません。
実行 authority となる `PATH` は継承せず、absolute な pinned executable を使います。

request builder は private です。profile 所有の argv だけをコピーし、`{workspace}` を
trusted canonical workspace に一度だけ置き換えます。unknown placeholder、追加引数、
noncanonical cwd、追加 name は拒否します。固定 element 内の shell metacharacter はデータ
として扱い、Task/Reviewer/Agent 本文は入力にしません。request、receipt、evidence、approval、
gate state は return-only constructor と issuer marker を使います。

issuer marker は同一 process 内の通常 constructor を防ぐための guard であり、暗号学的な
provenance ではありません。authority は trusted `ApprovalAdmissionPort` と durable
`VerificationStatePort` が持ちます。同一 Python process 内の任意コード実行まで脅威モデルに
含める場合の署名/HMAC envelope は上流の責務です。provider や Task 本文はこの authority 境界の
外にあります。

## 6操作のstate portでGateのsurfaceを変えない

`VerificationStatePort` は gate の生成時に必須です。production implementationがCAS、
persistence、idempotence、effect fencing、restart recoveryを担当します。Issue #74は既存6操作の
shapeを凍結し、deterministic fakeで確認しますが、SQLite implementationやfresh processでの
recovery proofは提供しません。[Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80)が提供するのは
schema-4の物理foundationとpure codecだけです。production adapter、non-empty lifecycle、restart proofは
含まれず、durableな後続責務は[#81](https://github.com/iamtatsuki05/dotfiles/issues/81)、
[#82](https://github.com/iamtatsuki05/dotfiles/issues/82)、[#83](https://github.com/iamtatsuki05/dotfiles/issues/83)へ分かれます。

```python
class VerificationStatePort(Protocol):
    def prepare_once(self, request) -> VerificationPrepareResult: ...
    def begin_effect_once(verification_ref, request_digest) -> VerificationEffectLease: ...
    def read(self, verification_ref) -> VerificationDurableRecord: ...
    def status(self, verification_ref) -> DurableRecordStatus: ...
    def record_receipt_once(verification_ref, effect, result, before, after) -> VerificationReceipt: ...
    def apply_terminal_once(verification_ref, receipt_ref, receipt_digest) -> VerificationTerminalResult: ...
```

`start(approval_ref)` は bound authority と named profile を解決し、approval target の
before snapshot を取得して fixed request を構成し、`prepare_once` を呼びます。matching
する prepared result と approved→verifying CAS が成功した後だけ handle を返します。in-memory
state は authority ではありません。

`resume(handle)` は record と status を読みます。durable state implementationがあれば、durable
reference、approval reference、request digestだけを持つhandleでprocess restart後のreplayを
支えられます。ただし#74のfakeが示すのはprocess内のcall orderとreplay contractだけで、SQLite
reopenやfresh process recoveryの証拠ではありません。`begin_effect_once` はopaqueなeffect nonce、
lease epoch、fencing tokenと、次のstatusのいずれかを返します。

| Effect status | gate の動作 |
| --- | --- |
| `RUN_ONCE` | fresh before を確認して runner を一度だけ呼ぶ |
| `RECEIPTED` | 保存済み receipt を再検証し、runner は呼ばない |
| `TERMINAL` | terminal receipt を再検証し、runner は呼ばない |
| `UNKNOWN` | `RecoveryRequired` とし、retry/fallback しない |

durable authority が concurrent/repeated call を判定します。prepared response loss や
unknown effect を absent と解釈せず、runner を二度呼びません。

## runner と snapshot の順序

runner が受け取るのは opaque な fixed request と store-issued effect lease だけです。runner
直前に named profile と executable を再解決し、fresh before snapshot を取得します。workspace、
device/inode、claim、target `HEAD`、tree/manifest の全 field を durable prepared request と
比較し、不一致なら runner より前に停止します。

runner 後は typed result、executable before/after、effect nonce/epoch/fencing、schema、
output 上限、cleanup、identity を検証します。after snapshot も検証してから
`record_receipt_once` を呼ぶため、不正な result/snapshot は receipt port に渡りません。

receipt を記録した後も named profile と request を再解決・再計算してから
`apply_terminal_once` を呼びます。profile/executable drift があれば receipt は recovery 用に
残り、terminal completion には進みません。

## outcome と receipt の束縛

`completed` になれるのは、`PASSED`、exit code `0`、schema 一致、bounded output、
`CleanupStatus.REAPED`、effect fence 一致、normalized receipt 一致がすべて揃う場合だけです。
`FAILED`、`TIMEOUT`、`OUTPUT_LIMIT`、`SCHEMA_INVALID` は spawned process の `REAPED` が証明
された場合だけ `verification_failed` になります。`RUNNER_UNAVAILABLE` は `NOT_STARTED` と
output なしを明示しなければなりません。cleanup/effect unknown、response loss、malformed port
value、snapshot drift は `RecoveryRequired` です。自動 retry や別 provider/backend fallback は
ありません。

`VerificationReceipt` は return-only です。raw output、prompt、command text、PID、環境変数値
は持ちません。canonical digest は receipt reference、full approval/routing/request/profile
binding、executable before/after、effect nonce/epoch/fencing、argv/cwd/env/schema、両 snapshot
（workspace/claim/target HEAD/tree/manifest）、result metadata、cleanup を束ねます。terminal
validation でも同じ receipt/result validator を再実行し、同じ ref の別 digest は replay として
受け付けません。

ここでいう`VerificationReceipt`は#51 Gate内のruntime valueであり、schema-4の永続operation
recordではありません。#80が定めるのはSQLの`record_version=1` discriminatorとpureな
request/receipt projectionだけです。58-fieldのoperation-row digestとStore-issued hydrationは
後続作業です。

resume/replay は profile を再解決し、fresh current snapshot を取得し、durable receipt の
after と比較します。記録済み executable-after も保持するため、古い after 観測だけで task を
completed にできません。

## 責務と制約

#49はreview transitionとapproval provenance、#50はpath/resource admissionとreservation identity、
#74はtyped owner-ref compositionとdeterministic fakeの境界をそれぞれ所有します。productionの
durable state/effect/receipt portとterminal CASは#11/#31/#33の責務です。schema-4 workは、
[Issue #80 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)、
[#81 task/review transition](https://github.com/iamtatsuki05/dotfiles/issues/81)、
[#82 verification transaction/adapter](https://github.com/iamtatsuki05/dotfiles/issues/82)、
[#83 image evidence、backup/restore、Doctor](https://github.com/iamtatsuki05/dotfiles/issues/83)に分かれます。
#80のproduction pathは新しい3 tableを空のままにし、non-empty image semantics、lifecycle、capture、
adapter、hydration、logical record digest、verification-aware Doctor/restoreを主張しません。#32が
recovery handoffを受け取ります。このmoduleが定義するのはverification contractとpureなport orchestrationだけです。
focused testはfake port・providerなし・実workspaceなしで行うため、SQLite durabilityやprovider
exactly-once executionは証明しません。
