"""Durable TaskSpec, review, and verification-admission transitions."""

from __future__ import annotations

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

    return state.get("version") == 4


def _legacy_role(target: object) -> Role:
    if isinstance(target, Role):
        return target
    _fail(ErrorCode.ORDER_VIOLATION, "legacy state requires a fixed Role target")
    raise AssertionError("unreachable")


def _named_context(
    state: Mapping[str, object],
) -> tuple[GraphSpec, tuple[TaskSpec, ...]]:
    """Load and cross-check a version-4 graph and its TaskSpec catalog.

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
    if (
        graph.coordination.mode != "agent"
        or graph.coordination.dispatch_mode != "serial"
    ):
        _fail(
            ErrorCode.INVALID_REQUEST,
            "named TaskSpec routing currently requires agent coordination and serial dispatch",
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
    }:
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
    revision = record.get("revision")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "verified task revision is missing")
    task_evidence = record.get("task_evidence")
    if not isinstance(task_evidence, Mapping):
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "implementation approval evidence is missing"
        )
    try:
        verdict = parse_review(
            json.dumps(dict(task_evidence), ensure_ascii=False),
            task=task,
            stage="implementation",
            revision=revision,
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
    if rounds["implementation"] >= max_rounds:
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


def prepare_dispatch(
    state: dict[str, object], request: TaskDispatch, *, revision: str | None = None
) -> tuple[dict[str, object], str]:
    """Validate and durably prepare one writer or reviewer assignment."""

    max_rounds = _max_review_rounds(state)
    if not isinstance(request, TaskDispatch) or not isinstance(request.task, TaskSpec):
        _fail(ErrorCode.INVALID_REQUEST, "TaskSpec is required")
    _require_declared_task(state, request.task)
    message = _require_message(request)
    tasks = _mutable_tasks(state)
    task = request.task
    current = tasks.get(task.task_id)
    named = _named_state(state)

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
        previous_body = _result_body(current)
        prompt = review_prompt(
            task,
            stage=stage,
            revision=resolved_revision,
            result_body=previous_body,
            message=message,
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
        elif status in {"implementation_changes_requested", "verification_failed"}:
            writer_target = _route_node(
                state, route, "implementation_writer", required=True
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
    return task


def validate_saved_tasks(state: Mapping[str, object]) -> None:
    if _named_state(state):
        _named_context(state)
    elif "task_specs" in state:
        parse_task_specs(state["task_specs"])
    if "tasks" not in state:
        return
    max_rounds = _max_review_rounds(state)
    tasks = state.get("tasks")
    if not isinstance(tasks, Mapping):
        _fail(ErrorCode.INVALID_REQUEST, "saved TaskSpecs are invalid")
    for key, record in tasks.items():
        task = _parse_saved_task(key, record, max_rounds, state=state)
        _require_declared_task(state, task)


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
