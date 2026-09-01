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

## 後続adapter

`ReviewPolicyStorePort.update(update, policy)`が最小の保存seamです。将来の実装はcompare-and-swap、
transaction、保存形式を担当し、受け入れ前の`validate_policy_update(update, policy)`呼び出しを
契約に含めます。`ReviewPolicyEffectPort.assign_reviewer(assignment, policy, expected_state)`も、
dispatch前の`validate_reviewer_assignment(assignment, policy, expected_state)`呼び出しが前提です。
`ReviewPolicyHandoffPort.save_authority(update, policy)`はraw projectionを受け取らず、policy-boundな
`ReviewPolicyUpdate`と実際の`SerialReviewPolicy`だけが入力です。#74の`PolicyVerificationHandoff`は
この入力を再検証し、内部で`policy_authority_projection(update, policy)`を呼び、boundedなprimitive fieldだけの
private recordへ変換して`save_review_authority`で保存します。続いて`read_review_authority`のexact readbackを
確認した場合だけ、return-onlyの`ReviewAuthorityRef`を発行します。raw projectionだけを保存するpublic portはありません。

handoffは受理済みupdateからrefを発行できますが、composerがadmitするのはcanonicalな
`REVIEW_DECISION` + `APPROVED`だけです。projection event kind 4種類は変えず、review transitionのauthorityもこのmoduleに残します。
malformed、foreign、stale、mutated、non-approvedな値はapproval compositionの前に拒否します。explanation、prompt、raw body、
provider outputを保存せず、retryやfallbackも追加しません。

このmoduleとhandoff adapterは、SQLite、schema-4 ledger record、process restart、`mark_unknown`、provider exactly-once proofを
実装しません。以前の[Issue #78](https://github.com/iamtatsuki05/dotfiles/issues/78)がdurable ledger、restart境界、full runtime
correlationを一括で所有する計画はhistoricalなものです。現在の責務は、[Issue #80 schema-4 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)、
[#81 task/review persistence](https://github.com/iamtatsuki05/dotfiles/issues/81)、
[#82 verification transactionとadapter](https://github.com/iamtatsuki05/dotfiles/issues/82)、
[#83 non-empty image evidence、backup/restore、Doctor](https://github.com/iamtatsuki05/dotfiles/issues/83)に分かれます。
handoffのdeterministic fakeはtest evidenceだけです。reducerは引き続き`ReviewerAssignment`だけを返し、外部processやbackendは呼び出しません。
