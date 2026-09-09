"""Common bounded driver for native and named Orca program coordinators."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Protocol, cast

from .contracts import (
    BackendRequest,
    BackendResult,
    DeliveryAck,
    DeliveryRef,
    ErrorCode,
    NodeRef,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    TaskDispatch,
    TaskVerify,
)
from .mcp_protocol import MAX_READ_LINES
from .native_delivery import container as native_container
from .native_delivery import containers as native_containers
from .native_question_channel import validate_question_request
from .orca_delivery import container as orca_container
from .orca_delivery import containers as orca_containers
from .program_policy import select_action
from .runtime import MAX_RESULT_BODY_CHARS
from .task_execution import task_consultation
from .task_spec import TaskSpec, parse_task_specs


class ProgramPort(Protocol):
    def program_snapshot(self) -> dict[str, object]: ...

    def program_transition(self, transition: str) -> None: ...

    def request(self, request: BackendRequest) -> BackendResult: ...


def _is_orca(state: Mapping[str, object]) -> bool:
    return state.get("runtime") == "orca"


def _delivery_container(
    state: Mapping[str, object], role: NodeRef | None
) -> Mapping[str, object]:
    if state.get("version") == 5:
        if role is None:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "parallel delivery requires an exact role",
            )
        selected = (
            orca_container(state, role.node_id)
            if _is_orca(state)
            else native_container(state, role.node_id)
        )
        return selected
    return orca_container(state) if _is_orca(state) else native_container(state)


def _delivery_containers(
    state: Mapping[str, object],
) -> tuple[tuple[str | None, Mapping[str, object]], ...]:
    return orca_containers(state) if _is_orca(state) else native_containers(state)


def _logical_task_id(assignment: Mapping[str, object]) -> str:
    task = TaskSpec.from_dict(assignment.get("task_spec"))
    return task.task_id


def _wait_or_read(
    backend: ProgramPort, state: Mapping[str, object], role: NodeRef
) -> None:
    owner = _delivery_container(state, role)
    pending = owner.get("pending_delivery_id")
    if pending is None:
        backend.request(RoleWait(role, 1000))
    elif owner.get("pending_delivery_kind") == "worker_done":
        stage = owner.get("pending_delivery_stage")
        if stage == "observed":
            lines = MAX_READ_LINES if _is_orca(state) else MAX_RESULT_BODY_CHARS
            backend.request(RoleRead(role, lines))
        elif stage == "read":
            backend.request(RoleRelease(role))
        else:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION, "completion delivery cannot be drained"
            )
    else:
        raise RuntimeFailure(
            ErrorCode.ORDER_VIOLATION, "answer every question before acknowledging"
        )


def _acknowledge(
    backend: ProgramPort, state: Mapping[str, object], role: NodeRef | None
) -> None:
    if _is_orca(state) and state.get("version") == 5 and role is None:
        batch = state.get("orca_delivery_batch")
        if not isinstance(batch, Mapping):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "Orca whole-batch Delivery is missing",
            )
        delivery = batch.get("delivery_id")
    else:
        owner = _delivery_container(state, role)
        delivery = owner.get("pending_delivery_id")
    if not isinstance(delivery, str) or not delivery:
        raise RuntimeFailure(ErrorCode.IDENTITY_MISMATCH, "pending delivery is missing")
    backend.request(DeliveryAck(DeliveryRef(delivery)))


def _orca_question_notice(
    question: Mapping[str, object],
    *,
    node_id: str | None,
    role_kind: object,
    task_id: object,
) -> dict[str, object] | None:
    if question.get("phase") != "observed":
        return None
    request = validate_question_request(question.get("request"))
    message_id = question.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Orca question message ID is invalid")
    fields = [field.field for field in request.questions]
    template = {field: "<answer>" for field in fields}
    body = (
        json.dumps(
            request.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n\nReply with JSON containing exactly these answer fields:\n"
        + json.dumps(
            template, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return {
        "node_id": node_id,
        "role_kind": role_kind,
        "task_id": task_id,
        "messages": [{"message_id": message_id, "body": body}],
        "answer_fields": fields,
        "answer_template": template,
    }


def _question_notice(
    state: Mapping[str, object],
    node_id: str,
    assignment: Mapping[str, object],
) -> dict[str, object] | None:
    key = "orca_question" if _is_orca(state) else "native_question"
    question = assignment.get(key)
    if not isinstance(question, Mapping):
        return None
    if _is_orca(state):
        return _orca_question_notice(
            question,
            node_id=node_id,
            role_kind=assignment.get("role_kind"),
            task_id=_logical_task_id(assignment),
        )
    if question.get("phase") != "observed":
        return None
    fields = validate_question_request(question["request"]).questions
    ids = cast(list[str], question["message_ids"])
    answers = cast(Mapping[str, str], question["answers"])
    messages = [
        {"message_id": message_id, "body": field.body}
        for message_id, field in zip(ids, fields, strict=True)
        if message_id not in answers
    ]
    if not messages:
        return None
    return {
        "node_id": node_id,
        "role_kind": assignment.get("role_kind"),
        "task_id": _logical_task_id(assignment),
        "messages": messages,
    }


def _notice(
    state: Mapping[str, object],
    *,
    reason: str | None,
    task_id: str | None,
    role: NodeRef | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "waiting_for_user",
        "task_id": task_id,
        "reason": reason,
    }
    if state.get("version") == 5:
        selected_node = role.node_id if role is not None else None
        questions = [
            item
            for node_id, assignment in _delivery_containers(state)
            if node_id is not None
            for item in [_question_notice(state, node_id, assignment)]
            if item is not None
        ]
        selected_question = next(
            (item for item in questions if item["node_id"] == selected_node),
            None,
        )
        if selected_question is not None:
            result["node_id"] = selected_question["node_id"]
            result["role_kind"] = selected_question["role_kind"]
            result["messages"] = selected_question["messages"]
            for key in ("answer_fields", "answer_template"):
                if key in selected_question:
                    result[key] = selected_question[key]
        result["questions"] = questions
    else:
        question_key = "orca_question" if _is_orca(state) else "native_question"
        question: object = state.get(question_key)
        if _is_orca(state):
            selected_node = role.node_id if role is not None else None
            selected_role_kind: object = None
            selected_task_id: object = task_id
            roles = state.get("roles")
            selected_assignment: Mapping[str, object] | None = None
            if isinstance(roles, Mapping):
                if selected_node is not None:
                    candidate = roles.get(selected_node)
                    if isinstance(candidate, Mapping):
                        selected_assignment = candidate
                elif len(roles) == 1:
                    candidate = next(iter(roles.values()))
                    if isinstance(candidate, Mapping):
                        selected_assignment = candidate
                if selected_assignment is not None:
                    selected_node = cast(str, selected_assignment.get("role"))
                    selected_role_kind = selected_assignment.get("role_kind")
                    selected_task_id = _logical_task_id(selected_assignment)
            if selected_assignment is not None:
                question = selected_assignment.get(question_key)
            if isinstance(question, Mapping):
                selected = _orca_question_notice(
                    question,
                    node_id=selected_node,
                    role_kind=selected_role_kind,
                    task_id=selected_task_id,
                )
                if selected is not None:
                    result["messages"] = selected["messages"]
                    result["answer_fields"] = selected["answer_fields"]
                    result["answer_template"] = selected["answer_template"]
        elif isinstance(question, Mapping) and question.get("phase") == "observed":
            fields = validate_question_request(question["request"]).questions
            ids = cast(list[str], question["message_ids"])
            answers = cast(Mapping[str, str], question["answers"])
            result["messages"] = [
                {"message_id": message_id, "body": field.body}
                for message_id, field in zip(ids, fields, strict=True)
                if message_id not in answers
            ]
    if "messages" in result or result.get("questions"):
        result["answer_command"] = (
            "agent-team answer --state STATE --message-id ID --body ANSWER"
        )
    records = state.get("tasks")
    record = records.get(task_id) if isinstance(records, Mapping) else None
    if isinstance(record, Mapping):
        consultation = task_consultation(state, record)
        if consultation is not None:
            result["consultation"] = consultation
            result["answer_command"] = (
                "agent-team answer --state STATE --consultation-id ID --body ANSWER"
            )
    return result


def _emit(event: Mapping[str, object]) -> None:
    print(json.dumps(dict(event), ensure_ascii=False, sort_keys=True), flush=True)


def drive(backend: ProgramPort) -> int:
    """Apply one canonical operation at a time from saved program evidence."""

    last_notice: dict[str, object] | None = None
    while True:
        try:
            state = backend.program_snapshot()
            action = select_action(state)
            if action.kind == "wait_user":
                notice = _notice(
                    state,
                    reason=action.message,
                    task_id=action.task_id,
                    role=action.role,
                )
                if notice != last_notice:
                    _emit(notice)
                    last_notice = notice
                time.sleep(0.25)
                continue
            last_notice = None
            if action.kind == "complete":
                _emit({"status": "completed"})
                return 0
            if action.kind in {"pause", "reject"}:
                _emit(
                    {
                        "status": "unresolved",
                        "task_id": action.task_id,
                        "reason": action.message,
                    }
                )
                return 1
            if action.kind in {"seal_wave", "reopen_wave", "verify_wave", "next_wave"}:
                backend.program_transition(action.kind)
            elif action.kind == "dispatch":
                assert (
                    action.role is not None
                    and action.task_id is not None
                    and action.message is not None
                )
                task = next(
                    task
                    for task in parse_task_specs(state["task_specs"])
                    if task.task_id == action.task_id
                )
                backend.request(TaskDispatch(action.role, task, action.message))
                _emit(
                    {
                        "status": "dispatched",
                        "task_id": task.task_id,
                        "node_id": action.role.node_id,
                    }
                )
            elif action.kind == "verify":
                assert action.task_id is not None
                backend.request(TaskVerify(action.task_id))
            elif action.kind == "acknowledge":
                _acknowledge(backend, state, action.role)
            elif action.kind == "wait":
                if action.role is None:
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "active assignment identity is unresolved",
                    )
                _wait_or_read(backend, state, action.role)
        except RuntimeFailure as exc:
            if exc.code in {ErrorCode.TEAM_ALREADY_RUNNING, ErrorCode.BUSY}:
                time.sleep(0.05)
                continue
            _emit({"status": "unresolved", "code": exc.code.value, "reason": str(exc)})
            return 1
        except (OSError, TypeError, ValueError, KeyError) as exc:
            _emit({"status": "unresolved", "reason": type(exc).__name__})
            return 1
