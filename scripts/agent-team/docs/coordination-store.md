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
| Cleanup burden | Close connections, checkpoint WAL, verify/remove WAL/SHM sidecars | Classify known temp files, preserve unrelated files, fsync file and directory |
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
  only the explicit trusted `RecoveryStoreTx.mark_prepared_unknown` authority may move them to
  `UNKNOWN_EFFECT`.
- A recovery-floor advance fences stale authority globally but never rewrites a `CLEANED`
  tombstone row or its events. Typed rebasing is limited to `INTENT`, `RECEIPTED`, and `COMPLETED`.
- The typed recovery seam maps SQLite snapshot-query failures and malformed persisted observation
  values to stable `StoreIntegrityError`; existing store errors are preserved without double wrapping.
- Use explicit transactions with `BEGIN IMMEDIATE`, bounded busy timeout, `foreign_keys=ON`, `WAL`, and `synchronous=FULL`.
- Keep the database and sidecars in the private agent-team state root. Close every connection and checkpoint before cleanup.
- Back up through SQLite's backup API. Migration and restore must be explicit operations with their own version and rollback checks.
- Do not fall back to atomic files or another backend when SQLite is unavailable, locked beyond the allowed timeout, corrupt, or version-incompatible.

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
names are owned by the later marker/ledger implementations. The doctor does
not invent a filename, construct `CoordinationStore`, or execute recovery.

The filesystem reader opens only an existing owner-only directory/file through
`O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK`, holds the existing lifetime gate
with a shared lock when present, and inventories every root-direct name. It
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
`RESTORE_COMMITTED` or the contractually allowed `RESTORE_ABORTED` edge; only
a later generation may begin with a new `RESTORE_PREPARED` record. The primary database, exact SQLite
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
those phases remain the contract of #55/#56.

The provider adapter is a trusted composition-root dependency. A malicious
full-shape in-process adapter is outside this Python value boundary; task data
and ordinary callers do not select or inject provider adapters.
