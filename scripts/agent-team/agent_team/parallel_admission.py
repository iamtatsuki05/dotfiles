"""Pure admission checks for the native ``program``/``parallel`` slice.

The caller owns graph dependency, wave, revision, permission, and state
mutation checks.  This module only answers whether a new assignment can be
reserved against the current canonical ``roles`` mapping.  An assignment
remains an occupant until its completion delivery is acknowledged, including
after the delivery has been released.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping

from .contracts import NodeRef, Role
from .named_graph import GraphSpec
from .task_spec import TaskSpec

_MAX_REASON_CHARS = 512


def _require_non_empty_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _catalog_by_id(catalog: tuple[TaskSpec, ...]) -> dict[str, TaskSpec]:
    if not isinstance(catalog, tuple):
        raise TypeError("catalog must be an immutable tuple")
    result: dict[str, TaskSpec] = {}
    for index, item in enumerate(catalog):
        if not isinstance(item, TaskSpec):
            raise TypeError(f"catalog[{index}] must be a TaskSpec")
        if item.task_id in result:
            raise ValueError(f"catalog contains duplicate task_id: {item.task_id}")
        result[item.task_id] = item
    return result


def _target_node(graph: GraphSpec, target: NodeRef) -> NodeRef:
    if not isinstance(graph, GraphSpec):
        raise TypeError("graph must be a GraphSpec")
    if not isinstance(target, NodeRef):
        raise TypeError("target must be a NodeRef")
    try:
        declared = graph.node(target.node_id)
    except KeyError as exc:
        raise ValueError(f"target node is undeclared: {target.node_id}") from exc
    if declared != target:
        raise ValueError(f"target node identity mismatch: {target.node_id}")
    return declared


def _validate_coordination(graph: GraphSpec) -> None:
    coordination = graph.coordination
    if coordination.mode != "program" or coordination.dispatch_mode != "parallel":
        raise ValueError("parallel program coordination is required")
    if (
        not isinstance(coordination.max_active, int)
        or isinstance(coordination.max_active, bool)
        or coordination.max_active < 1
    ):
        raise ValueError("coordination.max_active must be a positive integer")


def _assignment_task(
    assignment: Mapping[str, object],
    *,
    node_id: str,
    catalog: Mapping[str, TaskSpec],
) -> TaskSpec:
    """Validate the identity fields needed by admission.

    ``task_id`` is the provider/runner identity.  The logical TaskSpec key is
    read from the canonical ``task_spec`` payload; falling back to ``task_id``
    would let an opaque provider token select a different write scope.
    """

    _require_non_empty_text(
        assignment.get("task_id"),
        f"assignments[{node_id}].task_id",
    )
    if "task_spec" not in assignment:
        raise ValueError(f"assignments[{node_id}].task_spec is missing")
    try:
        assigned_task = TaskSpec.from_dict(assignment["task_spec"])
    except ValueError as exc:
        raise ValueError(f"assignments[{node_id}].task_spec is invalid") from exc

    task = catalog.get(assigned_task.task_id)
    if task is None:
        raise ValueError(
            f"assignments[{node_id}] references undeclared logical task: "
            f"{assigned_task.task_id}"
        )
    if task != assigned_task:
        raise ValueError(
            f"assignments[{node_id}].task_spec does not match catalog: "
            f"{assigned_task.task_id}"
        )
    return task


def _validated_assignments(
    graph: GraphSpec,
    assignments: Mapping[str, Mapping[str, object]],
    catalog: Mapping[str, TaskSpec],
) -> tuple[tuple[str, Role, TaskSpec], ...]:
    if not isinstance(assignments, Mapping):
        raise TypeError("assignments must be a mapping")

    validated: list[tuple[str, Role, TaskSpec]] = []
    for node_id, raw in assignments.items():
        if not isinstance(node_id, str) or not node_id:
            raise TypeError("assignment node IDs must be non-empty strings")
        if not isinstance(raw, Mapping):
            raise TypeError(f"assignments[{node_id}] must be a mapping")
        try:
            declared = graph.node(node_id)
        except KeyError as exc:
            raise ValueError(f"assignment node is undeclared: {node_id}") from exc

        role = raw.get("role")
        if role != node_id:
            raise ValueError(f"assignment node identity mismatch: {node_id}")
        role_kind = raw.get("role_kind")
        if not isinstance(role_kind, str):
            raise TypeError(f"assignments[{node_id}].role_kind must be a string")
        try:
            parsed_kind = Role(role_kind)
        except ValueError as exc:
            raise ValueError(
                f"assignments[{node_id}].role_kind is invalid: {role_kind!r}"
            ) from exc
        if declared.kind is not parsed_kind:
            raise ValueError(f"assignment role kind mismatch: {node_id}")
        task = _assignment_task(
            raw,
            node_id=node_id,
            catalog=catalog,
        )
        validated.append((node_id, parsed_kind, task))
    return tuple(sorted(validated, key=lambda item: item[0]))


def _path_key(path: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", path).removesuffix("/").casefold()
    return tuple(normalized.split("/"))


def _scope_keys(task: TaskSpec) -> tuple[tuple[str, ...], ...]:
    return tuple(_path_key(path) for path in task.allowed_paths)


def _is_ancestor_or_same(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and left == right[: len(left)]


def _scopes_overlap(
    left: tuple[tuple[str, ...], ...], right: tuple[tuple[str, ...], ...]
) -> bool:
    return any(
        _is_ancestor_or_same(left_path, right_path)
        or _is_ancestor_or_same(right_path, left_path)
        for left_path in left
        for right_path in right
    )


def _bounded_reason(reason: str) -> str:
    if len(reason) <= _MAX_REASON_CHARS:
        return reason
    return reason[:_MAX_REASON_CHARS]


def admission_blocker(
    graph: GraphSpec,
    catalog: tuple[TaskSpec, ...],
    assignments: Mapping[str, Mapping[str, object]],
    target: NodeRef,
    task: TaskSpec,
) -> str | None:
    """Return a stable busy reason, or ``None`` when admission is possible.

    The active assignment map is canonical state.  It is validated before any
    busy result is returned, so a malformed or incomplete assignment cannot be
    silently treated as available capacity.  Only Worker assignments carry a
    potential concurrent write scope; Planner and Reviewer assignments still
    consume the configured active slot and node identity.
    """

    declared_target = _target_node(graph, target)
    _validate_coordination(graph)
    by_task = _catalog_by_id(catalog)
    if not isinstance(task, TaskSpec):
        raise TypeError("task must be a TaskSpec")
    declared_task = by_task.get(task.task_id)
    if declared_task is None:
        raise ValueError(f"target task is undeclared: {task.task_id}")
    if declared_task != task:
        raise ValueError(f"target TaskSpec differs from catalog: {task.task_id}")

    active = _validated_assignments(graph, assignments, by_task)
    if len(active) > graph.coordination.max_active:
        raise ValueError("active assignments exceed coordination.max_active")

    for node_id, _kind, active_task in active:
        if node_id == declared_target.node_id:
            return _bounded_reason(
                "same_node_busy:"
                f"node_id={node_id}:logical_task_id={active_task.task_id}"
            )

    if len(active) == graph.coordination.max_active:
        return _bounded_reason(
            "max_active_reached:"
            f"node_id={declared_target.node_id}:logical_task_id={task.task_id}"
        )

    if declared_target.kind is not Role.WORKER:
        return None
    target_scope = _scope_keys(task)
    if not target_scope:
        return None

    for node_id, kind, active_task in active:
        if kind is not Role.WORKER:
            continue
        active_scope = _scope_keys(active_task)
        if _scopes_overlap(target_scope, active_scope):
            return _bounded_reason(
                "write_scope_conflict:"
                f"node_id={node_id}:logical_task_id={active_task.task_id}:"
                f"target_node_id={declared_target.node_id}:"
                f"target_logical_task_id={task.task_id}"
            )
    return None
