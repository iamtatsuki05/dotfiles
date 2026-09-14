"""Durable TaskSpec, review, and verification-admission transitions."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import NoReturn, cast

from .contracts import (
    ErrorCode,
    NodeRef,
    Role,
    RuntimeFailure,
    TaskDispatch,
    role_kind,
)
from .named_graph import GraphSpec, TaskRoute, validate_graph
from .task_spec import TaskSpec, parse_task_specs

_WRITER_ROLES = frozenset({Role.PLANNER, Role.WORKER})
_REVIEW_STAGES = frozenset({"plan", "implementation"})
_REVIEW_DECISIONS = frozenset({"approve", "request_changes", "consult"})
_VERIFICATION_KEYS = frozenset(
    {"revision", "passed", "commands", "error", "cleanup_confirmed"}
)
_VERIFICATION_COMMAND_KEYS = frozenset(
    {
        "name",
        "argv",
        "timeout_seconds",
        "returncode",
        "stdout_sha256",
        "stderr_sha256",
        "error",
    }
)
_MAX_VERIFICATION_ERROR_CHARS = 512
_MAX_CONSULTATION_ANSWER_CHARS = 16000
_TASK_STATUSES = frozenset(
    {
        "running",
        "awaiting_plan_review",
        "reviewing_plan",
        "plan_approved",
        "plan_changes_requested",
        "awaiting_implementation_review",
        "reviewing_implementation",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
        "verifying",
        "completed",
        "verification_failed",
        "failed",
    }
)


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise RuntimeFailure(code, message)


def task_json(task: TaskSpec) -> str:
    return json.dumps(task.as_dict(), ensure_ascii=False, sort_keys=True)


def task_digest(task: TaskSpec) -> str:
    return hashlib.sha256(task_json(task).encode("utf-8")).hexdigest()


def _max_review_rounds(state: Mapping[str, object]) -> int:
    value = state.get("max_review_rounds")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail(ErrorCode.INVALID_REQUEST, "max_review_rounds must be a positive integer")
    return value


def _mutable_tasks(state: dict[str, object]) -> dict[str, object]:
    tasks = state.get("tasks")
    if not isinstance(tasks, dict):
        _fail(ErrorCode.INVALID_REQUEST, "saved tasks are invalid")
    return tasks


def _require_message(request: TaskDispatch) -> str:
    if not isinstance(request.message, str) or not request.message.strip():
        _fail(ErrorCode.INVALID_REQUEST, "task dispatch message must be non-empty")
    return request.message


def _require_record_task(record: Mapping[str, object], task: TaskSpec) -> None:
    if record.get("spec") != task.as_dict():
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec cannot be replaced")
    if record.get("digest") != task_digest(task):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec digest does not match")


def _consultation_id(state: Mapping[str, object], record: Mapping[str, object]) -> str:
    review = record.get("review_result")
    evidence = review.get("task_evidence") if isinstance(review, Mapping) else None
    if not isinstance(review, Mapping) or not isinstance(evidence, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "consultation review evidence is missing")
    identity = [
        state.get("run_id"),
        record.get("digest"),
        evidence.get("stage"),
        review.get("dispatch_id"),
    ]
    if (
        any(not isinstance(item, str) or not item for item in identity)
        or evidence.get("stage") not in _REVIEW_STAGES
        or evidence.get("decision") != "consult"
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "consultation review identity is invalid")
    return (
        "consult-"
        + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )


def _valid_consultation_body(body: object) -> bool:
    return (
        isinstance(body, str)
        and bool(body.strip())
        and len(body) <= _MAX_CONSULTATION_ANSWER_CHARS
        and all(char.isprintable() or char in "\n\r\t" for char in body)
    )


def _validate_consultation_answer(
    state: Mapping[str, object], record: Mapping[str, object]
) -> None:
    if "consultation_answer" not in record:
        return
    answer = record["consultation_answer"]
    if not isinstance(answer, Mapping) or set(answer) != {
        "consultation_id",
        "body",
        "body_sha256",
    }:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved consultation answer is invalid")
    body = answer.get("body")
    if (
        not _valid_consultation_body(body)
        or answer.get("consultation_id") != _consultation_id(state, record)
        or answer.get("body_sha256")
        != hashlib.sha256(cast(str, body).encode("utf-8")).hexdigest()
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "saved consultation answer binding is invalid"
        )


def task_consultation(
    state: Mapping[str, object], record: Mapping[str, object]
) -> dict[str, object] | None:
    if record.get("status") != "consultation_required":
        return None
    consultation_id = _consultation_id(state, record)
    _validate_consultation_answer(state, record)
    review = cast(Mapping[str, object], record["review_result"])
    evidence = cast(Mapping[str, object], review["task_evidence"])
    return {
        "consultation_id": consultation_id,
        "stage": evidence["stage"],
        "findings": evidence["findings"],
        "answered": "consultation_answer" in record,
    }


def answer_task_consultation(
    state: dict[str, object], consultation_id: str, body: str
) -> dict[str, object]:
    validate_saved_tasks(state)
    if not _named_state(state):
        _fail(
            ErrorCode.INVALID_REQUEST, "consultation answers require a named task graph"
        )
    if (
        not isinstance(consultation_id, str)
        or not consultation_id
        or not _valid_consultation_body(body)
    ):
        _fail(
            ErrorCode.INVALID_REQUEST,
            "consultation answer requires an ID and bounded body",
        )
    for value in _mutable_tasks(state).values():
        if not isinstance(value, dict):
            continue
        pending = task_consultation(state, value)
        if pending is None or pending["consultation_id"] != consultation_id:
            continue
        answer = {
            "consultation_id": consultation_id,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
        if "consultation_answer" in value and value["consultation_answer"] != answer:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "consultation already has a different saved answer",
            )
        value["consultation_answer"] = answer
        return value
    _fail(
        ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
        "consultation does not match a pending review",
    )


def _consultation_answered(
    state: Mapping[str, object], record: Mapping[str, object]
) -> bool:
    pending = task_consultation(state, record)
    return pending is not None and pending["answered"] is True


def _review_rounds(record: Mapping[str, object]) -> dict[str, int]:
    raw = record.get("review_rounds")
    if not isinstance(raw, Mapping) or set(raw) != {"plan", "implementation"}:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved review rounds are invalid")
    rounds: dict[str, int] = {}
    for stage in ("plan", "implementation"):
        value = raw.get(stage)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved review rounds are invalid")
        rounds[stage] = value
    return rounds


def _result_body(record: Mapping[str, object]) -> str:
    result = record.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("body"), str):
        _fail(ErrorCode.ORDER_VIOLATION, "review requires the previous result body")
    return cast(str, result["body"])


def _named_state(state: Mapping[str, object]) -> bool:
    """Return whether ``state`` uses the explicit named-graph contract."""

    return state.get("version") in {4, 5}


def _legacy_role(target: object) -> Role:
    if isinstance(target, Role):
        return target
    _fail(ErrorCode.ORDER_VIOLATION, "legacy state requires a fixed Role target")
    raise AssertionError("unreachable")


def _named_context(
    state: Mapping[str, object],
) -> tuple[GraphSpec, tuple[TaskSpec, ...]]:
    """Load and cross-check a named graph and its TaskSpec catalog.

    Legacy state3 callers bypass this path and retain the fixed Role-only
    contract.
    """

    if not _named_state(state):
        _fail(ErrorCode.INVALID_REQUEST, "named graph is not enabled for this state")
    raw_graph = state.get("graph")
    if not isinstance(raw_graph, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "named state graph is missing")
    role_specs = state.get("role_specs")
    if not isinstance(role_specs, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "named state role_specs are missing")
    try:
        graph = GraphSpec.from_dict(raw_graph)
        catalog = parse_task_specs(state.get("task_specs"))
        validate_graph(graph, catalog)
    except RuntimeFailure:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "saved named graph or TaskSpec catalog is invalid",
        ) from exc
    if state.get("version") == 4 and graph.coordination.dispatch_mode != "serial":
        _fail(
            ErrorCode.INVALID_REQUEST,
            "named TaskSpec routing currently requires serial dispatch",
        )

    if state.get("version") == 5 and graph.coordination.dispatch_mode != "parallel":
        _fail(
            ErrorCode.INVALID_REQUEST,
            "parallel TaskSpec state requires parallel coordination",
        )

    node_ids = {node.node_id for node in graph.nodes}
    if set(role_specs) != node_ids:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "saved role_specs do not match the named graph nodes",
        )
    for node in graph.nodes:
        raw_spec = role_specs.get(node.node_id)
        if not isinstance(raw_spec, Mapping) or raw_spec.get("kind") != node.kind.value:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                f"saved role spec kind does not match node {node.node_id}",
            )
    return graph, catalog


def _named_graph(state: Mapping[str, object]) -> GraphSpec:
    graph, _catalog = _named_context(state)
    return graph


def _named_node(state: Mapping[str, object], node_id: object) -> NodeRef:
    graph = _named_graph(state)
    if not isinstance(node_id, str) or not node_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, "named node identity is missing")
    try:
        node = graph.node(node_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "named node is not in the saved graph"
        ) from exc
    if not isinstance(node, NodeRef):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved named node is invalid")
    return node


def _named_target(state: Mapping[str, object], target: object) -> NodeRef:
    if not isinstance(target, NodeRef):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "state version 4 requires an exact named node target",
        )
    node = _named_node(state, target.node_id)
    if node != target:
        _fail(ErrorCode.IDENTITY_MISMATCH, "named node identity or kind does not match")
    return node


def _named_route(state: Mapping[str, object], task_id: str) -> TaskRoute:
    graph = _named_graph(state)
    try:
        return graph.route(task_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "TaskSpec has no exact named route",
        ) from exc


def _route_node(
    state: Mapping[str, object], route: TaskRoute, field: str, *, required: bool
) -> NodeRef | None:
    if field == "plan_writer":
        node_id = route.plan_writer
    elif field == "plan_reviewer":
        node_id = route.plan_reviewer
    elif field == "implementation_writer":
        node_id = route.implementation_writer
    elif field == "implementation_reviewer":
        node_id = route.implementation_reviewer
    else:
        _fail(ErrorCode.INVALID_REQUEST, "named route field is invalid")
    if node_id is None:
        if required:
            _fail(
                ErrorCode.ORDER_VIOLATION,
                f"named route has no {field} for this stage",
            )
        return None
    return _named_node(state, node_id)


def _require_named_writer_route(
    state: Mapping[str, object],
    task: TaskSpec,
    target: object,
    *,
    initial: bool = False,
) -> NodeRef:
    node = _named_target(state, target)
    route = _named_route(state, task.task_id)
    if node.kind is Role.PLANNER:
        expected = _route_node(state, route, "plan_writer", required=True)
    elif node.kind is Role.WORKER:
        expected = _route_node(state, route, "implementation_writer", required=True)
    else:
        _fail(ErrorCode.ORDER_VIOLATION, "only a named writer may receive a TaskSpec")
    assert expected is not None
    if initial and node.kind is Role.WORKER and route.plan_writer is not None:
        _fail(
            ErrorCode.ORDER_VIOLATION,
            "named TaskSpec requires plan review before implementation",
        )
    if node != expected:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "TaskSpec writer does not match its exact named route",
        )
    return node


def _require_named_reviewer_route(
    state: Mapping[str, object], task: TaskSpec, stage: str, target: object
) -> NodeRef:
    node = _named_target(state, target)
    if node.kind is not Role.REVIEWER:
        _fail(ErrorCode.IDENTITY_MISMATCH, "TaskSpec reviewer must be a reviewer node")
    route = _named_route(state, task.task_id)
    field = "plan_reviewer" if stage == "plan" else "implementation_reviewer"
    expected = _route_node(state, route, field, required=True)
    assert expected is not None
    if node != expected:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "TaskSpec reviewer does not match its exact named route",
        )
    return node


def _record_target(record: Mapping[str, object], field: str) -> NodeRef:
    node_id = record.get(field)
    kind_field = "writer_kind" if field == "writer_role" else f"{field}_kind"
    raw_kind = record.get(kind_field)
    if not isinstance(node_id, str) or not node_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"saved {field} identity is missing")
    if not isinstance(raw_kind, str):
        _fail(ErrorCode.IDENTITY_MISMATCH, f"saved {field} kind is missing")
    try:
        kind = Role(raw_kind)
        return NodeRef(node_id=node_id, kind=kind)
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, f"saved {field} identity is invalid"
        ) from exc


def _set_record_target(record: dict[str, object], field: str, target: NodeRef) -> None:
    record[field] = target.node_id
    kind_field = "writer_kind" if field == "writer_role" else f"{field}_kind"
    record[kind_field] = target.kind.value


def _result_target(
    state: Mapping[str, object], result: Mapping[str, object]
) -> NodeRef:
    raw_id = result.get("role")
    raw_kind = result.get("role_kind")
    if not isinstance(raw_id, str) or not isinstance(raw_kind, str):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "named task result requires role and role_kind",
        )
    try:
        target = NodeRef(raw_id, Role(raw_kind))
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "named task result identity is invalid"
        ) from exc
    return _named_target(state, target)


def _validate_named_record(
    state: Mapping[str, object], task: TaskSpec, record: Mapping[str, object]
) -> None:
    """Verify saved ID/kind fields and stage route before a state transition."""

    role = _record_target(record, "role")
    writer = _record_target(record, "writer_role")
    role = _named_node(state, role.node_id)
    saved_role = _record_target(record, "role")
    if role != saved_role:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved role kind does not match the graph")
    saved_writer = _record_target(record, "writer_role")
    writer = _named_node(state, writer.node_id)
    if writer != saved_writer:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "saved writer kind does not match the graph",
        )

    status = record.get("status")
    stage = record.get("stage")
    if stage not in _REVIEW_STAGES:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved named TaskSpec stage is invalid")
    route = _named_route(state, task.task_id)

    if status == "running":
        if writer.kind not in _WRITER_ROLES or role != writer:
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "saved running writer identity is invalid"
            )
        expected = _route_node(
            state,
            route,
            "plan_writer" if writer.kind is Role.PLANNER else "implementation_writer",
            required=True,
        )
        assert expected is not None
        if writer != expected:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "saved writer is not the exact route target",
            )
        expected_stage = "plan" if writer.kind is Role.PLANNER else "implementation"
        if stage != expected_stage:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved writer stage is invalid")
        return

    if status in {
        "awaiting_plan_review",
        "reviewing_plan",
        "plan_approved",
        "plan_changes_requested",
    } or (
        is_plan_only(state, task.task_id)
        and status in {"verifying", "completed", "verification_failed"}
    ):
        expected_writer = _route_node(state, route, "plan_writer", required=True)
        expected_reviewer = _route_node(state, route, "plan_reviewer", required=True)
        assert expected_writer is not None and expected_reviewer is not None
        expected_role = (
            expected_writer if status == "awaiting_plan_review" else expected_reviewer
        )
        if writer != expected_writer or role != expected_role or stage != "plan":
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved plan route identity is invalid")
        return

    if status in {
        "awaiting_implementation_review",
        "reviewing_implementation",
        "implementation_approved",
        "implementation_changes_requested",
        "verifying",
        "completed",
        "verification_failed",
    }:
        expected_writer = _route_node(
            state, route, "implementation_writer", required=True
        )
        expected_reviewer = _route_node(
            state, route, "implementation_reviewer", required=True
        )
        assert expected_writer is not None and expected_reviewer is not None
        expected_role = (
            expected_writer
            if status == "awaiting_implementation_review"
            else expected_reviewer
        )
        if (
            writer != expected_writer
            or role != expected_role
            or stage != "implementation"
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "saved implementation route identity is invalid",
            )
        return

    if status == "consultation_required":
        expected_reviewer = _route_node(
            state,
            route,
            "plan_reviewer" if stage == "plan" else "implementation_reviewer",
            required=True,
        )
        assert expected_reviewer is not None
        expected_writer = _route_node(
            state,
            route,
            "plan_writer" if stage == "plan" else "implementation_writer",
            required=True,
        )
        assert expected_writer is not None
        if role != expected_reviewer or writer != expected_writer:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "saved consultation route identity is invalid",
            )
        return

    if status == "failed":
        if stage in _REVIEW_STAGES:
            expected_writer = _route_node(
                state,
                route,
                "plan_writer" if stage == "plan" else "implementation_writer",
                required=True,
            )
            expected_reviewer = _route_node(
                state,
                route,
                "plan_reviewer" if stage == "plan" else "implementation_reviewer",
                required=True,
            )
            assert expected_writer is not None and expected_reviewer is not None
            if writer != expected_writer or role not in {
                expected_writer,
                expected_reviewer,
            }:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "saved failed route identity is invalid",
                )
        return

    _fail(ErrorCode.IDENTITY_MISMATCH, "saved named TaskSpec status is invalid")


def _validate_named_rounds(
    record: Mapping[str, object], rounds: Mapping[str, int]
) -> None:
    status = record.get("status")
    stage = record.get("stage")
    if not isinstance(stage, str) or stage not in _REVIEW_STAGES:
        return
    consumed_statuses = {
        "reviewing_plan",
        "plan_approved",
        "plan_changes_requested",
        "reviewing_implementation",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
        "verifying",
        "completed",
        "verification_failed",
    }
    evidence = record.get("task_evidence")
    evidence_stage = evidence.get("stage") if isinstance(evidence, Mapping) else None
    requires_round = status in consumed_statuses or evidence_stage == stage
    if requires_round and rounds[stage] < 1:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            f"saved {stage} review round is inconsistent with its status",
        )


def _validate_named_plan_revision(
    record: Mapping[str, object],
) -> None:
    stage = record.get("stage")
    status = record.get("status")
    if stage != "plan" or status in {"failed"}:
        return
    revision = record.get("revision")
    if status == "running" and revision is None:
        return
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved plan revision is missing")
    writer_result = record.get("writer_result")
    if not isinstance(writer_result, Mapping) or not isinstance(
        writer_result.get("body"), str
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved plan writer result is missing")
    expected = hashlib.sha256(
        cast(str, writer_result["body"]).encode("utf-8")
    ).hexdigest()
    if revision != expected:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved plan revision does not match result")


def is_plan_only(state: Mapping[str, object], task_id: str) -> bool:
    return (
        _named_state(state)
        and _named_route(state, task_id).implementation_writer is None
    )


def verification_revision(record: Mapping[str, object]) -> str:
    if record.get("stage") == "plan":
        return _require_sha256(
            record.get("workspace_revision"), "approved workspace revision"
        )
    revision = record.get("revision")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "approved workspace revision is missing")
    return revision


def _validate_workspace_revision(
    state: Mapping[str, object], task: TaskSpec, record: Mapping[str, object]
) -> None:
    revision = record.get("workspace_revision")
    if not is_plan_only(state, task.task_id):
        if "workspace_revision" in record:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "workspace_revision belongs only to a plan-only task",
            )
        return
    if record.get("status") in {"running", "awaiting_plan_review"}:
        if revision is not None:
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "plan-only workspace revision is premature"
            )
    elif record.get("status") != "failed" or revision is not None:
        _require_sha256(revision, "plan-only workspace revision")


def _validate_named_review_evidence(
    state: Mapping[str, object],
    task: TaskSpec,
    record: Mapping[str, object],
    *,
    expected_decision: str,
) -> None:
    evidence = record.get("task_evidence")
    review_result = record.get("review_result")
    if not isinstance(evidence, Mapping) or not isinstance(review_result, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved review evidence is missing")
    if review_result.get("task_evidence") != dict(evidence):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved review evidence is inconsistent")
    if _result_target(state, review_result) != _record_target(record, "role"):
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "saved reviewer evidence identity is invalid"
        )
    stage = record.get("stage")
    revision = record.get("revision")
    if stage not in _REVIEW_STAGES or not isinstance(revision, str) or not revision:
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "saved review evidence binding is incomplete"
        )
    try:
        verdict = parse_review(
            json.dumps(dict(evidence), ensure_ascii=False, separators=(",", ":")),
            task=task,
            stage=stage,
            revision=revision,
        )
    except (TypeError, ValueError, RuntimeFailure) as exc:
        if isinstance(exc, RuntimeFailure) and exc.code is ErrorCode.IDENTITY_MISMATCH:
            raise
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "saved review evidence is invalid"
        ) from exc
    if verdict["decision"] != expected_decision:
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "saved review decision does not match status"
        )


def _validate_named_result(
    state: Mapping[str, object],
    result: object,
    expected_target: NodeRef,
    *,
    expected_dispatch: str | None = None,
    expected_outcome: str = "succeeded",
) -> tuple[str, str, str]:
    if not isinstance(result, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved task result is missing")
    if _result_target(state, result) != expected_target:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved task result identity is invalid")
    body = result.get("body")
    outcome = result.get("outcome")
    dispatch_id = result.get("dispatch_id")
    if not isinstance(body, str) or not isinstance(outcome, str):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved task result fields are invalid")
    if outcome != expected_outcome:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved task result outcome is invalid")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved task result dispatch is missing")
    if expected_dispatch is not None and dispatch_id != expected_dispatch:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved task result dispatch is invalid")
    return body, outcome, dispatch_id


def _validate_named_result_integrity(
    state: Mapping[str, object], task: TaskSpec, record: Mapping[str, object]
) -> None:
    status = record.get("status")
    route = _named_route(state, task.task_id)
    writer_field = (
        "plan_writer" if record.get("stage") == "plan" else "implementation_writer"
    )
    expected_writer = _route_node(state, route, writer_field, required=True)
    assert expected_writer is not None
    writer_statuses = {
        "awaiting_plan_review",
        "reviewing_plan",
        "awaiting_implementation_review",
        "reviewing_implementation",
    }
    review_statuses = {
        "plan_approved",
        "plan_changes_requested",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
        "verifying",
        "completed",
        "verification_failed",
    }
    if status in writer_statuses:
        status_text = status
        writer_dispatch = (
            record.get("dispatch_id")
            if status_text.startswith("awaiting_")
            else record.get("review_source_dispatch_id")
        )
        if not isinstance(writer_dispatch, str) or not writer_dispatch:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved writer dispatch is missing")
        writer_values = _validate_named_result(
            state,
            record.get("writer_result"),
            expected_writer,
            expected_dispatch=writer_dispatch,
        )
        result_values = _validate_named_result(
            state,
            record.get("result"),
            expected_writer,
            expected_dispatch=writer_dispatch,
        )
        if writer_values != result_values:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved writer result is inconsistent")
        return
    if status in review_statuses:
        expected_reviewer = _route_node(
            state,
            route,
            "plan_reviewer"
            if record.get("stage") == "plan"
            else "implementation_reviewer",
            required=True,
        )
        assert expected_reviewer is not None
        dispatch_id = record.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved reviewer dispatch is missing")
        writer_dispatch = record.get("review_source_dispatch_id")
        if not isinstance(writer_dispatch, str) or not writer_dispatch:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved writer dispatch is missing")
        writer_values = _validate_named_result(
            state,
            record.get("writer_result"),
            expected_writer,
            expected_dispatch=writer_dispatch,
        )
        review_values = _validate_named_result(
            state,
            record.get("review_result"),
            expected_reviewer,
            expected_dispatch=dispatch_id,
        )
        result_values = _validate_named_result(
            state,
            record.get("result"),
            expected_reviewer,
            expected_dispatch=dispatch_id,
        )
        if review_values != result_values:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved reviewer result is inconsistent")
        if not writer_values[0]:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved writer result body is invalid")
        return
    if status == "failed":
        role = _record_target(record, "role")
        failed_dispatch = record.get("dispatch_id")
        if not isinstance(failed_dispatch, str) or not failed_dispatch:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved failed dispatch is missing")
        _validate_named_result(
            state,
            record.get("result"),
            role,
            expected_outcome="failed",
            expected_dispatch=failed_dispatch,
        )


def _task_prompt(task: TaskSpec, message: str) -> str:
    return (
        "次のTaskSpecを遵守してください。完了条件・変更範囲・相談条件はこの仕様に従います。\n"
        + task_json(task)
        + "\n\n追加の依頼:\n"
        + message
    )


def review_prompt(
    task: TaskSpec,
    *,
    stage: str,
    revision: str,
    result_body: str,
    message: str = "",
) -> str:
    """Render a reviewer prompt with one exact JSON output contract."""

    if stage not in _REVIEW_STAGES:
        _fail(ErrorCode.INVALID_REQUEST, "review stage is invalid")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.INVALID_REQUEST, "review revision must be non-empty")
    if not isinstance(result_body, str):
        _fail(ErrorCode.INVALID_REQUEST, "review result body must be a string")
    if not isinstance(message, str):
        _fail(ErrorCode.INVALID_REQUEST, "review message must be a string")
    template = {
        "task_id": task.task_id,
        "stage": stage,
        "revision": revision,
        "decision": "approve",
        "findings": [],
    }
    supplement = f"\n\n追加の依頼:\n{message}" if message else ""
    return (
        "前段resultbody:\n"
        + result_body
        + "\n\nTaskSpec:\n"
        + task_json(task)
        + supplement
        + "\n\nレビュー結果は次のキーを持つJSON objectだけを出力してください。"
        "説明文やMarkdownを追加してはいけません。decisionはapprove、"
        "request_changes、consultのいずれかです。request_changesとconsultでは"
        "findingsを1件以上記載してください。\n"
        + json.dumps(template, ensure_ascii=False, separators=(",", ":"))
    )


def _decode_review(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail(ErrorCode.INVALID_REQUEST, "review verdict must be an object")
    keys = tuple(value)
    expected = {"task_id", "stage", "revision", "decision", "findings"}
    if any(not isinstance(key, str) for key in keys):
        _fail(ErrorCode.INVALID_REQUEST, "review verdict keys must be strings")
    if set(keys) != expected or len(keys) != len(set(keys)):
        _fail(ErrorCode.INVALID_REQUEST, "review verdict keys are not exact")
    return {cast(str, key): item for key, item in value.items()}


def parse_review(
    output: str, *, task: TaskSpec, stage: str, revision: str
) -> dict[str, object]:
    """Parse and bind the only accepted reviewer output."""

    if stage not in _REVIEW_STAGES:
        _fail(ErrorCode.INVALID_REQUEST, "review stage is invalid")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.INVALID_REQUEST, "review revision must be non-empty")

    def pairs(pairs_value: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs_value:
            if key in parsed:
                _fail(
                    ErrorCode.INVALID_REQUEST,
                    "review verdict contains duplicate keys",
                )
            parsed[key] = value
        return parsed

    try:
        decoded = json.loads(output, object_pairs_hook=pairs)
    except (TypeError, json.JSONDecodeError, RuntimeFailure) as exc:
        if isinstance(exc, RuntimeFailure):
            raise
        _fail(ErrorCode.INVALID_REQUEST, "review output must be one JSON object")
    verdict = _decode_review(decoded)
    if verdict.get("task_id") != task.task_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review task_id does not match TaskSpec")
    if verdict.get("stage") != stage:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review stage does not match assignment")
    if verdict.get("revision") != revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review revision does not match assignment")
    decision = verdict.get("decision")
    if not isinstance(decision, str) or decision not in _REVIEW_DECISIONS:
        _fail(ErrorCode.INVALID_REQUEST, "review decision is invalid")
    findings = verdict.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(item, str) or not item.strip() for item in findings
    ):
        _fail(ErrorCode.INVALID_REQUEST, "review findings must be a string array")
    if decision != "approve" and not findings:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "review findings are required for this decision",
        )
    return {
        "task_id": task.task_id,
        "stage": stage,
        "revision": revision,
        "decision": decision,
        "findings": list(findings),
    }


def _parse_saved_task(
    task_id: object,
    record: object,
    max_rounds: int,
    *,
    state: Mapping[str, object] | None = None,
) -> TaskSpec:
    if not isinstance(task_id, str) or not task_id or not isinstance(record, Mapping):
        _fail(ErrorCode.INVALID_REQUEST, "saved TaskSpec is invalid")
    task = TaskSpec.from_dict(record.get("spec"))
    if task.task_id != task_id or record.get("digest") != task_digest(task):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec identity is invalid")
    dispatch_id = record.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "saved TaskSpec dispatch identity is invalid",
        )
    status = record.get("status")
    if status not in _TASK_STATUSES:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec status is invalid")
    named = state is not None and _named_state(state)
    role = record.get("role")
    rounds = _review_rounds(record)
    if any(value > max_rounds for value in rounds.values()):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "saved review rounds exceed the configured limit",
        )
    stage = record.get("stage")
    if named:
        assert state is not None
        _validate_named_record(state, task, record)
    else:
        role = record.get("role")
        if role not in {item.value for item in Role}:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec role is invalid")
        if stage is not None and stage not in _REVIEW_STAGES:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec stage is invalid")
        writer_role = record.get("writer_role")
        if writer_role is not None and writer_role not in {
            Role.PLANNER.value,
            Role.WORKER.value,
        }:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec writer role is invalid")
        expected_role: str | None = None
        expected_stage: str | None = None
        if status == "running":
            expected_role = cast(str, writer_role) if writer_role is not None else role
            expected_stage = (
                "plan" if expected_role == Role.PLANNER.value else "implementation"
            )
        elif status == "awaiting_plan_review":
            expected_role, expected_stage = Role.PLANNER.value, "plan"
        elif status == "awaiting_implementation_review":
            expected_role, expected_stage = Role.WORKER.value, "implementation"
        elif status in {"reviewing_plan", "plan_approved", "plan_changes_requested"}:
            expected_role, expected_stage = Role.REVIEWER.value, "plan"
        elif status in {
            "reviewing_implementation",
            "implementation_approved",
            "implementation_changes_requested",
        }:
            expected_role, expected_stage = Role.REVIEWER.value, "implementation"
        elif status == "consultation_required":
            expected_role = Role.REVIEWER.value
        if expected_role is not None and role != expected_role:
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec role does not match status"
            )
        if expected_stage is not None and stage != expected_stage:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "saved TaskSpec stage does not match status",
            )
    revision = record.get("revision")
    if revision is not None and (not isinstance(revision, str) or not revision):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec revision is invalid")
    result = record.get("result")
    if result is not None and not isinstance(result, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec result is invalid")
    if status in {
        "awaiting_plan_review",
        "reviewing_plan",
        "plan_approved",
        "plan_changes_requested",
        "reviewing_implementation",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
    } and not isinstance(revision, str):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec review revision is missing")
    if status == "awaiting_implementation_review" and revision is not None:
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "implementation review revision is premature"
        )
    if status in {
        "awaiting_plan_review",
        "awaiting_implementation_review",
        "reviewing_plan",
        "reviewing_implementation",
        "plan_approved",
        "plan_changes_requested",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
    } and not isinstance(result, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec result is missing")
    if named:
        _validate_named_rounds(record, rounds)
        _validate_named_plan_revision(record)
        expected_decision = {
            "plan_approved": "approve",
            "plan_changes_requested": "request_changes",
            "implementation_approved": "approve",
            "implementation_changes_requested": "request_changes",
            "consultation_required": "consult",
            "verifying": "approve",
            "completed": "approve",
            "verification_failed": "approve",
        }.get(cast(str, status))
        if expected_decision is not None:
            assert state is not None
            _validate_named_review_evidence(
                state,
                task,
                record,
                expected_decision=expected_decision,
            )
        assert state is not None
        _validate_named_result_integrity(state, task, record)
        _validate_consultation_answer(state, record)
        _validate_workspace_revision(state, task, record)
    if status in {"completed", "verification_failed"}:
        _validate_verification(
            task,
            record,
            status=cast(str, status),
            require_complete=status == "completed",
        )
    return task


def _writer_status(status: object) -> Role | None:
    if status == "plan_approved":
        return Role.WORKER
    if status == "plan_changes_requested":
        return Role.PLANNER
    if status == "implementation_changes_requested":
        return Role.WORKER
    if status == "verification_failed":
        return Role.WORKER
    return None


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{field} is not a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{field} is not a SHA-256 digest")
    return value


def _require_bounded_error(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_VERIFICATION_ERROR_CHARS
        or not value.isprintable()
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{field} is not a bounded error")
    return value


def _validate_verification(
    task: TaskSpec,
    record: Mapping[str, object],
    *,
    status: str,
    require_complete: bool = True,
) -> Mapping[str, object]:
    revision = verification_revision(record)
    review_revision = record.get("revision")
    stage = record.get("stage")
    if (
        not isinstance(review_revision, str)
        or not review_revision
        or stage not in _REVIEW_STAGES
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "verified task review binding is missing")
    task_evidence = record.get("task_evidence")
    if not isinstance(task_evidence, Mapping):
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "implementation approval evidence is missing"
        )
    try:
        verdict = parse_review(
            json.dumps(dict(task_evidence), ensure_ascii=False),
            task=task,
            stage=stage,
            revision=review_revision,
        )
    except RuntimeFailure as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "implementation approval evidence is invalid",
        ) from exc
    if verdict["decision"] != "approve":
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "implementation approval evidence is not approved",
        )
    review_result = record.get("review_result")
    if not isinstance(review_result, Mapping) or review_result.get(
        "task_evidence"
    ) != dict(task_evidence):
        _fail(ErrorCode.IDENTITY_MISMATCH, "implementation review result is missing")
    writer_result = record.get("writer_result")
    if not isinstance(writer_result, Mapping) or not isinstance(
        writer_result.get("body"), str
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "implementation writer result is missing")

    verification = record.get("verification")
    if not isinstance(verification, Mapping) or set(verification) != _VERIFICATION_KEYS:
        _fail(ErrorCode.IDENTITY_MISMATCH, "verification evidence is incomplete")
    if verification.get("revision") != revision:
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "verification revision does not match approval"
        )
    passed = verification.get("passed")
    if not isinstance(passed, bool):
        _fail(ErrorCode.IDENTITY_MISMATCH, "verification passed field is invalid")
    cleanup_confirmed = verification.get("cleanup_confirmed")
    if not isinstance(cleanup_confirmed, bool):
        _fail(ErrorCode.IDENTITY_MISMATCH, "verification cleanup evidence is invalid")
    top_error = verification.get("error")
    if passed:
        if top_error is not None or cleanup_confirmed is not True:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "successful verification evidence is invalid",
            )
    elif not isinstance(top_error, str) or not top_error:
        _fail(ErrorCode.IDENTITY_MISMATCH, "failed verification evidence is incomplete")
    else:
        _require_bounded_error(top_error, "verification error")
    commands = verification.get("commands")
    if not isinstance(commands, list) or (
        len(commands) > len(task.verification)
        or require_complete
        and len(commands) != len(task.verification)
        or passed
        and (not require_complete or len(commands) != len(task.verification))
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "verification command evidence is incomplete"
        )
    for declared, observed in zip(task.verification, commands):
        if (
            not isinstance(observed, Mapping)
            or set(observed) != _VERIFICATION_COMMAND_KEYS
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "verification command evidence is invalid"
            )
        if (
            observed.get("name") != declared.name
            or observed.get("argv") != list(declared.argv)
            or observed.get("timeout_seconds") != declared.timeout_seconds
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "verification command binding is invalid"
            )
        returncode = observed.get("returncode")
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            _fail(ErrorCode.IDENTITY_MISMATCH, "verification returncode is invalid")
        _require_sha256(observed.get("stdout_sha256"), "verification stdout")
        _require_sha256(observed.get("stderr_sha256"), "verification stderr")
        command_error = observed.get("error")
        if passed:
            if returncode != 0 or command_error is not None:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "successful verification has a failed command",
                )
        elif returncode == 0:
            if command_error is not None:
                _fail(ErrorCode.IDENTITY_MISMATCH, "successful command has an error")
        elif not isinstance(command_error, str) or not command_error:
            _fail(ErrorCode.IDENTITY_MISMATCH, "failed command evidence is incomplete")
        else:
            _require_bounded_error(command_error, "verification command error")
    if status == "completed" and passed is not True:
        _fail(ErrorCode.IDENTITY_MISMATCH, "completed task has failed verification")
    if status == "verification_failed" and passed is not False:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "verification_failed task has no failed verification",
        )
    return verification


def _require_verification_retry(
    task: TaskSpec, record: Mapping[str, object], max_rounds: int
) -> None:
    _validate_verification(
        task,
        record,
        status="verification_failed",
        require_complete=False,
    )
    verification = cast(Mapping[str, object], record["verification"])
    if verification.get("cleanup_confirmed") is not True:
        _fail(
            ErrorCode.ORDER_VIOLATION,
            "verification cleanup is unconfirmed; user consultation required",
        )
    rounds = _review_rounds(record)
    if rounds[cast(str, record["stage"])] >= max_rounds:
        _fail(
            ErrorCode.ORDER_VIOLATION,
            "maximum review rounds reached; user consultation required",
        )


def _require_declared_task(state: Mapping[str, object], task: TaskSpec) -> None:
    if _named_state(state):
        _graph, declared = _named_context(state)
    else:
        declared = parse_task_specs(state.get("task_specs", []))
    match = next((item for item in declared if item.task_id == task.task_id), None)
    if match is None:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "TaskSpec must be declared by the user at startup",
        )
    if match != task:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "TaskSpec differs from the declared startup specification",
        )


def _require_agent_batch_drained(state: Mapping[str, object]) -> None:
    if state.get("roles") or state.get("pending_delivery_id") is not None:
        _fail(
            ErrorCode.BUSY,
            "consume all assignments and deliveries before changing the Main batch",
        )
    for record in cast(
        Mapping[str, Mapping[str, object]], state.get("tasks", {})
    ).values():
        verification = record.get("verification")
        if record.get("status") == "verifying" or (
            record.get("status") == "verification_failed"
            and isinstance(verification, Mapping)
            and verification.get("cleanup_confirmed") is not True
        ):
            _fail(
                ErrorCode.BUSY,
                "verification cleanup must be confirmed before changing the Main batch",
            )


def open_agent_batch(
    state: dict[str, object], task_ids: tuple[str, ...]
) -> Mapping[str, object]:
    if not is_agent_parallel(state):
        _fail(
            ErrorCode.INVALID_REQUEST,
            "task_batch_open requires agent parallel coordination",
        )
    validate_saved_tasks(state)
    _require_agent_batch_drained(state)
    if (
        not isinstance(task_ids, tuple)
        or not task_ids
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or len(set(task_ids)) != len(task_ids)
    ):
        _fail(
            ErrorCode.INVALID_REQUEST,
            "task_batch_open requires unique declared task IDs",
        )
    _graph, catalog = _named_context(state)
    ids = [task.task_id for task in catalog if task.task_id in task_ids]
    if len(ids) != len(task_ids):
        _fail(ErrorCode.INVALID_REQUEST, "Main batch contains an undeclared task")
    records = _mutable_tasks(state)
    if "agent_batch" in state:
        previous = agent_batch(state)
        if any(
            not isinstance(records.get(task_id), Mapping)
            or cast(Mapping[str, object], records[task_id]).get("status") != "completed"
            for task_id in cast(list[str], previous["task_ids"])
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "finish the current Main batch before opening another",
            )
    if any(task_id in records for task_id in ids):
        _fail(ErrorCode.ORDER_VIOLATION, "new Main batch requires unstarted tasks")
    for task in catalog:
        if task.task_id in ids and any(
            dependency in ids
            or not isinstance(records.get(dependency), Mapping)
            or cast(Mapping[str, object], records[dependency]).get("status")
            != "completed"
            for dependency in task.dependencies
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "Main batch dependencies must be completed outside the batch",
            )
    batch = {"task_ids": ids, "phase": "writers", "revision": None}
    agent_batch({**state, "agent_batch": batch})
    state["agent_batch"] = batch
    return batch


def prepare_dispatch(
    state: dict[str, object],
    request: TaskDispatch,
    *,
    revision: str | None = None,
    workspace_revision: str | None = None,
) -> tuple[dict[str, object], str]:
    if not is_agent_parallel(state):
        return _prepare_dispatch(
            state, request, revision=revision, workspace_revision=workspace_revision
        )
    validate_saved_tasks(state)
    if not isinstance(request, TaskDispatch) or not isinstance(request.task, TaskSpec):
        _fail(ErrorCode.INVALID_REQUEST, "TaskSpec is required")
    _require_declared_task(state, request.task)
    if "agent_batch" not in state:
        _fail(ErrorCode.ORDER_VIOLATION, "open a Main task batch before dispatch")
    batch = agent_batch(state)
    if request.task.task_id not in cast(list[str], batch["task_ids"]):
        _fail(ErrorCode.ORDER_VIOLATION, "task is outside the current Main batch")
    # A rejected route or prompt must not seal the batch or invalidate peer evidence.
    candidate = copy.deepcopy(state)
    record = _mutable_tasks(candidate).get(request.task.task_id)
    if role_kind(request.role) is Role.REVIEWER:
        final_review = is_plan_only(state, request.task.task_id) or (
            isinstance(record, Mapping) and record.get("stage") == "implementation"
        )
        if final_review and batch["phase"] == "writers":
            _require_agent_batch_drained(candidate)
            _transition_integration_wave(
                candidate,
                "agent_batch",
                "seal_wave",
                revision=workspace_revision
                if is_plan_only(state, request.task.task_id)
                else revision,
            )
    elif batch["phase"] != "writers":
        if not isinstance(record, Mapping) or record.get("status") not in {
            "plan_changes_requested",
            "implementation_changes_requested",
            "consultation_required",
            "verification_failed",
        }:
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "only a task needing revision may reopen the Main batch",
            )
        _require_agent_batch_drained(candidate)
        _transition_integration_wave(candidate, "agent_batch", "reopen_wave")
    prepared, prompt = _prepare_dispatch(
        candidate, request, revision=revision, workspace_revision=workspace_revision
    )
    state["tasks"] = candidate["tasks"]
    state["agent_batch"] = candidate["agent_batch"]
    return prepared, prompt


def prepare_agent_batch_verification(
    state: dict[str, object], task_id: str, revision: str
) -> None:
    if not is_agent_parallel(state):
        _fail(
            ErrorCode.INVALID_REQUEST,
            "Main batch verification requires agent parallel coordination",
        )
    validate_saved_tasks(state)
    _require_agent_batch_drained(state)
    batch = agent_batch(state)
    if task_id not in cast(list[str], batch["task_ids"]):
        _fail(ErrorCode.ORDER_VIOLATION, "verification task is outside the Main batch")
    record = _mutable_tasks(state).get(task_id)
    final_stage = "plan" if is_plan_only(state, task_id) else "implementation"
    if (
        not isinstance(record, Mapping)
        or record.get("status") != f"{final_stage}_approved"
    ):
        _fail(ErrorCode.ORDER_VIOLATION, "verification requires final review approval")
    if revision != batch["revision"] or verification_revision(record) != revision:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "verification revision differs from the sealed Main batch",
        )
    if batch["phase"] == "reviewers":
        _transition_integration_wave(state, "agent_batch", "verify_wave")
    elif batch["phase"] != "verification":
        _fail(
            ErrorCode.ORDER_VIOLATION,
            "verification requires the sealed, approved Main batch",
        )


def _prepare_dispatch(
    state: dict[str, object],
    request: TaskDispatch,
    *,
    revision: str | None = None,
    workspace_revision: str | None = None,
) -> tuple[dict[str, object], str]:
    """Validate and durably prepare one writer or reviewer assignment."""

    max_rounds = _max_review_rounds(state)
    if not isinstance(request, TaskDispatch) or not isinstance(request.task, TaskSpec):
        _fail(ErrorCode.INVALID_REQUEST, "TaskSpec is required")
    _require_declared_task(state, request.task)
    _admit_integration_dispatch(
        state, request, revision=revision, workspace_revision=workspace_revision
    )
    message = _require_message(request)
    tasks = _mutable_tasks(state)
    task = request.task
    current = tasks.get(task.task_id)
    named = _named_state(state)
    final_plan_review = (
        is_plan_only(state, task.task_id) and role_kind(request.role) is Role.REVIEWER
    )
    if workspace_revision is not None and not final_plan_review:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "workspace_revision requires a plan-only reviewer",
        )

    if current is None:
        new_writer_target: NodeRef | None
        if named:
            new_writer_target = _require_named_writer_route(
                state, task, request.role, initial=True
            )
            writer_kind = new_writer_target.kind
        else:
            legacy_role = _legacy_role(request.role)
            if legacy_role not in _WRITER_ROLES:
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "a new TaskSpec may be assigned only to Planner or Worker",
                )
            new_writer_target = None
            writer_kind = legacy_role
        for dependency in task.dependencies:
            prior = tasks.get(dependency)
            if not isinstance(prior, Mapping):
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    f"task dependency {dependency!r} is not completed",
                )
            _parse_saved_task(dependency, prior, max_rounds, state=state)
            if prior.get("status") != "completed":
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    f"task dependency {dependency!r} is not completed",
                )
        stage = "plan" if writer_kind is Role.PLANNER else "implementation"
        record: dict[str, object] = {
            "spec": task.as_dict(),
            "digest": task_digest(task),
            "dispatch_id": None,
            "status": "running",
            "stage": stage,
            "revision": None,
            "review_rounds": {"plan": 0, "implementation": 0},
        }
        if named:
            assert new_writer_target is not None
            _set_record_target(record, "role", new_writer_target)
            _set_record_target(record, "writer_role", new_writer_target)
        else:
            record["role"] = writer_kind.value
            record["writer_role"] = writer_kind.value
        prompt = _task_prompt(task, message)
        tasks[task.task_id] = record
        return record, prompt

    if not isinstance(current, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec is invalid")
    _require_record_task(current, task)
    status = current.get("status")
    if named and status == "running":
        active_target = _record_target(current, "role")
        requested_target = _named_target(state, request.role)
        if active_target != requested_target:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "active TaskSpec assignment has a different named node",
            )
        _fail(ErrorCode.ORDER_VIOLATION, "TaskSpec writer is already running")

    if named:
        _validate_named_record(state, task, current)
    rounds = _review_rounds(current)

    is_reviewer = (
        role_kind(request.role) is Role.REVIEWER
        if named
        else _legacy_role(request.role) is Role.REVIEWER
    )
    if is_reviewer:
        if status not in {"awaiting_plan_review", "awaiting_implementation_review"}:
            if status == "consultation_required":
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "user consultation is required before another dispatch",
                )
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "review requires an acknowledged plan or implementation result",
            )
        stage = "plan" if status == "awaiting_plan_review" else "implementation"
        if named:
            reviewer_target = _require_named_reviewer_route(
                state, task, stage, request.role
            )
        else:
            reviewer_target = None
        if rounds[stage] >= max_rounds:
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "maximum review rounds reached; user consultation required",
            )
        saved_revision = current.get("revision")
        if stage == "implementation":
            if not isinstance(revision, str) or not revision:
                _fail(
                    ErrorCode.INVALID_REQUEST,
                    "workspace revision is required for implementation review",
                )
            resolved_revision = revision
        else:
            if not isinstance(saved_revision, str) or not saved_revision:
                _fail(ErrorCode.IDENTITY_MISMATCH, "plan revision is missing")
            if revision is not None and revision != saved_revision:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH, "plan revision does not match result"
                )
            resolved_revision = saved_revision
        if final_plan_review:
            if workspace_revision is None:
                _fail(
                    ErrorCode.INVALID_REQUEST,
                    "workspace revision is required for final plan review",
                )
            _require_sha256(workspace_revision, "final plan workspace revision")
        previous_body = _result_body(current)
        prompt = review_prompt(
            task,
            stage=stage,
            revision=resolved_revision,
            result_body=previous_body,
            message=message,
        )
        if final_plan_review:
            prompt += (
                "\n\nこの計画の最終レビューで確認するworkspace revision:\n"
                + cast(str, workspace_revision)
            )
        updated = dict(current)
        updated_rounds = dict(rounds)
        updated_rounds[stage] += 1
        updated["review_rounds"] = updated_rounds
        updated["status"] = f"reviewing_{stage}"
        if named:
            assert reviewer_target is not None
            _set_record_target(updated, "role", reviewer_target)
        else:
            updated["role"] = Role.REVIEWER.value
        updated["stage"] = stage
        updated["review_source_dispatch_id"] = current.get("dispatch_id")
        updated["revision"] = resolved_revision
        if final_plan_review:
            updated["workspace_revision"] = workspace_revision
        updated.pop("consultation_answer", None)
        tasks[task.task_id] = updated
        return updated, prompt

    if named:
        writer_target: NodeRef | None
        route = _named_route(state, task.task_id)
        if status == "plan_approved" or status == "plan_changes_requested":
            writer_target = _route_node(
                state,
                route,
                "implementation_writer" if status == "plan_approved" else "plan_writer",
                required=status == "plan_approved",
            )
            if writer_target is None:
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "plan-only TaskSpec requires verification after plan approval",
                )
        elif status == "consultation_required":
            if not _consultation_answered(state, current):
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "user consultation is required before another dispatch",
                )
            stage = cast(str, current["stage"])
            if rounds[stage] >= max_rounds:
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "maximum review rounds reached; user consultation required",
                )
            writer_target = _route_node(
                state,
                route,
                "plan_writer" if stage == "plan" else "implementation_writer",
                required=True,
            )
        elif status in {"implementation_changes_requested", "verification_failed"}:
            writer_target = _route_node(
                state,
                route,
                "plan_writer"
                if status == "verification_failed" and is_plan_only(state, task.task_id)
                else "implementation_writer",
                required=True,
            )
        else:
            writer_target = None
        if status == "verification_failed":
            _require_verification_retry(task, current, max_rounds)
        if writer_target is None:
            if status == "consultation_required":
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "user consultation is required before another dispatch",
                )
            _fail(ErrorCode.ORDER_VIOLATION, "writer does not match the task stage")
        _require_named_writer_route(state, task, request.role)
        if writer_target != cast(NodeRef, request.role):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "writer does not match the exact named route",
            )
        expected_writer: Role | NodeRef | None = writer_target
    else:
        expected_writer = _writer_status(status)
    if status == "verification_failed" and not named:
        _require_verification_retry(task, current, max_rounds)
    if expected_writer is None:
        if status == "consultation_required":
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "user consultation is required before another dispatch",
            )
        _fail(ErrorCode.ORDER_VIOLATION, "writer does not match the task stage")
    if not named and _legacy_role(request.role) is not expected_writer:
        if status == "consultation_required":
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "user consultation is required before another dispatch",
            )
        _fail(ErrorCode.ORDER_VIOLATION, "writer does not match the task stage")
    updated = dict(current)
    updated["status"] = "running"
    if named:
        target = cast(NodeRef, request.role)
        _set_record_target(updated, "role", target)
        _set_record_target(updated, "writer_role", target)
        updated["stage"] = "implementation" if target.kind is Role.WORKER else "plan"
    else:
        legacy_role = _legacy_role(request.role)
        updated["role"] = legacy_role.value
        updated["writer_role"] = legacy_role.value
        updated["stage"] = "implementation" if legacy_role is Role.WORKER else "plan"
    updated["revision"] = None
    if is_plan_only(state, task.task_id):
        updated["workspace_revision"] = None
    prompt = _task_prompt(task, message)
    writer_result = current.get("writer_result")
    if isinstance(writer_result, Mapping) and isinstance(
        writer_result.get("body"), str
    ):
        prompt += "\n\n前段の作成結果（参照資料）:\n" + writer_result["body"]
    if isinstance(current.get("task_evidence"), Mapping):
        prompt += "\n\n同じTaskSpecに対するレビュー判定:\n" + json.dumps(
            dict(current["task_evidence"]), ensure_ascii=False
        )
    if status == "consultation_required":
        answer = cast(Mapping[str, object], current["consultation_answer"])
        prompt += (
            "\n\nこのレビュー相談に対するユーザーの回答（TaskSpecの制約は維持）:\n"
            + cast(str, answer["body"])
        )
    tasks[task.task_id] = updated
    return updated, prompt


def validate_task_assignment(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> TaskSpec | None:
    tasks = state.get("tasks")
    if "task_spec" not in assignment:
        if isinstance(tasks, Mapping) and any(
            isinstance(record, Mapping)
            and record.get("dispatch_id") == assignment.get("dispatch_id")
            for record in tasks.values()
        ):
            _fail(ErrorCode.IDENTITY_MISMATCH, "TaskSpec is missing")
        return None
    task = TaskSpec.from_dict(assignment["task_spec"])
    record = tasks.get(task.task_id) if isinstance(tasks, Mapping) else None
    if (
        not isinstance(record, Mapping)
        or record.get("spec") != task.as_dict()
        or record.get("digest") != task_digest(task)
        or record.get("dispatch_id") != assignment.get("dispatch_id")
        or record.get("status") not in _TASK_STATUSES
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "TaskSpec does not match its saved dispatch")
    if _named_state(state):
        _validate_named_record(state, task, record)
        if "role" not in assignment or "role_kind" not in assignment:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "named TaskSpec assignment requires role and role_kind",
            )
        assignment_target = _result_target(state, assignment)
        if assignment_target != _record_target(record, "role"):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "named TaskSpec assignment node does not match its saved dispatch",
            )
        if (
            is_plan_only(state, task.task_id)
            and assignment_target.kind is Role.REVIEWER
        ):
            if assignment.get("task_workspace_revision") != verification_revision(
                record
            ):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "plan-only assignment workspace revision is invalid",
                )
        elif "task_workspace_revision" in assignment:
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "unexpected assignment workspace revision"
            )
    return task


def validate_saved_tasks(state: Mapping[str, object]) -> None:
    if _named_state(state):
        _named_context(state)
    elif "task_specs" in state:
        parse_task_specs(state["task_specs"])
    if "agent_batch" in state and not is_agent_parallel(state):
        _fail(ErrorCode.IDENTITY_MISMATCH, "Main batch requires agent parallel state")
    if is_agent_parallel(state) and "program_wave" in state:
        _fail(ErrorCode.IDENTITY_MISMATCH, "Main state cannot own a program wave")
    if "tasks" not in state:
        if "agent_batch" in state:
            agent_batch(state)
        return
    max_rounds = _max_review_rounds(state)
    tasks = state.get("tasks")
    if not isinstance(tasks, Mapping):
        _fail(ErrorCode.INVALID_REQUEST, "saved TaskSpecs are invalid")
    for key, record in tasks.items():
        task = _parse_saved_task(key, record, max_rounds, state=state)
        _require_declared_task(state, task)
    if _is_program(state):
        program_wave(state)
    elif is_agent_parallel(state):
        if "agent_batch" in state:
            agent_batch(state)
        elif tasks or state.get("roles"):
            _fail(ErrorCode.IDENTITY_MISMATCH, "Main tasks require an explicit batch")


def _is_program(state: Mapping[str, object]) -> bool:
    return _named_state(state) and _named_graph(state).coordination.mode == "program"


def is_agent_parallel(state: Mapping[str, object]) -> bool:
    return (
        state.get("version") == 5 and _named_graph(state).coordination.mode == "agent"
    )


def program_wave(state: Mapping[str, object]) -> Mapping[str, object]:
    return _integration_wave(state, "program_wave")


def agent_batch(state: Mapping[str, object]) -> Mapping[str, object]:
    batch = _integration_wave(state, "agent_batch")
    members = cast(list[str], batch["task_ids"])
    records = cast(Mapping[str, Mapping[str, object]], state["tasks"])
    if any(
        record.get("status") != "completed"
        for task_id, record in records.items()
        if task_id not in members
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "unfinished task is outside the Main batch")
    for assignment in cast(
        Mapping[str, Mapping[str, object]], state.get("roles", {})
    ).values():
        raw_task = assignment.get("task_spec")
        if not isinstance(raw_task, Mapping) or raw_task.get("task_id") not in members:
            _fail(ErrorCode.IDENTITY_MISMATCH, "assignment is outside the Main batch")
    return batch


def _integration_wave(state: Mapping[str, object], key: str) -> Mapping[str, object]:
    """Validate the bounded integration barrier, without duplicating task records."""

    graph, catalog = _named_context(state)
    owner = "program" if key == "program_wave" else "agent"
    if graph.coordination.mode != owner or (
        owner == "agent" and state.get("version") != 5
    ):
        _fail(
            ErrorCode.INVALID_REQUEST,
            f"integration group requires {owner} coordination",
        )
    wave = state.get(key)
    if not isinstance(wave, Mapping) or set(wave) != {"task_ids", "phase", "revision"}:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            f"{owner} integration wave is missing or invalid",
        )
    ids = wave["task_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(task_id, str) for task_id in ids)
        or len(ids) != len(set(ids))
        or ids != [task.task_id for task in catalog if task.task_id in ids]
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            f"{owner} wave members do not match declared tasks",
        )
    phase, revision = wave["phase"], wave["revision"]
    if phase == "writers":
        if revision is not None:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "writer wave cannot retain an approved revision",
            )
    elif phase in {"reviewers", "verification"}:
        if (
            not isinstance(revision, str)
            or len(revision) != 64
            or any(char not in "0123456789abcdef" for char in revision)
        ):
            _fail(ErrorCode.IDENTITY_MISMATCH, "integrated wave revision is invalid")
    else:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{owner} wave phase is invalid")
    records = state.get("tasks")
    if not isinstance(records, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{owner} wave task records are missing")
    for task in catalog:
        if task.task_id not in ids:
            continue
        for dependency in task.dependencies:
            prior = records.get(dependency)
            if (
                dependency in ids
                or not isinstance(prior, Mapping)
                or prior.get("status") != "completed"
            ):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    f"{owner} wave contains an unresolved dependency",
                )
        record = records.get(task.task_id)
        final_stage = "plan" if is_plan_only(state, task.task_id) else "implementation"
        if (
            phase == "writers"
            and isinstance(record, Mapping)
            and record.get("status")
            in {
                f"reviewing_{final_stage}",
                f"{final_stage}_approved",
                "verifying",
                "completed",
            }
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "writer wave retains active implementation approval",
            )
        if phase != "writers":
            if (
                not isinstance(record, Mapping)
                or record.get("stage") != final_stage
                or record.get("status")
                not in {
                    f"awaiting_{final_stage}_review",
                    f"reviewing_{final_stage}",
                    f"{final_stage}_approved",
                    f"{final_stage}_changes_requested",
                    "consultation_required",
                    "verifying",
                    "completed",
                    "verification_failed",
                    "failed",
                }
            ):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "integrated wave has an unfinished writer",
                )
            record_revision = record.get(
                "workspace_revision" if final_stage == "plan" else "revision"
            )
            if record_revision not in {None, revision}:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "task revision differs from its integration wave",
                )
            if phase == "verification" and record.get("status") not in {
                f"{final_stage}_approved",
                "verifying",
                "completed",
                "verification_failed",
            }:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "verification wave contains an unapproved task",
                )
    return wave


def new_program_wave(state: Mapping[str, object]) -> dict[str, object]:
    graph, catalog = _named_context(state)
    if graph.coordination.mode != "program" or not catalog:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "program coordination requires declared TaskSpecs",
        )
    records = state.get("tasks")
    if not isinstance(records, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "program task records are missing")

    def completed(task_id: str) -> bool:
        record = records.get(task_id)
        return isinstance(record, Mapping) and record.get("status") == "completed"

    ids = [
        task.task_id
        for task in catalog
        if not completed(task.task_id)
        and all(completed(dependency) for dependency in task.dependencies)
    ]
    if not ids:
        _fail(ErrorCode.ORDER_VIOLATION, "no incomplete program tasks are ready")
    return {"task_ids": ids, "phase": "writers", "revision": None}


def _admit_integration_dispatch(
    state: Mapping[str, object],
    request: TaskDispatch,
    *,
    revision: str | None,
    workspace_revision: str | None,
) -> None:
    if _is_program(state):
        wave = program_wave(state)
    elif is_agent_parallel(state):
        wave = agent_batch(state)
    else:
        return
    if request.task.task_id not in cast(list[str], wave["task_ids"]):
        _fail(ErrorCode.ORDER_VIOLATION, "task is outside the current integration wave")
    if role_kind(request.role) is Role.REVIEWER:
        records = cast(Mapping[str, Mapping[str, object]], state["tasks"])
        current = records.get(request.task.task_id)
        stage = current.get("stage") if current is not None else None
        final_plan = is_plan_only(state, request.task.task_id)
        expected_phase = (
            "writers" if stage == "plan" and not final_plan else "reviewers"
        )
        if wave["phase"] != expected_phase:
            _fail(
                ErrorCode.ORDER_VIOLATION, "review must follow its integration barrier"
            )
        if (
            expected_phase == "reviewers"
            and (workspace_revision if final_plan else revision) != wave["revision"]
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "review revision differs from the sealed wave",
            )
    elif wave["phase"] != "writers":
        _fail(
            ErrorCode.ORDER_VIOLATION,
            "reopen the integration wave before starting a writer",
        )


def transition_program_wave(
    state: dict[str, object], transition: str, *, revision: str | None = None
) -> None:
    _transition_integration_wave(state, "program_wave", transition, revision=revision)


def _transition_integration_wave(
    state: dict[str, object], key: str, transition: str, *, revision: str | None = None
) -> None:
    """Apply one controller-owned barrier transition under the existing run lock."""

    validate_saved_tasks(state)
    wave = _integration_wave(state, key)
    if state.get("roles") or state.get("pending_delivery_id") is not None:
        _fail(
            ErrorCode.BUSY,
            "consume all assignments and deliveries before changing the wave",
        )
    records = _mutable_tasks(state)
    members = [records.get(task_id) for task_id in cast(list[str], wave["task_ids"])]
    if any(not isinstance(record, dict) for record in members):
        _fail(ErrorCode.ORDER_VIOLATION, "wave writers are not finished")
    selected = cast(list[dict[str, object]], members)
    statuses = [record.get("status") for record in selected]
    final_stages = [
        "plan"
        if is_plan_only(state, TaskSpec.from_dict(record["spec"]).task_id)
        else "implementation"
        for record in selected
    ]
    if transition == "seal_wave":
        if wave["phase"] != "writers" or any(
            status != f"awaiting_{stage}_review"
            for status, stage in zip(statuses, final_stages, strict=True)
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "all wave writers must finish before integrated review",
            )
        next_wave = {**wave, "phase": "reviewers", "revision": revision}
        _integration_wave({**state, key: next_wave}, key)
        state[key] = next_wave
    elif transition == "verify_wave":
        if wave["phase"] != "reviewers" or any(
            status != f"{stage}_approved"
            for status, stage in zip(statuses, final_stages, strict=True)
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "every wave task requires review approval before verification",
            )
        state[key] = {**wave, "phase": "verification"}
    elif transition == "next_wave" and key == "program_wave":
        if wave["phase"] != "verification" or any(
            status != "completed" for status in statuses
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "finish the current integration wave before admitting successors",
            )
        state[key] = new_program_wave(state)
    elif transition == "reopen_wave":
        if any(
            record.get("status") == "consultation_required"
            and not _consultation_answered(state, record)
            for record in selected
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "resolve user consultation before reopening the wave",
            )
        if wave["phase"] not in {"reviewers", "verification"} or not any(
            status
            in {
                "implementation_changes_requested",
                "plan_changes_requested",
                "verification_failed",
                "consultation_required",
            }
            for status in statuses
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "reopening requires changes or failed verification",
            )
        max_rounds = _max_review_rounds(state)
        if any(
            _review_rounds(record)[stage] >= max_rounds
            for record, stage in zip(selected, final_stages, strict=True)
        ):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "maximum review rounds reached; wave remains unresolved",
            )
        for record, stage in zip(selected, final_stages, strict=True):
            if record.get("status") in {f"{stage}_approved", "completed"}:
                writer = _record_target(record, "writer_role")
                writer_result = cast(Mapping[str, object], record["writer_result"])
                _set_record_target(record, "role", writer)
                record.update(
                    status=f"awaiting_{stage}_review",
                    revision=hashlib.sha256(
                        cast(str, writer_result["body"]).encode("utf-8")
                    ).hexdigest()
                    if stage == "plan"
                    else None,
                    dispatch_id=writer_result["dispatch_id"],
                    result=dict(writer_result),
                )
                if stage == "plan":
                    record["workspace_revision"] = None
        state[key] = {**wave, "phase": "writers", "revision": None}
    else:
        _fail(ErrorCode.INVALID_REQUEST, "unknown program wave transition")


def _review_verdict(
    record: Mapping[str, object], result: Mapping[str, object]
) -> dict[str, object]:
    task = TaskSpec.from_dict(record.get("spec"))
    stage = record.get("stage")
    revision = record.get("revision")
    if stage not in _REVIEW_STAGES or not isinstance(revision, str) or not revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review binding is incomplete")
    evidence = result.get("task_evidence")
    if not isinstance(evidence, Mapping):
        _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "trusted review verdict is missing")
    try:
        encoded = json.dumps(dict(evidence), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE, "trusted review verdict is invalid"
        ) from exc
    return parse_review(
        encoded,
        task=task,
        stage=stage,
        revision=revision,
    )


def acknowledge_task(state: dict[str, object], result: Mapping[str, object]) -> None:
    """Consume one provider result without granting completion authority."""

    if "tasks" not in state:
        return
    named = _named_state(state)
    _max_review_rounds(state)
    tasks = _mutable_tasks(state)
    dispatch_id = result.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, "task result dispatch identity is missing")
    matches = [
        (task_id, record)
        for task_id, record in tasks.items()
        if isinstance(record, Mapping) and record.get("dispatch_id") == dispatch_id
    ]
    if len(matches) != 1:
        _fail(ErrorCode.IDENTITY_MISMATCH, "task result dispatch identity is unknown")
    task_id, current = matches[0]
    if not isinstance(current, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec is invalid")
    record = dict(current)
    status = record.get("status")
    task: TaskSpec | None = None
    target: NodeRef | None = None
    role: object
    if named:
        task = TaskSpec.from_dict(record.get("spec"))
        if task.task_id != task_id:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec identity is invalid")
        _validate_named_record(state, task, current)
        target = _result_target(state, result)
        if target != _record_target(record, "role"):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "task result node does not match assignment",
            )
        role = target.node_id
    else:
        role = result.get("role")
    outcome = result.get("outcome")
    body = result.get("body")
    if (
        not isinstance(role, str)
        or not isinstance(outcome, str)
        or not isinstance(body, str)
    ):
        _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "task result fields are invalid")
    if not named:
        expected_role = record.get("role")
        if role != expected_role:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "task result role does not match assignment",
            )
    if outcome not in {"succeeded", "failed"}:
        _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "task result outcome is invalid")
    if status not in {"running", "reviewing_plan", "reviewing_implementation"}:
        _fail(ErrorCode.ORDER_VIOLATION, "task result was already consumed")

    if outcome == "failed":
        record["status"] = "failed"
        record["result"] = dict(result)
        tasks[task_id] = record
        return

    if status == "running":
        if named:
            assert target is not None
            if target.kind not in _WRITER_ROLES:
                _fail(ErrorCode.IDENTITY_MISMATCH, "writer result node is invalid")
            writer_kind = target.kind
        else:
            if role not in {Role.PLANNER.value, Role.WORKER.value}:
                _fail(ErrorCode.IDENTITY_MISMATCH, "writer result role is invalid")
            writer_kind = Role(role)
        record["status"] = (
            "awaiting_plan_review"
            if writer_kind is Role.PLANNER
            else "awaiting_implementation_review"
        )
        record["stage"] = "plan" if writer_kind is Role.PLANNER else "implementation"
        record["revision"] = (
            hashlib.sha256(body.encode("utf-8")).hexdigest()
            if writer_kind is Role.PLANNER
            else None
        )
        record["result"] = dict(result)
        record["writer_result"] = dict(result)
        tasks[task_id] = record
        return

    if named:
        assert target is not None
        if target.kind is not Role.REVIEWER:
            _fail(ErrorCode.IDENTITY_MISMATCH, "review result node is invalid")
    elif role != Role.REVIEWER.value:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review result role is invalid")
    verdict = _review_verdict(record, result)
    decision = verdict["decision"]
    stage = cast(str, record["stage"])
    record["task_evidence"] = verdict
    record["review_result"] = dict(result)
    record["result"] = dict(result)
    if decision == "consult":
        record["status"] = "consultation_required"
    elif stage == "plan":
        record["status"] = (
            "plan_approved" if decision == "approve" else "plan_changes_requested"
        )
    else:
        record["status"] = (
            "implementation_approved"
            if decision == "approve"
            else "implementation_changes_requested"
        )
    tasks[task_id] = record
