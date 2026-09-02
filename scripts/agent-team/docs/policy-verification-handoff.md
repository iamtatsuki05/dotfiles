# Policy/verification handoff

[日本語](policy-verification-handoff_JA.md)

Issue #74 freezes the boundary that hands review and completion authority to the
verification entry point. The boundary has three owners: #49 issues review
authority, #50 issues completion admission, and #51 runs the fixed verification
gate. The handoff adapter composes those owner-issued values without moving
their policy decisions into a new module.

This is a contract and implementation-status reference for maintainers. Read
the flow and status sections first. The owner sections are lookup material when
changing one of the three seams.

## The frozen flow is owner-issued at every boundary

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

The caller cannot replace an owner value with a projection, route decision,
reservation result, digest, task text, or provider output. `ApprovalRef` is
transportable as a bounded identifier, but it is authority only when it resolves
to the exact approval and both exact owner records in the injected contract
registry.

## Current status: #74 handoff and #81 review producer are implemented; #80 provides the foundation

The current #74 code provides the private handoff module,
`PolicyVerificationHandoff`, and an injected package-private contract registry.
It also provides focused deterministic tests. The registry is process-local
test/composition infrastructure; this package does not provide a production
SQLite implementation or a durable codec for these private records.

| Area | Current implementation status | Not established here |
| --- | --- | --- |
| Review authority | Validate the actual `ReviewPolicyUpdate` and `SerialReviewPolicy`, derive the canonical projection internally, save a bounded record, read it back exactly, then issue `ReviewAuthorityRef`. | A new review transition, a caller-supplied projection, or a durable schema. |
| Completion admission | Call the existing `route_task()` once with typed path/resource/profile inputs and the reservation port; issue `CompletionAdmissionRef` only for an eligible matching result. | A second route, retry, alternate lane/provider/backend, or provider proof. |
| Composition | Resolve and revalidate both owner records, compare only their overlap, retain #49-only fields as #49 provenance, and save/read back the bound approval. | A claim that #50 owns or verified #49-only runtime fields. |
| Verification entry | Preserve `VerificationGate.start(ApprovalRef)`, `resume(VerificationHandle)`, and the six existing state-port operations. | SQLite durability, fresh-process replay, `mark_unknown`, or provider exactly-once. |
| Schema-4 review checkpoint | #81 consumes the actual #49 update plus its bound `ReviewAuthorityRef` and commits the closed three-edge task/workflow suffix through the normal Store. | `CompletionAdmissionRef`, `ApprovalRef`, verification rows, external effects, or image/restore authority. |

The focused suite proves the handoff contract and a deterministic fake state
model. It does not turn that fake into production persistence.

The [Issue #80 schema-4 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)
fixes the physical twelve-table image and pure task/verification codecs, but it
does not change the #74 handoff into a durable authority. The normal Store's
[#81 review checkpoint producer](https://github.com/iamtatsuki05/dotfiles/issues/81)
now writes only the full `task_policy_states` row and its three policy-event
suffix entries. `verification_operations` and `verification_receipts` remain
empty, and backup, inspect, restore, and Doctor still reject this non-empty
task-row image until #83 supplies the image-evidence contract. Actual
completion admission, capture/context, verification lifecycle, and durable
verification records remain downstream work in #82.

## #49 issues review authority from the actual update and policy

`PolicyVerificationHandoff.save_authority(update, policy)` accepts exactly the
typed `ReviewPolicyUpdate` and the actual `SerialReviewPolicy`. It revalidates
both values, calls `policy_authority_projection(update, policy)` internally,
and converts only the bounded typed fields to a private record. The record is
persisted through `save_review_authority`, read through
`read_review_authority`, and compared exactly before the return-only
`ReviewAuthorityRef` is issued.

The existing four projection event kinds remain unchanged:
`ASSIGNMENT`, `WORKER_COMPLETION`, `REVIEW_REQUEST`, and `REVIEW_DECISION`.
An owner reference may describe any accepted policy update, but the composer
admits only a canonical `REVIEW_DECISION` whose decision is `APPROVED` and
whose phase is `approved`. A pending, changed, failed, stale, late, foreign,
wrong-reviewer, self-review, old-attempt, or target-mismatched update does not
become approval authority.

`ReviewAuthorityRef` is an opaque return-only value with a reference and
digest. Its normal constructor, copy/deepcopy, pickle, foreign issuer, and
field mutation are rejected. The issuer marker and module-private weak
object-identity binding are in-process misuse guards, not a cryptographic
claim; the exact owner record and readback are the authority.

Owner identifiers and workspace paths keep the safe Unicode grammar already
accepted by #49, #50, and #51. Exact comparison uses UTF-8 bytes without Unicode
normalization, while edge whitespace, control characters, surrogates, and line
separators remain invalid.

There is no public raw-projection save path. The adapter does not store review
explanations, prompt text, task/reviewer/agent bodies, or provider output.

## #50 issues completion admission from one actual route

`PolicyVerificationHandoff.issue_completion_admission(...)` passes typed task,
path observation, resource claim, profile, and reservation-port inputs to the
existing `route_task()` exactly once. It does not accept a caller-provided
`LaneRoutingDecision`, `PathAdmission`, `ResourceReservationResult`, routing
digest, or reservation digest as authority.

The handoff takes immutable primitive snapshots before and after routing. They
cover the full canonical `TaskSpec`, lane profile, topology, serial policy,
path/resource observations, and reservation request identity. Mutation by the
reservation port is rejected as input drift before a completion record is
saved.

The route must satisfy every completion postcondition:

- lane is `NORMAL` or `EXPRESS`;
- `dispatch_mode` is `DispatchMode.SERIAL`;
- `serial_review_required` and `completion_gate_required` are true;
- `permits_workspace_write` is true;
- `parallel_candidate` is false and `reason_code` is `None`; and
- a resource-bearing task has the same request identity and a matching
  `RESERVED` result from the reservation port.

For a task without resource claims, the route carries no reservation authority
and does not call the reservation port. Research/read-only routes, non-
candidates, `CONFLICT`, `UNKNOWN`, or `STALE` results, port exceptions, and
identity mismatches issue no ref and do not retry or fall back.

`CompletionAdmissionRef` is return-only. Its bound record keeps canonical
primitive identity and digests for the workspace, path claims/observations,
resource claims, lane and gate flags, policy/profile binding, and reservation
binding. Raw route objects, reservation objects, mutable observations, task
text, and provider output are not stored.

## The composer compares overlap, not invented cross-owner fields

The trusted composer accepts only the two opaque owner refs:

```python
compose(
    review_ref: ReviewAuthorityRef,
    completion_ref: CompletionAdmissionRef,
) -> ApprovalRef
```

It resolves and revalidates each ref against its own exact Store record. The
cross-owner comparison is limited to fields present on both records:

- team, task, and workspace identity;
- Worker and Reviewer pair;
- verification profile and lane;
- serial policy fingerprint; and
- each owner record's reference and digest, checked against that record.

The completion owner does not have `Run`, `Dispatch`, `Attempt`, Worker or
Reviewer terminal, review round, target `HEAD`/tree, or `claim_ref`. Those are
#49-owned runtime fields. The composer validates them in the #49 record and
retains them in the bound approval as #49 provenance; it does not claim that
#50 route/reservation data was compared against them. The adapter never adds
those fields as raw arguments or guesses them from task text, path names, or a
reservation ID. The #81 review producer consumes this #49 evidence through
its process-local binding seam, but it does not accept a #50
`CompletionAdmissionRef`, create an `ApprovalRef`, or persist owner-private
records. The full runtime join remains downstream of the [Issue #80
foundation](https://github.com/iamtatsuki05/dotfiles/issues/80) and belongs to
the verification work in [#82](https://github.com/iamtatsuki05/dotfiles/issues/82)
and image work in [#83](https://github.com/iamtatsuki05/dotfiles/issues/83).
Each review ref is bound to the exact `PolicyVerificationHandoff` and registry
that issued it. A text-identical ref from another handoff is foreign and is
rejected before the #81 Store transaction.

After the overlap check, the composer derives a stable approval identity,
passes the validated bound approval to #51's private factory, saves an approval
record containing both owner refs/digests and the #51 authority digest, and
requires exact readback before returning `ApprovalRef`. `resolve(ApprovalRef)`
rechecks the approval and both owner records before returning the bound value.

Foreign, bare, forged, mutated, wrong-issuer, missing, or digest-mismatched
refs are rejected before approval or state mutation. There are no
`projection_to_ref()`, `decision_to_ref()`, `receipt_to_ref()`, or bare-ref
fallback aliases.

## Registry save and exact readback are part of issuance

The package-private registry contract has one explicit save/read pair for each
record:

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

The handoff does not trust a save response alone. It saves the bounded
record, reads the exact reference, validates type/issuer/identity/digest, and
returns an owner ref only after equality with the intended record. The same
reference and digest is an idempotent replay. The same reference with another
record is a conflict and never overwrites the stored value. If a save response
is lost, success is accepted only when readback proves the exact record;
otherwise a bounded recovery-required error is returned.

The record shape contains primitive identity and domain-separated digests only.
Raw request/result/receipt bodies, task or reviewer text, paths as authority
payload, reservation objects, provider output, secrets, and tokens do not
cross this boundary. These module-local record classes and issuer sentinels are
not the durable hydration API for #80 or its downstream consumers. The #80
projection codecs remain pure; Store-issued hydration belongs to [#82](https://github.com/iamtatsuki05/dotfiles/issues/82).

## #51 keeps both Gate entrances and all six state operations

The existing caller-facing Gate remains unchanged:

```python
class VerificationGate:
    def start(self, approval_ref: ApprovalRef) -> VerificationHandle: ...
    def resume(self, handle: VerificationHandle) -> VerificationTerminalResult: ...
```

The injected `VerificationStatePort` keeps exactly these six operations:

- `prepare_once`
- `begin_effect_once`
- `read`
- `status`
- `record_receipt_once`
- `apply_terminal_once`

The handoff exposes the same injected state port through `state_port()` after
checking that all six methods are present. It does not add a public
`mark_unknown` operation or turn a verification call into any existing
`start`/`prompt`/`wait`/`reply`/`read`/`release`/`ack`/`stop` action. The Gate
continues to own fixed-request, snapshot, receipt, and terminal validation;
the handoff only supplies the owner-bound approval and shared port.

## The deterministic fake is test evidence, not SQLite evidence

The focused handoff tests inject one in-process fake object for the owner
registry and the six-method state port. The contract model is deterministic and
thread-safe. It checks one winner for dual task/workflow sequence preparation
and all-or-none state transitions. The existing #51 Gate suite owns concurrent
effect-once and `RECEIPTED`/`TERMINAL` replay; a thin #74 integration test runs
the real Gate with the handoff and the same injected state port. Rejected or
mismatched refs leave state and effects unchanged.

Those tests establish call order, call counts, issuer checks, overlap checks,
and the handoff's rejection behavior. They do not establish SQLite schema,
transaction durability, process restart/reopen, crash recovery, cross-process
atomicity, or provider-side exactly-once execution.

## Schema-4 work is split across Issues #80–#83

The [Issue #80 schema-4 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80)
fixes the physical Store contract: `STORE_SCHEMA=4`, provider/workflow event
schemas `2/1`, the exact twelve-table object set, the version-1 manifest with
values `4/2/4`, the read-only WAL/SHM-aware pre-gate, and pure version-1
TaskPolicy/approval/request/receipt codecs. The three new ledger tables remain
empty in the #80 production path; an image with any non-empty new table fails
closed. #80 proves only the empty schema-4 backup/restore round trip.

The [#81 task and review transition](https://github.com/iamtatsuki05/dotfiles/issues/81)
now owns the normal-Store task row and closed three-event review suffix. It
does not create verification rows or consume a #50 completion admission. The
downstream [#82 verification transactions and adapter](https://github.com/iamtatsuki05/dotfiles/issues/82)
owns actual completion admission, owner capture/context, snapshot hydration,
the 58-field operation-row digest, and verification lifecycle. [#83 image
evidence, backup/restore, and Doctor](https://github.com/iamtatsuki05/dotfiles/issues/83)
owns non-empty image semantics and those image boundaries. Exact schema-2 and
schema-3 images remain `StoreMigrationRequiredError` to target schema `4`;
#80 does not migrate them.

The schema-4 children define their own canonical payload and decoder boundaries.
They consume the #74 owner-ref/approval contract; they must not reconstruct
`_ReviewAuthorityRecord`, `_CompletionAdmissionRecord`, or `_ApprovalRecord`
with `object.__new__`, copy their module-local issuer sentinels, or implement
the package-private registry protocol as a durable wire contract.

The #74 handoff itself still must not claim SQLite persistence, restart
recovery, durable `mark_unknown`, or provider exactly-once. #81's normal-Store
review suffix is a separate consumer contract, not a durable copy of the
private #74 registry records. A deterministic fake, an in-memory registry, a
terminal state, or an effect result is not a substitute for the verification
proof owned by #82.

## Rejection and non-goals are fail-closed

The #74 handoff rejects malformed or foreign authority before any approval or
state effect. It does not:

- accept raw bodies, action payloads, projections, decisions, results, receipts,
  or caller-provided digests as authority;
- alias verification onto another lifecycle action;
- infer missing identity from task prose, paths, reservation IDs, terminal
  liveness, or process output;
- retry a rejected route or select another lane, provider, backend, or profile;
- add SQL, DDL, schema migration, full ledger, `mark_unknown`, or restart
  recovery; or
- reimplement #49 review transitions, #50 path/resource semantics, or #51
  fixed-profile and runner semantics.

The existing owner modules remain the policy authorities. #74 adds only the
typed handoff and composition boundary that later durable work can consume.

## Focused verification map

The implementation is checked with the existing owner suites plus
`test_policy_verification_handoff_authority.py` and
`test_policy_verification_handoff_composer.py`. The focused handoff tests cover
actual update/policy issuance, actual route/reservation issuance,
safe Unicode owner identities/workspaces, nested input mutation rejection,
approved-only composition, overlap and digest mutation, bare/foreign/forged
ref rejection, save/readback behavior, unchanged Gate signatures, the six
state operations, explicit fake call-order traces, dual-CAS/all-or-none
behavior, and the existing Gate's effect-once/replay behavior through the
handoff.

SQLite reopen, crash injection, provider login/effect tests, schema-4
validation, durable `mark_unknown`, and production migration remain outside
#74. The #80 foundation and #81 review producer have their own Store evidence;
verification lifecycle and non-empty image evidence remain acceptance work for
#82 and #83.
