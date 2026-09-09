"""Named Orca task and Delivery operations around the saved Run snapshot."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from . import mcp_server as remote
from . import orca_delivery
from .adapters import remove_owned_tree
from .cleanup import (
    cleanup_assignment_phase,
    cleanup_journal_path,
    load_cleanup_journal,
)
from .contracts import (
    AckReceipt,
    Assignment,
    Attach,
    AttachReceipt,
    BackendRequest,
    BackendResult,
    CompletionIdentity,
    DeliveryAck,
    DeliveryRef,
    DispatchRef,
    ErrorCode,
    MessageReply,
    NodeRef,
    NormalizedEvent,
    Outcome,
    ReadReceipt,
    ReleaseReceipt,
    ReplyReceipt,
    Role,
    RoleGet,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleStatusReceipt,
    RoleTarget,
    RoleWait,
    RunRef,
    RuntimeFailure,
    StatusReceipt,
    StopResult,
    TaskBatchOpen,
    TaskBatchReceipt,
    TaskConsultationReply,
    TaskDispatch,
    TaskGet,
    TaskRef,
    TaskStatusReceipt,
    TaskVerify,
    TerminalRef,
    WaitReceipt,
    role_id,
    role_kind,
)
from .locking import _LifecycleReservation
from .mcp_protocol import MAX_READ_LINES, MAX_TIMEOUT_MS, MIN_TIMEOUT_MS
from .named_graph import GraphSpec
from .orca_controller import controller_key, controller_terminal, is_program
from .parallel_admission import admission_blocker
from .runtime import (
    MAX_PROMPT_CHARS,
    _prompt_path,
    validate_prompt_file,
    write_state,
)
from .task_execution import (
    acknowledge_task,
    answer_task_consultation,
    is_agent_parallel,
    is_plan_only,
    open_agent_batch,
    prepare_dispatch,
    task_consultation,
)
from .task_spec import TaskSpec, parse_task_specs
from .workspace_revision import snapshot_revision

if TYPE_CHECKING:
    from .backend import OrcaBackend

STOP_WAIT_SECONDS = 30.0


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise RuntimeFailure(code, message)


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(ErrorCode.IDENTITY_MISMATCH, f"Orca {field} is invalid")
    return cast(dict[str, object], value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"Orca {field} is invalid")
    return value


def _record(state: Mapping[str, object], task_id: str) -> dict[str, object]:
    tasks = _object(state.get("tasks"), "tasks")
    if task_id not in tasks:
        _fail(ErrorCode.INVALID_REQUEST, "task is unknown")
    return _object(tasks[task_id], "TaskSpec record")


def _completion_identity(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> CompletionIdentity:
    return CompletionIdentity(
        RunRef(_string(state.get("run_id"), "run_id")),
        TaskRef(_string(assignment.get("task_id"), "task_id")),
        DispatchRef(_string(assignment.get("dispatch_id"), "dispatch_id")),
        TerminalRef(_string(assignment.get("terminal_handle"), "terminal_handle")),
    )


def _is_parallel_state(state: Mapping[str, object]) -> bool:
    """Recognize the named Orca v5 parallel state shape.

    Full graph and state validation remains owned by the runtime validator and
    ``task_execution``.  This small discriminator only chooses the v4 serial
    or v5 parallel operation path without treating program/parallel state as
    an Orca task state.
    """

    graph = state.get("graph")
    coordination = graph.get("coordination") if isinstance(graph, Mapping) else None
    return (
        state.get("runtime") == "orca"
        and state.get("version") == 5
        and isinstance(coordination, Mapping)
        and coordination.get("mode") in {"agent", "program"}
        and coordination.get("dispatch_mode") == "parallel"
    )


def _delivery_containers(
    state: Mapping[str, object],
) -> tuple[tuple[str | None, Mapping[str, object]], ...]:
    try:
        return orca_delivery.containers(state)
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(ErrorCode.IDENTITY_MISMATCH, str(exc)) from exc


def _delivery_container(
    state: Mapping[str, object], node_id: str | None = None
) -> Mapping[str, object]:
    try:
        return orca_delivery.container(state, node_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(ErrorCode.IDENTITY_MISMATCH, str(exc)) from exc


def _delivery_dict(state: Mapping[str, object], node_id: str) -> dict[str, object]:
    value = _delivery_container(state, node_id)
    if not isinstance(value, dict):
        _fail(ErrorCode.IDENTITY_MISMATCH, "parallel Orca assignment is not mutable")
    return value


def _clear_delivery(container: dict[str, object]) -> None:
    for key in (
        "pending_delivery_id",
        "pending_delivery_kind",
        "pending_delivery_stage",
        "pending_question_ids",
        "replied_question_ids",
    ):
        container.pop(key, None)


def _release_is_closing(assignment: Mapping[str, object]) -> bool:
    release = assignment.get("orca_release")
    return isinstance(release, Mapping) and release.get("phase") == "closing"


class OrcaTasks:
    def __init__(self, backend: OrcaBackend, *, stopping: bool = False) -> None:
        self.backend = backend
        self.path = backend._state_path(backend._require_state())
        self.stopping = stopping

    @contextmanager
    def transaction(self, *, progress: bool = True) -> Iterator[dict[str, object]]:
        reservation = _LifecycleReservation(self.path, create_parent=False)
        reservation.acquire_for_publication()
        try:
            state = self.backend._reload_state_locked()
            parallel = _is_parallel_state(state)
            if state.get("runtime") != "orca" or (
                state.get("version") != 4 and not parallel
            ):
                _fail(ErrorCode.INVALID_REQUEST, "typed Orca tasks require named state")
            if parallel:
                try:
                    if not (is_agent_parallel(state) or is_program(state)):
                        _fail(
                            ErrorCode.INVALID_REQUEST,
                            "typed Orca tasks require named parallel state",
                        )
                except RuntimeFailure:
                    raise
                except (TypeError, ValueError, KeyError) as exc:
                    raise RuntimeFailure(
                        ErrorCode.IDENTITY_MISMATCH,
                        "saved parallel Orca graph is invalid",
                    ) from exc
            if progress:
                batch = state.get("orca_delivery_batch")
                if (
                    parallel
                    and isinstance(batch, Mapping)
                    and batch.get("phase") != "observed"
                    and not self.stopping
                ):
                    _fail(
                        ErrorCode.BUSY,
                        "Orca Run batch validation or ACK effect is unconfirmed",
                    )
                if not parallel and "pending_orca_effect" in state:
                    _fail(
                        ErrorCode.BUSY,
                        "Orca reply or acknowledgement effect is unconfirmed",
                    )
                if (
                    "pending_role_start" in state
                    or "pending_coordinator_start" in state
                ):
                    _fail(ErrorCode.BUSY, "Orca startup cleanup is pending")
                if self.stopping:
                    if state.get("orca_stop_requested") is not True:
                        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca stop flag disappeared")
                elif state.get("orca_stop_requested") is True:
                    _fail(ErrorCode.BUSY, "Orca team stop is pending")
            yield state
        finally:
            reservation.release()

    def save(self, state: dict[str, object]) -> None:
        write_state(self.path, state, require_existing=True, reservation_held=True)

    def _begin_effect(
        self,
        state: dict[str, object],
        operation: str,
        *,
        message_id: str | None = None,
        body: str | None = None,
    ) -> None:
        state["pending_orca_effect"] = {
            "operation": operation,
            "run_id": state["run_id"],
            controller_key(state): controller_terminal(state),
            "delivery_id": state["pending_delivery_id"],
            "message_id": message_id,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()
            if body is not None
            else None,
        }
        self.save(state)

    def _begin_parallel_reply(
        self,
        state: dict[str, object],
        assignment: dict[str, object],
        *,
        message_id: str,
        body: str,
    ) -> None:
        if assignment.get("pending_orca_effect") is not None:
            _fail(
                ErrorCode.BUSY,
                "Orca reply effect is unconfirmed",
            )
        assignment["pending_orca_effect"] = {
            "operation": "reply",
            "run_id": state["run_id"],
            controller_key(state): controller_terminal(state),
            "delivery_id": assignment["pending_delivery_id"],
            "message_id": message_id,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
        self.save(state)

    def _local_status(self, state: Mapping[str, object]) -> StatusReceipt | None:
        batch = state.get("orca_delivery_batch")
        batch_unconfirmed = (
            isinstance(batch, Mapping) and batch.get("phase") != "observed"
        )
        parallel_cleanup_pending = _is_parallel_state(state) and any(
            isinstance(assignment, Mapping)
            and ("pending_orca_effect" in assignment or _release_is_closing(assignment))
            for _node_id, assignment in _delivery_containers(state)
        )
        status = (
            "cleanup_pending"
            if "pending_role_start" in state
            or "pending_coordinator_start" in state
            or "pending_orca_effect" in state
            or parallel_cleanup_pending
            or batch_unconfirmed
            else "stopping"
            if state.get("orca_stop_requested") is True
            else None
        )
        if status is None:
            return None
        team_id, run_id = str(state["team_id"]), str(state["run_id"])
        self.backend.last_status_response = {
            "status": status,
            "team_id": team_id,
            "run_id": run_id,
            **(
                {"pending_coordinator_start": state["pending_coordinator_start"]}
                if "pending_coordinator_start" in state
                else {}
            ),
            **(
                {"pending_role_start": state["pending_role_start"]}
                if "pending_role_start" in state
                else {}
            ),
            **(
                {"pending_orca_effect": state["pending_orca_effect"]}
                if "pending_orca_effect" in state
                else {}
            ),
            **(
                {"orca_delivery_batch": copy.deepcopy(batch)}
                if batch is not None
                else {}
            ),
        }
        return StatusReceipt(status, team_id, RunRef(run_id))

    def status(self) -> StatusReceipt:
        with self.transaction(progress=False) as current:
            local = self._local_status(current)
            if local is not None:
                return local
            self.backend._retry_published_startup_marker(current)
            state = copy.deepcopy(current)
        receipt = self.backend._status_snapshot(state)
        with self.transaction(progress=False) as current:
            local = self._local_status(current)
            if local is not None:
                return local
            if (
                "orca_delivery_batch" in current
                and self.backend.last_status_response is not None
            ):
                self.backend.last_status_response["orca_delivery_batch"] = (
                    copy.deepcopy(current["orca_delivery_batch"])
                )
            return receipt

    def attach(self, request: Attach) -> AttachReceipt:
        role = request.role
        with self.transaction(progress=False) as current:
            self.backend._require_main_start_complete(current)
            self.backend._require_target(current, role)
            if current.get("orca_stop_requested") is True:
                _fail(ErrorCode.BUSY, "Orca team stop is pending")
            self.backend._retry_published_startup_marker(current)
            terminal_id = self.backend._terminal_for_role(current, role)
            expected = (
                copy.deepcopy(self.assignment(current, role))
                if role_kind(role) is not Role.MAIN
                else None
            )
            state = copy.deepcopy(current)
        self.verify_remote(state, role if expected is not None else None)
        workspace = Path(str(state["workspace"]))
        self.backend._verify_terminal(
            terminal_id=terminal_id,
            workspace=workspace,
            worktree_id=str(state["worktree_id"]),
            title=f"{state['team_id']}-{'main' if expected is None else role_id(role)}",
        )
        with self.transaction(progress=False) as current:
            self.backend._require_main_start_complete(current)
            if current.get("orca_stop_requested") is True:
                _fail(ErrorCode.BUSY, "Orca team stop is pending")
            if self.backend._terminal_for_role(current, role) != terminal_id:
                _fail(ErrorCode.IDENTITY_MISMATCH, "Orca attach target changed")
            if expected is not None:
                current_assignment = self.assignment(current, role, expected)
                if (
                    "orca_release" in current_assignment
                    if _is_parallel_state(current)
                    else "orca_release" in current
                ):
                    _fail(ErrorCode.BUSY, "Orca terminal release is in progress")
            verdict = self.backend._client.terminal_switch(
                terminal_id=terminal_id, cwd=workspace
            )
            self.backend._require_terminal_switch(verdict, terminal_id=terminal_id)
        self.backend.last_attach_response = {
            "status": "focused",
            "role": role_id(role),
            "terminal": terminal_id,
        }
        return AttachReceipt(
            role, TerminalRef(terminal_id), RunRef(str(state["run_id"]))
        )

    def stop(self) -> StopResult:
        if _is_parallel_state(self.backend._require_state()):
            return self._parallel_stop()
        with self.transaction(progress=False) as state:
            if "pending_orca_effect" in state:
                _fail(
                    ErrorCode.BUSY,
                    "Orca reply or acknowledgement effect is unconfirmed",
                )
            if "pending_role_start" in state or "pending_coordinator_start" in state:
                _fail(ErrorCode.BUSY, "Orca startup cleanup is pending")
            if any(
                _object(record, "task").get("status") == "verifying"
                for record in _object(state["tasks"], "tasks").values()
            ):
                _fail(ErrorCode.BUSY, "verification cleanup is unconfirmed")
            release = state.get("orca_release")
            if isinstance(release, Mapping) and release.get("phase") == "closing":
                _fail(ErrorCode.BUSY, "Orca terminal cleanup is unconfirmed")
            journal_path = cleanup_journal_path(self.path)
            if journal_path.exists() or journal_path.is_symlink():
                journal = load_cleanup_journal(state, self.path)
                if journal["coordinator" if is_program(state) else "main"] in {
                    "started",
                    "unknown",
                } or any(
                    _object(entry, "cleanup assignment")["remote"]
                    in {"worker_started", "terminal_started", "unknown"}
                    for entry in cast(list[object], journal["assignments"])
                ):
                    _fail(ErrorCode.BUSY, "Orca remote cleanup is unconfirmed")
            state["orca_stop_requested"] = True
            self.save(state)
            roles = _object(state["roles"], "roles")
            expected = (
                copy.deepcopy(_object(next(iter(roles.values())), "assignment"))
                if roles
                else None
            )
            target = (
                NodeRef(str(expected["role"]), Role(str(expected["role_kind"])))
                if isinstance(expected, Mapping)
                else None
            )
        draining = OrcaTasks(self.backend, stopping=True)
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        while True:
            with draining.transaction() as state:
                if target is not None:
                    draining.assignment(state, target, expected)
                completion = state.get("orca_result")
                if completion is not None:
                    if (
                        _object(completion, "result").get("cleanup_confirmed")
                        is not True
                    ):
                        _fail(ErrorCode.BUSY, "ACP process cleanup is unconfirmed")
                    break
                if target is None:
                    break
            if time.monotonic() >= deadline:
                _fail(
                    ErrorCode.BUSY,
                    "ACP process cleanup is unconfirmed after stop request",
                )
            time.sleep(0.05)
        if (
            isinstance(completion, Mapping)
            and completion["notification_expected"] is True
        ):
            target = NodeRef(
                str(completion["role"]), Role(str(completion["role_kind"]))
            )
            with draining.transaction() as state:
                pending_kind = state.get("pending_delivery_kind")
                stage = state.get("pending_delivery_stage")
                if pending_kind not in {None, "worker_done"} or stage == "invalid":
                    _fail(
                        ErrorCode.ORDER_VIOLATION,
                        "inspect the pending Orca Delivery before stop",
                    )
            if pending_kind is None:
                while True:
                    if time.monotonic() >= deadline:
                        _fail(ErrorCode.BUSY, "Orca completion Delivery is unconfirmed")
                    receipt = draining.wait(RoleWait(target, MIN_TIMEOUT_MS))
                    if receipt.delivery_id is not None:
                        break
                    if time.monotonic() >= deadline:
                        _fail(ErrorCode.BUSY, "Orca completion Delivery is unconfirmed")
                    time.sleep(0.05)
                stage = "observed"
            if stage == "observed":
                draining.read(RoleRead(target, MAX_READ_LINES))
                stage = "read"
            if stage == "read":
                draining.release(RoleRelease(target))
            with draining.transaction() as state:
                delivery_id = _string(state.get("pending_delivery_id"), "Delivery ID")
            draining.ack(DeliveryAck(DeliveryRef(delivery_id)))
        self.backend._await_program_exit()
        with draining.transaction():
            return self.backend._stop_locked()

    def _parallel_stop(self) -> StopResult:
        from .orca_parallel_stop import stop_parallel

        return stop_parallel(self)

    def verify_remote(
        self, state: Mapping[str, object], role: RoleTarget | None = None
    ) -> None:
        workspace = Path(_string(state.get("workspace"), "workspace"))
        worktree_id = _string(state.get("worktree_id"), "worktree_id")
        self.backend._verify_worktree(workspace=workspace, worktree_id=worktree_id)
        self.backend._verify_run(
            run_id=_string(state.get("run_id"), "run_id"),
            team_id=_string(state.get("team_id"), "team_id"),
            workspace=workspace,
            coordinator_handle=controller_terminal(state),
        )
        if role is not None:
            assignment = (
                self.assignment(state, role)
                if _is_parallel_state(state)
                else self.backend._assignment_for_role(state, role)
            )
            self.backend._verify_assignment(
                assignment=assignment,
                role=role_id(role),
                run_id=_string(state.get("run_id"), "run_id"),
                worktree_id=worktree_id,
                workspace=workspace,
            )

    def assignment(
        self,
        state: Mapping[str, object],
        role: RoleTarget,
        expected: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if _is_parallel_state(state):
            if not isinstance(role, NodeRef) or role_kind(role) is Role.MAIN:
                _fail(
                    ErrorCode.INVALID_REQUEST,
                    "parallel Orca requires an exact named node",
                )
            self.backend._require_target(state, role)
            raw = _delivery_container(state, role.node_id)
            if not isinstance(raw, dict):
                _fail(ErrorCode.IDENTITY_MISMATCH, "Orca assignment is invalid")
            assignment = raw
            if (
                assignment.get("role") != role.node_id
                or assignment.get("role_kind") != role.kind.value
            ):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca assignment identity does not match its node",
                )
        else:
            assignment = self.backend._assignment_for_role(state, role)
        if expected is not None and any(
            assignment.get(key) != expected.get(key)
            for key in (
                "role",
                "role_kind",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
                "prompt_path",
                "provider_private_root",
                "snapshot_root",
                "task_spec",
                "task_stage",
                "task_revision",
                "task_workspace_revision",
            )
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "Orca assignment changed during remote operation",
            )
        return assignment

    def _parallel_progress(self, assignment: Mapping[str, object]) -> None:
        if "pending_orca_effect" in assignment:
            _fail(
                ErrorCode.BUSY,
                "Orca reply or acknowledgement effect is unconfirmed",
            )

    def execute(self, request: BackendRequest) -> BackendResult:
        if isinstance(request, (RolePrompt, TaskDispatch)):
            return self.prompt(request)
        if isinstance(request, RoleWait):
            return self.wait(request)
        if isinstance(request, RoleRead):
            return self.read(request)
        if isinstance(request, RoleRelease):
            return self.release(request)
        if isinstance(request, MessageReply):
            return self.reply(request)
        if isinstance(request, DeliveryAck):
            return self.ack(request)
        if isinstance(request, TaskVerify):
            return self.verify(request)
        if isinstance(request, TaskBatchOpen):
            with self.transaction() as state:
                if not _is_parallel_state(state):
                    _fail(
                        ErrorCode.INVALID_REQUEST,
                        "task batches require named Orca agent parallel state",
                    )
                batch = open_agent_batch(state, request.task_ids)
                self.save(state)
                return TaskBatchReceipt(
                    tuple(cast(list[str], batch["task_ids"])),
                    str(batch["phase"]),
                    cast(str | None, batch["revision"]),
                )
        with self.transaction(
            progress=not isinstance(request, (TaskGet, RoleGet))
        ) as state:
            if isinstance(request, TaskGet):
                record = _record(state, request.task_id)
                consultation = task_consultation(state, record)
                return TaskStatusReceipt(
                    request.task_id,
                    str(record["status"]),
                    copy.deepcopy(
                        {
                            **record,
                            **(
                                {"consultation": consultation}
                                if consultation is not None
                                else {}
                            ),
                        }
                    ),
                )
            if isinstance(request, TaskConsultationReply):
                record = answer_task_consultation(
                    state, request.consultation_id, request.body
                )
                self.save(state)
                task = TaskSpec.from_dict(record["spec"])
                return TaskStatusReceipt(
                    task.task_id, str(record["status"]), copy.deepcopy(record)
                )
            if isinstance(request, RoleGet):
                assignment = self.assignment(state, request.role)
                return RoleStatusReceipt(
                    request.role,
                    "completed"
                    if "orca_result"
                    in (assignment if _is_parallel_state(state) else state)
                    else "running",
                )
        _fail(ErrorCode.INVALID_REQUEST, "unsupported named Orca request")

    def prompt(self, request: RolePrompt | TaskDispatch) -> Assignment:
        from .orca_dispatch import start_assignment

        if _is_parallel_state(self.backend._require_state()):
            return self._parallel_prompt(request, start_assignment)

        if (
            not isinstance(request.role, NodeRef)
            or role_kind(request.role) is Role.MAIN
        ):
            _fail(ErrorCode.INVALID_REQUEST, "named Orca requires a dispatchable node")
        if role_kind(request.role) is Role.WORKER and not isinstance(
            request, TaskDispatch
        ):
            _fail(
                ErrorCode.INVALID_REQUEST,
                "Worker requires task_dispatch with a TaskSpec",
            )
        text = request.message if isinstance(request, TaskDispatch) else request.text
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_PROMPT_CHARS
        ):
            _fail(
                ErrorCode.INVALID_REQUEST,
                "role prompt is empty or exceeds the character limit",
            )
        with self.transaction() as saved:
            self.backend._require_target(saved, request.role)
            if (
                saved.get("roles")
                or saved.get("pending_delivery_id") is not None
                or "orca_result" in saved
            ):
                _fail(
                    ErrorCode.BUSY,
                    "consume the active role and Delivery before dispatch",
                )
            if any(
                _object(record, "task").get("status") == "verifying"
                for record in _object(saved["tasks"], "tasks").values()
            ):
                _fail(
                    ErrorCode.BUSY,
                    "verification cleanup must be confirmed before dispatch",
                )
            state = copy.deepcopy(saved)
            record = None
            if isinstance(request, TaskDispatch):
                prior = _object(state["tasks"], "tasks").get(request.task.task_id)
                revision = None
                workspace_revision = None
                if role_kind(request.role) is Role.REVIEWER:
                    if (
                        isinstance(prior, Mapping)
                        and prior.get("status") == "awaiting_implementation_review"
                    ):
                        revision = snapshot_revision(Path(str(state["workspace"])))
                    elif is_plan_only(state, request.task.task_id):
                        workspace_revision = snapshot_revision(
                            Path(str(state["workspace"]))
                        )
                record, text = prepare_dispatch(
                    state,
                    request,
                    revision=revision,
                    workspace_revision=workspace_revision,
                )
                if len(text) > MAX_PROMPT_CHARS:
                    _fail(
                        ErrorCode.INVALID_REQUEST,
                        "TaskSpec prompt exceeds character limit",
                    )
            self.verify_remote(saved)
            return start_assignment(
                self.path, state, request, task_record=record, text=text
            )

    def _parallel_prompt(
        self,
        request: RolePrompt | TaskDispatch,
        start_assignment: Callable[..., Assignment],
    ) -> Assignment:
        if not isinstance(request, TaskDispatch) or not isinstance(
            request.role, NodeRef
        ):
            _fail(
                ErrorCode.INVALID_REQUEST,
                "parallel Orca requires a named TaskDispatch with a declared TaskSpec",
            )
        if role_kind(request.role) is Role.MAIN:
            _fail(ErrorCode.INVALID_REQUEST, "parallel Orca cannot dispatch Main")
        text = request.message
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_PROMPT_CHARS
        ):
            _fail(
                ErrorCode.INVALID_REQUEST,
                "role prompt is empty or exceeds the character limit",
            )
        with self.transaction() as saved:
            self.backend._require_target(saved, request.role)
            roles = _object(saved.get("roles"), "roles")
            if request.role.node_id in roles:
                self._parallel_progress(_delivery_dict(saved, request.role.node_id))
            try:
                blocker = admission_blocker(
                    GraphSpec.from_dict(saved.get("graph")),
                    parse_task_specs(saved.get("task_specs")),
                    cast(Mapping[str, Mapping[str, object]], roles),
                    request.role,
                    request.task,
                )
            except RuntimeFailure:
                raise
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "saved parallel Orca admission state is invalid",
                ) from exc
            if blocker is not None:
                _fail(ErrorCode.BUSY, blocker)
            state = copy.deepcopy(saved)
            prior = _object(state["tasks"], "tasks").get(request.task.task_id)
            revision = None
            workspace_revision = None
            if role_kind(request.role) is Role.REVIEWER:
                if (
                    isinstance(prior, Mapping)
                    and prior.get("status") == "awaiting_implementation_review"
                ):
                    revision = snapshot_revision(Path(str(state["workspace"])))
                elif is_plan_only(state, request.task.task_id):
                    workspace_revision = snapshot_revision(
                        Path(str(state["workspace"]))
                    )
            record, prepared_text = prepare_dispatch(
                state,
                request,
                revision=revision,
                workspace_revision=workspace_revision,
            )
            if len(prepared_text) > MAX_PROMPT_CHARS:
                _fail(
                    ErrorCode.INVALID_REQUEST,
                    "TaskSpec prompt exceeds character limit",
                )
            self.verify_remote(saved)
            return start_assignment(
                self.path,
                state,
                request,
                task_record=record,
                text=prepared_text,
            )

    def _parallel_delivery_target(
        self,
        state: Mapping[str, object],
        result: dict[str, object],
    ) -> tuple[str, str, list[str]] | None:
        candidates: list[tuple[str, str, list[str]]] = []
        for node_id, raw in _delivery_containers(state):
            if node_id is None or not isinstance(raw, Mapping):
                continue
            assignment = copy.deepcopy(dict(raw))
            dispatch_id = assignment.get("dispatch_id")
            if not isinstance(dispatch_id, str) or not dispatch_id:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "parallel Orca assignment is missing Dispatch identity",
                )
            try:
                observed = remote._observe_delivery(result, assignment, dispatch_id)
            except (RuntimeError, TypeError, ValueError) as exc:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "Orca Delivery could not be validated",
                ) from exc
            if observed is not None:
                candidates.append((node_id, observed[0], observed[1]))
        if len(candidates) > 1:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "Orca Delivery matches more than one assignment",
            )
        return candidates[0] if candidates else None

    def _parallel_observe_delivery(
        self,
        state: dict[str, object],
        node_id: str,
        result: dict[str, object],
        delivery_id: str,
    ) -> WaitReceipt:
        assignment = _delivery_dict(state, node_id)
        existing = assignment.get("pending_delivery_id")
        if existing is not None:
            if existing == delivery_id:
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "acknowledge the pending Delivery before waiting again",
                )
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "parallel Orca assignment already has a pending Delivery",
            )
        kind = remote._delivery_kind(result)
        try:
            observed = remote._observe_delivery(
                result,
                assignment,
                _string(assignment.get("dispatch_id"), "dispatch_id"),
            )
            if observed is None:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca Delivery does not match its assignment",
                )
            observed_kind, question_ids = observed
            messages = result.get("messages")
            if not isinstance(messages, list) or any(
                not isinstance(message, dict) for message in messages
            ):
                _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "Orca messages are invalid")
            typed_messages = cast(list[dict[str, object]], messages)
            if any(
                message.get("run_id") != state.get("run_id")
                for message in typed_messages
            ):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH, "Orca message Run does not match state"
                )
            identity = _completion_identity(state, assignment)
            if observed_kind == "worker_done":
                completion = _object(assignment.get("orca_result"), "trusted result")
                payload = _object(
                    remote._message_payload(typed_messages[0]),
                    "completion payload",
                )
                if (
                    completion.get("cleanup_confirmed") is not True
                    or completion.get("outcome") != payload.get("outcome")
                    or completion.get("body") != typed_messages[0].get("body")
                ):
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca worker_done differs from the trusted runner result",
                    )
                completion["delivery_id"] = delivery_id
                events = (
                    NormalizedEvent.worker_done(
                        identity=identity,
                        outcome=Outcome(str(completion["outcome"])),
                        body=str(completion["body"]),
                        delivery_id=DeliveryRef(delivery_id),
                    ),
                )
            elif observed_kind == "question":
                from .orca_questions import observe_question

                if len(typed_messages) != 1:
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca form requires one question message",
                    )
                events = (
                    observe_question(state, assignment, typed_messages[0], delivery_id),
                )
            else:
                events = (
                    NormalizedEvent.escalation(
                        identity=identity,
                        delivery_id=DeliveryRef(delivery_id),
                        body=_string(typed_messages[0].get("body"), "escalation body"),
                    ),
                )
            assignment.update(
                {
                    "pending_delivery_id": delivery_id,
                    "pending_delivery_kind": observed_kind,
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": question_ids,
                    "replied_question_ids": [],
                }
            )
        except (RuntimeFailure, RuntimeError, ValueError, TypeError):
            assignment.update(
                {
                    "pending_delivery_id": delivery_id,
                    "pending_delivery_kind": kind,
                    "pending_delivery_stage": "invalid",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                }
            )
            raise
        return WaitReceipt(DeliveryRef(delivery_id), events)

    @staticmethod
    def _batch_record(result: Mapping[str, object]) -> dict[str, object]:
        messages = result.get("messages")
        return {
            "delivery_id": _string(result.get("deliveryId"), "Delivery ID"),
            "phase": "observed",
            "members": [],
            "message_count": len(messages) if isinstance(messages, list) else None,
            "messages_sha256": hashlib.sha256(
                json.dumps(
                    messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "error": None,
        }

    @staticmethod
    def _require_no_batch(state: Mapping[str, object]) -> None:
        batch = state.get("orca_delivery_batch")
        if isinstance(batch, Mapping):
            _fail(
                ErrorCode.ORDER_VIOLATION,
                f"process every event and acknowledge pending Run Delivery {batch['delivery_id']} before waiting again",
            )

    def _parallel_wait(self, request: RoleWait) -> WaitReceipt:
        target = request.role
        if not isinstance(target, NodeRef) or role_kind(target) is Role.MAIN:
            _fail(ErrorCode.INVALID_REQUEST, "parallel Orca wait requires a named node")
        with self.transaction() as current:
            self._parallel_progress(self.assignment(current, target))
            self._require_no_batch(current)
            state = copy.deepcopy(current)
        self.verify_remote(state, target)
        result = remote.run_orca(
            state,
            [
                "orchestration",
                "check",
                "--terminal",
                controller_terminal(state),
                "--run",
                str(state["run_id"]),
                "--wait",
                "--types",
                "worker_done,escalation,question",
                "--timeout-ms",
                str(request.timeout_ms),
                "--json",
            ],
            timeout_ms=request.timeout_ms + 5_000,
        )
        if result.get("deliveryId") is None:
            if result.get("messages"):
                _fail(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "Orca messages have no Delivery ID",
                )
            return WaitReceipt(None, ())
        batch = self._batch_record(result)
        delivery_id = str(batch["delivery_id"])
        messages = result.get("messages")
        selected: list[tuple[str, dict[str, object]]] = []
        invalid: RuntimeFailure | None = None
        try:
            if not isinstance(messages, list) or not messages:
                _fail(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "Orca Delivery messages must be a nonempty array",
                )
            for message in messages:
                if not isinstance(message, dict):
                    _fail(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "Orca Delivery message is invalid",
                    )
                owner_match = self._parallel_delivery_target(
                    state, {"messages": [message]}
                )
                if owner_match is None:
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca Delivery message has no matching assignment",
                    )
                node_id = owner_match[0]
                if any(previous == node_id for previous, _message in selected):
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca Delivery contains conflicting events for one assignment",
                    )
                selected.append((node_id, message))
        except RuntimeFailure as exc:
            invalid = exc
        with self.transaction() as current:
            self._require_no_batch(current)
            # A stale remote response must never acquire the next assignment's state.
            for node_id, _message in selected:
                expected = _delivery_dict(state, node_id)
                self.assignment(
                    current,
                    NodeRef(node_id, Role(str(expected["role_kind"]))),
                    expected,
                )
            proposed = copy.deepcopy(current)
            events: list[NormalizedEvent] = []
            members: list[dict[str, object]] = []
            if invalid is None:
                try:
                    for node_id, message in selected:
                        owner = _delivery_dict(proposed, node_id)
                        receipt = self._parallel_observe_delivery(
                            proposed,
                            node_id,
                            {"messages": [message]},
                            delivery_id,
                        )
                        members.append(
                            {
                                **{
                                    field: owner[field]
                                    for field in (
                                        "role",
                                        "role_kind",
                                        "task_id",
                                        "dispatch_id",
                                        "terminal_handle",
                                        "launch_nonce",
                                    )
                                },
                                "message_id": _string(message.get("id"), "message ID"),
                                "kind": message["type"],
                            }
                        )
                        events.extend(receipt.events)
                    if len({row["message_id"] for row in members}) != len(members):
                        _fail(
                            ErrorCode.IDENTITY_MISMATCH,
                            "Orca Delivery contains duplicate message IDs",
                        )
                except (RuntimeFailure, RuntimeError, ValueError, TypeError) as exc:
                    invalid = (
                        exc
                        if isinstance(exc, RuntimeFailure)
                        else RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            str(exc)[:240],
                        )
                    )
            if invalid is not None:
                expected_roles = _object(state["roles"], "roles")
                if set(_object(current["roles"], "roles")) != set(expected_roles):
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca assignment changed during remote operation",
                    )
                for node_id, raw in expected_roles.items():
                    expected = _object(raw, "assignment")
                    self.assignment(
                        current,
                        NodeRef(node_id, Role(str(expected["role_kind"]))),
                        expected,
                    )
                current["orca_delivery_batch"] = {
                    **batch,
                    "phase": "invalid",
                    "error": str(invalid)[:240],
                }
                self.save(current)
                raise invalid
            proposed["orca_delivery_batch"] = {**batch, "members": members}
            self.save(proposed)
        return WaitReceipt(DeliveryRef(delivery_id), tuple(events))

    def wait(self, request: RoleWait) -> WaitReceipt:
        if (
            type(request.timeout_ms) is not int
            or not MIN_TIMEOUT_MS <= request.timeout_ms <= MAX_TIMEOUT_MS
        ):
            _fail(ErrorCode.INVALID_REQUEST, "Orca wait timeout is invalid")
        if _is_parallel_state(self.backend._require_state()):
            return self._parallel_wait(request)
        with self.transaction() as current:
            assignment = copy.deepcopy(self.assignment(current, request.role))
            if current.get("pending_delivery_id") is not None:
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "acknowledge the pending Delivery before waiting again",
                )
            state = copy.deepcopy(current)
        self.verify_remote(state, request.role)
        result = remote.run_orca(
            state,
            [
                "orchestration",
                "check",
                "--terminal",
                controller_terminal(state),
                "--run",
                str(state["run_id"]),
                "--wait",
                "--types",
                "worker_done,escalation,question",
                "--timeout-ms",
                str(request.timeout_ms),
                "--json",
            ],
            timeout_ms=request.timeout_ms + 5000,
        )
        with self.transaction() as current:
            active = self.assignment(current, request.role, assignment)
            if current.get("pending_delivery_id") is not None:
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "another Delivery was observed during wait",
                )
            delivery_id = result.get("deliveryId")
            if delivery_id is None:
                if result.get("messages"):
                    _fail(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "Orca messages have no Delivery ID",
                    )
                return WaitReceipt(None, ())
            delivery_id = _string(delivery_id, "Delivery ID")
            proposed = copy.deepcopy(current)
            proposed_assignment = self.assignment(proposed, request.role, active)
            kind = remote._delivery_kind(result)
            try:
                observed = remote._observe_delivery(
                    result, proposed_assignment, str(active["dispatch_id"])
                )
                if observed is None:
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca Delivery does not match the active assignment",
                    )
                observed_kind, question_ids = observed
                messages = cast(list[dict[str, object]], result["messages"])
                if any(
                    message.get("run_id") != proposed["run_id"] for message in messages
                ):
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca message Run does not match state",
                    )
                identity = _completion_identity(proposed, proposed_assignment)
                if observed_kind == "worker_done":
                    completion = _object(proposed.get("orca_result"), "trusted result")
                    payload = _object(
                        remote._message_payload(messages[0]), "completion payload"
                    )
                    if (
                        completion.get("cleanup_confirmed") is not True
                        or completion.get("outcome") != payload.get("outcome")
                        or completion.get("body") != messages[0].get("body")
                    ):
                        _fail(
                            ErrorCode.IDENTITY_MISMATCH,
                            "Orca worker_done differs from the trusted runner result",
                        )
                    completion["delivery_id"] = delivery_id
                    events = (
                        NormalizedEvent.worker_done(
                            identity=identity,
                            outcome=Outcome(str(completion["outcome"])),
                            body=str(completion["body"]),
                            delivery_id=DeliveryRef(delivery_id),
                        ),
                    )
                elif observed_kind == "question":
                    from .orca_questions import observe_question

                    if len(messages) != 1:
                        _fail(
                            ErrorCode.IDENTITY_MISMATCH,
                            "Orca form requires one question message",
                        )
                    events = (
                        observe_question(
                            proposed, proposed_assignment, messages[0], delivery_id
                        ),
                    )
                else:
                    events = (
                        NormalizedEvent.escalation(
                            identity=identity,
                            delivery_id=DeliveryRef(delivery_id),
                            body=_string(messages[0].get("body"), "escalation body"),
                        ),
                    )
                proposed.update(
                    {
                        "pending_delivery_id": delivery_id,
                        "pending_delivery_kind": observed_kind,
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": question_ids,
                        "replied_question_ids": [],
                    }
                )
            except (RuntimeFailure, RuntimeError, ValueError, TypeError):
                # Retaining an invalid remote Delivery must not erase a trusted
                # runner result that arrived before the malformed event.
                current.update(
                    {
                        "pending_delivery_id": delivery_id,
                        "pending_delivery_kind": kind,
                        "pending_delivery_stage": "invalid",
                        "pending_question_ids": [],
                        "replied_question_ids": [],
                    }
                )
                self.save(current)
                raise
            self.save(proposed)
            return WaitReceipt(DeliveryRef(delivery_id), events)

    def _completed(
        self, state: Mapping[str, object], role: RoleTarget, *, stage: str
    ) -> tuple[dict[str, object], dict[str, object]]:
        assignment = self.assignment(state, role)
        completion = _object(state.get("orca_result"), "trusted result")
        if (
            state.get("pending_delivery_kind") != "worker_done"
            or state.get("pending_delivery_stage") != stage
            or completion.get("delivery_id") != state.get("pending_delivery_id")
            or completion.get("role") != role_id(role)
            or assignment.get("completion_observed") is not True
        ):
            _fail(ErrorCode.ORDER_VIOLATION, f"Orca completion requires stage {stage}")
        return assignment, completion

    def _parallel_completed(
        self, state: Mapping[str, object], role: RoleTarget, *, stage: str
    ) -> tuple[dict[str, object], dict[str, object]]:
        if not isinstance(role, NodeRef) or role_kind(role) is Role.MAIN:
            _fail(ErrorCode.INVALID_REQUEST, "parallel Orca requires a named node")
        assignment = self.assignment(state, role)
        completion = _object(assignment.get("orca_result"), "trusted result")
        if (
            assignment.get("pending_delivery_kind") != "worker_done"
            or assignment.get("pending_delivery_stage") != stage
            or completion.get("delivery_id") != assignment.get("pending_delivery_id")
            or completion.get("role") != role_id(role)
            or assignment.get("completion_observed") is not True
        ):
            _fail(ErrorCode.ORDER_VIOLATION, f"Orca completion requires stage {stage}")
        return assignment, completion

    def read(self, request: RoleRead) -> ReadReceipt:
        if type(request.lines) is not int or not 1 <= request.lines <= MAX_READ_LINES:
            _fail(ErrorCode.INVALID_REQUEST, "Orca read line limit is invalid")
        if _is_parallel_state(self.backend._require_state()):
            return self._parallel_read(request)
        with self.transaction() as current:
            assignment, completion = self._completed(
                current, request.role, stage="observed"
            )
            expected = copy.deepcopy(assignment)
            state = copy.deepcopy(current)
        self.verify_remote(state, request.role)
        result = remote.run_orca(
            state,
            [
                "orchestration",
                "worker-read",
                "--dispatch",
                str(expected["dispatch_id"]),
                "--limit",
                str(request.lines),
                "--json",
            ],
        )
        remote._validate_worker_read_response(
            result,
            dispatch_id=str(expected["dispatch_id"]),
            terminal_handle=str(expected["terminal_handle"]),
        )
        with self.transaction() as current:
            self.assignment(current, request.role, expected)
            _, completion = self._completed(current, request.role, stage="observed")
            current["pending_delivery_stage"] = "read"
            self.save(current)
            return ReadReceipt(str(completion["body"]))

    def _parallel_read(self, request: RoleRead) -> ReadReceipt:
        if (
            not isinstance(request.role, NodeRef)
            or role_kind(request.role) is Role.MAIN
        ):
            _fail(ErrorCode.INVALID_REQUEST, "parallel Orca read requires a named node")
        with self.transaction() as current:
            self._parallel_progress(self.assignment(current, request.role))
            assignment, completion = self._parallel_completed(
                current, request.role, stage="observed"
            )
            expected = copy.deepcopy(assignment)
            state = copy.deepcopy(current)
        self.verify_remote(state, request.role)
        result = remote.run_orca(
            state,
            [
                "orchestration",
                "worker-read",
                "--dispatch",
                str(expected["dispatch_id"]),
                "--limit",
                str(request.lines),
                "--json",
            ],
        )
        remote._validate_worker_read_response(
            result,
            dispatch_id=str(expected["dispatch_id"]),
            terminal_handle=str(expected["terminal_handle"]),
        )
        with self.transaction() as current:
            self.assignment(current, request.role, expected)
            _assignment, completion = self._parallel_completed(
                current, request.role, stage="observed"
            )
            _delivery_dict(current, request.role.node_id)["pending_delivery_stage"] = (
                "read"
            )
            self.save(current)
            return ReadReceipt(str(completion["body"]))

    def release(self, request: RoleRelease) -> ReleaseReceipt:
        if _is_parallel_state(self.backend._require_state()):
            return self._parallel_release(request)
        with self.transaction() as current:
            assignment, completion = self._completed(
                current, request.role, stage="read"
            )
            if completion.get("cleanup_confirmed") is not True:
                _fail(ErrorCode.BUSY, "ACP process cleanup is unconfirmed")
            expected = copy.deepcopy(assignment)
            state = copy.deepcopy(current)
            release = current.get("orca_release")
            if isinstance(release, Mapping) and release.get("phase") != "closed":
                _fail(
                    ErrorCode.BUSY,
                    "Orca terminal close effect is unconfirmed; ownership evidence is retained",
                )
            if release is None:
                validate_prompt_file(
                    Path(str(assignment["prompt_path"])),
                    self.path.parent,
                    role=request.role,
                    launch_nonce=str(assignment["launch_nonce"]),
                )
        if release is None:
            self.verify_remote(state, request.role)
            released = remote.run_orca(
                state,
                [
                    "orchestration",
                    "worker-release",
                    "--dispatch",
                    str(expected["dispatch_id"]),
                    "--json",
                ],
            )
            if (
                released.get("dispatchId") != expected["dispatch_id"]
                or released.get("state") != "retained"
                or released.get("reason") != "no_owned_resource"
                or released.get("processAction") != "none"
                or released.get("archive") is not None
            ):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "named Orca Dispatch no longer has context-only terminal ownership",
                )
            with self.transaction() as current:
                self.assignment(current, request.role, expected)
                _, completion = self._completed(current, request.role, stage="read")
                if "orca_release" in current:
                    _fail(
                        ErrorCode.BUSY, "Orca terminal release is already in progress"
                    )
                current["orca_release"] = {
                    "phase": "closing",
                    "identity": {
                        field: completion[field]
                        for field in (
                            "role",
                            "role_kind",
                            "run_id",
                            "task_id",
                            "dispatch_id",
                            "terminal_handle",
                            "launch_nonce",
                            "delivery_id",
                        )
                    },
                    "terminal_close": None,
                }
                self.save(current)
            # This Terminal was created by this launcher, outside Orca's supervised
            # worker resource table.  worker-release does not prove its cleanup.
            closed = self.backend._client.terminal_close(
                terminal_id=str(expected["terminal_handle"]),
                cwd=Path(str(state["workspace"])),
            )
            self.backend._require_terminal_close(
                closed, terminal_id=str(expected["terminal_handle"])
            )
            with self.transaction() as current:
                self.assignment(current, request.role, expected)
                self._completed(current, request.role, stage="read")
                release_record = _object(current.get("orca_release"), "release receipt")
                if release_record.get("phase") != "closing":
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca terminal release phase changed",
                    )
                release_record.update(
                    {"phase": "closed", "terminal_close": asdict(closed)}
                )
                self.save(current)
        with self.transaction() as current:
            self.assignment(current, request.role, expected)
            self._completed(current, request.role, stage="read")
            release_record = _object(current.get("orca_release"), "release receipt")
            if release_record.get("phase") != "closed":
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca terminal cleanup is not confirmed",
                )
            expected_prompt = _prompt_path(
                self.path.parent, request.role, str(expected["launch_nonce"])
            )
            if Path(str(expected["prompt_path"])).resolve(
                strict=False
            ) != expected_prompt.resolve(strict=False):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca prompt path does not match its launch identity",
                )
            for field in ("snapshot_root", "provider_private_root"):
                remove_owned_tree(Path(str(expected[field])))
            cleanup_assignment_phase(
                request.role,
                expected,
                state_path=self.path,
                execution="background",
                transport="acp",
                local_stage="prompt_started",
                remove_tree=remove_owned_tree,
            )
            _object(current["roles"], "roles").pop(role_id(request.role))
            current["pending_delivery_stage"] = "released"
            release_record["phase"] = "released"
            self.save(current)
        return ReleaseReceipt("released")

    def _parallel_release(self, request: RoleRelease) -> ReleaseReceipt:
        if (
            not isinstance(request.role, NodeRef)
            or role_kind(request.role) is Role.MAIN
        ):
            _fail(
                ErrorCode.INVALID_REQUEST, "parallel Orca release requires a named node"
            )
        with self.transaction() as current:
            assignment, completion = self._parallel_completed(
                current, request.role, stage="read"
            )
            self._parallel_progress(assignment)
            if completion.get("cleanup_confirmed") is not True:
                _fail(ErrorCode.BUSY, "ACP process cleanup is unconfirmed")
            expected = copy.deepcopy(assignment)
            state = copy.deepcopy(current)
            release = assignment.get("orca_release")
            if isinstance(release, Mapping) and release.get("phase") != "closed":
                _fail(
                    ErrorCode.BUSY,
                    "Orca terminal close effect is unconfirmed; ownership evidence is retained",
                )
            if release is None:
                validate_prompt_file(
                    Path(str(assignment["prompt_path"])),
                    self.path.parent,
                    role=request.role,
                    launch_nonce=str(assignment["launch_nonce"]),
                )
        if release is None:
            self.verify_remote(state, request.role)
            released = remote.run_orca(
                state,
                [
                    "orchestration",
                    "worker-release",
                    "--dispatch",
                    str(expected["dispatch_id"]),
                    "--json",
                ],
            )
            if (
                released.get("dispatchId") != expected["dispatch_id"]
                or released.get("state") != "retained"
                or released.get("reason") != "no_owned_resource"
                or released.get("processAction") != "none"
                or released.get("archive") is not None
            ):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "named Orca Dispatch no longer has context-only terminal ownership",
                )
            with self.transaction() as current:
                self.assignment(current, request.role, expected)
                assignment, completion = self._parallel_completed(
                    current, request.role, stage="read"
                )
                if assignment.get("orca_release") is not None:
                    _fail(
                        ErrorCode.BUSY, "Orca terminal release is already in progress"
                    )
                assignment["orca_release"] = {
                    "phase": "closing",
                    "identity": {
                        field: completion[field]
                        for field in (
                            "role",
                            "role_kind",
                            "run_id",
                            "task_id",
                            "dispatch_id",
                            "terminal_handle",
                            "launch_nonce",
                            "delivery_id",
                        )
                    },
                    "terminal_close": None,
                }
                self.save(current)
            closed = self.backend._client.terminal_close(
                terminal_id=str(expected["terminal_handle"]),
                cwd=Path(str(state["workspace"])),
            )
            self.backend._require_terminal_close(
                closed, terminal_id=str(expected["terminal_handle"])
            )
            with self.transaction() as current:
                assignment = self.assignment(current, request.role, expected)
                self._parallel_completed(current, request.role, stage="read")
                release_record = _object(
                    assignment.get("orca_release"), "release receipt"
                )
                if release_record.get("phase") != "closing":
                    _fail(
                        ErrorCode.IDENTITY_MISMATCH,
                        "Orca terminal release phase changed",
                    )
                release_record.update(
                    {"phase": "closed", "terminal_close": asdict(closed)}
                )
                self.save(current)
        with self.transaction() as current:
            assignment = self.assignment(current, request.role, expected)
            self._parallel_completed(current, request.role, stage="read")
            release_record = _object(assignment.get("orca_release"), "release receipt")
            if release_record.get("phase") != "closed":
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca terminal cleanup is not confirmed",
                )
            expected_prompt = _prompt_path(
                self.path.parent, request.role, str(expected["launch_nonce"])
            )
            if Path(str(expected["prompt_path"])).resolve(
                strict=False
            ) != expected_prompt.resolve(strict=False):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca prompt path does not match its launch identity",
                )
            for field in ("snapshot_root", "provider_private_root"):
                remove_owned_tree(Path(str(expected[field])))
            cleanup_assignment_phase(
                request.role,
                expected,
                state_path=self.path,
                execution="background",
                transport="acp",
                local_stage="prompt_started",
                remove_tree=remove_owned_tree,
            )
            assignment["pending_delivery_stage"] = "released"
            release_record["phase"] = "released"
            self.save(current)
        return ReleaseReceipt("released")

    def reply(self, request: MessageReply) -> ReplyReceipt:
        if _is_parallel_state(self.backend._require_state()):
            return self._parallel_reply(request)
        from .orca_questions import accept_reply, prepare_reply

        with self.transaction() as current:
            roles = _object(current["roles"], "roles")
            if len(roles) != 1:
                _fail(
                    ErrorCode.ORDER_VIOLATION, "question reply requires one active role"
                )
            assignment = _object(next(iter(roles.values())), "assignment")
            prepare_reply(current, assignment, request.message_id._value, request.body)
            state, expected = copy.deepcopy(current), copy.deepcopy(assignment)
            role = NodeRef(str(assignment["role"]), Role(str(assignment["role_kind"])))
        self.verify_remote(state, role)
        with self.transaction() as current:
            if current != state:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca state changed before question reply",
                )
            assignment = self.assignment(current, role, expected)
            self._begin_effect(
                current,
                "reply",
                message_id=request.message_id._value,
                body=request.body,
            )
            result = remote.run_orca(
                state,
                [
                    "orchestration",
                    "reply",
                    "--id",
                    request.message_id._value,
                    "--body",
                    request.body,
                    "--run",
                    str(state["run_id"]),
                    "--from",
                    controller_terminal(state),
                    "--json",
                ],
            )
            accept_reply(
                current,
                assignment,
                result,
                message_id=request.message_id._value,
                body=request.body,
            )
            current.pop("pending_orca_effect")
            self.save(current)
        return ReplyReceipt(True)

    def _parallel_reply(self, request: MessageReply) -> ReplyReceipt:
        from .orca_questions import accept_reply, prepare_reply

        message_id = request.message_id._value
        with self.transaction() as current:
            matches: list[tuple[str, dict[str, object]]] = []
            for node_id, raw in _delivery_containers(current):
                if node_id is None or not isinstance(raw, dict):
                    continue
                question = raw.get("orca_question")
                if (
                    isinstance(question, Mapping)
                    and question.get("message_id") == message_id
                ):
                    matches.append((node_id, raw))
            if len(matches) != 1:
                _fail(
                    ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                    "message does not match exactly one Orca question",
                )
            node_id, assignment = matches[0]
            self._parallel_progress(assignment)
            prepare_reply(current, assignment, message_id, request.body)
            if _object(assignment["orca_question"], "question")["phase"] == "replied":
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "Orca question reply is already confirmed",
                )
            role = NodeRef(node_id, Role(str(assignment["role_kind"])))
            expected = copy.deepcopy(assignment)
            state = copy.deepcopy(current)
        self.verify_remote(state, role)
        with self.transaction() as current:
            assignment = self.assignment(current, role, expected)
            self._parallel_progress(assignment)
            prepare_reply(current, assignment, message_id, request.body)
            if _object(assignment["orca_question"], "question")["phase"] == "replied":
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "Orca question reply is already confirmed",
                )
            self._begin_parallel_reply(
                current,
                assignment,
                message_id=message_id,
                body=request.body,
            )
            state = copy.deepcopy(current)
        result = remote.run_orca(
            state,
            [
                "orchestration",
                "reply",
                "--id",
                message_id,
                "--body",
                request.body,
                "--run",
                str(state["run_id"]),
                "--from",
                controller_terminal(state),
                "--json",
            ],
        )
        with self.transaction() as current:
            assignment = self.assignment(current, role, expected)
            effect = _object(
                assignment.get("pending_orca_effect"), "pending Orca effect"
            )
            if (
                effect.get("operation") != "reply"
                or effect.get("message_id") != message_id
            ):
                _fail(ErrorCode.IDENTITY_MISMATCH, "Orca reply effect identity changed")
            accept_reply(
                current,
                assignment,
                result,
                message_id=message_id,
                body=request.body,
            )
            assignment.pop("pending_orca_effect", None)
            self.save(current)
        return ReplyReceipt(True)

    def ack(self, request: DeliveryAck) -> AckReceipt:
        if _is_parallel_state(self.backend._require_state()):
            return self._parallel_ack(request)
        with self.transaction() as current:
            delivery_id = request.delivery_id._value
            if current.get("pending_delivery_id") != delivery_id:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Delivery ID does not match the pending Delivery",
                )
            kind = current.get("pending_delivery_kind")
            proposed = copy.deepcopy(current)
            if kind == "worker_done":
                if current.get("pending_delivery_stage") != "released" or current.get(
                    "roles"
                ):
                    _fail(
                        ErrorCode.ORDER_VIOLATION,
                        "read and release before acknowledging completion",
                    )
                result = _object(current.get("orca_result"), "result")
                if result.get("logical_task_id") is not None:
                    acknowledge_task(proposed, result)
                proposed.pop("orca_result")
                proposed.pop("orca_release")
            elif kind == "question":
                from .orca_questions import acknowledge_question

                roles = _object(proposed["roles"], "roles")
                if len(roles) != 1:
                    _fail(
                        ErrorCode.ORDER_VIOLATION,
                        "question ACK requires one active assignment",
                    )
                acknowledge_question(
                    proposed,
                    _object(next(iter(roles.values())), "assignment"),
                    delivery_id,
                )
            else:
                _fail(ErrorCode.ORDER_VIOLATION, "this Delivery requires inspection")
            remote._clear_pending_delivery(proposed)
            state = copy.deepcopy(current)
        self.verify_remote(state)
        with self.transaction() as current:
            if current != state:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca state changed before Delivery acknowledgement",
                )
            self._begin_effect(current, "ack")
            result = remote.run_orca(
                state,
                [
                    "orchestration",
                    "check",
                    "--terminal",
                    controller_terminal(state),
                    "--run",
                    str(state["run_id"]),
                    "--ack",
                    delivery_id,
                    "--peek",
                    "--json",
                ],
            )
            if result.get("acknowledged") != delivery_id:
                _fail(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "Orca did not acknowledge the requested Delivery",
                )
            self.save(proposed)
        return AckReceipt(True)

    def _ready_batch(
        self, state: dict[str, object], delivery_id: str
    ) -> tuple[dict[str, object], tuple[NodeRef, ...]]:
        from .orca_questions import OrcaQuestionError, acknowledge_question

        batch = _object(state.get("orca_delivery_batch"), "Orca Delivery batch")
        if batch.get("delivery_id") != delivery_id:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "Delivery ID does not match the saved Run batch",
            )
        if batch.get("phase") != "observed":
            _fail(ErrorCode.BUSY, "Orca batch validation or ACK effect is unconfirmed")
        proposed = copy.deepcopy(state)
        roles: list[NodeRef] = []
        members = batch.get("members")
        if not isinstance(members, list) or not members:
            _fail(ErrorCode.IDENTITY_MISMATCH, "Orca batch members are missing")
        for member_value in members:
            member = _object(member_value, "Orca batch member")
            role = NodeRef(str(member["role"]), Role(str(member["role_kind"])))
            assignment = self.assignment(state, role)
            self._parallel_progress(assignment)
            prepared = self.assignment(proposed, role)
            kind = member["kind"]
            if kind == "worker_done":
                release = assignment.get("orca_release")
                if (
                    assignment.get("pending_delivery_stage") != "released"
                    or not isinstance(release, Mapping)
                    or release.get("phase") != "released"
                ):
                    _fail(
                        ErrorCode.ORDER_VIOLATION,
                        f"read and release {role.node_id} before acknowledging the Run batch",
                    )
                result = _object(assignment.get("orca_result"), "result")
                if result.get("logical_task_id") is not None:
                    acknowledge_task(proposed, result)
                _object(proposed["roles"], "roles").pop(role.node_id)
            elif kind == "question":
                try:
                    acknowledge_question(proposed, prepared, delivery_id)
                except OrcaQuestionError as exc:
                    raise RuntimeFailure(
                        ErrorCode.ORDER_VIOLATION,
                        f"answer every question from {role.node_id} before acknowledging the Run batch",
                    ) from exc
            else:
                _fail(
                    ErrorCode.ORDER_VIOLATION, "the whole Run batch requires inspection"
                )
            roles.append(role)
        proposed.pop("orca_delivery_batch")
        return proposed, tuple(roles)

    def _parallel_ack(self, request: DeliveryAck) -> AckReceipt:
        delivery_id = request.delivery_id._value
        with self.transaction() as current:
            self._ready_batch(current, delivery_id)
            state = copy.deepcopy(current)
        self.verify_remote(state)
        with self.transaction() as current:
            if current != state:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca state changed before Run batch acknowledgement",
                )
            proposed, _roles = self._ready_batch(current, delivery_id)
            batch = _object(current["orca_delivery_batch"], "Orca Delivery batch")
            batch["phase"] = "acknowledging"
            self.save(current)
            result = remote.run_orca(
                current,
                [
                    "orchestration",
                    "check",
                    "--terminal",
                    controller_terminal(current),
                    "--run",
                    str(current["run_id"]),
                    "--ack",
                    delivery_id,
                    "--peek",
                    "--json",
                ],
            )
            if result.get("acknowledged") != delivery_id:
                _fail(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "Orca did not acknowledge the requested Run batch",
                )
            self.save(proposed)
        return AckReceipt(True)

    def verify(self, request: TaskVerify) -> TaskStatusReceipt:
        from .task_verification import verify_approved_task

        with self.transaction() as state:
            self.verify_remote(state)
            return verify_approved_task(
                state, request.task_id, save=lambda: self.save(state)
            )
