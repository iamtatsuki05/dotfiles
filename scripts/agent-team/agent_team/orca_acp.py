"""Publish typed scoped ACP results through the Orca completion contract."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn, cast

from . import orca_delivery
from .contracts import ErrorCode, RuntimeFailure
from .locking import _LifecycleReservation
from .named_graph import GraphSpec, NamedGraphError
from .runtime import (
    MAX_RESULT_BODY_CHARS,
    read_state,
    write_state,
)
from .scoped_acp import native_profile
from .task_execution import parse_review, validate_task_assignment
from .task_spec import TaskSpec

_ACP_ROLES = frozenset({"planner", "worker", "reviewer"})


def _send_worker_done(
    state: dict[str, object],
    assignment: dict[str, object],
    *,
    outcome: str,
    body: str,
) -> None:
    """Call the existing CLI sender lazily so this module stays import-safe."""

    from .cli import _send_worker_done

    _send_worker_done(state, assignment, outcome=outcome, body=body)


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise RuntimeFailure(code, message)


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"Orca completion {field} is invalid")
    return value


def _validate_named_orca_state(
    state: Mapping[str, object], *, role: str, role_kind: str
) -> dict[str, object]:
    if state.get("version") not in {4, 5} or state.get("runtime") != "orca":
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion requires named state version 4 or 5",
        )
    try:
        graph = GraphSpec.from_dict(state.get("graph"))
        node = graph.node(role)
    except (KeyError, NamedGraphError, TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion role is not present in the saved graph",
        ) from exc
    if graph.coordination.mode not in {
        "agent",
        "program",
    } or graph.coordination.dispatch_mode != (
        "parallel" if state["version"] == 5 else "serial"
    ):
        _fail(
            ErrorCode.INVALID_REQUEST,
            "Orca completion requires coordination matching the state version",
        )
    if role_kind not in _ACP_ROLES or node.kind.value != role_kind:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion role kind does not match graph",
        )
    role_specs = state.get("role_specs")
    if not isinstance(role_specs, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca completion role_specs are missing")
    spec = role_specs.get(role)
    if not isinstance(spec, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca completion role spec is missing")
    spec = cast(Mapping[str, object], spec)
    try:
        expected = native_profile(str(spec.get("provider")), role_kind)
    except ValueError as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion role spec has an unsupported provider profile",
        ) from exc
    if spec.get("kind") != role_kind or any(
        spec.get(key) != value for key, value in expected.items()
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion role spec does not match the selected profile",
        )
    return dict(spec)


def _assignment_for_completion(
    state: Mapping[str, object],
    *,
    role: str,
    role_kind: str,
    run_id: str,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    launch_nonce: str,
) -> tuple[dict[str, object], TaskSpec | None]:
    _validate_named_orca_state(state, role=role, role_kind=role_kind)
    if state.get("run_id") != run_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca completion Run does not match state")
    roles = state.get("roles")
    if not isinstance(roles, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca completion roles are missing")
    roles = cast(Mapping[str, object], roles)
    assignment_value = roles.get(role)
    if not isinstance(assignment_value, dict):
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca completion assignment is not active")
    assignment = cast(dict[str, object], assignment_value)
    if assignment.get("role") != role or assignment.get("role_kind") != role_kind:
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca completion assignment role is invalid")
    if assignment.get("launcher_owned_terminal") is not True:
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca terminal ownership is unproven")
    for field, expected in (
        ("task_id", task_id),
        ("dispatch_id", dispatch_id),
        ("terminal_handle", terminal_handle),
        ("launch_nonce", launch_nonce),
    ):
        if assignment.get(field) != expected:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                f"Orca completion {field} does not match assignment",
            )
    raw_task = assignment.get("task_spec")
    try:
        validated = validate_task_assignment(state, assignment)
    except (RuntimeFailure, TypeError, ValueError) as exc:
        if isinstance(exc, RuntimeFailure):
            raise
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion TaskSpec binding is invalid",
        ) from exc
    if raw_task is None:
        if role_kind == "worker" or validated is not None:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "Orca Worker completion requires a TaskSpec binding",
            )
        return assignment, None
    try:
        task = TaskSpec.from_dict(raw_task)
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion TaskSpec binding is invalid",
        ) from exc
    if raw_task != task.as_dict():
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca completion TaskSpec serialization changed",
        )
    if validated is None or validated != task:
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "Orca completion TaskSpec binding is invalid"
        )
    return assignment, task


def _validated_task_evidence(
    assignment: Mapping[str, object],
    task: TaskSpec | None,
    *,
    role_kind: str,
    outcome: str,
    task_evidence: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if outcome != "succeeded":
        return None
    if task_evidence is None:
        if role_kind == "reviewer" and task is not None:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "successful Orca review completion is missing TaskSpec evidence",
            )
        return None
    if role_kind != "reviewer" or task is None:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "TaskSpec evidence requires a successful Orca reviewer completion",
        )
    stage = assignment.get("task_stage")
    revision = assignment.get("task_revision")
    if not isinstance(stage, str) or not isinstance(revision, str) or not revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca review binding is incomplete")
    try:
        encoded = json.dumps(
            dict(task_evidence), ensure_ascii=False, separators=(",", ":")
        )
        return parse_review(encoded, task=task, stage=stage, revision=revision)
    except RuntimeFailure:
        raise
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "Orca review evidence is invalid",
        ) from exc


def publish_completion(
    state_path: Path,
    *,
    role: str,
    role_kind: str,
    run_id: str,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    launch_nonce: str,
    outcome: str,
    body: str,
    cleanup_confirmed: bool,
    task_evidence: Mapping[str, object] | None = None,
) -> str:
    """Persist and, when safe, publish one selected scoped ACP completion."""

    if outcome not in {"succeeded", "failed"}:
        _fail(ErrorCode.INVALID_REQUEST, "Orca completion outcome is invalid")
    if not isinstance(body, str) or len(body) > MAX_RESULT_BODY_CHARS:
        _fail(ErrorCode.INVALID_REQUEST, "Orca completion body is too large")
    if type(cleanup_confirmed) is not bool:
        _fail(ErrorCode.INVALID_REQUEST, "Orca cleanup confirmation is invalid")
    for value, field in (
        (role, "role"),
        (role_kind, "role_kind"),
        (run_id, "run_id"),
        (task_id, "task_id"),
        (dispatch_id, "dispatch_id"),
        (terminal_handle, "terminal_handle"),
        (launch_nonce, "launch_nonce"),
    ):
        _required_string(value, field)

    reservation = _LifecycleReservation(state_path, create_parent=False)
    send_payload: tuple[dict[str, object], dict[str, object], str, str] | None = None
    effective_outcome = outcome
    locked = False
    try:
        reservation.acquire_for_publication()
        locked = True
        state = read_state(state_path)
        if "native_result" in state:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "Orca completion must not use native_result",
            )
        assignment, task = _assignment_for_completion(
            state,
            role=role,
            role_kind=role_kind,
            run_id=run_id,
            task_id=task_id,
            dispatch_id=dispatch_id,
            terminal_handle=terminal_handle,
            launch_nonce=launch_nonce,
        )
        delivery = cast(dict[str, object], orca_delivery.container(state, role))
        if "orca_result" in delivery:
            _fail(ErrorCode.IDENTITY_MISMATCH, "Orca completion was already published")
        stop_requested = state.get("orca_stop_requested")
        if stop_requested is not None and type(stop_requested) is not bool:
            _fail(ErrorCode.IDENTITY_MISMATCH, "Orca stop request flag is invalid")
        question = assignment.get("orca_question")
        if question is not None and (
            not isinstance(question, Mapping)
            or not isinstance(question.get("phase"), str)
        ):
            _fail(ErrorCode.IDENTITY_MISMATCH, "Orca question state is invalid")
        question_pending = (
            isinstance(question, Mapping) and question.get("phase") != "recorded"
        )
        question_recorded = (
            isinstance(question, Mapping) and question.get("phase") == "recorded"
        )
        send_allowed = (
            cleanup_confirmed and stop_requested is not True and not question_pending
        )
        if not send_allowed:
            effective_outcome = "failed"
        if question_recorded and cleanup_confirmed:
            assignment.pop("orca_question", None)
        evidence = _validated_task_evidence(
            assignment,
            task,
            role_kind=role_kind,
            outcome=effective_outcome,
            task_evidence=task_evidence,
        )
        result: dict[str, object] = {
            "role": role,
            "role_kind": role_kind,
            "run_id": run_id,
            "task_id": task_id,
            "dispatch_id": dispatch_id,
            "terminal_handle": terminal_handle,
            "launch_nonce": launch_nonce,
            "outcome": effective_outcome,
            "body": body,
            "cleanup_confirmed": cleanup_confirmed,
            "notification_expected": send_allowed,
        }
        if task is not None:
            result["logical_task_id"] = task.task_id
        if evidence is not None:
            result["task_evidence"] = evidence
        delivery["orca_result"] = result
        write_state(
            state_path,
            state,
            require_existing=True,
            reservation_held=True,
        )
        if send_allowed:
            send_payload = (
                copy.deepcopy(state),
                copy.deepcopy(assignment),
                effective_outcome,
                body,
            )
    finally:
        if locked:
            reservation.release()

    if send_payload is not None:
        send_state, send_assignment, send_outcome, send_body = send_payload
        _send_worker_done(
            send_state,
            send_assignment,
            outcome=send_outcome,
            body=send_body,
        )
    return effective_outcome
