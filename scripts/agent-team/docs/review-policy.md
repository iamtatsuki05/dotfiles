# Serial review policy

[日本語](review-policy_JA.md)

`agent_team.review_policy` is the pure policy seam for a normal-lane write
task and for an express-lane write task already admitted by Issue #50. It
consumes a validated `TeamDefinition`, a v4 `TaskSpec`, and a v4
`TaskPolicyStateV4` observation. It returns immutable typed update/effect
intents; it does not start a provider, inspect a terminal or process, read a
prompt, open a workspace, or persist data.

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

## Future adapters

`ReviewPolicyStorePort.update(update, policy)` is the minimal persistence seam.
A future implementation owns compare-and-swap, transactionality, and storage;
it must call `validate_policy_update(update, policy)` before accepting the
typed update. `ReviewPolicyEffectPort.assign_reviewer(assignment, policy, expected_state)`
must call `validate_reviewer_assignment(assignment, policy, expected_state)`
before dispatch; the expected state must be the matching `worker_done` state.
`ReviewPolicyHandoffPort.save_authority(update, policy)` receives the
policy-bound `ReviewPolicyUpdate` and actual `SerialReviewPolicy`, not a raw
projection. Before any durable write, an implementation must call
`policy_authority_projection(update, policy)` and persist only that canonical
return value. There is no public raw-projection save port.
This module does not implement SQLite, JSON state, locks, journals, or Orca.
The reducer only returns `ReviewerAssignment`; it never invokes an external
process or backend. A durable workflow engine can therefore persist the
policy-bound authority projection without copying this policy's transition
table or retaining explanatory/raw provider text. A caller cannot make
synthetic approval authority by constructing or replacing a projection.
