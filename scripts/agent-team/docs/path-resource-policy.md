# Path/resource admission and lane routing

[日本語](path-resource-policy_JA.md)

`agent_team.path_resource_policy` is the pure admission seam for Issue #50.
It adapts the explicit path and resource declarations in a v4 `TaskSpec`,
checks one trusted workspace snapshot, and returns a deterministic routing
decision. It does not change `TaskSpec`, topology, review state, config, or
the store.

## Path claims

`PathClaim` requires all three values:

- `relative_path`: a normalized POSIX path relative to the observed workspace;
- `kind`: `PathKind.EXACT` for one entry or `PathKind.DIRECTORY` for that entry
  and descendants on component boundaries; and
- `access`: `PathAccess.READ` or `PathAccess.WRITE`.

Absolute paths, `..` components, control characters, trailing slashes, and
unsupported glob characters are rejected; `.` is reserved for an explicit
workspace-root claim. A claim is never inferred from
a suffix, task prose, or a missing value. `PathClaimPolicy.from_task_spec`
requires exactly one typed claim for every `allowed_paths` and
`do_not_modify` string. It sorts the accepted claims canonically and rejects
duplicate or overlapping allowed claims. Deny claims and explicitly supplied
`reserved_roots` are checked before allows.

`PathObservation` is a typed value supplied by a trusted snapshot producer. It
contains the observed canonical path, entry kind, device/inode, link count,
parent identity, and ancestor-symlink flag. Admission never calls
`Path.resolve()`, walks a directory, or opens a file. Missing, outside,
symlink, special-file, device-mismatched, hard-linked, incomplete, or
case-colliding observations are rejected. Every operation requires the complete
observation chain `.` → every lexical ancestor → each target. Ancestors must be
existing directories with a matching parent device/inode chain; the root
directory is also required to report a positive link count. Different
observed paths cannot reuse a non-null `(device, inode)` identity, even when a
forged snapshot says `nlink=1`. The producer and a later backend must handle
any time-of-check/time-of-use race after this point.

The operation must be explicit through `PathMutation`:

- `READ` checks one existing entry;
- `CREATE` checks a missing target;
- `MODIFY` checks an existing regular file;
- `DELETE` checks the source and its parent; and
- `RENAME` checks source, destination, and both parents.

Every touched path must be allowed with the required access. Exact matching is
not a string-prefix check, so `src/a` does not match `src/ab`. A failed path
admission returns `PathAdmission(candidate=False, reason_code=...)` and never
invokes a resource port.

## Resource claims

`ResourceClaimPolicy` wraps the existing `ResourceClaim` and requires an
explicit `ResourceKey` and `ResourceMode`. `adapt_resource_claims` takes an
explicit `frozenset` of known keys and performs a one-to-one binding. It does
not lowercase keys, derive a key from the claim name, or choose a default
mode. Two claims with the same key conflict unless both modes are
`ResourceMode.SHARED`.

Resource-bearing tasks also require an explicit `ResourceReservationAuthority`
with an opaque non-empty `owner_id`, a non-negative `lease_epoch`, and a
positive `fencing_token`. The authority is carried by
`ResourceReservationRequest` and must be echoed exactly by the result. A
missing or foreign owner, epoch, or token is not a candidate. A task with no
resource claim does not create or carry an authority and does not call the
reservation port.

`route_task` receives an immutable `known_keys` set and runs the same
one-to-one `adapt_resource_claims` check at its public boundary. It therefore
cannot treat an unknown key, duplicate TaskSpec claim, or forged mode as
known just because a port echoes it.

`ResourceReservationPort` is only the consumer boundary:

```python
def reserve(
    request: ResourceReservationRequest,
) -> ResourceReservationResult: ...
```

The caller supplies an opaque reservation identity and authority. The request
also carries a canonical SHA-256 digest over the task and reservation IDs,
claim name/key/mode values, and authority. The result must echo the task ID,
digest, authority, and sorted claim-key tuple exactly. A route invokes the port
once, only after pure path/profile/lane checks pass. Only a matching
`RESERVED` result can become a candidate. An exact idempotent request may be
accepted again only when the downstream authority explicitly returns the same
identity; a prohibited duplicate must return `CONFLICT` or `STALE`.
`CONFLICT`, `UNKNOWN`, `STALE`, failed calls, replays, and identity mismatches
remain non-candidates. SQLite, locks, leases, owner/epoch/fencing authority,
release, and race resolution belong to the downstream #31/#11
implementation; this module has no fallback provider or local reservation
state.

Public boundaries require exact runtime types for opaque authority and
string-backed key/digest fields. Authority and request identities are compared
field by field, without invoking subclass equality or string conversion. For
the topology/profile binding, `TeamDefinition`, `AgentNode`, `ProfileRef`,
`Edge`, and `EdgeKind` must also be exact typed values. Its identity is a
canonical tuple of built-in fields, so an equality-overriding team subclass
cannot stand in for the policy's team. For malformed observation, binding,
known-key, path-claim, reserved-root, and TaskSpec path collections, diagnostics use a
fixed precedence (`invalid-type`, `invalid-task`, `empty-value`, `unsafe-text`,
path errors, `unknown-resource-mode`, `unknown-resource-key`, then duplicate /
missing / extra claim errors), so tuple order and hash seed cannot change the
rejection reason.

## Lane matrix

`route_task` accepts only the lane explicitly present in `TaskSpec` and always
sets `parallel_candidate=False`.

| Lane | Required facts | Decision |
| --- | --- | --- |
| `normal` | Matching `SerialReviewPolicy`, workspace-write Worker, read-only Reviewer, admitted write path, and any required reservation | `SERIAL`; review and completion gates remain required |
| `express` | All normal facts plus `kind=small-change`, no dependencies, one exact write claim for an existing regular-file modification, and no exclusive resource | Same serial review and completion gates as normal |
| `research` | `kind=research`, verified read-only Worker/profile, read-only path claims, read operation, and no resource claim | `READ_ONLY`; no workspace-write permission or completion authority |

The serial policy must match the exact `TaskSpec`, fixed Worker/Reviewer pair,
the same `TeamDefinition`, and recomputed policy fingerprint. Routing rebuilds a
canonical policy from validated TaskSpec, topology, pair, worker, dependency,
and round fields, then compares each field and the fingerprint using only
built-in values. Equality-overriding dataclass or string subclasses are never
authority. The profile binding carries that `TeamDefinition`; routing
recomputes Worker and Reviewer permissions from its node profiles instead of
trusting a caller boolean.
Missing or mismatched profiles, policy fingerprints, path observations,
resource keys, and lane/kind combinations fail closed. No task text, agent
self-report, provider status, or alternate backend can change the lane.

## Ownership boundary

This module provides values and pure checks only. The composition root supplies
validated topology/profile bindings and trusted observations. The review policy
owns Worker → Reviewer transitions, a future reservation/store owns atomic
ownership and fencing, and a future workflow engine owns durable completion.
No provider process, terminal, filesystem scan, database, lock file, or
workspace-write completion is created here.
