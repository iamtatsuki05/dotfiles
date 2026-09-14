"""Pure named-team graph values and validation.

This module describes a graph before a backend or a coordinator is selected.
Node IDs are identities; :class:`~agent_team.contracts.Role` values are the
fixed role kinds used for stage and permission checks.  No provider, model,
transport, authentication, or runtime fallback belongs here.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Literal, NoReturn, cast

from .contracts import NodeRef, Role
from .task_spec import TaskSpec, parse_task_specs

CoordinationMode = Literal["agent", "program"]
DispatchMode = Literal["serial", "parallel"]
EdgeKind = Literal["delegates-to", "reviewed-by", "consults-to"]

_COORDINATION_MODES = frozenset({"agent", "program"})
_DISPATCH_MODES = frozenset({"serial", "parallel"})
_EDGE_KINDS = frozenset({"delegates-to", "reviewed-by", "consults-to"})
_NODE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_TASK_ID = re.compile(r"[a-z](?:[a-z0-9-]*[a-z0-9])?\Z")
_MAX_NODES = 128
_MAX_EDGES = 256

_COORDINATION_FIELDS = frozenset({"mode", "entry_nodes", "dispatch_mode", "max_active"})
_EDGE_FIELDS = frozenset({"source", "target", "kind"})
_NODE_FIELDS = frozenset({"node_id", "kind"})
_ROUTE_FIELDS = frozenset(
    {
        "task_id",
        "plan_writer",
        "plan_reviewer",
        "implementation_writer",
        "implementation_reviewer",
    }
)
_GRAPH_FIELDS = frozenset({"nodes", "edges", "coordination", "routes"})


class NamedGraphError(ValueError):
    """Raised when a named graph cannot be executed as declared."""


def _fail(field: str, message: str) -> NoReturn:
    raise NamedGraphError(f"{field} {message}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(field, "must be a non-empty string")
    if not value.strip():
        _fail(field, "must not be whitespace-only")
    if "\x00" in value:
        _fail(field, "must not contain NUL")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail(field, "must not contain lone surrogate code points")
    return value


def _slug(value: object, field: str, pattern: re.Pattern[str]) -> str:
    text = _text(value, field)
    if len(text) > 64:
        _fail(field, "must be at most 64 characters")
    if pattern.fullmatch(text) is None:
        _fail(field, "must be a lowercase slug")
    return text


def _node_slug(value: object, field: str) -> str:
    return _slug(value, field, _NODE_ID)


def _task_slug(value: object, field: str) -> str:
    return _slug(value, field, _TASK_ID)


def _optional_node_slug(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _node_slug(value, field)


def _tuple(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        _fail(field, "must be an immutable tuple")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    values = _tuple(value, field)
    return tuple(
        _node_slug(item, f"{field}[{index}]") for index, item in enumerate(values)
    )


def _array(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        _fail(field, "must be an array")
    return tuple(value)


def _mapping(value: object, field: str, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail(field, "must be an object")
    items = tuple(value.items())
    keys = tuple(key for key, _item in items)
    if any(not isinstance(key, str) for key in keys):
        _fail(field, "keys must be strings")
    string_keys = cast(tuple[str, ...], keys)
    if len(set(string_keys)) != len(string_keys):
        _fail(field, "contains duplicate keys")
    unknown = set(string_keys) - fields
    missing = fields - set(string_keys)
    if unknown:
        _fail(field, f"contains unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        _fail(field, f"is missing keys: {', '.join(sorted(missing))}")
    return {key: item for key, item in items}


def _role(value: object, field: str) -> Role:
    if not isinstance(value, str):
        _fail(field, "must be a role kind string")
    try:
        return Role(value)
    except ValueError as exc:
        _fail(field, f"has an unknown role kind: {value!r}")
        raise AssertionError("unreachable") from exc


def _edge_kind(value: object, field: str) -> EdgeKind:
    if not isinstance(value, str) or value not in _EDGE_KINDS:
        _fail(field, f"has an unknown edge kind: {value!r}")
    return cast(EdgeKind, value)


@dataclass(frozen=True, slots=True)
class Coordination:
    """Explicit graph scheduling mode and entry points."""

    mode: CoordinationMode
    entry_nodes: tuple[str, ...]
    dispatch_mode: DispatchMode
    max_active: int

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in _COORDINATION_MODES:
            _fail("coordination.mode", f"has an unknown mode: {self.mode!r}")
        _string_tuple(self.entry_nodes, "coordination.entry_nodes")
        if (
            not isinstance(self.dispatch_mode, str)
            or self.dispatch_mode not in _DISPATCH_MODES
        ):
            _fail(
                "coordination.dispatch_mode",
                f"has an unknown dispatch mode: {self.dispatch_mode!r}",
            )
        if not isinstance(self.max_active, int) or isinstance(self.max_active, bool):
            _fail("coordination.max_active", "must be an integer")
        if self.max_active < 1:
            _fail("coordination.max_active", "must be positive")

    @classmethod
    def from_dict(cls, value: object) -> Coordination:
        raw = _mapping(value, "coordination", _COORDINATION_FIELDS)
        entry_nodes = _array(raw["entry_nodes"], "coordination.entry_nodes")
        return cls(
            mode=cast(CoordinationMode, raw["mode"]),
            entry_nodes=tuple(
                _node_slug(item, f"coordination.entry_nodes[{index}]")
                for index, item in enumerate(entry_nodes)
            ),
            dispatch_mode=cast(DispatchMode, raw["dispatch_mode"]),
            max_active=raw["max_active"]
            if isinstance(raw["max_active"], int)
            and not isinstance(raw["max_active"], bool)
            else cast(int, raw["max_active"]),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "entry_nodes": list(self.entry_nodes),
            "dispatch_mode": self.dispatch_mode,
            "max_active": self.max_active,
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One directed relationship between exact named nodes."""

    source: str
    target: str
    kind: EdgeKind

    def __post_init__(self) -> None:
        _node_slug(self.source, "edge.source")
        _node_slug(self.target, "edge.target")
        _edge_kind(self.kind, "edge.kind")

    @classmethod
    def from_dict(cls, value: object) -> GraphEdge:
        raw = _mapping(value, "edge", _EDGE_FIELDS)
        return cls(
            source=_node_slug(raw["source"], "edge.source"),
            target=_node_slug(raw["target"], "edge.target"),
            kind=_edge_kind(raw["kind"], "edge.kind"),
        )

    def as_dict(self) -> dict[str, object]:
        return {"source": self.source, "target": self.target, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class TaskRoute:
    """Named writer/reviewer bindings for one exact TaskSpec ID."""

    task_id: str
    plan_writer: str | None
    plan_reviewer: str | None
    implementation_writer: str | None
    implementation_reviewer: str | None

    def __post_init__(self) -> None:
        _task_slug(self.task_id, "route.task_id")
        _optional_node_slug(self.plan_writer, "route.plan_writer")
        _optional_node_slug(self.plan_reviewer, "route.plan_reviewer")
        _optional_node_slug(self.implementation_writer, "route.implementation_writer")
        _optional_node_slug(
            self.implementation_reviewer, "route.implementation_reviewer"
        )

    @classmethod
    def from_dict(cls, value: object) -> TaskRoute:
        raw = _mapping(value, "route", _ROUTE_FIELDS)
        return cls(
            task_id=_task_slug(raw["task_id"], "route.task_id"),
            plan_writer=_optional_node_slug(raw["plan_writer"], "route.plan_writer"),
            plan_reviewer=_optional_node_slug(
                raw["plan_reviewer"], "route.plan_reviewer"
            ),
            implementation_writer=_optional_node_slug(
                raw["implementation_writer"], "route.implementation_writer"
            ),
            implementation_reviewer=_optional_node_slug(
                raw["implementation_reviewer"], "route.implementation_reviewer"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "plan_writer": self.plan_writer,
            "plan_reviewer": self.plan_reviewer,
            "implementation_writer": self.implementation_writer,
            "implementation_reviewer": self.implementation_reviewer,
        }


@dataclass(frozen=True, slots=True)
class GraphSpec:
    """An immutable named graph plus explicit task routing."""

    nodes: tuple[NodeRef, ...]
    edges: tuple[GraphEdge, ...]
    coordination: Coordination
    routes: tuple[TaskRoute, ...]

    def __post_init__(self) -> None:
        nodes = _tuple(self.nodes, "nodes")
        if any(not isinstance(node, NodeRef) for node in nodes):
            _fail("nodes", "must contain only NodeRef values")
        edges = _tuple(self.edges, "edges")
        if any(not isinstance(edge, GraphEdge) for edge in edges):
            _fail("edges", "must contain only GraphEdge values")
        if not isinstance(self.coordination, Coordination):
            _fail("coordination", "must contain a Coordination value")
        routes = _tuple(self.routes, "routes")
        if any(not isinstance(route, TaskRoute) for route in routes):
            _fail("routes", "must contain only TaskRoute values")

    def node(self, node_id: str) -> NodeRef:
        """Return the node whose ID exactly equals ``node_id``."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def route(self, task_id: str) -> TaskRoute:
        """Return the route whose task ID exactly equals ``task_id``."""
        for route in self.routes:
            if route.task_id == task_id:
                return route
        raise KeyError(task_id)

    @property
    def main_node(self) -> NodeRef | None:
        main_nodes = tuple(node for node in self.nodes if node.kind is Role.MAIN)
        if len(main_nodes) > 1:
            raise NamedGraphError("nodes contains more than one main role kind")
        return main_nodes[0] if main_nodes else None

    @classmethod
    def from_dict(cls, value: object) -> GraphSpec:
        raw = _mapping(value, "graph", _GRAPH_FIELDS)
        nodes_value = _array(raw["nodes"], "nodes")
        nodes: list[NodeRef] = []
        for index, item in enumerate(nodes_value):
            node_raw = _mapping(item, f"nodes[{index}]", _NODE_FIELDS)
            node_id = _node_slug(node_raw["node_id"], f"nodes[{index}].node_id")
            kind = _role(node_raw["kind"], f"nodes[{index}].kind")
            try:
                nodes.append(NodeRef(node_id=node_id, kind=kind))
            except (TypeError, ValueError) as exc:
                raise NamedGraphError(f"nodes[{index}] is invalid") from exc

        edges_value = _array(raw["edges"], "edges")
        edges = tuple(GraphEdge.from_dict(item) for item in edges_value)
        routes_value = _array(raw["routes"], "routes")
        routes = tuple(TaskRoute.from_dict(item) for item in routes_value)
        return cls(
            nodes=tuple(nodes),
            edges=edges,
            coordination=Coordination.from_dict(raw["coordination"]),
            routes=routes,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "nodes": [
                {"node_id": node.node_id, "kind": node.kind.value}
                for node in self.nodes
            ],
            "edges": [edge.as_dict() for edge in self.edges],
            "coordination": self.coordination.as_dict(),
            "routes": [route.as_dict() for route in self.routes],
        }


def _validate_catalog(task_specs: tuple[TaskSpec, ...]) -> frozenset[str]:
    if not isinstance(task_specs, tuple):
        _fail("task_specs", "must be an immutable tuple")
    if any(not isinstance(task, TaskSpec) for task in task_specs):
        _fail("task_specs", "must contain only TaskSpec values")
    try:
        parsed = parse_task_specs([task.as_dict() for task in task_specs])
    except ValueError as exc:
        raise NamedGraphError(f"task catalog is invalid: {exc}") from exc
    return frozenset(task.task_id for task in parsed)


def _validate_nodes(graph: GraphSpec) -> dict[str, NodeRef]:
    if len(graph.nodes) > _MAX_NODES:
        _fail("nodes", f"must contain at most {_MAX_NODES} nodes")
    by_id: dict[str, NodeRef] = {}
    for index, node in enumerate(graph.nodes):
        node_id = _node_slug(node.node_id, f"nodes[{index}].node_id")
        if not isinstance(node.kind, Role):
            _fail(f"nodes[{index}].kind", "must be a Role value")
        if node_id in by_id:
            _fail("nodes", f"contains duplicate node ID: {node_id}")
        by_id[node_id] = node
    return by_id


def _validate_coordination(
    graph: GraphSpec,
    by_id: Mapping[str, NodeRef],
) -> tuple[str, ...]:
    coordination = graph.coordination
    if coordination.mode not in _COORDINATION_MODES:
        _fail("coordination.mode", f"has an unknown mode: {coordination.mode!r}")
    if coordination.dispatch_mode not in _DISPATCH_MODES:
        _fail(
            "coordination.dispatch_mode",
            f"has an unknown dispatch mode: {coordination.dispatch_mode!r}",
        )
    if not isinstance(coordination.max_active, int) or isinstance(
        coordination.max_active, bool
    ):
        _fail("coordination.max_active", "must be an integer")
    if coordination.max_active < 1:
        _fail("coordination.max_active", "must be positive")
    if coordination.dispatch_mode == "serial" and coordination.max_active != 1:
        _fail("coordination.max_active", "must be 1 for serial dispatch")
    entries = tuple(coordination.entry_nodes)
    if len(set(entries)) != len(entries):
        _fail("coordination.entry_nodes", "must not contain duplicate node IDs")
    for index, entry in enumerate(entries):
        _node_slug(entry, f"coordination.entry_nodes[{index}]")
        if entry not in by_id:
            _fail(
                f"coordination.entry_nodes[{index}]",
                f"references undeclared node: {entry}",
            )

    main_nodes = tuple(node for node in by_id.values() if node.kind is Role.MAIN)
    if coordination.mode == "agent":
        if len(main_nodes) != 1:
            _fail(
                "nodes",
                f"agent coordination requires exactly one main node; found {len(main_nodes)}",
            )
        main_id = main_nodes[0].node_id
        if entries != (main_id,):
            _fail(
                "coordination.entry_nodes",
                "agent mode entries must be exactly the main node",
            )
    else:
        if main_nodes:
            _fail("nodes", "program coordination cannot contain a main node")
        if not entries:
            _fail("coordination.entry_nodes", "program mode requires explicit entries")
    return entries


def _validate_edges(
    graph: GraphSpec,
    by_id: Mapping[str, NodeRef],
) -> tuple[dict[str, list[GraphEdge]], set[tuple[str, str, EdgeKind]]]:
    if len(graph.edges) > _MAX_EDGES:
        _fail("edges", f"must contain at most {_MAX_EDGES} edges")
    outgoing: dict[str, list[GraphEdge]] = {node_id: [] for node_id in by_id}
    signatures: set[tuple[str, str, EdgeKind]] = set()
    for index, edge in enumerate(graph.edges):
        source = _node_slug(edge.source, f"edges[{index}].source")
        target = _node_slug(edge.target, f"edges[{index}].target")
        kind = _edge_kind(edge.kind, f"edges[{index}].kind")
        signature = (source, target, kind)
        if signature in signatures:
            _fail("edges", f"contains duplicate edge: {source} {kind} {target}")
        signatures.add(signature)
        if source not in by_id:
            _fail(f"edges[{index}].source", f"references undeclared node: {source}")
        if target not in by_id:
            _fail(f"edges[{index}].target", f"references undeclared node: {target}")
        if source == target:
            _fail(f"edges[{index}]", "must not be a self-edge")
        source_kind = by_id[source].kind
        target_kind = by_id[target].kind
        if kind == "delegates-to" and target_kind is Role.MAIN:
            _fail(f"edges[{index}]", "delegates-to cannot target the main node")
        if kind == "reviewed-by" and (
            source_kind not in {Role.PLANNER, Role.WORKER}
            or target_kind is not Role.REVIEWER
        ):
            _fail(
                f"edges[{index}]",
                "reviewed-by must connect a planner or worker to a reviewer",
            )
        outgoing[source].append(edge)
    return outgoing, signatures


def _validate_automatic_dag(
    by_id: Mapping[str, NodeRef], signatures: set[tuple[str, str, EdgeKind]]
) -> None:
    predecessors: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for source, target, kind in signatures:
        if kind in {"delegates-to", "reviewed-by"}:
            predecessors[target].add(source)
    try:
        tuple(TopologicalSorter(predecessors).static_order())
    except CycleError as exc:
        raise NamedGraphError(
            "automatic delegates/review graph contains a cycle"
        ) from exc


def _validate_reachability(
    by_id: Mapping[str, NodeRef],
    entries: tuple[str, ...],
    outgoing: Mapping[str, list[GraphEdge]],
) -> None:
    reachable = set(entries)
    queued = set(entries)
    pending: deque[str] = deque(entries)
    while pending:
        source = pending.popleft()
        for edge in outgoing[source]:
            if edge.kind == "consults-to":
                # Consultation is a one-hop communication leaf.  It never
                # grants implicit execution through the target's own edges.
                reachable.add(edge.target)
                continue
            reachable.add(edge.target)
            if edge.target not in queued:
                queued.add(edge.target)
                pending.append(edge.target)
    missing = tuple(node_id for node_id in by_id if node_id not in reachable)
    if missing:
        _fail(
            "nodes",
            "contains nodes unreachable from coordination entries: "
            + ", ".join(missing),
        )


def _require_pair(
    writer: str | None,
    reviewer: str | None,
    field: str,
) -> bool:
    if (writer is None) != (reviewer is None):
        _fail(field, "writer and reviewer must be both present or both absent")
    return writer is not None


def _validate_routes(
    graph: GraphSpec,
    by_id: Mapping[str, NodeRef],
    signatures: set[tuple[str, str, EdgeKind]],
    task_ids: frozenset[str],
) -> None:
    route_ids = tuple(route.task_id for route in graph.routes)
    if len(set(route_ids)) != len(route_ids):
        _fail("routes", "must not contain duplicate task IDs")
    if set(route_ids) != set(task_ids):
        missing = sorted(set(task_ids) - set(route_ids))
        unknown = sorted(set(route_ids) - set(task_ids))
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        _fail(
            "routes", "must match the task catalog exactly (" + "; ".join(details) + ")"
        )

    for index, route in enumerate(graph.routes):
        has_plan = _require_pair(
            route.plan_writer,
            route.plan_reviewer,
            f"routes[{index}].plan",
        )
        has_implementation = _require_pair(
            route.implementation_writer,
            route.implementation_reviewer,
            f"routes[{index}].implementation",
        )
        if not has_plan and not has_implementation:
            _fail(f"routes[{index}]", "must contain at least one writer/reviewer pair")

        if has_plan:
            assert route.plan_writer is not None
            assert route.plan_reviewer is not None
            _require_route_node(
                route.plan_writer,
                Role.PLANNER,
                by_id,
                f"routes[{index}].plan_writer",
            )
            _require_route_node(
                route.plan_reviewer,
                Role.REVIEWER,
                by_id,
                f"routes[{index}].plan_reviewer",
            )
            _require_review_edge(
                route.plan_writer,
                route.plan_reviewer,
                signatures,
                f"routes[{index}].plan",
            )

        if has_implementation:
            assert route.implementation_writer is not None
            assert route.implementation_reviewer is not None
            _require_route_node(
                route.implementation_writer,
                Role.WORKER,
                by_id,
                f"routes[{index}].implementation_writer",
            )
            _require_route_node(
                route.implementation_reviewer,
                Role.REVIEWER,
                by_id,
                f"routes[{index}].implementation_reviewer",
            )
            _require_review_edge(
                route.implementation_writer,
                route.implementation_reviewer,
                signatures,
                f"routes[{index}].implementation",
            )

        if has_plan and has_implementation:
            assert route.plan_writer is not None
            assert route.implementation_writer is not None
            if (
                route.plan_writer,
                route.implementation_writer,
                "delegates-to",
            ) not in signatures:
                _fail(
                    f"routes[{index}]",
                    "plan-to-implementation delegates-to edge is required",
                )


def _require_route_node(
    node_id: str,
    expected_kind: Role,
    by_id: Mapping[str, NodeRef],
    field: str,
) -> None:
    if node_id not in by_id:
        _fail(field, f"references undeclared node: {node_id}")
    if by_id[node_id].kind is not expected_kind:
        _fail(
            field,
            f"must reference a {expected_kind.value} node: {node_id}",
        )


def _require_review_edge(
    writer: str,
    reviewer: str,
    signatures: set[tuple[str, str, EdgeKind]],
    field: str,
) -> None:
    if (writer, reviewer, "reviewed-by") not in signatures:
        _fail(field, f"requires reviewed-by edge from {writer} to {reviewer}")


def validate_graph(graph: GraphSpec, task_specs: tuple[TaskSpec, ...]) -> None:
    """Validate a graph and its exact TaskSpec routing catalog.

    The validator is pure: it only examines immutable values and raises
    :class:`NamedGraphError` (a ``ValueError``) on the first invalid contract.
    Consultations contribute their target as a communication leaf for
    reachability, but never cause traversal through that target.
    """

    if not isinstance(graph, GraphSpec):
        _fail("graph", "must be a GraphSpec")
    task_ids = _validate_catalog(task_specs)
    by_id = _validate_nodes(graph)
    entries = _validate_coordination(graph, by_id)
    outgoing, signatures = _validate_edges(graph, by_id)
    _validate_automatic_dag(by_id, signatures)
    _validate_reachability(by_id, entries, outgoing)
    _validate_routes(graph, by_id, signatures, task_ids)
