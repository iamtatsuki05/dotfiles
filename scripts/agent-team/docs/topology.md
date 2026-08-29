# Team topology domain

[日本語](topology_JA.md)

The topology module defines an immutable, backend-neutral `TeamDefinition`.
It describes which agents exist and how they delegate work, review results,
or escalate questions. It does not start an agent, read configuration, call a
provider, or invoke Orca.

## Data model

The module is `agent_team.topology` and uses only Python 3.11 standard-library
types.

- `TeamId` and `NodeId` are typed string identifiers.
- `ProfileRef` contains `provider`, `transport`, and requested `permission`.
- `AgentNode` contains `node_id`, `label`, `profile`, and `is_main`.
- `Edge` connects `source` to `target` with one `EdgeKind`:
  `delegates-to`, `reviewed-by`, or `escalates-to`.
- `TeamDefinition` contains a `team_id`, a tuple of nodes, and a tuple of
  edges. All value objects are frozen.

Example:

```python
from agent_team.topology import (
    AgentNode,
    Edge,
    EdgeKind,
    ProfileRef,
    Permission,
    TeamDefinition,
    TeamId,
    NodeId,
)

team = TeamDefinition(
    TeamId("build-team"),
    (
        AgentNode(
            NodeId("main"),
            "Main",
            ProfileRef("claude", "direct", "orchestrator"),
            is_main=True,
        ),
        AgentNode(
            NodeId("worker"),
            "Worker",
            ProfileRef("codex", "direct", "workspace-write"),
        ),
    ),
    (Edge(NodeId("main"), NodeId("worker"), EdgeKind.DELEGATES_TO),),
)
```

## Profile resolver boundary

Validation receives a read-only `ProfileResolver`:

```python
class ProfileResolver(Protocol):
    def resolve(self, profile: ProfileRef) -> frozenset[Permission] | None: ...
```

The resolver returns provider/transport availability and the permissions it
actually supports. `None` means that the provider/transport is unknown. A
requested permission outside `permissions` is a `permission-mismatch`.
The topology module never imports `agent_team.registry` and never executes a
provider command. The caller decides how to adapt the verified registry to
this protocol.

## Validation contract

`validate_team(definition, resolver)` returns an immutable
`ValidationResult`. Its `errors` are sorted by code and message, so the same
input yields the same result. Renderers raise `TopologyValidationError` with
the same ordered issues before returning any output.

The following rules are deliberately explicit:

- The graph cannot be empty and has exactly one `is_main` node.
- The Main node uses `orchestrator`; non-Main nodes cannot use that permission.
- `is_main` is a strict boolean. Edge endpoints use the exact canonical node ID.
- Node IDs are unique without case or surrounding-whitespace ambiguity.
- Labels are non-empty, have no surrounding whitespace, and are unique after
  case-folding.
- Every edge kind and endpoint is known. Duplicate edges are rejected.
- A `delegates-to` target has at most one incoming delegation edge.
- A source has at most one `reviewed-by` edge.
- A source has at most one `escalates-to` edge.
- Self-edges are rejected; a self `reviewed-by` is reported as `self-review`.
- Each relationship kind is acyclic by itself. A mixed path may return to Main,
  so `Main delegates Worker -> Worker reviewed by Reviewer -> Reviewer escalates
  to Main` remains valid without making delegation or review recursive.
- Every node must be reachable from Main by following the directed edges.

Malformed identifiers, labels, profile fields, and permissions fail during
validation. C0/C1/DEL controls, Unicode line separators, lone surrogates, empty
strings, and ambiguous outer whitespace are not accepted. The validator also
fails closed when a resolver reports a supported lookup or operational error,
or returns a value outside the resolver contract. Unexpected programming
errors are not hidden.

## Deterministic renderers

The module exposes `render_json`, `render_ascii`, `render_mermaid`, and the
format-checked `render_topology`. Nodes and edges are sorted independently of
their input tuple order.

- JSON is UTF-8 JSON with sorted object keys, stable arrays, and a final
  newline. It contains only topology data.
- ASCII is a line-oriented representation using JSON-quoted values. It does
  not use Markdown bullets, links, or executable strings.
- Mermaid uses generated `n_<sha256-prefix>` node IDs. User IDs appear only in
  escaped labels, never in Mermaid syntax positions. Labels escape quotes,
  backslashes, HTML delimiters, Markdown-sensitive punctuation, and control
  characters. Edge labels come only from the fixed `EdgeKind` enum.

Only `json`, `ascii`, and `mermaid` are accepted. Unknown or case-variant
format names raise `TopologyFormatError`; there is no implicit fallback.
Rendering an invalid definition raises `TopologyValidationError` before any
output is returned.

Version-4 inspection already adapts this module through `agent-team teams` and
`agent-team graph`. `teams` validates every configured team and lists only its
ID, name, validity, and errors; it does not select or render a topology.
`graph` validates and renders the explicitly selected topology. `agent-team
start --team ... --dry-run` validates selection and returns only its
three-field plan. None of these paths starts a provider or creates runtime
resources. Config version 3, runtime state, MCP, the Orca lifecycle, and the
default team remain intentionally unconnected; later integration must adapt
this pure contract explicitly.
