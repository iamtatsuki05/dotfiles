# Policy/verification handoff

[English](policy-verification-handoff.md)

Issue #74が凍結するのは、review authorityとcompletion authorityをverificationの入口へ
渡す境界です。ownerは3つに分かれ、#49がreview authority、#50がcompletion admission、
#51が固定されたverification gateを担当します。handoff adapterはこの3つのpolicyを
新しいmoduleへ移さず、ownerが発行した値だけを束ねます。

この文書は、contractと実装状況を確認するmaintainer向けのreferenceです。最初にflowと
statusを読み、変更対象に応じてownerごとの節を参照してください。

## すべての境界でowner-issuedの値を渡す

```text
#49 actual ReviewPolicyUpdate + SerialReviewPolicy
  -> #49-issued opaque ReviewAuthorityRef

#50 actual route_task() + matching reservation result
  -> #50-issued opaque CompletionAdmissionRef

ReviewAuthorityRef + CompletionAdmissionRef
  -> trusted PolicyVerificationHandoff composer -> opaque ApprovalRef

VerificationGate.start(ApprovalRef)
  -> #51-issued opaque VerificationHandle

VerificationGate.resume(VerificationHandle)
  -> #51-issued VerificationTerminalResult or RecoveryRequired
```

callerは、ownerの値をprojection、route decision、reservation result、digest、Task本文、
provider outputへ置き換えられません。`ApprovalRef` はbounded identifierとしてtransport
できますが、注入したcontract registry内のexactなapproval recordと2つのowner recordへ
解決できる場合だけauthorityになります。

## 現在の状態: #74、#81、#82のschema-4 pathは実装済み

現在の #74 codeには、private module `PolicyVerificationHandoff` と、注入するpackage-privateな
contract registryがあります。focusedなdeterministic testもあります。registryはprocess-localな
test/composition infrastructureです。このpackageは、これらのprivate record向けproduction SQLite
implementationやdurable codecを提供しません。

| 領域 | 現行の実装状況 | ここでは証明しないこと |
| --- | --- | --- |
| Review authority | 実際の`ReviewPolicyUpdate`と`SerialReviewPolicy`を検証し、canonical projectionを内部で導出し、bounded recordを保存してexact readbackを行った後に`ReviewAuthorityRef`を発行します。 | 新しいreview transition、callerが作ったprojection、durable schema。 |
| Completion admission | typedなpath/resource/profile入力とreservation portで既存の`route_task()`を1回呼び、条件を満たすmatching resultだけから`CompletionAdmissionRef`を発行します。 | 2回目のroute、retry、別lane/provider/backend、provider proof。 |
| Composition | 2つのowner recordを解決・再検証し、共通部分だけを比較し、#49専有fieldを#49 provenanceとして保持してbound approvalを保存・readbackします。 | #50が#49専有runtime fieldを所有または検証したという主張。 |
| Verification entry | `VerificationGate.start(ApprovalRef)`、`resume(VerificationHandle)`と、既存state portの6操作を維持します。 | SQLite durability、fresh process replay、`mark_unknown`、provider exactly-once。 |
| Schema-4 review checkpoint | #81が実際の#49 updateと、それに束縛された`ReviewAuthorityRef`を受け取り、normal Storeを通じて3 edgeのclosedなtask/workflow suffixをcommitします。 | `CompletionAdmissionRef`、`ApprovalRef`、verification row、external effect、image/restore authority。 |
| Store-backed verification | #82が#81のcurrent pairと保持済みの#50/#74 owner refを使い、Store-issued contextをcaptureし、privateな`agent_team.verification_store` adapterでverification operation/receipt lifecycleを保存します。 | fullなnon-empty imageのinspect、backup/restore、verification-aware Doctor、provider-side exactly-once。 |

focused suiteが証明するのはhandoff contractとdeterministic fakeのstate modelです。fakeを
production persistenceの証拠へ読み替えません。

[Issue #80 schema-4 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)は、schema-4の
物理的な12 tableとtask/verificationのpure codecを固定します。ただし、#74 handoff自体をdurableな
authorityへ変えるものではありません。現在の[#81 review checkpoint producer](https://github.com/iamtatsuki05/dotfiles/issues/81)は、
normal Storeでfullな`task_policy_states` rowと3件のpolicy-event suffixを書きます。実装済みの
[#82 verification path](https://github.com/iamtatsuki05/dotfiles/issues/82)は、このcurrent pairと保持済みのowner refから
contextをcaptureし、Gateをhydrateし、prepare/effect/receipt/terminal/unknown lifecycleを通して
`verification_operations`と`verification_receipts`を保存します。fullなnon-empty imageのinspect、backup、restore、Doctorは
#83の範囲です。

## #49は実際のupdateとpolicyからreview authorityを発行する

`PolicyVerificationHandoff.save_authority(update, policy)` が受け取るのは、typedな
`ReviewPolicyUpdate`と実際の`SerialReviewPolicy`だけです。2つを再検証し、内部で
`policy_authority_projection(update, policy)`を呼び、boundedなtyped fieldだけをprivate
recordへ変換して`save_review_authority`で保存します。
`read_review_authority`で読み戻してexact比較した後だけ、return-onlyの
`ReviewAuthorityRef`を発行します。

既存のprojection event kind 4種類は変更しません。対象は`ASSIGNMENT`、
`WORKER_COMPLETION`、`REVIEW_REQUEST`、`REVIEW_DECISION`です。owner refは受理済みの
policy updateを表せますが、composerがadmitするのはcanonicalな
`REVIEW_DECISION`、decisionが`APPROVED`、phaseが`approved`のものだけです。pending、
changed、failed、stale、late、foreign、wrong reviewer、self-review、old attempt、
target mismatchのupdateはapproval authorityになりません。

`ReviewAuthorityRef`はreferenceとdigestを持つopaqueなreturn-only valueです。通常の
constructor、copy/deepcopy、pickle、foreign issuer、field mutationは拒否します。issuer markerと
module-privateなweak object-identity bindingはprocess内の誤用を防ぐguardであり、暗号学的な
主張ではありません。authorityはexactなowner recordとreadbackにあります。

owner identifierとworkspace pathには、#49、#50、#51が受理する既存のsafe Unicode grammarを
そのまま適用します。exact比較ではUnicode normalizationを行わず、UTF-8 bytesを比較します。
前後の空白、control character、surrogate、line separatorは引き続き拒否対象です。

publicなraw-projection save経路はありません。review explanation、prompt本文、
task/reviewer/agent本文、provider outputも保存しません。

## #50は1回の実際のrouteからcompletion admissionを発行する

`PolicyVerificationHandoff.issue_completion_admission(...)` は、typedなtask、path
observation、resource claim、profile、reservation portの入力を既存の`route_task()`へ
1回だけ渡します。callerが用意した`LaneRoutingDecision`、`PathAdmission`、
`ResourceReservationResult`、routing digest、reservation digestをauthorityとして受けません。

routeの前後では、full canonical `TaskSpec`、lane profile、topology、serial policy、path/resource
observation、reservation request identityのimmutable primitive snapshotを比較します。
reservation portがnested inputを変更した場合はinput driftとして扱い、completion recordの保存前に
拒否します。

completion postconditionはすべて満たす必要があります。

- laneは`NORMAL`または`EXPRESS`です。
- `dispatch_mode`は`DispatchMode.SERIAL`です。
- `serial_review_required`と`completion_gate_required`はtrueです。
- `permits_workspace_write`はtrueです。
- `parallel_candidate`はfalse、`reason_code`は`None`です。
- resourceを持つtaskでは、request identityと一致するreservation portの`RESERVED` resultが必要です。

resource claimがないtaskでは、routeはreservation authorityを持たず、reservation portも
呼びません。research/read-only route、non-candidate、`CONFLICT`、`UNKNOWN`、`STALE`、
port exception、identity mismatchではrefを発行せず、retryやfallbackもしません。

`CompletionAdmissionRef`はreturn-onlyです。bound recordに保持するのは、workspace、path
claim/observation、resource claim、laneとgate flag、policy/profile binding、reservation
bindingのcanonical primitiveとdigestです。raw route object、reservation object、mutable
observation、Task本文、provider outputは保存しません。

## composerは共通項目だけを比較し、ownerにないfieldを補わない

trusted composerが受け取るのは、2つのopaque owner refだけです。

```python
compose(
    review_ref: ReviewAuthorityRef,
    completion_ref: CompletionAdmissionRef,
) -> ApprovalRef
```

各refを、それぞれに対応するexactなStore recordへ解決して再検証します。owner間で比較
するのは、両方のrecordに存在する次のfieldだけです。

- team、task、workspaceのidentity
- WorkerとReviewerのpair
- verification profileとlane
- serial policy fingerprint
- 各owner recordのreferenceとdigest（そのrecordとの対応を確認）

completion ownerは`Run`、`Dispatch`、`Attempt`、Worker/Reviewer terminal、review round、
target `HEAD`/tree、`claim_ref`を持ちません。これらは#49専有のruntime fieldです。
composerは#49 record内でそれらを検証し、bound approvalには#49 provenanceとして保持します。
#50のroute/reservationと照合済みとは主張しません。adapterがそれらをraw引数として追加したり、
Task本文、path名、reservation IDから推測したりもしない契約です。#81のreview producerは、この#49 evidenceを
process-local binding seam経由で消費しますが、#50の`CompletionAdmissionRef`を受け取らず、`ApprovalRef`を作らず、
owner-private recordも保存しません。実装済みの[#82 verification](https://github.com/iamtatsuki05/dotfiles/issues/82)は、#81の
current-pair read後に保持されたowner refを使ってStore-backed captureを行います。full image workは
[#83 image](https://github.com/iamtatsuki05/dotfiles/issues/83)の責務です。
review refは、発行元の`PolicyVerificationHandoff`とregistryにも束縛します。別handoffが発行した
text-identicalなrefはforeignとして扱い、#81 Store transactionの前に拒否します。

overlapの検査後、composerはstableなapproval identityを導出し、検証済みbound approvalを
#51のprivate factoryへの入力とします。両方のowner ref/digestと#51 authority digestを含むapproval
recordを保存し、exact readbackが済んだ場合だけ`ApprovalRef`を返す契約です。
`resolve(ApprovalRef)`は、approvalと2つのowner recordを再確認してからbound valueを返します。

foreign、bare、forged、mutated、wrong issuer、missing、digest mismatchのrefは、approvalや
stateを変更する前に拒否します。`projection_to_ref()`、`decision_to_ref()`、
`receipt_to_ref()`、bare-ref fallback aliasは提供しません。

## issuanceにはregistry saveとexact readbackが含まれる

package-privateなregistry contractには、各recordについて明示的なsave/read pairが1つあります。

```python
class _PolicyVerificationRegistryPort(Protocol):
    def save_review_authority(self, record): ...
    def read_review_authority(self, reference): ...
    def save_completion_admission(self, record): ...
    def read_completion_admission(self, reference): ...
    def save_approval(self, record): ...
    def read_approval(self, reference): ...
    def state_port(self): ...
```

実装はsave responseだけを信頼しません。bounded recordを保存し、exact referenceで読み戻し、
type、issuer、identity、digestを検証し、意図したrecordとの一致を確認してからowner refを
返します。同じreferenceとdigestならidempotent replayです。同じreferenceに別recordを渡す
場合はconflictであり、保存済みの値を上書きしません。save responseを失った場合も、exact
readbackで同じrecordを証明できたときだけ成功とします。証明できなければboundedな
recovery-required errorを返します。

recordはprimitive identityとdomain-separated digestだけで構成します。raw request/result/
receipt本文、TaskやReviewerの本文、authority payloadとしてのpath、reservation object、
provider output、secret、tokenはこの境界を越えません。module-localなrecord classとissuer sentinelは、
#80のdurable hydration APIではありません。#80のprojection codecはpureなままであり、Store-issued hydrationは
[#82](https://github.com/iamtatsuki05/dotfiles/issues/82)のprivate adapterで実装します。

## #51のGate入口2つとstate操作6つを維持する

既存のcaller-facing Gateは変わりません。

```python
class VerificationGate:
    def start(self, approval_ref: ApprovalRef) -> VerificationHandle: ...
    def resume(self, handle: VerificationHandle) -> VerificationTerminalResult: ...
```

injected `VerificationStatePort`も、次の6操作をそのまま持ちます。

- `prepare_once`
- `begin_effect_once`
- `read`
- `status`
- `record_receipt_once`
- `apply_terminal_once`

handoffの`state_port()`は、6操作がそろっていることを確認してから同じinjected state portを
返します。publicな`mark_unknown`操作は追加しません。また、verification callを既存の
`start`/`prompt`/`wait`/`reply`/`read`/`release`/`ack`/`stop` actionへaliasしません。Gateは
fixed request、snapshot、receipt、terminalの検証を引き続き担当し、handoffはowner-bound
approvalとshared portだけを渡します。

## Issue #82はprivateなStore adapter経由でhandoffを利用する

package-privateな`agent_team.verification_store` moduleは、#81のStore-issued current pairと保持済みの
#50/#74 owner refを利用します。`capture_approval_binding`は1つのrevisionでcontextを読み、handoffの
`compose`と`resolve`を各1回だけ呼び、最初のGate start用にapprovalをstageします。正確なadapter factoryは次のとおりです。

```python
StoreVerificationAdapter.from_capture(
    store, snapshot, staged_admission, profile_resolver
)
StoreVerificationAdapter.from_store(
    store, root_key, verification_ref, owner_id, profile_resolver
)
```

adapterは既存6操作のstate portを実装し、privateな`_read_with_status`と`_mark_unknown`を接続しますが、
publicなGate、CLI、MCP、Protocolのsurfaceには追加しません。`PREPARED`、`EFFECT_PREPARED`、`RECEIPTED`、
`TERMINAL`、`UNKNOWN_EFFECT`を保存・hydrateします。freshな`RECEIPTED`/`TERMINAL`のre-entryはrunner/effectを
0回でexact replayし、arm済みまたはunknownのeffectは明示的なrecoveryへ止めます。Store commit結果が不明な
場合は、利用可能なcleanup capabilityを付けた`RecoveryRequired`を返し、transaction自体をblind retryしません。
#82のunknown boundaryは8つの固定reason codeを持ち、`restore_invalidation`とfullなnon-empty imageのinspect、
backup/restore、Doctorは#83の責務です。provider-side exactly-onceは主張しません。

## deterministic fakeはSQLiteの証拠ではない

focused handoff testsは、owner registryと6操作のstate portを同じin-process fake objectへ
注入します。contract modelはdeterministicかつthread-safeです。task/workflow sequenceの
dual-CASで1つだけをwinnerにすることと、all-or-noneのstate transitionを確認します。
concurrent effect-onceと`RECEIPTED`/`TERMINAL` replayは既存の#51 Gate suiteが所有し、#74の
薄いintegration testが実Gateをhandoffと同じinjected state portへ接続します。拒否または
mismatchのrefではstateとeffectを変更しません。

このtestが示すのはcall order、call count、issuer check、overlap check、handoffのrejection
だけです。#82のSQLite transaction/reopen contractはStore-backed testで確認します。どちらのsuiteも
provider-side exactly-once executionは示しません。

## schema-4 workはIssue #80–#83に分かれる

[Issue #80 schema-4 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)は、物理的な
Store contractを固定します。対象は`STORE_SCHEMA=4`、provider/workflow event schema `2/1`、
正確な12 table、version-1 manifestの`4/2/4`、read-only WAL/SHM-aware pre-gate、
TaskPolicy/approval/request/receiptのpure version-1 codecを含みます。新しい3 tableは#80のproduction pathで
空のままです。いずれかがnon-emptyならfail-closedにし、#80で証明するbackup/restoreは空のschema-4
imageのround tripに限定します。

[#81 task/review transition](https://github.com/iamtatsuki05/dotfiles/issues/81)は、normal Storeのtask rowとclosedな3件の
policy-event suffixを担当します。verification rowは作らず、#50 completion admissionも消費しません。
実装済みの[#82 verification transaction/adapter](https://github.com/iamtatsuki05/dotfiles/issues/82)は保持済みのcompletion admissionを
消費し、ownerのcapture/context、snapshot hydration、58-fieldのoperation-row digest、verification lifecycleを担当します。
[#83 image evidence、backup/restore、Doctor](https://github.com/iamtatsuki05/dotfiles/issues/83)はfullなnon-empty image semanticsと
image境界を担当します。exact schema-2とschema-3 imageはtarget schema `4`への`StoreMigrationRequiredError`となり、#80はmigrationを行いません。

schema-4の各childは、独自のcanonical payloadとdecoder境界を定義します。#74から消費するのはowner refと
approval contractだけです。`_ReviewAuthorityRecord`、`_CompletionAdmissionRecord`、`_ApprovalRecord`を
`object.__new__`で復元したり、module-localなissuer sentinelを複製したり、package-private registry protocolを
durable wire contractとして実装したりしません。

#74 handoff自体にとって、SQLite persistence、restart recovery、durable `mark_unknown`、provider exactly-onceは
引き続き主張の対象外です。#81のnormal Store review suffixは、#74のprivate registry recordをdurableにコピーするものではありません。
deterministic fake、in-memory registry、terminal state、effect resultは、#82が所有するverification proofの代替になりません。

## rejectionとnon-goalはfail-closedにする

#74 handoffは、malformedまたはforeignなauthorityをapprovalやstate effectの前に拒否します。
次の経路は作りません。

- raw body、action payload、projection、decision、result、receipt、caller-provided digestをauthorityとして受ける
- verificationを別のlifecycle actionへaliasする
- Task本文、path、reservation ID、terminal liveness、process outputから欠落したidentityを推測する
- rejected routeをretryする、または別lane、provider、backend、profileを選ぶ
- SQL、DDL、schema migration、full ledger、`mark_unknown`、restart recoveryを追加する
- #49 review transition、#50 path/resource semantics、#51 fixed-profile/runner semanticsを再実装する

policyのauthorityは既存owner moduleに残ります。#74が追加するのは、後続のdurable workが
消費するtyped handoffとcomposition境界だけです。

## focused verificationの対応範囲

実装は既存owner suiteに加え、`test_policy_verification_handoff_authority.py`と
`test_policy_verification_handoff_composer.py`で確認します。focused handoff testsは、
実際のupdate/policy issuance、実際のroute/reservation issuance、safe Unicodeのowner identity/
workspace、nested input mutation rejection、approved-only composition、overlapとdigest mutation、
bare/foreign/forged ref rejection、save/readback、変更していないGate signature、state操作6つ、
fakeの明示的なcall-order trace、dual-CAS/all-or-none、handoff経由で既存Gateが示す
effect-once/replayを対象にします。

SQLite reopen、crash injection、provider login/effect test、schema-4 validation、production
migrationは#74の範囲外です。#80 foundation、#81 review producer、#82 Store-backed verification pathには
それぞれStore evidenceがあります。fullなnon-empty image evidenceは#83のacceptanceに残ります。
