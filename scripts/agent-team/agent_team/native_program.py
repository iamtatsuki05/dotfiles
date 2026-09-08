"""Drive declared native tasks without a Main model or a second task ledger."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
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
from .native_controller import controller_keys
from .native_question_channel import validate_question_request
from .program_policy import select_action
from .runtime import MAX_RESULT_BODY_CHARS, read_state
from .task_execution import task_consultation
from .task_spec import parse_task_specs


class ProgramPort(Protocol):
    def program_snapshot(self) -> dict[str, object]: ...

    def program_transition(self, transition: str) -> None: ...

    def request(self, request: BackendRequest) -> BackendResult: ...


def _emit(event: Mapping[str, object]) -> None:
    print(json.dumps(dict(event), ensure_ascii=False, sort_keys=True), flush=True)


def _wait_or_read(
    backend: ProgramPort, state: Mapping[str, object], role: NodeRef
) -> None:
    pending = state.get("pending_delivery_id")
    if pending is None:
        backend.request(RoleWait(role, 1000))
    elif state.get("pending_delivery_kind") == "worker_done":
        stage = state.get("pending_delivery_stage")
        if stage == "observed":
            backend.request(RoleRead(role, MAX_RESULT_BODY_CHARS))
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


def _notice(
    state: Mapping[str, object], *, reason: str | None, task_id: str | None
) -> dict[str, object]:
    question = state.get("native_question")
    result: dict[str, object] = {
        "status": "waiting_for_user",
        "task_id": task_id,
        "reason": reason,
    }
    if isinstance(question, Mapping) and question.get("phase") == "observed":
        fields = validate_question_request(question["request"]).questions
        ids = cast(list[str], question["message_ids"])
        answers = cast(Mapping[str, str], question["answers"])
        result["messages"] = [
            {"message_id": message_id, "body": field.body}
            for message_id, field in zip(ids, fields, strict=True)
            if message_id not in answers
        ]
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


def drive(backend: ProgramPort) -> int:
    """Apply one canonical operation at a time, reselecting from saved evidence."""

    last_notice: dict[str, object] | None = None
    while True:
        try:
            state = backend.program_snapshot()
            action = select_action(state)
            if action.kind == "wait_user":
                notice = _notice(state, reason=action.message, task_id=action.task_id)
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
                delivery = state.get("pending_delivery_id")
                if not isinstance(delivery, str):
                    raise RuntimeFailure(
                        ErrorCode.IDENTITY_MISMATCH, "pending delivery is missing"
                    )
                backend.request(DeliveryAck(DeliveryRef(delivery)))
            elif action.kind == "wait":
                if action.role is None:
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "active assignment identity is unresolved",
                    )
                _wait_or_read(backend, state, action.role)
        except RuntimeFailure as exc:
            if exc.code in {ErrorCode.TEAM_ALREADY_RUNNING, ErrorCode.BUSY}:
                # A publisher or stop may change the next action while it owns
                # the reservation; retry by reading state, not replaying intent.
                time.sleep(0.05)
                continue
            _emit({"status": "unresolved", "code": exc.code.value, "reason": str(exc)})
            return 1
        except (OSError, TypeError, ValueError, KeyError) as exc:
            _emit({"status": "unresolved", "reason": type(exc).__name__})
            return 1


def _ready_state(path: Path, run_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5.0
    while True:
        state = read_state(path)
        keys = controller_keys(state)
        if state.get("run_id") != run_id or keys.pid != "coordinator_pid":
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "program run identity changed"
            )
        native = cast(Mapping[str, object], state["native"])
        if native.get("phase") == "stopping":
            raise RuntimeFailure(ErrorCode.BUSY, "program run is stopping")
        process = native.get(keys.process)
        if isinstance(process, Mapping):
            if native.get("phase") != "running" or process.get(keys.pid) != os.getpid():
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "program child receipt does not match this process",
                )
            return state
        if time.monotonic() >= deadline:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "program child receipt was not published"
            )
        time.sleep(0.05)


def run(path: Path, run_id: str) -> int:
    from .cli import _management_plan_from_state, _runtime_engine, _start_spec
    from .native_backend import NativeBackend

    try:
        state = _ready_state(path, run_id)
        plan = _management_plan_from_state(state)
        _engine, backend = _runtime_engine(plan, resume_existing=True)
        if not isinstance(backend, NativeBackend):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "program coordinator requires a native runtime",
            )
        backend.start(_start_spec(plan, attach=False))
        return drive(backend)
    except (RuntimeFailure, OSError, TypeError, ValueError) as exc:
        _emit(
            {
                "status": "unresolved",
                "reason": str(exc)
                if isinstance(exc, RuntimeFailure)
                else type(exc).__name__,
            }
        )
        return 1
