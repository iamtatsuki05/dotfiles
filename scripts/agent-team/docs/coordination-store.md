# Coordination store decision

[日本語](coordination-store_JA.md)

## Decision

Use SQLite as the durable coordination store for the future local backend. Do not add an atomic-file runtime fallback.

The decision applies to a single host and a local filesystem. SQLite provides transactional multi-record updates, writer serialization, schema constraints, indexed diagnosis, and a consistent backup API without maintaining a custom locking and recovery protocol. The atomic-file prototype was invalidated for production: its restore reused fencing tokens, its diagnosis mutated an empty store, and it accepted a synthetic receipt without provider proof.

Neither approach can prove that an external effect did not happen when a process exits after the effect and before its receipt. The runtime must preserve this as `UNKNOWN_EFFECT`; it must not retry, close a resource, or switch backends automatically.

## Comparison

Both prototypes used Python 3.13.15 on macOS arm64 and only the standard library. The main agent reran every scenario after inspecting the code.

| Dimension | SQLite | Atomic file |
|---|---|---|
| Scenarios | 14/14 safety scenarios passed | 14/14 probes passed; three deliberately reproduced disqualifying behavior |
| Concurrent writers | Two writers inserted 64 total rows without loss | Two writers serialized through blocking `flock` |
| Stale writer | Fake provider rejected a stale-token call after observing the newer token | Revision CAS plus owner, attempt, and fencing token rejected a stale local receipt |
| Crash diagnosis | Distinguished intent-only from effect-without-receipt after reopen | Distinguished partial temp, complete candidate, renamed primary, and backup |
| Lock contention | `BEGIN IMMEDIATE` returned `database is locked` after the bounded timeout | Non-blocking `flock` returned `LockBusyError` |
| Backup | `Connection.backup()` restored an integrity-checked copy | One previous revision could be explicitly restored |
| Cleanup burden | Close connections, checkpoint WAL, and verify SQLite-owned WAL/SHM lifecycle | Classify known temp files, preserve unrelated files, fsync file and directory |
| Portability | Python `sqlite3` on supported platforms | POSIX `fcntl.flock`; not a Windows implementation |
| Implementation risk | Schema and transaction code | Custom database, lock, CAS, backup, and recovery protocol |

## Selection and production readiness are separate

The evidence supports selecting SQLite. It does not approve a production LocalBackend yet. The SQLite spike validates local transactions, same-operation contention, a process-safe fake provider, unknown-effect handling, lease expiry, a read-only doctor, guarded WAL cleanup, and a recovery epoch under the tested conditions.

Production implementation remains blocked until its own contract tests cover:

- restoring `RECEIPTED` and `COMPLETED` operations without stranding or replaying them,
  while leaving `CLEANED` tombstones unchanged;
- a strongly consistent provider status query when its database has an uncheckpointed WAL;
- both stale-call orderings around reclaim, including old-call-before-new-call;
- opaque provider receipt provenance, effect identity, fencing token, and epoch verification;
- expiry/force/audit rules for recovery and a complete doctor mutation vocabulary;
- clock behavior, writer-marker races, atomic restore, directory durability, and an append-only transition journal.

These blockers belong to the production store/LocalBackend issue. They do not reopen the SQLite-versus-atomic-file decision, and they must not trigger an atomic-file fallback.

The SQLite spike used Python SQLite 3.53.1, `WAL`, `synchronous=FULL`, `BEGIN IMMEDIATE`, and a 150 ms busy timeout. Its measured contender returned `database is locked`; end-to-end elapsed time exceeded the configured timeout because connection setup was also measured. WAL and SHM disappeared only after all connections closed.

## Required runtime contract

- Commit operation intent before invoking an external effect.
- Store operation ID, attempt, owner, `lease_epoch`, heartbeat/expiry, fencing token, idempotency key, phase, and exact resource receipt.
- Require matching owner, attempt, and fencing token for every mutation.
- Require the provider to enforce lease epoch/fencing for both old-call-first and new-call-first orderings.
- Use an external idempotency key or status lookup. SQLite is not an exactly-once effect mechanism.
- Treat effect-without-receipt as `UNKNOWN_EFFECT`. `doctor` may report it but must not mutate or retry it.
- A normal `CoordinationStore` reopen must not automatically change
  `FENCE_RESERVATION_STARTED` or `EFFECT_PREPARED`. These remain recovery-required markers;
  only the private trusted transaction returned by
  `CoordinationStore._recovery_transaction()` may call
  `_RecoveryStoreTx.mark_prepared_unknown` to move them to `UNKNOWN_EFFECT`.
- A recovery-floor advance fences stale authority globally but never rewrites a `CLEANED`
  tombstone row or its events. Typed rebasing is limited to `INTENT`, `RECEIPTED`, and `COMPLETED`.
- The typed recovery seam maps SQLite snapshot-query failures and malformed persisted observation
  values to stable `StoreIntegrityError`; existing store errors are preserved without double wrapping.
- Use explicit transactions with `BEGIN IMMEDIATE`, bounded busy timeout, `foreign_keys=ON`, `WAL`, and `synchronous=FULL`.
- Keep the database and sidecars in the private agent-team state root. Close every connection and checkpoint before cleanup.
- Back up through SQLite's backup API. Migration and restore must be explicit operations with their own version and rollback checks.
- Do not fall back to atomic files or another backend when SQLite is unavailable, locked beyond the allowed timeout, corrupt, or version-incompatible.

## Historical v3 workflow checkpoint store (Issue #72)

Issue #72 added the durable workflow checkpoint and compare-and-swap (CAS)
contract to the SQLite store. This section preserves the historical v3 P0
contract; it is not the current schema-4 foundation and is not a claim that the
later WorkflowEngine or external-effect adapters are complete.

### Version boundary and migration

The version boundary is explicit:

- `STORE_SCHEMA=3` is required in `PRAGMA user_version`, `store_meta`, and the
  exact schema validator.
- The existing provider journal remains `EVENT_SCHEMA_VERSION=2`.
  Workflow journal rows use the separate `WORKFLOW_EVENT_SCHEMA_VERSION=1`.
- A fresh v3 database creates the existing provider tables and exactly these
  four workflow tables: `workflow_checkpoints`, `workflow_operations`,
  `workflow_receipts`, and `workflow_events`.
- `BACKUP_MANIFEST_VERSION=1` keeps its ten-field wire shape. New manifests
  record `store_schema=3`, `event_schema_version=2`, and
  `sqlite_user_version=3`.

An existing database is classified before normal initialization. Only an
otherwise valid, exact v2 database is reported as
`StoreMigrationRequiredError`, a typed `StoreSchemaError` subclass; the
read-only Doctor reports the same condition as `MIGRATION_REQUIRED`.
Malformed, mixed, missing, unknown, or future schema objects remain distinct
schema/integrity errors. The Store does not add columns, fill defaults, create
an empty checkpoint, or silently accept a v2 image. A v2 manifest/database
pair is likewise rejected by v3 `inspect()` and `restore()`.

Issue #48 owns the explicit v2-to-v3 migration gate. Its migration ID,
quiescence, backup, epoch/fencing, candidate validation, cutover, and readback
must complete before the v3 Store accepts the resulting artifact. The P0 Store
does not infer or perform that migration, and it never falls back to another
backend.

### Checkpoint and typed composition seam

`WorkflowCheckpointV4` is a fixed, strict value. It binds the root and
workspace/config/state-root identity, Run and Main terminal, serial workflow
state, workflow/task sequence projections, assignment, Delivery and message
order, reply/read/release state, policy/review/verification references, the
last operation, and `updated_ns`. The payload is canonical UTF-8 JSON with a
fixed field set. Its `checkpoint_digest` is computed over the canonical body
without the digest field, using the dedicated v4 domain; it is stored as
`sha256:<64 lowercase hex>`. Scalar columns and payload bytes are checked in
both directions on write and load. Device/inode plus a content digest bind the
configuration; a canonical pathname alone is not authority.

The fresh-start boundary is represented by a separate strict
`WorkflowRootSeed`, not by an incomplete v4 checkpoint. A start operation has a
caller-supplied stable operation ID and expected workflow sequence zero. The
seed has no Run, Main terminal, task, assignment, Delivery, or authority and
is valid only in its dedicated seed codec. The seed, start intent, and first
workflow event are committed together. Only a verified start receipt can
promote that row once to a full checkpoint; a partial seed/intent/event is
`RecoveryRequired`. `load_checkpoint()` returns `None` only for a missing root
at a fresh start. A full checkpoint starts at workflow sequence 2; sequence 0
and 1 belong only to the strict seed protocol.

The public composition seam is typed and opaque:

- `WorkflowCheckpointDraft` is reducer input. The Store supplies the version,
  timestamp, canonical bytes, and digest; caller-provided digest or timestamp
  is not trusted.
- `OperationHandle` is a Store-issued frozen value bound to its Store instance,
  root, stable operation ID, sequence, and owner/fence identity. It does not
  expose a connection, row, lock, or mutable state, and it cannot be recreated
  from a dictionary, copy, pickle, or synthetic object.
- `DurableReceipt` is an immutable value issued by a trusted effect adapter.
  P0 does not expose a JSON/status/body factory for forging one and does not
  call a provider, Orca, Herdr, terminal, or backend.
- `WorkflowStorePort` exposes load, begin, effect commit, transition commit,
  lookup, and unknown marking. It returns observations only, never raw result
  bodies, provider payloads, SQL rows, or SQLite connections.

`load_checkpoint()` returns a durable `WorkflowRootSeed` while start is
unresolved and `WorkflowCheckpointV4` after the verified start commit. It
returns `None` only for an absent root. A seed is an observation, not effect
authority.
`lookup_operation()` returns only a verified committed lookup; absent, intent,
and unknown operations raise `RecoveryRequired` instead of returning `None`.

All identifiers, enum values, integer ranges, nullability, nested cardinality,
Delivery order, and opaque references are fail-closed. Prompt, reply, event,
terminal output, provider response, credential, token, environment, and other
raw bodies are not persisted.

### Four workflow tables and transaction rules

| Table | Durable responsibility |
|---|---|
| `workflow_checkpoints` | One current aggregate per root, including canonical checkpoint bytes/digest and scalar projections |
| `workflow_operations` | Stable operation intent, expected sequence pair, identity, and `INTENT`/`UNKNOWN_EFFECT`/`COMMITTED` lifecycle |
| `workflow_receipts` | Immutable, identity-bound verified receipt; it cannot exist as a success without its operation and checkpoint commit |
| `workflow_events` | Append-only workflow journal with its own event schema, workflow sequence, and canonical post-mutation checkpoint/seed snapshot; its ID is not a CAS value |

Each workflow event stores `checkpoint_bytes`, the canonical post-mutation
checkpoint or seed bytes, and its self-excluding digest. Image validation decodes every historical
snapshot and checks its root, workflow/task sequence, state, clock, operation,
receipt, evidence, and adjacent transition against the current aggregate and
the other journal rows. This proves semantic consistency inside the validated
SQLite image. It is not an external signature against an actor that can rewrite
the entire database and every digest coherently; artifact identity and content
binding remain the backup/migration authority.
The stored `event_digest` binds every other event field, including the audit
`workflow_event_id`, and the canonical snapshot under
`WORKFLOW_EVENT_DIGEST_DOMAIN`, including transition actor, request, and
evidence identity.

The provider tables and their provider status constraints remain separate. A
workflow mutation checks the root and expected workflow sequence in one
`BEGIN IMMEDIATE` transaction. Task-policy sequence is an authority-issued
expected/next pair; P0 compares the pair and does not calculate or reissue the
policy sequence. The first prompt accepts only `None -> 1`; a prompt for an
existing task preserves the complete task-policy reference, and every other
effect action has a null next-task sequence. Only an authority transition may
advance an existing task sequence. The checkpoint's task
reference/digest/sequence projection is atomic with the workflow row.
Issue #74 now provides owner-issued refs, approval composition, and a
deterministic fake contract. It does not make policy/verification authority
state atomic with this v3 row. That production join was part of the then-planned
Issue #78 schema-4 ledger; current schema-4 work is split across Issues #80–#83
below.

`begin_operation()` commits the intent, checkpoint marker, and first journal
event together. `commit_effect()` commits the verified receipt, next
checkpoint, operation status, and second journal event together. A policy or
verification transition uses the same checkpoint/event CAS without an
external effect. Duplicate operation/effect identity, stale sequence, foreign
Run/Task/Dispatch/terminal, old attempt, wrong generation, or wrong fence is
rejected before any external effect. `commit_effect()` rechecks the current
recovery epoch and fencing-token floor in its commit transaction; a fence that
advanced after begin is recorded as unknown/recovery, never as a committed
stale effect. Policy and verification transitions preserve assignment,
Delivery, reply/read/release markers, the non-target authority, and an
unchanged task-policy reference exactly. The generic P0 transition also
preserves workflow state. Issue #74 provides the typed owner evidence and
composition contract, but this generic v3 transition still cannot accept a
review/verification state edge. The historical plan assigned the full
task/verification ledger and atomic state transition to Issue #78; current work
is split across the downstream schema-4 children below.

A Delivery produced by `wait` always begins as `PENDING` with no ACK operation.
Only the matching ACK begin transaction may change it to
`ACK_INTENT/<operation_id>`. A finite `WORKER_DONE/SUCCEEDED` observation maps
to `WORKER_DONE`; `WORKER_DONE/FAILED` maps to a committed `FAILED` checkpoint,
not success or unknown. While its assignment and failed Delivery remain, normal
effect actions fail closed. A generic transition may record authority only
while preserving the current state; leaving `FAILED` requires a separate
explicit recovery contract that does not reuse the failed assignment or
Delivery.

For prompt, `receipt.result_digest` must equal the canonical assignment digest.
For wait with a Delivery, it must equal `delivery_content_digest`, computed
from Delivery ID, consumer generation, ordered message IDs, and ordered event
projections under `DELIVERY_DIGEST_DOMAIN`. ACK lifecycle fields and the digest
field itself are excluded so ACK begin preserves the immutable Delivery-content
identity. A timeout has no Delivery and instead requires `result_kind="timeout"`
and the fixed `wait_timeout_digest()` under `WAIT_TIMEOUT_DIGEST_DOMAIN`.

Doctor does not merge provider and workflow operation namespaces. If the same
operation ID exists in both tables, it returns `UNREADABLE` with high
confidence and requires operator review.

An effect followed by response loss, a missing receipt, or an uncertain commit
is never treated as a retryable ordinary conflict. `mark_unknown()` stores
`UNKNOWN_EFFECT`, `RECOVERY_REQUIRED`, the last-operation marker, and its
journal event atomically, and returns an immutable `UnknownCommit`. Repeating
the exact unknown is idempotent; a committed operation cannot be downgraded,
and an absent operation cannot be synthesized. `lookup_operation()` verifies
the operation, receipt, checkpoint marker, and event in one read snapshot.
Intent, unknown, absent, or mismatched evidence returns `RecoveryRequired` and
does not authorize retry, status query, release, acknowledgement, cleanup, or
backend fallback. P0 makes no external-effect exactly-once claim.

### Private durable effect adapter (Issue #73)

The private `workflow_effect_adapter.py` seam consumes only the typed Store
port; it does not access SQLite rows, connections, or locks. It preserves the
public `TeamRuntime`/`BackendPort` three-method surface and CLI/MCP envelopes.
The current public backend and Orca implementation fail before an effect with
`DurabilityUnsupported`, because they lack role-effect metadata, consumer
generation, exact Delivery/read lookup, and provider proof. Current Orca STOP
also lacks the ordered composite-stop proof and pure lookup required by the
private contract. This adapter is not wired into CLI or MCP.
Durable `StartSpec.attach=True` is rejected because its focus stage lacks the
same composite proof.

The common backend capability is effect-key idempotency or pure lookup,
attempt/fence enforcement, and consumer generation. Capability is action
scoped after that common gate: WAIT requires exact Delivery lookup, READ exact
read lookup, and STOP ordered composite-stop proof plus pure lookup. The
adapter executes `load -> authority -> begin -> backend once -> validate
post-effect authority/observation -> Store receipt -> projector -> commit`.
START/PROMPT bind effect-allocated post-effect identities, including
generation. Receipt and observation fields are snapshotted and rechecked;
raw prompt/reply/output is bounded to 1 MiB of UTF-8 and durable state keeps
only digests and opaque references. This prevents raw-body persistence but
does not conceal equality for low-entropy inputs.

The adapter's origin-only `DurableDeliveryLookup` is a committed WAIT origin;
it does not reconstruct later ACK/reply lifecycle. `DurableReadLookup` obtains
read output only through the backend's digest-bound pure lookup. Only a
committed effect is replayed with zero backend execute/projector calls;
WAIT/READ/RELEASE/STOP may make one digest-bound pure backend lookup. `INTENT`,
`UNKNOWN_EFFECT`, response loss, and restart ambiguity remain
`RecoveryRequired`; explicit stable-ID recovery belongs to #32. Deterministic
fake authority/backend/projector tests with the real Store prove adapter
validation and call counts, not provider-side exactly-once or a #31 cross-store
atomic join. WorkflowEngine reducer wiring remains #33. The owner-ref handoff
contract is #74. The historical #78 plan covered the schema-4 durable ledger
and restart join; current schema-4 work is split across Issues #80–#83.

### Historical v3 backup, inspect, restore, and Doctor boundary (Issue #72)

The version-1 backup artifact remains the same two-file database/manifest pair
and keeps its exact field shape, candidate namespace, final identity/content
readback, and inspect fail-closed behavior. In the v3 Store, backup and inspect
also validate the four workflow tables, checkpoint bytes/digest, operation
intent, immutable receipt, and append-only event as one SQLite image. They do
not use a v2 manifest as a migration instruction.

The PR #70 restore ledger and tombstone protocol remains the provider-operation
restore authority. Until a dedicated workflow restore binding is introduced,
restore preflight rejects both of these cases before candidate promotion or
primary replacement:

- the source image contains any non-empty workflow table; or
- the current primary contains any non-empty workflow table.

This is deliberately fail-closed for completed, uncertain, and stale workflow
rows alike. P0 does not silently restore, replay, roll back, or rebind workflow
rows, and it does not copy the provider-only restore protocol into the workflow
namespace. Backup and read-only inspect preserve the evidence; restore remains
blocked until its dedicated binding contract is implemented.

Doctor remains read-only. It reports v2 as `MIGRATION_REQUIRED`; missing,
unknown, future, or malformed v3 workflow schema/checkpoint state as a recovery
or integrity observation; and pending or unknown workflow operations as
operator review. It does not migrate, synthesize a seed/checkpoint, query a
provider, retry an effect, or choose another backend.

### Historical scope and next issues

Issue #72 owns the historical v3 Store schema, strict codecs, typed opaque
values, CAS, workflow journal, and the backup/inspect/Doctor boundary above.
WorkflowEngine reducer wiring is Issue #33. The durable backend/effect adapter
is Issue #73. Issue #74 implements owner refs and approval composition without
changing this schema. Schema-4 work is now split between the [Issue #80
foundation](https://github.com/iamtatsuki05/dotfiles/issues/80), [#81 task and
review transitions](https://github.com/iamtatsuki05/dotfiles/issues/81), [#82
verification transactions and adapter](https://github.com/iamtatsuki05/dotfiles/issues/82),
and [#83 image, backup/restore, and Doctor work](https://github.com/iamtatsuki05/dotfiles/issues/83).
The #74 deterministic fake and this historical v3 Store are not evidence for
the non-empty schema-4 lifecycle.

## Current schema-4 foundation (Issue #80)

[Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80) fixes the
schema-4 physical object set and the pure payload boundary. It is a
foundation-only child: its writer does not make the verification ledger
non-empty or claim the later lifecycle, image, or recovery semantics. The
normal Store now has the narrower [Issue #81 review checkpoint
producer](https://github.com/iamtatsuki05/dotfiles/issues/81) path described
below; that path does not expand #80's verification-ledger or image claims.

### Version and object boundary

The current marker is `STORE_SCHEMA=4` in `PRAGMA user_version`,
`store_meta`, and the exact schema validator. Provider events remain
`EVENT_SCHEMA_VERSION=2`; workflow events remain
`WORKFLOW_EVENT_SCHEMA_VERSION=1`. The version-1 backup manifest keeps its
exact ten fields and records `store_schema=4`, `event_schema_version=2`, and
`sqlite_user_version=4` (`4/2/4`).

Schema 4 consists of the existing nine tables plus exactly these three tables:
`task_policy_states`, `verification_operations`, and `verification_receipts`.
The total is twelve tables. There is no `verification_events` table. The
composite keys, `RESTRICT` foreign keys, deferred pointers, triggers, and
status/null matrix are part of the physical contract, but #80's production
path writes no row to the three new tables.

### Read-only pre-gate and empty-ledger boundary

An established image is classified before the root-mutating initialization
path. The classifier uses bounded, identity-checked copies of the database
and its exact sidecars. Structural WAL is copied as part of the image, while
the ephemeral SHM cache is reconstructed by SQLite only in a private
temporary copy. The established source, lifetime gate, marker, root fileset,
and DB/WAL/SHM bytes remain unchanged; the classifier does not checkpoint,
truncate, delete, or create source sidecars.

Before any normal writable open of the established source, the classifier
rejects a main/WAL page-size mismatch, an unsupported WAL format, and a current
schema-4 main header that is not in WAL mode. A non-empty WAL is checked before
the private copy is opened. With no WAL, the private copy is classified first
so legacy and malformed-image diagnostics retain their meaning; a current
rollback-mode image is then rejected before the source is opened. SQLite's
standard headers do not authenticate the origin of a same-page-size WAL, so
WAL salt and checksum fields are not treated as database identity. The later
image-evidence work in #83 owns the stronger source/pair provenance boundary.

An exact schema-4 image whose three new tables are empty is the structural
open baseline for the #80 foundation writer. If a foundation-only path finds
any new-table row, it fails closed because its non-empty semantic validator is
not provided. The normal Store's #81 path is a narrow exception for a full
`task_policy_states` row and its closed three-event review suffix; it does not
admit rows in `verification_operations` or `verification_receipts`. It does
not report successful non-empty verification open, inspection, backup,
restore, or three-layer image evidence. The historical nine-table
provider/workflow contract remains separate and is not silently reinterpreted
as verification evidence.

After the exact object, integrity, and foreign-key checks, the schema-4
empty-ledger gate runs before workflow transition and high-water semantics.
The workflow validator reads cursor frames instead of materializing all four
workflow tables: it retains one checkpoint or operation, at most one receipt,
at most two operation events, and the previous/last root event state. This
bounds its Python working set without introducing an arbitrary row limit.
The existing `StoreImageObservation.operations` tuple remains an intentional
provider-observation output contract; #80 does not claim a process-wide bound
for that caller-requested result or for SQLite's internal temporary workspace.

An exact schema-2 or exact schema-3 image is reported as
`StoreMigrationRequiredError` with its source schema and target `4`.
Malformed, mixed, missing, extra, and future images remain distinct
schema/integrity errors. #80 does not migrate an image or introduce an
implicit v3 intermediate, default, alias, or backend fallback.

### Pure task and verification codecs

The four payload codecs are version `1`: the 15-field `TaskPolicyStateV4`, an
approval-binding snapshot, a body-free verification request, and a full
normalized receipt. They use fixed field order, canonical compact UTF-8 JSON,
one trailing LF, explicit null, strict integer parsing, duplicate/missing/
unknown/future rejection, decode/re-encode equality, domain-separated
digests, bounded BLOBs, and byte-exact Unicode. Request data keeps argv
digests and environment names, not argv or environment values; raw bodies and
secrets are not stored.

The codecs validate internal value consistency only. They do not capture live
owner authority, resolve a context, issue a Store adapter, hydrate a Gate
value, or implement the 58-field operation-row digest. SQL
`record_version=1` is only a row discriminator. The #81 producer owns the
normal-Store task/review transactions and full task-row projection described
below. Live capture/context, Store adapter, snapshot hydration, the
58-field operation-row digest, verification lifecycle transactions, semantic
image validation, verification-aware Doctor, and non-empty backup/restore
belong to [#82](https://github.com/iamtatsuki05/dotfiles/issues/82) and
[#83](https://github.com/iamtatsuki05/dotfiles/issues/83).

For #80, backup and restore evidence is limited to the version-1
two-file-manifest round trip for an exact schema-4 image with an empty new
ledger. The #81 task-row image is accepted only by the normal Store validator;
non-empty task-row images are not inspected, backed up, restored, or treated
as successful by #80's image paths.

## Schema-4 review checkpoint producer (Issue #81)

`agent_team._review_workflow_store.ReviewCheckpointProducer` is the
package-private producer for the normal schema-4 Store. It consumes an actual
#49 `ReviewPolicyUpdate`, the actual `SerialReviewPolicy`, and the matching
#74 owner-issued `ReviewAuthorityRef`. The #74 process-local binding seam
revalidates the issuer, reference, policy binding, and complete nested causal
graph before a Store transaction is planned. The producer does not accept a
raw projection, `CompletionAdmissionRef`, `ApprovalRef`, route or reservation
result, caller-built checkpoint/event, or caller-supplied digest. It does not
generate or reconstruct a policy update. It revalidates the supplied update
through the existing pure reducer contract, without rerunning route,
reservation, or owner-ref issuance.

The producer and its opaque commit request remain bound to the exact handoff,
that handoff's owner registry Store, and the registered `CoordinationStore` at
every lower Store call. Registration
fixes the base commit, read, and event-projection functions, the `StoreError`
type, the state-root path/device/inode, and the checkpoint issuer. Producer
reinitialization, post-issuance mutation, an unregistered port, or a foreign
commit/read result fails closed. Commit and read results are correlated again
with the request/current root and complete policy-event prefix. Only a genuine
Store error retains its cleanup capability; arbitrary port exception text is
mapped to a bounded producer error.

### Three independent policy transactions

The producer accepts exactly the following #49 edges, in this order. The
workflow state and task-policy phase intentionally differ on the final edge:

| Actual event | Task-policy edge | Workflow edge | Durable result |
| --- | --- | --- | --- |
| `WorkerCompletion(kind=SUCCEEDED)` | `ASSIGNED(T) -> WORKER_DONE(T+1)` | `WORKER_DONE(W) -> WORKER_DONE(W+1)` | Materialize the exact assigned task preimage once, then write the next full task row and one policy event. |
| `ReviewRequest` | `WORKER_DONE(T) -> REVIEW_PENDING(T+1)` | `WORKER_DONE(W) -> REVIEW_PENDING(W+1)` | Write the next full task row and checkpoint, then return one `ReviewerAssignment` intent. |
| `ReviewDecision(kind=APPROVED)` | `REVIEW_PENDING(T) -> APPROVED(T+1)` | `REVIEW_PENDING(W) -> REVIEW_PENDING(W+1)` | Write the next full task row and one state-preserving policy event. No effect is executed. |

Each edge is a separate short `BEGIN IMMEDIATE` transaction. It updates the
full `task_policy_states` projection, current workflow checkpoint, and one
`workflow_events` row atomically. The task row and checkpoint reference are
compared in both directions. Task and workflow sequences advance by one.
Review events use `kind='policy_transition'`, actor
`review-policy-producer-v1`, `operation_id=NULL`, and `receipt_id=NULL`.
The Store-owned authority digest is copied to both `review_authority` and
`evidence_ref`; the authority and request digests use separate domains. Their
timestamp and global workflow event ID are excluded from those digests; the
event's own digest remains Store-defined.

The first transaction requires no existing task row and an exact
`ASSIGNED(T)` reference in the current checkpoint. It materializes that
preimage before advancing to `WORKER_DONE`, so an injected fault rolls back
the preimage, checkpoint, and event together. The next two transactions
require the existing full row and exact sequence, bytes, digest, identity, and
checkpoint CAS preconditions. Current-only commits, sequence jumps, synthetic
events, stale or foreign authority, nested-value mutation, unresolved
operations, and non-empty verification rows fail without a partial write.

### Normal reopen and authority boundary

Normal schema-4 open and `load_review_checkpoint()` validate only the closed
producer suffix: at most three ordered policy events, the full task-row
projection, matching checkpoint snapshots, and the complete workflow prefix.
The read observation includes the canonical checkpoint bytes immediately before
the first policy event, allowing the producer to recompute the first request
digest as well as every later request digest. Those predecessor bytes are
Store-read evidence, not another caller input.
After the three commits, a fresh reopen returns a workflow
`REVIEW_PENDING` checkpoint with `W0 >= 2` and a task row at policy phase
`APPROVED`. `verification_operations` and `verification_receipts` remain
empty. The returned `ReviewerAssignment` is a post-commit intent only; no
backend, runner, reviewer process, reservation, or external effect is
called.

The generic `commit_transition()` remains state-preserving for the historical
row-empty workflow boundary. Once a schema-4 root has a task-ledger row, only
the dedicated task/review or verification writer may advance it; the generic
transition and public lifecycle `begin_operation()` reject that root instead
of desynchronizing the task row and checkpoint. The schema-3 validator remains
unchanged. Backup, inspect,
restore, and Doctor use the foundation's
fail-closed image path and do not accept the #81 task-row image until #83
provides its non-empty image-evidence contract. A pre-issued #50
`CompletionAdmissionRef` is retained only by the trusted composition root;
#81 neither receives nor persists it. #81 also does not compose or resolve a
#74 `ApprovalRef` or create verification authority. Those operations belong to
[#82](https://github.com/iamtatsuki05/dotfiles/issues/82).

## Additional environment validation

- Disk-full, I/O error, and long-running concurrent backup cases.
- Real Orca/Herdr effect lookup and idempotency behavior.
- Power-loss or filesystem-fault testing beyond process `SIGKILL`.
- Migration interruption, backup restore, and rollback with the selected production schema.

These are acceptance conditions for the LocalBackend implementation. They do not change the store selection.

## Evidence

The throwaway code and detailed measurements are stored in the execution session, not in the runtime package:

- SQLite harness SHA-256: `6dc978a709f3e7511956bb4206701495beb0371e9ef7c9933a811a5ead3ca9e5`
- SQLite tests SHA-256: `66159d6e70f3d3766196daee4909a4dcd5e1a254169cb9757de2a52bd0cc5c75`
- Atomic-file harness SHA-256: `3798bdeef42629555b08aca4a0ef222efad476f257ffd15d4eae5099785ec490`

The prototypes are disposable evidence. Production code must be implemented from this contract and covered by its own tests.

## Read-only doctor substrate

`agent_team.doctor` provides the read-only `ReadOnlyDoctor`, `StateFilesystem`,
and `RecoveryLedgerReader` seams for recovery work. Callers must pass the
stable writer-marker and recovery-ledger basenames explicitly because their
names are owned by the marker and ledger implementations. The doctor does
not invent a filename, construct `CoordinationStore`, or execute recovery.

The filesystem reader opens only an existing owner-only directory/file through
`O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK`, holds the existing lifetime gate
and stable writer marker with shared locks when present, and inventories every root-direct name. It
retains type, owner, mode,
link count, device/inode, size, timestamps, and SHA-256 for safe regular files.
It rejects unsafe root entries and identity changes, and compares the complete
fileset again before returning a report. Missing roots and gates are observed;
they are never created. A non-zero SQLite WAL, pending restore ledger, active
writer marker, schema mismatch, or identity race remains fail-closed. Reports
contain only finite states/actions/mutations and validated opaque owner
identities; they do not expose paths, SQLite rows, or provider payloads.

For an existing primary database, the already validated read-only descriptor
remains open while bounded bytes are deserialized into an in-memory SQLite
database. SQLite never reopens the state pathname, so a path swap cannot make
it open a FIFO or replacement database; the final fileset check returns an
unreadable observation. If deserialization or the in-memory validator fails,
the doctor stops without a pathname fallback.

The recovery ledger is strict append-only JSONL: every line is one complete
object terminated by the canonical newline, with no arrays, padded/blank
lines, partial final records, or terminal-first generation. Each generation
must progress through `RESTORE_PREPARED`, `RESTORE_REPLACED`, and then
`RESTORE_COMMITTED` or the contractually allowed `RESTORE_ABORTED` edge.
Generation numbering starts at one and advances by exactly one; only
`RESTORE_PREPARED` may follow a terminal generation, and `RESTORE_ABORTED`
may follow only `RESTORE_PREPARED`. This applies both to a standalone first
generation and to every generation appended after a terminal prefix. The
primary database, exact SQLite
sidecars, marker, and ledger basenames are regular files only; unrelated
owner-only directories remain inventory entries but are never opened.

## Explicit recovery coordinator

`agent_team.recovery.RecoveryCoordinator` is the mutation boundary for the
recovery operations owned by this child issue. Its normal `recover` operation
accepts only an exact `CLAIMED` or `FENCE_PENDING` identity at
`now_ns >= lease_expires_ns`; the private typed store transaction performs the
compare-and-swap (CAS) state/event transition. It never retries, closes, or
executes a provider effect. A concurrent caller loses the CAS and receives a
conflict. An expired plain `FENCE_PENDING` has no provider marker, so recovery
uses the typed store reclaim path to allocate the next unused attempt while
retaining the old attempt and event history. A proof-bearing `CLAIMED` remains
`UNKNOWN_EFFECT` because its external outcome is ambiguous.

`force_recover` requires an exact operator identity, one of the finite
`FORCE_REASON_CODES` as an exact built-in string, and a `RecoveryAuthorizer`
result whose operation, operator, reason, and audit reference are exact
validated strings and all match. Equality-overriding values, booleans,
missing/default authorization, or self-asserted values are rejected. The store-issued
`RecoveryFloorReservation` advances the epoch/token floor; the coordinator
does not calculate tokens. Force recovery preserves uncertainty for claims
and prepared effects, while known `INTENT`, `RECEIPTED`, and `COMPLETED`
states use the typed rebase authority. `CLEANED` remains immutable.

`resolve_unknown` performs at most a provider `status` query. The adapter must
expose the complete trusted `ProviderPort` shape and an exact
`ProviderCapabilities` value; a status-only object is rejected. The returned
status must have the exact `ProviderStatus` runtime type and each field is
validated before comparison. It accepts only a strongly consistent status
whose operation, effect, provider, owner,
attempt, epoch, fencing token, and fence proof exactly match the current
identity. Only `ABSENT` returns the operation to `INTENT`; a matching
`COMPLETED` status stores a verified receipt as `RECEIPTED`. Weak, old,
timeout, WAL-pending, or mismatched observations remain blocked and never
invoke `execute`.

Before that query, the coordinator reads the current global recovery epoch
through the store's read-only typed seam. An already-stale operation therefore
makes zero provider calls; the transaction CAS still rejects a global-epoch
race after a query.

`RecoveryLayout` is an exact frozen/slotted value. It fixes the marker identity
and the canonical `recovery.ledger` basename, and every public coordinator
entry point revalidates it before inspecting or mutating state. The layout has
no public setter that can hide a pending restore ledger.

The writer in `agent_team.recovery` owns the fixed basename `recovery.ledger`
and emits `RECOVERY_LEDGER_VERSION=1` records compatible with
`RecoveryLedgerReader`. First-ever creation requires the separate typed
`RecoveryLedgerInitialization` authority; normal `append()` never creates a
missing ledger. It keeps one validated root descriptor through create/append/
readback, opens every ledger read/write with `O_NONBLOCK` plus the required
no-follow/close-on-exec/append flags, and rejects FIFO or symlink swaps without
blocking. It appends with `O_APPEND|O_NOFOLLOW`, fsyncs the ledger and containing
directory, and rejects malformed, duplicate, partial, version-incompatible,
non-monotonic, or missing ledgers. It reconstructs and canonical-encodes exact
record fields and verifies the complete post-write bytes. It implements no
backup, restore, checkpoint, sidecar cleanup, or writer-marker lifecycle;
`agent_team.backup`, `agent_team.restore`, and `agent_team.wal` own those
separate responsibilities.

The unowned mutation API participates in the same startup/quiescence protocol
as normal Store open. An initialized root uses the WAL controller's exclusive
lifetime-gate and marker span; a bootstrap or explicitly missing-gate root
holds the Store-compatible root startup lock through recheck, append, fsync,
and readback. Owner-aware restore append reuses its existing quiescence owner
and never reacquires those locks. Read-only access remains shared. A writer,
Store, Doctor filesystem, WAL controller, backup, or restore object retains
close-uncertain descriptors, resources, or sessions with their observed
identity and requires cleanup retry before new I/O. It never closes a reused
or identity-unknown descriptor by number alone.

Commit response loss, rollback failure, temporary SQLite-connection cleanup
failure, and a full bounded descriptor registry are all cleanup uncertainty,
not success. The body error remains primary. An opaque best-effort composite
retry capability carries every cleanup owner, attempts every member, removes
members that succeed, and retains only failures for an idempotent retry. A
registry overflow never drops the current resource. Cleanup retry does not
re-run SQL, a phase append, a primary replacement, or a provider operation.

The provider adapter is a trusted composition-root dependency. A malicious
full-shape in-process adapter is outside this Python value boundary; task data
and ordinary callers do not select or inject provider adapters.

## Stable writer marker and WAL sidecar controller

`agent_team.wal.WalSidecarController` owns the exact marker basename
`writer.marker` and the SQLite sidecars `coordination.sqlite3-wal`,
`coordination.sqlite3-shm`, and `coordination.sqlite3-journal`. Only a truly
empty root (no database, marker, sidecar, ledger, unknown entry, FIFO, or
symlink) may bootstrap. A valid
`CoordinationStore` creates `writer.marker` once with
`O_CREAT|O_EXCL|O_NOFOLLOW`, owner-only mode `0600`, and a containing-directory
`fsync`. Store and read-only filesystem users hold a shared marker lock from
open until close. The marker is never unlinked or recreated, so its path and
device/inode remain stable across store and cleanup cycles. Invalid schema and
read-only doctor paths do not create it. A missing database/marker pair, a
zero-byte or truncated database, a database-without-marker, or a
marker-without-database fails before SQLite or schema initialization can
mutate state. A non-zero database with an empty schema is rejected immediately
after read-only SQLite schema inspection and before initialization.

The marker contains one canonical version-1 record: `CLEAN` during normal
operation or `CLEANUP_PREPARED` while sidecars are being removed. A prepared or
malformed marker makes normal store open fail closed and makes doctor require
operator review. A missing marker in an already initialized database also
fails closed; only the first empty-store initialization may create it.

The mutation lock order is lifetime gate first, then writer marker. Both
exclusive guards use `LOCK_NB` with a finite deadline; failure is a typed busy
result/error and never a fallback. After both guards are held, the controller
keeps validated root, gate, marker, and database descriptors and rechecks their
type, owner, mode, link count, device, and inode before any effect.

Backup/restore consumers can call `hold_quiescence()` to receive an opaque
`QuiescenceSession`. Its typed `checkpoint`, `cleanup`, `assert_identity`, and
`copy_database_to` methods retain the same exclusive guards across multiple
phases, so `BackupRestore` does not duplicate marker/lifetime locking.
`copy_database_to` requires its exact checkpoint request, performs one
controller-owned SQLite backup call into memory, and writes the serialized image
to a newly created `O_CREAT|O_EXCL|O_NOFOLLOW` mode-`0600` target held by file
descriptor. It validates source/target identities, exact target sidecars, and
the final target bytes, and returns the target `sha256:` digest together with
the identities. `SQLiteBackup` must no-follow open the basename and revalidate
its identity, size, and digest immediately before consumption; the
result is not a publication or restore authority. The controller never
overwrites an existing known or unknown file. The package-private database
rebind refreshes the held descriptor after an authorized primary replacement;
backup, restore, and replacement policy remain outside this module.

Checkpoint callers must pass one `CheckpointRequest` whose mode is exactly
`PASSIVE`, `FULL`, `RESTART`, or `TRUNCATE`. The returned
`CheckpointResult` preserves SQLite's `(busy, log, checkpointed)` values.
When the held database was opened from a snapshot with no pre-existing WAL or
rollback journal, a non-safe checkpoint tuple cannot be inode-bound by the
standard-library pathname connection and is therefore recovery-required, even
if serialized bytes happen to match. A safe tuple still requires the
serialize-to-held-bytes binding check. An explicit checkpoint of a canonical
pre-existing WAL may return SQLite's exact busy tuple after sidecar identity
validation; cleanup and source-copy reject that WAL before opening SQLite.
Cleanup performs no Python `unlink`. A non-empty rollback journal or any
pre-existing non-zero WAL is blocked before SQLite opens; these preflight
results have no checkpoint tuple and the WAL is not parsed or consumed. An
established `CoordinationStore` applies the same pending-journal check before
its SQLite connection is opened. Otherwise, `busy != 0`,
`log != checkpointed`, or an active reader/writer returns a typed blocked
result. The controller then durably writes `CLEANUP_PREPARED` and lets
SQLite own the exact transition `journal_mode=DELETE`,
`locking_mode=EXCLUSIVE`, `journal_mode=WAL`. A journal or non-zero WAL
appearing after `CLEANUP_PREPARED` is a recovery-required failure and leaves
the marker prepared; only an actual busy DELETE transition (including SQLite
returning the original `wal` mode) restores `CLEAN` and returns blocked. Any
later transition, sidecar, identity, or durability uncertainty leaves the
marker prepared for operator review. Exact
sidecar absence and root-directory `fsync` are checked while the exclusive
SQLite connection is held, then the marker is durably returned to `CLEAN` and
the connection is closed. A sidecar created after that close is new activity
and is not consumed by the completed cleanup. Exact WAL/SHM structure and
rollback-journal state are rechecked immediately before the SQLite transition;
an arbitrary filesystem writer after that final check is outside this
SQLite-lock protocol and is reported conservatively when observed, rather than
claimed impossible. Unknown provider, terminal, prompt, and other files are
never globbed or removed.

## Version-1 backup artifact

`agent_team.backup.SQLiteBackup` creates and inspects a backup in the existing
owner-only state root. Version 1 uses exactly two final files: a caller-chosen
database basename and its derived `<name>.manifest`. Nested paths, external
archive directories, retention, rotation, and generation pointers are not
part of this version.

`create()` holds one quiescence session while
`copy_database_to(CheckpointRequest("TRUNCATE"), ...)` checkpoints and copies
the primary. It then closes that session and validates the copied image through
the Store-owned image reader. The manifest is canonical UTF-8 JSON followed by
one LF and has ten fixed fields: its version and database basename, the Store
and event schema versions, SQLite `user_version`, integrity result, database
size and SHA-256 digest, and the captured recovery epoch and fencing-token
floor. On the then-current Issue #72 head, the three version values are
`store_schema=3`, `event_schema_version=2`, and `sqlite_user_version=3`.
The historical PR #70 v2 image used the corresponding values `2`, `2`, and
`2`; that old pair is not a v3 migration input. Duplicate, missing, unknown,
non-canonical, or incorrectly typed fields are rejected. The manifest never
supplies a recovery-floor mutation.

The destination name must be one exact basename. Path components, reserved
names, the restore-candidate namespace `.coordination.sqlite3.restore-`, and
wildcard characters (`*`, `?`, `[`, and `]`) fail fast before retained
resources are retried or opened. Any name beginning with that candidate prefix
belongs to restore and cannot be a backup destination. `create()` returns its own
`BackupArtifact` only after a final pair inspection rechecks the published
database and manifest identities, canonical manifest content, database
size/digest, and captured floor against the values planned for that call. A
visible pathname or an earlier inspection is not sufficient; a final manifest
content or identity mismatch is an error.

The database and manifest are separate files, so POSIX does not provide one
operation that makes the pair visible atomically. Publication replaces the
database, then the manifest, and finally fsyncs the containing directory. A
crash between those replacements may leave a missing or old/new mixed pair.
`inspect()` accepts only a complete pair whose basename, identities, size,
digest, schema, integrity, and captured floor agree. It does not fall back to
the old half, roll back the new half, or promote an orphan temp file. Unsafe
types, links, owner/mode mismatches, artifact sidecars, and close uncertainty
fail closed. `create()` also refuses success when its publication or directory
fsync is uncertain. A later read-only `inspect()` validates the current bytes
and identities; it cannot prove how an earlier directory fsync completed.
Likewise, a non-cooperating same-UID process can swap a pathname after the
last explicit precondition check and before `os.replace`; that final syscall
window is outside the version-1 guarantee. An observed mismatch is preserved
and never reported as a successful artifact.

## Candidate-first restore and durable fencing

`agent_team.restore.BackupRestore` exposes `restore()` and `resume()` as the
only high-level restore operations. A caller supplies a freshly inspected
`BackupArtifact`, an opaque actor identifier, and an audit reference. The
result contains the terminal phase, restore generation, source and candidate
digests, Store-issued final `RecoveryFloor`, and the destination-only
operation/effect identities preserved as tombstones. It contains no path,
descriptor, SQLite row, provider payload, or per-operation token.

Restore uses one lifetime-gate-exclusive, writer-marker-exclusive
`QuiescenceSession` and one session-issued owner through final readback. Before
candidate creation it checks the source against every previously committed
tombstone, so an old backup cannot resurrect a retired operation ID or effect
key. The Store authority reads the source and current primary, issues an
epoch/token floor strictly above the source, destination, ledger, and attempt
high-water marks, and applies the status policy in one in-memory
`BEGIN IMMEDIATE` transaction. The fully serialized, fsynced, and verified
candidate is the only image eligible for descriptor-relative primary
replacement. The supplied restore timestamp must be at least the source and
destination durable clock high-water marks; an older value fails with
`ClockRollbackError` rather than being clamped.

The ten operation statuses preserve external truth:

`INTENT`, `FENCE_PENDING`, `FENCE_RESERVATION_STARTED`, `CLAIMED`,
`EFFECT_PREPARED`, `UNKNOWN_EFFECT`, `UNKNOWN`, `RECEIPTED`, `COMPLETED`,
and `CLEANED` are the operation-state policy. `RESTORE_INCOMPLETE` is a
separate incomplete-restore sentinel; it is rejected as a restore source and
is outside this ten-status policy.

- `INTENT` stays `INTENT` with attempt zero under the new epoch.
- `FENCE_PENDING`, `FENCE_RESERVATION_STARTED`, `CLAIMED`, `EFFECT_PREPARED`,
  `UNKNOWN_EFFECT`, and `UNKNOWN` retain their status and evidence, but their
  old lease authority becomes stale. Restore never queries, executes, retries,
  or reclaims the provider effect.
- `RECEIPTED` keeps the effect/proof/owner/attempt identity while Store-issued
  exact expected epoch/token values update the attempt and receipt atomically.
  Candidate and resume verification reject any token other than the planned
  exact token.
- `COMPLETED` keeps its terminal receipt and gains only the restore epoch/event.
- `CLEANED` permits the global `RecoveryFloor` and store-wide durable clock to
  advance, but its row, attempt, receipt, events, and `updated_ns` remain
  immutable.
- A source containing `RESTORE_INCOMPLETE` is rejected for operator review.

Restore performs no provider execution or status query, automatic retry,
backend fallback, or terminal-resource close. The PR #70 restore contract was
defined against its historical v2 image and did not change the DDL,
`STORE_SCHEMA`, `EVENT_SCHEMA_VERSION`, or SQLite `user_version`. On the
then-current Issue #72 head, restore preflight validates the v3 image (`STORE_SCHEMA=3`, provider
`EVENT_SCHEMA_VERSION=2`, workflow event schema `1`, and SQLite
`user_version=3`) and still does not perform a v2-to-v3 migration. A source or
current primary containing workflow rows is rejected until the dedicated
workflow restore binding described above exists.

The existing nine-field `recovery.ledger` version 1 remains unchanged. A
separate strict append-only `recovery.tombstones` version 1 records each
generation's source/old-primary/candidate digests, destination-only operation
and effect keys, and the old primary's epoch, fencing-token, and clock
high-water marks. Normal Store open validates both histories before it creates
or opens the lifetime gate, marker, database, or SQLite connection, then checks
them again while holding the shared lifetime gate. Pending, partial,
malformed, cross-generation, phase, digest, or high-water inconsistencies stop
normal open. Committed tombstones also reject later `create_intent` collisions.
`ReadOnlyDoctor` runs the same ledger/tombstone pair preflight and reports a
pending or malformed restore pair as `RESTORE_INCOMPLETE` with operator review,
without changing either log. A malformed bare `recovery.ledger` with no
tombstone history retains the older `UNREADABLE` classification and still
requires operator review.

When the marker is clean, all six uncertain operation statuses—
`FENCE_PENDING`, `FENCE_RESERVATION_STARTED`, `CLAIMED`, `EFFECT_PREPARED`,
`UNKNOWN_EFFECT`, and `UNKNOWN`—are reported as `UNKNOWN_EFFECT` with
`safe_action=OPERATOR_REVIEW` and low confidence. This action is an operator
advisory, not permission to call the coordinator. The public observation does
not establish provider proof, expiry, or a current recovery epoch, and the
coordinator accepts only narrower exact preconditions. Doctor therefore makes
no provider call for these statuses. Marker, pair, or identity hazards may
produce an even more conservative review state.

For a restored operation whose recovery epoch intentionally differs from its
retained lease epoch, Doctor uses only the fully validated canonical
ledger/tombstone history as authorization. The committed recovery epoch must
match the current Store image, the current token high-water must be at least
the committed floor, and the Store-owned whole-image high-water checks must
pass. A caller-selected diagnostic ledger cannot authorize this exception;
an aborted-only history or row/token corruption remains `UNREADABLE`.

### Stable restore-history binding

For a committed generation, the Store derives a stable
`restore-history-binding` digest from the latest committed log fields:
generation, actor, the audit-reference digest, source and previous-primary
digests, previous recovery epoch/token/clock high-water marks, and the final
epoch/token floor. It also includes the current generation's tombstone batch
and the cumulative union of identities from all committed tombstones. The
candidate's restore events carry this binding, and the normal-open verifier
uses a matching primary restore event as the history anchor.

`candidate_digest` has a separate, exact-evidence role. During the current
generation's candidate apply, primary replacement, final result, and resume,
it binds the expected candidate bytes and, after replacement, the full primary
image. Those bytes are expected to remain unchanged between the relevant
checks, so these paths retain their exact digest and identity comparisons.
The stable normal-open binding deliberately excludes `candidate_digest`:
including it would self-reference the event stored in the primary and would
make later legitimate writes that change the primary invalidate the history
anchor. Under version 1's no-wire contract, a consistent rewrite of only
`candidate_digest` in already committed history is therefore not authenticated
or necessarily detected by normal open or a new restore. It does not change
the cumulative tombstone identity fence. A signature, attestation, or
versioned durable anchor is required to cover that case.

The constructor performs the recovery pair preflight before normal state is
opened and verifies the binding against the opened image before later PRAGMA
or schema initialization. `create_intent()` repeats the pair and binding checks
under the shared lifetime gate. A new restore generation verifies the current
committed binding before it creates a candidate or appends recovery records.
This rejects post-open history tampering and prevents an older backup from
re-anchoring a forged history. The mutable operation/status/token plan is not
part of this stable binding; candidate and resume paths retain their separate
exact operation, evidence, receipt, and expected-token checks.

If the latest generation is `RESTORE_ABORTED`/`ABORTED`, normal-open state uses
the immediately preceding committed generation's handle and cumulative union,
when one exists; otherwise the state is empty. The aborted generation's
identities are not active. If both the current batch
and cumulative union are empty, there is no collision anchor to verify. A
zero-event restore is then subject to the guarantee boundary below, while a
zero-event restore with a nonempty current or cumulative tombstone union fails
before candidate transaction commit, ledger preparation, or
`RESTORE_COMMITTED`.

Ledger and tombstone records are separate append-only files and are not one
atomic publication. These are the only allowed same-generation pairs,
including the two response-loss states:

| Recovery ledger | Tombstone log | Meaning |
|---|---|---|
| `RESTORE_PREPARED` | `PREPARED` | Normal prepared state |
| `RESTORE_PREPARED` | `ABORTED` | Abort record persisted before ledger response |
| `RESTORE_REPLACED` | `PREPARED` | Normal replaced state |
| `RESTORE_REPLACED` | `COMMITTED` | Tombstone commit persisted before ledger response |
| `RESTORE_COMMITTED` | `COMMITTED` | Terminal committed state |
| `RESTORE_ABORTED` | `ABORTED` | Terminal aborted state |

Every other cross-product pair, missing half, invalid history prefix, or
high-water inconsistency is recovery-required.

### Pair-level resume durability barrier

`RestoreLedger.read_for_resume()` is the first consumer of recovery state. It
uses the already held quiescence owner, opens existing log files only, and
acquires their non-blocking locks in the fixed `tombstone` then `ledger`
order. It strictly parses and classifies both byte streams before any fsync or
mutation. Missing, partial, malformed, unsafe, mixed, or generation-skewed
state therefore causes a fail-closed review with the logs, primary, and
candidate untouched.

For a valid pair, the barrier fsyncs each existing log and then the state-root
directory. It reads the exact bytes back from the same locked descriptors and
rechecks file and root identities and the pair classification. Visible bytes,
a successful JSON parse, or a previous process's result is not proof of
durability. Any file/root fsync, readback, identity, unlock, or close
uncertainty yields no durability proof and no phase or success result. The
barrier does not create an absent log, trim a partial record, or silently
repair either file. Only after the barrier succeeds may `resume()` append the
single missing record for a permitted tombstone-first state; it never reapplies
the candidate transaction or primary replacement as part of that proof.

The durable phases have exact meanings:

- `RESTORE_PREPARED`: the candidate transaction, image verification, and
  tombstone evidence are durable; the primary is still old unless replacement
  durability is explicitly known.
- `RESTORE_REPLACED`: descriptor-relative replacement, directory fsync,
  descriptor rebind, and primary image verification completed.
- `RESTORE_COMMITTED`: tombstone and ledger commit records are durable. The
  high-level operation returns success only after a subsequent final artifact,
  primary, fileset, sidecar, and owner readback. A terminal record can remain
  when that final readback fails; `resume()` repeats the verification rather
  than assuming that a record alone proves success.
- `RESTORE_ABORTED`: an explicit operator proved that replacement did not
  occur. The high-level restore path never aborts automatically.

`resume()` never recreates or reapplies a missing candidate. Because prepare
publishes the tombstone before the ledger record, it accepts exactly two
tombstone-first response-loss states: the first generation with no ledger, or
the next generation after a complete terminal prefix. It verifies the source,
old primary, candidate, prior epoch/token/clock high-water, committed identity
set, and candidate floor before appending only the missing prepared ledger
record. Every other one-sided or generation-skew state remains operator review.
For `RESTORE_PREPARED` plus an `ABORTED` tombstone, resume verifies the old
primary before appending only the missing abort ledger record and never applies
or replaces the candidate. A prepared old primary plus the exact candidate can
be verified and replaced. A prepared new
primary with no candidate is deliberately operator review because a crash
between rename and directory fsync cannot be distinguished afterward. A
replaced generation performs only final verification and commit; a committed
generation is a verified no-op. Mixed, missing, ambiguous, or mismatched state
is never rolled back, promoted, or silently repaired.

## Backup and restore guarantee boundary

The tested guarantee covers Python's standard `sqlite3`, the default local
POSIX VFS, an owner-only local state root, cooperating `CoordinationStore`
clients, deterministic fault barriers, and observable pathname/inode races.
There is no portable POSIX compare-and-unlink operation, so a non-cooperating
same-UID process that swaps a pathname after the final explicit identity check
is outside the contract. Unknown and already mismatched identities are
preserved, and observed uncertainty is reported conservatively. The contract
does not detect a coherent same-UID rewrite of the primary, its
`transition_events`, and both recovery logs. A zero-event restore with an
empty active tombstone union has no durable provenance anchor. The binding is
a SHA-256 integrity reference, not a cryptographic signature or attestation.
The final non-cooperating syscall race remains outside the guarantee even when
the path swap is observable in some runs. Custom or `nolock` VFSes,
network/distributed filesystems, power-loss guarantees beyond the executed
fsync contract, provider execution/status, automatic retry, backend fallback,
and schema migration are also outside version 1.

The current-generation `candidate_digest` checks above are still required for
apply, replacement, final-result, and resume evidence. They do not extend the
stable normal-open guarantee: a candidate-digest-only rewrite of a committed
history is outside version-1 authentication and does not alter the cumulative
tombstone fence.

The historical PR #70 backup/restore work did not change the DDL,
`STORE_SCHEMA`, `EVENT_SCHEMA_VERSION`, or SQLite `user_version`: it targeted
the then-current v2 provider image. Issue #72 is the explicit schema boundary
after that history. Its then-current head changes `STORE_SCHEMA` and SQLite
`user_version` to `3`, keeps provider `EVENT_SCHEMA_VERSION=2`, and introduces
`WORKFLOW_EVENT_SCHEMA_VERSION=1` for the four workflow tables. The
`BACKUP_MANIFEST_VERSION=1` field shape, the nine-field `recovery.ledger` v1,
and the thirteen-field `recovery.tombstones` v1 remain wire-compatible; new
manifests carry values `3/2/3`. The stable restore-history binding remains in
the existing restore-event `evidence_ref` field. No v2 artifact is silently
migrated or restored.
