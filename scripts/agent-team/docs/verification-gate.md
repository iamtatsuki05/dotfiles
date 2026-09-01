# Durable fixed-argv verification gate

[日本語](verification-gate_JA.md)

`agent_team.verification_gate` is the pure completion boundary for normal and
admitted express write tasks. Its only public operation flow is:

```python
gate = VerificationGate(admission, profiles, snapshots, runner, state)
handle = gate.start(approval_ref)
terminal = gate.resume(handle)
```

The caller supplies only an opaque approval reference and the returned opaque
handle. It cannot supply a review update, routing object, request, runner
result, receipt, evidence, or state. The gate has no subprocess, shell,
filesystem, registry, database, receipt file, lease, or persistence code.
The owner-issued composition that supplies its approval is documented in
[Policy/verification handoff](policy-verification-handoff.md).

## Authority admission

`ApprovalAdmissionPort` is a trusted composition-root adapter from an opaque
`ApprovalRef` to a private bound #49/#50 authority. In the #74 composition,
`PolicyVerificationHandoff` is that adapter: it receives the actual #49 update
and #50 route/reservation through their owner seams, then resolves only the
stored, bound approval for the gate. The composer compares only fields both
owner records possess; #49-only runtime fields remain #49 provenance and are
not claimed as cross-checked with #50. The verification gate never accepts raw
updates, projections, routes, reservations, or caller-provided digests, and it
does not reimplement their validators.

`ApprovedReview` is return-only and binds Run, Team, workspace, Task, Dispatch,
Attempt, Worker/Reviewer nodes and terminals, review round, target `HEAD`,
allowed-claim tree/manifest digest, claim reference, review fingerprint,
profile reference, approval sequence, routing digest, and reservation digest.
The approval and routing digests are included in every later request and
receipt digest.

## Fixed profile and request

The trusted `VerificationProfileResolver` is the only source of verification
execution facts. It returns verification-specific
`VerificationProfileIdentity` and `VerificationExecutableIdentity` values;
these are intentionally distinct from #19/#35 profile types. The composition
root adapts the trusted registry to this protocol. This module does not define,
select, or promote profiles.

The profile fixes:

- an absolute canonical executable path, exact version, and lowercase SHA-256;
- a typed argv template and its digest, with at most one exact `{workspace}`
  element;
- the literal `canonical-workspace` cwd policy;
- a sorted finite safe environment name/value set;
- bounded timeout and output limits; and
- a normalized result-schema identity and profile-binding digest.

Only `CI`, `LANG`, `LC_ALL`, `LC_CTYPE`, `NO_COLOR`, `TERM`, and `TZ` are in
the safe environment set. `ORCA_*`, proxy/endpoint/config/home variables,
`LD_*`, `DYLD_*`, `PYTHON*`, loader/interpreter overrides, and provider-secret
names are rejected. The request carries safe values only; receipts carry names
and the profile-binding digest, never environment values. No inherited `PATH`
is an execution authority: the executable path is absolute and pinned.

The request builder is private. It copies profile-owned argv elements only,
replaces `{workspace}` with the trusted canonical workspace once, and rejects
unknown placeholders, extra arguments, noncanonical cwd, and extra names.
Shell metacharacters in a fixed element remain data. Task, Reviewer, and Agent
text are never inputs. Requests, receipts, evidence, approvals, and gate
state use return-only constructors and issuer markers.

Issuer markers are an in-process construction guard, not cryptographic
provenance. The trusted `ApprovalAdmissionPort` and durable
`VerificationStatePort` remain the authorities; signed or HMAC-bound envelopes
belong upstream if the threat model includes arbitrary code executing inside
the same Python process. Provider/task text is outside that authority boundary.

## The six-method state port keeps the Gate surface unchanged

The `VerificationStatePort` is required at gate construction. Its production
implementation owns CAS, persistence, idempotence, effect fencing, and restart
recovery. Issue #74 freezes the existing six-method shape and checks it with a
deterministic fake; #74 does not provide a SQLite implementation or a fresh-
process recovery proof. [Issue #80](https://github.com/iamtatsuki05/dotfiles/issues/80)
provides only the schema-4 physical foundation and pure codecs: it does not
provide the production adapter, non-empty lifecycle, or restart proof. Those
durable responsibilities continue in [#81](https://github.com/iamtatsuki05/dotfiles/issues/81),
[#82](https://github.com/iamtatsuki05/dotfiles/issues/82), and
[#83](https://github.com/iamtatsuki05/dotfiles/issues/83).

```python
class VerificationStatePort(Protocol):
    def prepare_once(self, request) -> VerificationPrepareResult: ...
    def begin_effect_once(verification_ref, request_digest) -> VerificationEffectLease: ...
    def read(self, verification_ref) -> VerificationDurableRecord: ...
    def status(self, verification_ref) -> DurableRecordStatus: ...
    def record_receipt_once(verification_ref, effect, result, before, after) -> VerificationReceipt: ...
    def apply_terminal_once(verification_ref, receipt_ref, receipt_digest) -> VerificationTerminalResult: ...
```

`start(approval_ref)` resolves the bound authority, resolves the named profile,
captures the approval target's before snapshot, builds the fixed request, and
calls `prepare_once`. It returns a handle only after a matching store-issued
prepared result and approved-to-verifying CAS. No in-memory state is an
authority.

`resume(handle)` reads and cross-checks the record and status. With a durable
state implementation, a handle containing only the durable reference, approval
reference, and request digest can support process-restart replay. The #74 fake
only demonstrates the in-process call order and replay contract; it is not
evidence of SQLite reopen or fresh-process recovery. `begin_effect_once` issues
an opaque effect nonce, lease epoch, and fencing token with one of:

| Effect status | Gate action |
| --- | --- |
| `RUN_ONCE` | Fresh-before check, then one runner call |
| `RECEIPTED` | Revalidate stored receipt; never call runner |
| `TERMINAL` | Revalidate terminal receipt; never call runner |
| `UNKNOWN` | `RecoveryRequired`; never retry or fallback |

The durable authority decides concurrent/repeated calls. A prepared response
loss or unknown effect is not treated as absent and cannot trigger a second
runner call.

## Runner and snapshot ordering

The runner receives only the opaque fixed request and store-issued effect
lease. Immediately before it runs, the gate re-resolves the profile and
executable, captures a fresh before snapshot, and compares every workspace,
device/inode, claim, target `HEAD`, and tree/manifest field to the durable
prepared request. A mismatch stops before the runner.

After the runner returns, the gate validates the exact typed result, executable
before/after identity, effect nonce/epoch/fencing, schema, output bounds,
cleanup, and result identity. It captures and validates the after snapshot
before `record_receipt_once`. Invalid result or snapshot values are typed
recovery and are never handed to the receipt port.

After a receipt is recorded, the gate re-resolves the named profile and
recomputes the request before `apply_terminal_once`. Profile/executable drift
therefore leaves the durable receipt for explicit recovery and cannot create a
terminal completion.

## Outcomes and receipt binding

Only `PASSED` with exit code `0`, matching schema, bounded output,
`CleanupStatus.REAPED`, matching effect fence, and a valid normalized receipt
can become `completed`. `FAILED`, `TIMEOUT`, `OUTPUT_LIMIT`, and
`SCHEMA_INVALID` become `verification_failed` only when a spawned process is
proven `REAPED`. `RUNNER_UNAVAILABLE` must prove `NOT_STARTED` and no output.
Unknown cleanup/effect, response loss, malformed port values, or snapshot drift
raise `RecoveryRequired`; there is no automatic retry or provider/backend
fallback.

`VerificationReceipt` is return-only and contains no raw output, prompt,
command text, PID, or environment value. Its canonical digest binds the exact
receipt reference, full approval/routing/request/profile binding, executable
before and after, effect nonce/epoch/fencing, argv/cwd/env/schema, both complete
snapshots, result metadata, and cleanup. Terminal validation reruns the same
receipt/result validator. Same-reference different-digest receipts are not
replays.

This `VerificationReceipt` is the #51 Gate-level runtime value, not a
schema-4 persisted operation record. #80 defines only the SQL
`record_version=1` discriminator and pure request/receipt projections; the
58-field operation-row digest and Store-issued hydration remain downstream
work.

Resume/replay always re-resolves the profile, captures a fresh current
snapshot, compares it to the durable receipt's after snapshot, and retains the
recorded executable-after identity. A stale saved-after observation cannot
complete a task.

## Ownership and limitations

#49 owns review transitions and approval provenance. #50 owns path/resource
admission and reservation identity. #74 owns the typed owner-ref composition
and the deterministic fake boundary. #11/#31/#33 own the production durable
state/effect/receipt port and terminal CAS. Schema-4 work is split across
[Issue #80 foundation](https://github.com/iamtatsuki05/dotfiles/issues/80),
[#81 task/review transitions](https://github.com/iamtatsuki05/dotfiles/issues/81),
[#82 verification transactions and adapter](https://github.com/iamtatsuki05/dotfiles/issues/82),
and [#83 image evidence, backup/restore, and Doctor](https://github.com/iamtatsuki05/dotfiles/issues/83).
#80's production path keeps the three new tables empty and does not claim
non-empty image semantics, lifecycle, capture, adapter, hydration, logical
record digest, or verification-aware Doctor/restore. #32 consumes the
resulting recovery handoff. This module defines only the verification contract
and pure port orchestration. Focused tests use fake ports and no provider, user
workspace, or process, so they do not prove SQLite durability or provider
exactly-once execution.
