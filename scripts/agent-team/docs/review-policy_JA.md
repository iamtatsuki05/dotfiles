# Serial review policy

[English](review-policy.md)

`agent_team.review_policy`は、normal laneのwrite taskと、Issue #50でadmit済みのexpress laneのwrite taskに対する
純粋なpolicy seamです。検証済みの`TeamDefinition`、v4の`TaskSpec`、v4の`TaskPolicyStateV4`を受け取り、
不変なtyped update/effect intentを返します。provider、terminal、process、prompt、
workspaceを調べたり、データを保存したりしません。このseamを使うowner-issued handoffは
[Policy/verification handoff](policy-verification-handoff_JA.md)にまとめています。

## 固定pair

topologyの検証後は`resolve_worker_reviewer_pair(definition, worker_node)`を利用します。
選択したWorkerから出る`reviewed-by` edgeは1本だけを対象にします。unknown node、self-review、
重複または曖昧なedge、unknown Reviewer、permissionの不一致は拒否対象です。
返す`ReviewPair`は、canonicalなWorkerとReviewerのnode identityだけを持ち、入力順に左右されません。

`SerialReviewPolicy`は`TaskLane.NORMAL`と`TaskLane.EXPRESS`を受け付け、`TaskLane.RESEARCH`は拒否します。
EXPRESS taskはIssue #50のadmissionを通過済みであることが前提です。express固有のsmall-change、single exact change、
dependency、exclusive resourceの条件は#50が所有し、#49は推測・再判定しません。両laneは同じ固定Worker/Reviewer pairと
serial gateを使います。pairのWorkerはworkspace-write、Reviewerはread-onlyでなければなりません。
`max_review_rounds`は正の整数をcallerが明示し、task本文やpromptから推測しません。依存関係の観測値は`DependencyState`で
明示します。宣言した全dependencyが存在し、`approved`または`completed`になるまでassignmentを受け付けません。
active write assignmentが2件以上ある状態も拒否します。

## typed eventとstate

`ReviewPolicyState`は、既存の不変なv4 task stateに`RunId`、現在の`WorkerAssignment`、
`WorkerCompletion`、`ReviewDecision`、typedな`last_event`、任意の`reason_code`、policy bindingの観測値を加えたwrapperです。
`TaskPolicyStateV4`へfieldを追加したり、置き換えたりしません。

wrapperはreducerの前にstateの因果関係を検証する設計です。`pending`にはassignmentやevent観測値を
含めず、`assigned`にはmatchingするassignmentだけを保持します。`worker_done`と
`review_pending`にはmatchingする成功completionだけが条件で、decisionは不要です。
`approved`と`changes_requested`には、それぞれ対応するReviewer decisionが必要です。
`ask_user`と`failed`は、typedなWorker outcomeまたはReviewer decisionだけをoriginに限定します。
review round上限の`reason_code == "review-limit"`だけは例外的に明示する値です。このpolicyは
`verifying`、`completed`、`verification_failed`を拒否対象です。wrapperはRun/Task/Dispatch/Worker/
Reviewer/Attempt/completionのidentity、round、target pair、sequenceの対応も確認するため、手作りの
観測値でgateを迂回できません。

eventの種類は意図的に限定しています。

- `AssignmentCommand`は`pending`から初回roundを開始するか、`changes_requested`から明示した
  new attemptを開始します。
- `WorkerCompletion`はRun、Task、Dispatch、Attempt、Worker、Reviewer、sender terminal、
  completion ID、review round、対象の`GitObjectId`と`TreeDigest`を持ちます。`succeeded`だけが
  `worker_done`へ進み、`timeout`と`failed`は`failed`、`question`と`escalation`は`ask_user`へ進みます。
- `ReviewRequest`は受理済みのtyped completionを持ち、`worker_done`から`review_pending`へ進める仕様です。
  updateにはtypedな`ReviewerAssignment` effect intentが1件含まれます。
- `ReviewDecision`はTask/Run/Dispatch/Worker/Reviewer/Attempt/completionの完全なidentity、
  reviewer terminal、round、対象identityのpair、decision reference、completion originのsequence、
  `ReviewDecisionKind`を持つ定義です。`review_pending`を離れるにはmatching Reviewerのdecisionが必要です。
  `ReviewerAssignment` effectは、identity・round・target pairが一致する成功completionからだけ作れます。effectにはcanonicalな`PolicyFingerprint`も保持します。

`reduce_policy(current, event, policy)`は、まずcurrent observationの`last_event`、identity、phase、round、
target、sequenceを検証し、その後でeventのexpected sequenceとcurrent sequenceを照合する流れです。受理した
eventは、`last_event.expected_sequence == next_state.sequence - 1`として次のstateへ記録します。
`ReviewPolicyUpdate`は、選択したteam/task spec、固定pair、round上限、dependency contractから計算した
canonicalな`PolicyFingerprint`を保持します。callerがround上限をupdateごとに差し替えるfieldはありません。
`validate_policy_update(update, policy)`は、event type、phase edge、wrapper identity、選択pair、許可されたlane、dependency、
実際のround上限、next observation、origin event、effect identityを再検証します。
`ReviewPolicyUpdate.task_update(policy)`で、policy bindingを確認した後に既存の`ExpectedSequenceUpdate` seamへ渡せます。
古いeventはcodeが`stale-sequence`の`ReviewPolicyError`になり、updateやeffect intentを返しません。
transportや再構成後にstore adapterが再検証できるよう、公開関数`validate_policy_update`と
`validate_reviewer_assignment(effect, policy, expected_state)`を提供します。effectはmatchingするcurrent
`worker_done` stateと照合してからdispatchへ渡します。

`ReviewRequest`は、completionのauthorityであるidentity、completion ID、origin sequence、outcome kind、
target pairだけを比較します。説明文とhandoff側の説明文は比較せず、handoffを許可・拒否する根拠にもなりません。

## schema-4 review checkpoint producer（Issue #81）

pureなreducerがpolicyのauthorityであることは変わりません。package-privateな
`ReviewCheckpointProducer`は、通常のschema-4 `CoordinationStore`へ接続する別の保存seamです。
受け取るのは、実際の`ReviewPolicyUpdate`、実際の`SerialReviewPolicy`、および対応する#74 owner-issued
`ReviewAuthorityRef`だけです。Storeへ渡す前に、#74のprocess-local binding proofがupdate、policy、issuer、
reference、nestedな因果値全体を再検証します。raw projection、`CompletionAdmissionRef`、`ApprovalRef`、
route result、reservation result、caller指定のcheckpointやeventを代替入力にはしません。taskのroute、owner refの
発行、approvalのcompose、backend、runner、reviewer process、reservationの呼び出しも行いません。

producerはimmutableであり、再初期化やrequest発行後のmutationでは束縛先を変更できません。handoffのowner registry
Storeもimmutableな束縛に含めます。opaque requestを受け付けるのはexactな登録済みStoreだけです。Store method/errorの束縛、
state-root identity、checkpoint issuer、
返されたcommit/read evidenceを再検証してから結果を信頼します。未登録portは呼び出さず、foreign observationを
拒否し、cleanup capabilityを保持するのはgenuine Store errorだけです。永続化境界の全体は
[coordination Store contract](coordination-store_JA.md#schema-4-review-checkpoint-producerissue-81)で定義します。

受け付ける#49のactual edgeは次の3つだけで、順序も固定します。各edgeは別々のtransactionでcommitします。

| actual event | task-policy edge | workflow edge | 結果 |
| --- | --- | --- | --- |
| `WorkerCompletion(kind=SUCCEEDED)` | `ASSIGNED(T) -> WORKER_DONE(T+1)` | `WORKER_DONE(W) -> WORKER_DONE(W+1)` | current checkpointのexactなassigned preimageを一度だけmaterializeし、次のfull task rowとpolicy eventを保存します。 |
| `ReviewRequest` | `WORKER_DONE(T) -> REVIEW_PENDING(T+1)` | `WORKER_DONE(W) -> REVIEW_PENDING(W+1)` | 次のfull task rowとcheckpointを保存した後、`ReviewerAssignment` intentを1件だけ返します。 |
| `ReviewDecision(kind=APPROVED)` | `REVIEW_PENDING(T) -> APPROVED(T+1)` | `REVIEW_PENDING(W) -> REVIEW_PENDING(W+1)` | 次のfull task rowとstate-preservingなpolicy eventを保存します。effectは実行しません。 |

各commitは、`task_policy_states`、current workflow checkpoint、1件の`workflow_events` rowを同じtransactionで
更新し、task rowとcheckpoint referenceを双方向に比較します。task/workflow sequenceは常に1つ進みます。eventのshapeは
`kind='policy_transition'`、固定したproducer actor、`operation_id=NULL`、`receipt_id=NULL`です。Store-owned
authority digestは`checkpoint.review_authority`と`event.evidence_ref`の両方へ保存します。authority digestとrequest digestは
別domainで計算し、timestampとglobal workflow event IDを含めません。event自体のdigestはStoreの定義に従います。

最初のedgeでは、task rowがまだ存在せず、current checkpointのtask referenceがexactな`ASSIGNED(T)`であることが
必要です。同じtransaction内でpreimageをmaterializeしてから`WORKER_DONE`へ進むため、faultがあればtask row、
checkpoint、eventをまとめてrollbackします。後続edgeは、既存のfull rowに対してsequence、digest、bytes、identity、
checkpointのguardを要求する契約です。current-onlyのcommit、sequence jump、synthetic event、stale checkpoint、foreign
owner ref、nested valueのmutation、unresolved operation、verification tableの非空は、partial writeなしで拒否します。

通常のschema-4 openと`load_review_checkpoint()`が検証するのは、producerが作るclosed suffixだけです。最大3件の
ordered policy event、full task row projection、matchingするcheckpoint snapshot、completeなworkflow prefixを
確認します。3つのcommitが完了したfresh current pairは、workflowが`REVIEW_PENDING`で`W0 >= 2`、task rowがpolicy
phase `APPROVED`です。read observationは最初のpolicy event直前にあるcanonical checkpoint bytesも含み、producerは
先頭と後続のrequest digestをすべて再計算します。
verification ledgerの`verification_operations`と`verification_receipts`は空のままで、
`ReviewerAssignment`はcommit後のintentにすぎません。
generic `commit_transition()`はtask-ledger rowのないrootでstate-preserving contractを維持します。#81が
task rowを作った後は、専用のtask-aware writerだけがrootを進め、generic transitionとpublic lifecycle
entry pointは拒否します。
schema-3 validatorは変更しません。backup、inspect、restore、Doctorは、
別の#83 evidence contractができるまでtask rowを含むimageを拒否します。

`policy_authority_projection(update, policy)`は、検証済みupdateからcanonicalなprojectionを返すreturn-only factoryです。
`PolicyAuthorityProjection`に含めるのはtyped identity、phase/event kind、completion/decision kindとreference、
target pair、reason、sequence、policy fingerprintだけです。通常constructorは無効化し、policy-bound factoryから発行します。
constructorの非公開性はAPI形状上のガードにすぎず、暗号学的な境界として扱いません。保存時のauthorityはpolicy-boundな
canonical再検証で確定します。そのため、Pythonの内部機構で組み立てたprojectionもhandoff authorityの受理対象外です。
explanation、prompt、raw provider output、任意commandのfieldは持たせません。

`validate_policy_authority_projection(projection, update, policy)`は、adapterが受け取ったprojectionを確認するための
optionalな検証seamです。`validate_policy_update`の後にcanonical projectionを再計算し、実際のRun/workspace、固定pair、
sequence、eventとdecisionのidentity、review round、fingerprint、targetを含む全fieldの完全一致を要求します。特に
approved projectionにはtarget identityを2つとも必須にします。この関数はraw projectionをauthorityへ昇格させません。

normal taskとIssue #50でadmit済みexpress taskのserial pathは同じです。

```text
pending -> assigned -> worker_done -> review_pending -> approved
                                  \-> changes_requested -> assigned (new attempt/dispatch)
```

`ReviewDecisionKind.CHANGES_REQUESTED`は旧attemptを失効させます。retryには新しい`AttemptId`と
新しい`DispatchId`の両方が必要で、roundも1つ増やします。明示した上限に達した場合は
`reason_code == "review-limit"`の`ask_user`を返し、`verifying`や`completed`は発行しません。
current/nextのroundがpolicyの明示上限を超えるstateは、どのeventでも拒否します。上限と同じroundのapprovalは
許可しますが、同じroundのchanges requestはreview-limitの`ask_user`へ進めます。

typed identityと対象の2つのdigestはすべて照合します。duplicate、foreign、late、wrong Reviewer、
stale attempt、old roundのeventは、stateを変えずに拒否します。explanationは人間向けのbounded text
にすぎず、`APPROVED`という文字列、terminalのidle/done、process exit、Main/Agentのprompt本文は
event typeでもapproval authorityでもありません。

## 後続adapterと現在のStore境界

`ReviewPolicyStorePort.update(update, policy)`は、pureなpolicy integrationの最小seamとして残ります。typed updateを
受け入れる前に`validate_policy_update(update, policy)`を呼ぶことが契約です。現在のschema-4 producerは、上記transaction
のために別のpackage-privateな`ReviewWorkflowStorePort.commit_review_policy()` seamを使います。genericな
`commit_transition()`のcontractを緩めたり、置き換えたりしません。`ReviewPolicyEffectPort.assign_reviewer(assignment,
policy, expected_state)`はdispatch前に`validate_reviewer_assignment(assignment, policy, expected_state)`を呼び、
matchingする`worker_done` stateをexpected stateにします。
`ReviewPolicyHandoffPort.save_authority(update, policy)`はraw projectionを受け取らず、policy-boundな
`ReviewPolicyUpdate`と実際の`SerialReviewPolicy`だけが入力です。#74の`PolicyVerificationHandoff`は
この入力を再検証し、内部で`policy_authority_projection(update, policy)`を呼び、boundedなprimitive fieldだけの
private recordへ変換して`save_review_authority`で保存します。続いて`read_review_authority`のexact readbackを
確認した場合だけ、return-onlyの`ReviewAuthorityRef`を発行します。raw projectionだけを保存するpublic portはありません。

handoffは受理済みupdateからrefを発行できますが、composerがadmitするのはcanonicalな
`REVIEW_DECISION` + `APPROVED`だけです。projection event kind 4種類は変えず、review transitionのauthorityもこのmoduleに残します。
malformed、foreign、stale、mutated、non-approvedな値はapproval compositionの前に拒否します。explanation、prompt、raw body、
provider outputを保存せず、retryやfallbackも追加しません。

このmoduleとhandoff adapterは、Store transaction、process restart、`mark_unknown`、provider exactly-once proofを
実装しません。以前の[Issue #78](https://github.com/iamtatsuki05/dotfiles/issues/78)がdurable ledger、restart境界、full runtime
correlationを一括で所有する計画はhistoricalなものです。Issue #81が所有するのは上記のnormal Store向けtask/review suffixだけで、
Issue #80はschema-4のphysical foundation、[#82 verification transactionとadapter](https://github.com/iamtatsuki05/dotfiles/issues/82)は
actual completion admission、capture/context、verification lifecycle、[#83 non-empty image evidence、backup/restore、Doctor](https://github.com/iamtatsuki05/dotfiles/issues/83)は
image境界を所有します。handoffのdeterministic fakeはtest evidenceだけです。reducerはpureなまま`ReviewerAssignment` intentを返し、
外部processやbackendは呼び出しません。
