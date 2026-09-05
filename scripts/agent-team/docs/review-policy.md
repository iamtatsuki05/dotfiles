# Serial review policy

[日本語](review-policy_JA.md)

`agent_team.review_policy` is the pure policy seam for a normal-lane write
task and for an express-lane write task already admitted by Issue #50. It
consumes a validated `TeamDefinition`, a v4 `TaskSpec`, and a v4
`TaskPolicyStateV4` observation. It returns immutable typed update/effect
intents; it does not start a provider, inspect a terminal or process, read a
prompt, open a workspace, or persist data. The owner-issued handoff built on
this seam is documented in [Policy/verification handoff](policy-verification-handoff.md).

## Fixed pair

Call `resolve_worker_reviewer_pair(definition, worker_node)` after topology
validation. The function follows exactly one `reviewed-by` edge from the
selected Worker. It rejects an unknown node, self-review, duplicate or
ambiguous edges, unknown reviewer nodes, and permission mismatches. The
resulting `ReviewPair` is deterministic and contains only the canonical Worker
and Reviewer node identities.

`SerialReviewPolicy` accepts `TaskLane.NORMAL` and `TaskLane.EXPRESS`, and
rejects `TaskLane.RESEARCH`. An EXPRESS task must already have passed Issue
#50's admission policy; #49 owns neither express admission nor its
small-change, single-exact-change, dependency, or exclusive-resource rules.
Both accepted lanes use the same fixed Worker/Reviewer pair and serial gate.
The Worker's node must be the workspace-write side of the pair and the
Reviewer's node must be read-only. The caller must supply a positive
`max_review_rounds`; no value is inferred from task prose or a prompt.
Dependency observations are explicit `DependencyState` values. Every declared
dependency must be present and in `approved` or `completed` before an
assignment is accepted. The policy also rejects more than one active write
assignment.

## Typed events and state

`ReviewPolicyState` wraps the existing immutable v4 task state with the
`RunId`, current `WorkerAssignment`, `WorkerCompletion`, `ReviewDecision`,
typed `last_event`, optional `reason_code`, and policy-binding observations.
It does not add fields to or replace `TaskPolicyStateV4`.

The wrapper validates causal state before the reducer sees it. `pending` has
no assignment or event observations; `assigned` has only its matching
assignment; `worker_done` and `review_pending` require a matching successful
completion and no decision; `approved` and `changes_requested` require the
corresponding Reviewer decision. `ask_user` and `failed` accept only their
typed Worker outcome or Reviewer decision (with `reason_code ==
"review-limit"` reserved for the review-round limit). `verifying`,
`completed`, and `verification_failed` are rejected by this policy. The
wrapper checks Run/Task/Dispatch/Worker/Reviewer/Attempt/completion identity,
round, target pair, and sequence correlation, so a hand-built observation
cannot bypass the gate.

The event types are deliberately closed:

- `AssignmentCommand` starts the first round from `pending`, or explicitly
  starts a new attempt from `changes_requested`.
- `WorkerCompletion` carries Run, Task, Dispatch, Attempt, Worker, Reviewer,
  sender terminal, completion ID, review round, and target `GitObjectId` plus
  `TreeDigest`. Only `succeeded` can enter `worker_done`; `timeout` and
  `failed` enter `failed`, while `question` and `escalation` enter
  `ask_user`.
- `ReviewRequest` carries the accepted typed completion and moves
  `worker_done` to `review_pending`. The resulting update includes one typed
  `ReviewerAssignment` effect intent.
- `ReviewDecision` carries the complete Task/Run/Dispatch/Worker/Reviewer/
  Attempt/completion identity, reviewer terminal, round, target pair, decision
  reference, completion-origin sequence, and a `ReviewDecisionKind`. Only a
  matching Reviewer decision can leave `review_pending`. A
  `ReviewerAssignment` effect is constructible only from one matching
  successful completion with the same identity, round, and target pair. The
  effect also carries the canonical `PolicyFingerprint` and is not dispatch
  authority until checked against the current `worker_done` state.

`reduce_policy(current, event, policy)` first validates the current observation,
including its causal `last_event`, identities, phase, round, target, and
sequence. It then compares the event's explicit expected sequence with the
current sequence. Every accepted next state stores that event as `last_event`
with `last_event.expected_sequence == next_state.sequence - 1`. It returns
`ReviewPolicyUpdate` containing the prior/next observation, the expected
sequence, the canonical `PolicyFingerprint` computed from the selected
team/task spec, fixed pair, round limit, and dependency contract, and typed
effect intents. It does not carry a caller-chosen round limit.
`validate_policy_update(update, policy)` revalidates the event type, phase
edge, wrapper identity, selected pair, accepted lane, dependencies, actual
round limit, next observations, origin event, and effect identity before a
future store can accept it. `ReviewPolicyUpdate.task_update(policy)` adapts to the
existing `ExpectedSequenceUpdate` seam after policy-bound validation. A stale
event raises `ReviewPolicyError` with code `stale-sequence`, and no update or
effect intent is returned. The public `validate_reviewer_assignment(effect,
policy, expected_state)` function provides the same policy binding plus current
Run/Dispatch/Attempt/terminal/target authority for an effect adapter.

`ReviewRequest` compares the accepted completion through its authoritative
identity, completion ID, origin sequence, outcome kind, and target pair. Its
explanatory text and the handoff copy's explanatory text are not compared and
cannot authorize or reject a handoff.

## Schema-4 review checkpoint producer (Issue #81)

The pure reducer remains the policy authority. The package-private
`ReviewCheckpointProducer` is the separate schema-4 persistence seam for a
normal `CoordinationStore`. It accepts an actual `ReviewPolicyUpdate`, the
actual `SerialReviewPolicy`, and the matching #74 owner-issued
`ReviewAuthorityRef`. The #74 process-local binding proof revalidates the
update, policy, issuer, reference, and complete nested causal values before
the Store sees them. A raw projection, `CompletionAdmissionRef`, `ApprovalRef`,
route result, reservation result, caller-supplied checkpoint, or event is not
an alternative input. The producer does not route a task, issue an owner ref,
compose approval, or invoke a backend, runner, reviewer process, or
reservation.

The producer is immutable and cannot be rebound through reinitialization or
post-issuance mutation. The handoff's owner registry Store is part of that
immutable binding. Its opaque request is accepted only by the exact registered
Store; Store method/error bindings, state-root identity, checkpoint
issuer, and returned commit/read evidence are revalidated before the result is
trusted. Unregistered ports are not invoked, foreign observations are rejected,
and only genuine Store errors preserve cleanup capability. The full persistence
boundary is specified in the
[coordination Store contract](coordination-store.md#schema-4-review-checkpoint-producer-issue-81).

The producer accepts exactly these three actual #49 edges, in order. Each edge
is one separate transaction:

| Actual event | Task-policy edge | Workflow edge | Result |
| --- | --- | --- | --- |
| `WorkerCompletion(kind=SUCCEEDED)` | `ASSIGNED(T) -> WORKER_DONE(T+1)` | `WORKER_DONE(W) -> WORKER_DONE(W+1)` | Materialize the exact assigned task preimage once, then commit the next full task row and one policy event. |
| `ReviewRequest` | `WORKER_DONE(T) -> REVIEW_PENDING(T+1)` | `WORKER_DONE(W) -> REVIEW_PENDING(W+1)` | Commit the next full task row and checkpoint, then return one `ReviewerAssignment` intent. |
| `ReviewDecision(kind=APPROVED)` | `REVIEW_PENDING(T) -> APPROVED(T+1)` | `REVIEW_PENDING(W) -> REVIEW_PENDING(W+1)` | Commit the next full task row and one state-preserving policy event; no effect is executed. |

Every commit updates `task_policy_states`, the current workflow checkpoint,
and one `workflow_events` row together. The task row and checkpoint reference
are compared in both directions. Task and workflow sequences advance by one;
the event is `kind='policy_transition'`, has the fixed producer actor, and
has `operation_id` and `receipt_id` set to `NULL`. The Store-owned authority
digest is written to both `checkpoint.review_authority` and
`event.evidence_ref`; the request and authority digests are separate
domain-separated values. Their timestamp and global workflow event ID are not
digest inputs; the event's own digest remains Store-defined.

The first edge requires an absent task row and an exact `ASSIGNED(T)` task
reference in the current checkpoint. It materializes that preimage inside the
same transaction before advancing to `WORKER_DONE`; a fault rolls back the
task row, checkpoint, and event together. The later edges require the existing
full row and guarded sequence, digest, bytes, identity, and checkpoint
preconditions. A current-only commit, sequence jump, synthetic event, stale
checkpoint, foreign owner ref, nested-value mutation, unresolved operation,
or non-empty verification table is rejected without a partial write.

Normal schema-4 open and `load_review_checkpoint()` validate only the closed
producer suffix: up to the three ordered policy events, full task row
projection, matching checkpoint snapshots, and the complete workflow prefix.
The read observation also includes the canonical checkpoint bytes immediately
before the first policy event, so the producer independently recomputes the
first and all subsequent request digests.
After all three commits, the fresh current pair is a workflow
`REVIEW_PENDING` checkpoint with `W0 >= 2` and a task row at policy phase
`APPROVED`; `verification_operations` and `verification_receipts` remain
empty, and
`ReviewerAssignment` is only a post-commit intent. The generic
`commit_transition()` remains state-preserving for roots without a task-ledger
row; after #81 creates that row, only a dedicated task-aware writer may
advance the root, and generic transition/lifecycle entry points reject it. The
schema-3 validator is unchanged. Backup,
inspect, restore, and Doctor continue to reject task-row images until their
separate #83 evidence contract is implemented.

`policy_authority_projection(update, policy)` is the return-only factory for a
validated update. `PolicyAuthorityProjection` contains only typed identity,
phase/event kind, completion/decision kind and reference, target pair, reason,
sequence, and policy fingerprint. Its normal constructor is disabled and the
value is issued by the policy-bound factory. Constructor privacy is an
API-shape guard, not a cryptographic boundary: persistence authority comes
from policy-bound canonical revalidation, so objects assembled through Python
internals are not accepted as handoff authority. It has no explanation,
prompt, raw provider output, or arbitrary command field.

`validate_policy_authority_projection(projection, update, policy)` is an
optional verification seam for an adapter. It recomputes the canonical
projection after `validate_policy_update` and requires exact equality for all
fields, including the actual Run/workspace, fixed pair, sequence, event and
decision identity, review round, fingerprint, and target. In particular, an
approved projection must have both target identities. It does not turn a raw
projection into authority.

The legal serial path is identical for normal tasks and Issue #50-admitted
express tasks:

```text
pending -> assigned -> worker_done -> review_pending -> approved
                                  \-> changes_requested -> assigned (new attempt/dispatch)
```

`ReviewDecisionKind.CHANGES_REQUESTED` invalidates the old attempt. A retry
must provide both a new `AttemptId` and a new `DispatchId`, and must increment
the round. At the explicit limit, the reducer returns `ask_user` with
`reason_code == "review-limit"`; it never emits `verifying` or `completed`.
The current and next round must be at most the explicit policy limit. Approval
at exactly the limit remains valid; another changes request becomes the
review-limit `ask_user` outcome.

The reducer compares all typed identities and both target digests. Duplicate,
foreign, late, wrong-reviewer, stale-attempt, and old-round events are
rejected without changing the supplied state. Explanations are bounded text
for humans only: strings such as `APPROVED`, terminal idle/done state, process
exit, and Main/Agent prompt text are not event types or approval authority.

## Future adapters and the current Store boundary

`ReviewPolicyStorePort.update(update, policy)` remains the minimal pure-policy
integration seam. It must call `validate_policy_update(update, policy)` before
accepting the typed update. The current schema-4 producer uses its separate
package-private `ReviewWorkflowStorePort.commit_review_policy()` seam for the
transaction described above; it does not weaken or replace the generic
`commit_transition()` contract. `ReviewPolicyEffectPort.assign_reviewer(assignment,
policy, expected_state)` must call `validate_reviewer_assignment(assignment,
policy, expected_state)` before dispatch; the expected state must be the
matching `worker_done` state.
`ReviewPolicyHandoffPort.save_authority(update, policy)` receives the
policy-bound `ReviewPolicyUpdate` and actual `SerialReviewPolicy`, not a raw
projection. The #74 `PolicyVerificationHandoff` implementation revalidates
those inputs, calls `policy_authority_projection(update, policy)` internally,
converts only bounded primitive fields into its private record, and performs
`save_review_authority` followed by exact `read_review_authority` readback
before issuing the return-only `ReviewAuthorityRef`. There is no public
raw-projection save port.

The handoff may issue a ref for an accepted update, but its composer admits
only canonical `REVIEW_DECISION` + `APPROVED`. The four projection event kinds
remain unchanged, and review transitions stay owned by this module. A
malformed, foreign, stale, mutated, or non-approved value is rejected before
approval composition. The handoff stores no explanation, prompt, raw body, or
provider output, and it does not add retry or fallback behavior.

This module and the handoff adapter do not implement the Store transaction,
process restart, `mark_unknown`, or provider exactly-once proof. The earlier
[Issue #78](https://github.com/iamtatsuki05/dotfiles/issues/78) plan to own the
durable ledger, restart boundary, and full runtime correlation is historical.
Issue #81 now owns only the normal-Store task/review suffix described above;
Issue #80 owns the schema-4 physical foundation, [#82 verification
transactions and adapter](https://github.com/iamtatsuki05/dotfiles/issues/82)
owns actual completion admission, capture/context, and verification
lifecycle, and [#83 non-empty image evidence, backup/restore, and
Doctor](https://github.com/iamtatsuki05/dotfiles/issues/83) owns those image
boundaries. The handoff's deterministic fake is test evidence only. The
reducer remains pure, returns `ReviewerAssignment` as an intent, and never
invokes an external process or backend.
