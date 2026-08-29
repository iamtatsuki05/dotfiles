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

- restoring `RECEIPTED`, `COMPLETED`, and `CLEANED` operations without stranding or replaying them;
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
