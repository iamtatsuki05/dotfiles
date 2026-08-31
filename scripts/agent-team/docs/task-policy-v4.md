# Task policy schema version 4

[日本語](task-policy-v4_JA.md)

`agent_team.task_policy` defines the pure, backend-neutral contract for task
policy data. It does not open a SQLite database or JSON file, acquire a lock,
inspect a workspace, start a provider, or perform a workflow transition.

## TaskSpec

`TaskSpec` is an immutable description of one task. Every field is explicit:

| Field | Meaning |
|---|---|
| `task_id` | Stable task identity. |
| `title`, `context`, `goal` | Human-readable task data. |
| `acceptance` | A non-empty tuple of acceptance statements. |
| `allowed_paths` | A non-empty tuple of declared path strings. Path semantics are a later policy concern. |
| `do_not_modify` | Explicit deny-path declarations; an empty tuple is allowed and is not inferred. |
| `dependencies` | Task IDs that must precede this task. |
| `verification` | An explicitly named verification-profile reference. |
| `escalation_node` | An explicitly named topology node, or explicit `null`. |
| `kind` | `implementation`, `small-change`, or `research`. |
| `lane` | `normal`, `express`, or `research`. |
| `resource_claims` | Explicit named logical resource claims. Each value is a `ResourceClaim`. |

The parser accepts only the listed fields. It does not derive a lane, argv,
permission, provider, or default value from task prose. Lists from an input
mapping are converted to immutable tuples by `parse_task_spec`; direct typed
construction must already use tuples and enum values. In particular, a mapping
with `resource_claims = ["workspace"]` becomes a
`tuple[ResourceClaim, ...]` by constructing `ResourceClaim("workspace")` for
each string. Direct typed construction must pass
`tuple[ResourceClaim, ...]`; passing `tuple[str, ...]` is rejected.
The identity wrappers such as `TaskId` and `TeamId` are Python `NewType`
labels: Python cannot distinguish their runtime values from `str`. The
nominal annotations still keep typed callers from mixing identities, while
the mapping parser is the explicit place that validates and wraps strings.

Text is bounded and must not be empty, surrounding whitespace, C0/C1/DEL
controls, lone surrogates, or Unicode line separators. Unknown enum values,
missing fields, non-string mapping keys, and over-limit arrays fail before a
caller can create a runtime resource.

## Dependency validation

`validate_task_specs` requires explicit registered team IDs, topology node IDs,
and verification-profile names. It rejects an unknown team, escalation node,
or verification profile; duplicate task IDs (including case-fold ambiguity),
duplicate dependencies, self-dependencies, unknown dependencies, duplicate
resource claims, and dependency cycles. The returned `ValidationResult` and
its `ValidationIssue` values are immutable and sorted by code and message.

`task_dependency_order` uses a deterministic Kahn traversal. Ready tasks are
ordered by `(casefold(task_id), task_id)`, so input tuple order does not affect
the result. `canonical_task_json` validates before rendering, emits tasks in
the same stable order, includes the dependency order, and uses UTF-8 JSON with
sorted keys and a final newline.

## TaskPolicyStateV4

`TaskPolicyStateV4` is an immutable observation/data record with an explicit
`version = 4`. This is the exact 15-field envelope. It describes the logical
state contract; it does not replace the existing runtime `state.json` version
3 and does not perform a v3-to-v4 migration.

| Field | Type | Nullable | Contract |
|---|---|---|---|
| `version` | `int` | no | Exactly `4`. |
| `team_id` | `TeamId` | no | Stable team identity. |
| `workspace` | `WorkspaceIdentity` | no | Lexically canonical absolute workspace path value; it does not prove symlink, device, or inode identity. |
| `sequence` | `int` | no | Non-negative monotonic sequence. |
| `task_id` | `TaskId` | no | Stable task identity. |
| `attempt_id` | `AttemptId \| None` | yes | Must be present together with `dispatch_id`. |
| `dispatch_id` | `DispatchId \| None` | yes | Must be present together with `attempt_id`. |
| `worker_node` | `NodeId \| None` | yes | Topology worker-node identity when observed. |
| `reviewer_node` | `NodeId \| None` | yes | Topology reviewer-node identity when observed. |
| `review_round` | `int` | no | Non-negative review round. |
| `target_head` | `GitObjectId \| None` | yes | Lowercase 40- or 64-character Git object ID; paired with `target_tree_digest`. |
| `target_tree_digest` | `TreeDigest \| None` | yes | Lowercase 64-character SHA-256 digest; paired with `target_head`. |
| `claim_ref` | `ClaimRef \| None` | yes | Opaque logical claim reference. |
| `receipt_ref` | `ReceiptRef \| None` | yes | Opaque logical receipt reference. |
| `phase` | `TaskPhase` | no | One of the 11 literals listed below. |

The fixed `phase` literals are `pending`, `assigned`, `worker_done`,
`review_pending`, `approved`, `changes_requested`, `verifying`, `completed`,
`failed`, `ask_user`, and `verification_failed`.

Optional identity fields are always present in the canonical state envelope as
explicit `null` values. Attempt/dispatch and `HEAD`/tree digest pairs cannot
be half-populated. The workspace check is lexical only; filesystem symlink,
device, and inode identity are outside this module. The state value has no `complete`, transition, or storage
method: a `completed` observation is not completion authority. A future store
must establish mutation authority independently.

`TreeDigest` is the SHA-256 of a future trusted snapshot port's canonical tree
manifest, not a Git tree object ID. This slice fixes its value format but does
not compute or attest that manifest.

`parse_task_state` requires the exact v4 field set. A v3 envelope, another
version, a missing field, an unknown field, or a non-canonical workspace is an
explicit error. The existing v3 runtime `state.json` remains on its current
contract; this logical v4 record is not a replacement, migration, or writeback
path. No v3-to-v4 conversion, field completion, v4-to-v3 writeback, or
backend fallback exists.

`ExpectedSequenceUpdate` is the typed handoff to a future store port.
`apply_expected_sequence_update` checks exact expected sequence, an increment
of one, and immutable team/workspace/task identity. A stale sequence raises
`StateConflictError`; the helper has no persistence side effect and does not
implement SQLite, CAS, locks, journals, leases, or transitions.

## Later integration

Configuration parsing can call the pure mapping parser after an explicit team
has been selected. A persistence implementation in the backend roadmap must
adapt these values through `TaskPolicyStatePort` without exposing SQL rows or
file formats. Review gates, path canonicalization, lane execution, provider
effects, and recovery belong to later policy/backend slices.
