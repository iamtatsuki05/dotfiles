"""Pure, backend-neutral team topology values and renderers."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from graphlib import CycleError, TopologicalSorter
from typing import Final, Literal, NamedTuple, NewType, Protocol

TeamId = NewType("TeamId", str)
NodeId = NewType("NodeId", str)
Permission = Literal["orchestrator", "read-only", "workspace-write"]
_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {"orchestrator", "read-only", "workspace-write"}
)
_FORMATS: Final[frozenset[str]] = frozenset({"json", "ascii", "mermaid"})


class EdgeKind(str, Enum):
    DELEGATES_TO = "delegates-to"
    REVIEWED_BY = "reviewed-by"
    ESCALATES_TO = "escalates-to"


@dataclass(frozen=True, slots=True)
class ProfileRef:
    provider: str
    transport: str
    permission: Permission


class ProfileResolver(Protocol):
    """Read-only boundary to the caller's verified profile registry."""

    def resolve(self, profile: ProfileRef) -> frozenset[Permission] | None: ...


@dataclass(frozen=True, slots=True)
class AgentNode:
    node_id: NodeId
    label: str
    profile: ProfileRef
    is_main: bool = False


@dataclass(frozen=True, slots=True)
class Edge:
    source: NodeId
    target: NodeId
    kind: EdgeKind


@dataclass(frozen=True, slots=True)
class TeamDefinition:
    team_id: TeamId
    nodes: tuple[AgentNode, ...]
    edges: tuple[Edge, ...]


class ValidationIssue(NamedTuple):
    code: str
    message: str


class ValidationResult(NamedTuple):
    valid: bool
    errors: tuple[ValidationIssue, ...]


class TopologyValidationError(ValueError):
    def __init__(self, errors: tuple[ValidationIssue, ...]) -> None:
        self.issues = errors
        super().__init__(
            "; ".join(f"{error.code}: {error.message}" for error in errors)
            or "team topology is invalid"
        )


class TopologyFormatError(ValueError):
    pass


def _key(value: str) -> str:
    return value.casefold()


def _add(issues: list[ValidationIssue], code: str, message: str) -> None:
    issues.append(ValidationIssue(code, message))


def _text_error(value: object, context: str) -> str | None:
    if not isinstance(value, str):
        return f"{context} must be a string"
    if not value or not value.strip():
        return f"{context} must not be empty"
    if value != value.strip() or any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        return f"{context} contains unsafe whitespace or control characters"
    return None


def _profile_issue(
    profile: ProfileRef, resolver: ProfileResolver
) -> ValidationIssue | None:
    for value, context in (
        (profile.provider, "profile.provider"),
        (profile.transport, "profile.transport"),
    ):
        if (message := _text_error(value, context)) is not None:
            return ValidationIssue("invalid-profile", message)
    if (
        not isinstance(profile.permission, str)
        or profile.permission not in _PERMISSIONS
    ):
        return ValidationIssue(
            "invalid-permission", f"permission is invalid: {profile.permission!r}"
        )
    try:
        permissions = resolver.resolve(profile)
    except (AttributeError, LookupError, OSError, RuntimeError, TypeError, ValueError):
        return ValidationIssue(
            "resolver-error",
            f"profile resolver failed for {profile.provider}/{profile.transport}",
        )
    if permissions is None:
        return ValidationIssue(
            "unknown-profile",
            f"profile is not known: {profile.provider}/{profile.transport}",
        )
    if not isinstance(permissions, frozenset) or any(
        item not in _PERMISSIONS for item in permissions
    ):
        return ValidationIssue(
            "resolver-contract",
            f"profile resolver returned an invalid result for {profile.provider}/{profile.transport}",
        )
    if profile.permission not in permissions:
        return ValidationIssue(
            "permission-mismatch",
            f"permission {profile.permission!r} is not available for {profile.provider}/{profile.transport}",
        )
    return None


def _has_cycle(nodes: tuple[str, ...], graph: dict[str, tuple[str, ...]]) -> bool:
    predecessors: dict[str, set[str]] = {node: set() for node in nodes}
    for source, targets in graph.items():
        for target in targets:
            predecessors[target].add(source)
    try:
        tuple(TopologicalSorter(predecessors).static_order())
    except CycleError:
        return True
    return False


def validate_team(
    definition: TeamDefinition, resolver: ProfileResolver
) -> ValidationResult:
    """Return stable issues; all relationship kinds are directed and acyclic."""
    if not isinstance(definition, TeamDefinition):
        raise TypeError("definition must be a TeamDefinition")
    issues: list[ValidationIssue] = []
    if (message := _text_error(definition.team_id, "team.id")) is not None:
        _add(issues, "invalid-team-id", message)
    if not definition.nodes:
        _add(issues, "empty-graph", "team topology must contain at least one node")

    by_key: dict[str, AgentNode] = {}
    labels: set[str] = set()
    for node in definition.nodes:
        node_key = _key(str(node.node_id))
        if (message := _text_error(node.node_id, "node.id")) is not None:
            _add(issues, "invalid-node-id", message)
        elif node_key in by_key:
            _add(
                issues, "duplicate-node-id", f"node ID is duplicated: {node.node_id!s}"
            )
        else:
            by_key[node_key] = node
        if (message := _text_error(node.label, "node.label")) is not None:
            _add(issues, "invalid-label", message)
        elif _key(node.label) in labels:
            _add(issues, "duplicate-label", f"node label is ambiguous: {node.label!r}")
        else:
            labels.add(_key(node.label))
        if (issue := _profile_issue(node.profile, resolver)) is not None:
            issues.append(issue)
        if not isinstance(node.is_main, bool):
            _add(
                issues,
                "invalid-main-flag",
                f"node main flag must be boolean: {node.node_id!s}",
            )
        if node.is_main is True and node.profile.permission != "orchestrator":
            _add(
                issues,
                "main-permission",
                "the main node must use orchestrator permission",
            )
        if node.is_main is False and node.profile.permission == "orchestrator":
            _add(
                issues,
                "non-main-permission",
                f"non-main node cannot use orchestrator permission: {node.node_id!s}",
            )

    main_nodes = tuple(node for node in definition.nodes if node.is_main is True)
    if len(main_nodes) != 1:
        _add(
            issues,
            "main-cardinality",
            f"team must have exactly one main node; found {len(main_nodes)}",
        )

    graph: defaultdict[str, set[str]] = defaultdict(set)
    graphs_by_kind: dict[EdgeKind, defaultdict[str, set[str]]] = {
        kind: defaultdict(set) for kind in EdgeKind
    }
    seen: set[tuple[str, str, EdgeKind]] = set()
    counts: dict[EdgeKind, defaultdict[str, int]] = {
        EdgeKind.DELEGATES_TO: defaultdict(int),
        EdgeKind.REVIEWED_BY: defaultdict(int),
        EdgeKind.ESCALATES_TO: defaultdict(int),
    }
    for edge in definition.edges:
        if not isinstance(edge.kind, EdgeKind):
            _add(issues, "invalid-edge-kind", f"edge kind is invalid: {edge.kind!r}")
            continue
        source, target = _key(str(edge.source)), _key(str(edge.target))
        for value, context in (
            (edge.source, "edge.source"),
            (edge.target, "edge.target"),
        ):
            if (message := _text_error(value, context)) is not None:
                _add(issues, "invalid-edge", message)
        signature = source, target, edge.kind
        if signature in seen:
            _add(
                issues,
                "duplicate-edge",
                f"edge is duplicated: {edge.source!s} {edge.kind.value} {edge.target!s}",
            )
        seen.add(signature)
        if source not in by_key:
            _add(issues, "unknown-node", f"edge source is unknown: {edge.source!s}")
        if target not in by_key:
            _add(issues, "unknown-node", f"edge target is unknown: {edge.target!s}")
        if source not in by_key or target not in by_key:
            continue
        if str(edge.source) != str(by_key[source].node_id) or str(edge.target) != str(
            by_key[target].node_id
        ):
            _add(
                issues,
                "noncanonical-endpoint",
                f"edge endpoints must match canonical node IDs: {edge.source!s} -> {edge.target!s}",
            )
            continue
        if source == target:
            _add(
                issues,
                "self-review" if edge.kind is EdgeKind.REVIEWED_BY else "self-edge",
                f"edge cannot point to itself: {edge.source!s} {edge.kind.value}",
            )
            continue
        graph[source].add(target)
        graphs_by_kind[edge.kind][source].add(target)
        counts[edge.kind][target if edge.kind is EdgeKind.DELEGATES_TO else source] += 1

    limits = (
        (EdgeKind.DELEGATES_TO, "delegation-cardinality", "incoming", "delegates-to"),
        (EdgeKind.REVIEWED_BY, "review-cardinality", "outgoing", "reviewed-by"),
        (EdgeKind.ESCALATES_TO, "escalation-cardinality", "outgoing", "escalates-to"),
    )
    for kind, code, direction, name in limits:
        for key, count in sorted(counts[kind].items()):
            if count > 1:
                _add(
                    issues,
                    code,
                    f"{name} has more than one {direction} edge for {by_key[key].node_id!s}",
                )

    node_keys = tuple(sorted(by_key))
    stable_graph = {key: tuple(sorted(graph.get(key, set()))) for key in node_keys}
    for kind in EdgeKind:
        relation_graph = {
            key: tuple(sorted(graphs_by_kind[kind].get(key, set())))
            for key in node_keys
        }
        if _has_cycle(node_keys, relation_graph):
            _add(issues, "cycle", f"{kind.value} cycle is not allowed")
    if len(main_nodes) == 1:
        main_key = _key(str(main_nodes[0].node_id))
        reachable = {main_key}
        pending: deque[str] = deque(reachable)
        while pending:
            for target in stable_graph.get(pending.popleft(), ()):
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)
        for key in node_keys:
            if key not in reachable:
                _add(
                    issues,
                    "unreachable-node",
                    f"node is not reachable from main: {by_key[key].node_id!s}",
                )
    errors = tuple(sorted(set(issues), key=lambda item: (item.code, item.message)))
    return ValidationResult(not errors, errors)


def _validated(
    definition: TeamDefinition, resolver: ProfileResolver
) -> tuple[tuple[AgentNode, ...], tuple[Edge, ...]]:
    result = validate_team(definition, resolver)
    if not result.valid:
        raise TopologyValidationError(result.errors)
    nodes = tuple(
        sorted(definition.nodes, key=lambda n: (_key(str(n.node_id)), str(n.node_id)))
    )
    edges = tuple(
        sorted(
            definition.edges,
            key=lambda e: (
                e.kind.value,
                _key(str(e.source)),
                str(e.source),
                _key(str(e.target)),
                str(e.target),
            ),
        )
    )
    return nodes, edges


def _quote(value: str, *, ascii_only: bool = False) -> str:
    return json.dumps(value, ensure_ascii=ascii_only)


def render_json(definition: TeamDefinition, resolver: ProfileResolver) -> str:
    """Render stable JSON containing topology data only."""
    nodes, edges = _validated(definition, resolver)
    data = {
        "team_id": str(definition.team_id),
        "nodes": [
            {
                "id": str(node.node_id),
                "label": node.label,
                "main": node.is_main,
                "profile": {
                    "provider": node.profile.provider,
                    "transport": node.profile.transport,
                    "permission": node.profile.permission,
                },
            }
            for node in nodes
        ],
        "edges": [
            {
                "source": str(edge.source),
                "target": str(edge.target),
                "kind": edge.kind.value,
            }
            for edge in edges
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_ascii(definition: TeamDefinition, resolver: ProfileResolver) -> str:
    """Render quoted lines without Markdown or executable strings."""
    nodes, edges = _validated(definition, resolver)
    lines = [f"TEAM {_quote(str(definition.team_id), ascii_only=True)}", "NODES"]
    for node in nodes:
        profile = f"{node.profile.provider}/{node.profile.transport}/{node.profile.permission}"
        lines.append(
            f"NODE id={_quote(str(node.node_id), ascii_only=True)} label={_quote(node.label, ascii_only=True)} profile={_quote(profile, ascii_only=True)} main={'true' if node.is_main else 'false'}"
        )
    lines.append("EDGES")
    for edge in edges:
        lines.append(
            f"EDGE source={_quote(str(edge.source), ascii_only=True)} kind={_quote(edge.kind.value, ascii_only=True)} target={_quote(str(edge.target), ascii_only=True)}"
        )
    return "\n".join(lines) + "\n"


def _mermaid_text(value: str) -> str:
    replacements = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#x27;",
        "\\": "&#92;",
        "`": "&#96;",
        "[": "&#91;",
        "]": "&#93;",
        "{": "&#123;",
        "}": "&#125;",
        "|": "&#124;",
        "*": "&#42;",
        "_": "&#95;",
        "~": "&#126;",
        "(": "&#40;",
        ")": "&#41;",
    }
    return "".join(replacements.get(character, character) for character in value)


def render_mermaid(definition: TeamDefinition, resolver: ProfileResolver) -> str:
    """Render a Mermaid flowchart with generated safe node IDs."""
    nodes, edges = _validated(definition, resolver)
    aliases = {
        _key(str(node.node_id)): "n_"
        + hashlib.sha256(str(node.node_id).encode()).hexdigest()[:16]
        for node in nodes
    }
    if len(aliases) != len(set(aliases.values())):
        raise TopologyValidationError(
            (
                ValidationIssue(
                    "renderer-collision", "safe Mermaid node identifiers are not unique"
                ),
            )
        )
    lines = ["flowchart TD"]
    for node in nodes:
        label = _mermaid_text(f"{node.label} (id: {node.node_id})")
        lines.append(f'    {aliases[_key(str(node.node_id))]}["{label}"]')
    for edge in edges:
        lines.append(
            f"    {aliases[_key(str(edge.source))]} -->|{edge.kind.value}| {aliases[_key(str(edge.target))]}"
        )
    return "\n".join(lines) + "\n"


def render_topology(
    definition: TeamDefinition, output_format: str, resolver: ProfileResolver
) -> str:
    """Dispatch to one explicit format and reject unknown values."""
    if output_format not in _FORMATS:
        raise TopologyFormatError("output format must be one of: ascii, json, mermaid")
    if output_format == "json":
        return render_json(definition, resolver)
    if output_format == "ascii":
        return render_ascii(definition, resolver)
    return render_mermaid(definition, resolver)
