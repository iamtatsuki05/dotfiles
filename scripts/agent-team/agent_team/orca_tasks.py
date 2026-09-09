"""Named Orca task and Delivery operations around the saved Run snapshot."""

from __future__ import annotations

import copy
import hashlib
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from . import mcp_server as remote
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
from .runtime import (
    MAX_PROMPT_CHARS,
    _prompt_path,
    validate_prompt_file,
    write_state,
)
from .task_execution import (
    acknowledge_task,
    answer_task_consultation,
    is_plan_only,
    prepare_dispatch,
    task_consultation,
)
from .task_spec import TaskSpec
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
            if state.get("runtime") != "orca" or state.get("version") != 4:
                _fail(ErrorCode.INVALID_REQUEST, "typed Orca tasks require named state")
            if progress:
                if "pending_orca_effect" in state:
                    _fail(
                        ErrorCode.BUSY,
                        "Orca reply or acknowledgement effect is unconfirmed",
                    )
                if "pending_role_start" in state:
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
            "main_terminal": state["main_terminal"],
            "delivery_id": state["pending_delivery_id"],
            "message_id": message_id,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()
            if body is not None
            else None,
        }
        self.save(state)

    def _local_status(self, state: Mapping[str, object]) -> StatusReceipt | None:
        status = (
            "cleanup_pending"
            if "pending_role_start" in state or "pending_orca_effect" in state
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
                {"pending_role_start": state["pending_role_start"]}
                if "pending_role_start" in state
                else {}
            ),
            **(
                {"pending_orca_effect": state["pending_orca_effect"]}
                if "pending_orca_effect" in state
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
            return self._local_status(current) or receipt

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
                self.assignment(current, role, expected)
                if "orca_release" in current:
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
        with self.transaction(progress=False) as state:
            if "pending_orca_effect" in state:
                _fail(
                    ErrorCode.BUSY,
                    "Orca reply or acknowledgement effect is unconfirmed",
                )
            if "pending_role_start" in state:
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
                if journal["main"] in {"started", "unknown"} or any(
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
        with draining.transaction():
            return self.backend._stop_locked()

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
            coordinator_handle=_string(state.get("main_terminal"), "main_terminal"),
        )
        if role is not None:
            self.backend._verify_assignment(
                assignment=self.backend._assignment_for_role(state, role),
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
                self.assignment(state, request.role)
                return RoleStatusReceipt(
                    request.role, "completed" if "orca_result" in state else "running"
                )
        _fail(ErrorCode.INVALID_REQUEST, "unsupported named Orca request")

    def prompt(self, request: RolePrompt | TaskDispatch) -> Assignment:
        from .orca_dispatch import start_assignment

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

    def wait(self, request: RoleWait) -> WaitReceipt:
        if (
            type(request.timeout_ms) is not int
            or not MIN_TIMEOUT_MS <= request.timeout_ms <= MAX_TIMEOUT_MS
        ):
            _fail(ErrorCode.INVALID_REQUEST, "Orca wait timeout is invalid")
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
                str(state["main_terminal"]),
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

    def read(self, request: RoleRead) -> ReadReceipt:
        if type(request.lines) is not int or not 1 <= request.lines <= MAX_READ_LINES:
            _fail(ErrorCode.INVALID_REQUEST, "Orca read line limit is invalid")
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

    def release(self, request: RoleRelease) -> ReleaseReceipt:
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

    def reply(self, request: MessageReply) -> ReplyReceipt:
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
                    str(state["main_terminal"]),
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

    def ack(self, request: DeliveryAck) -> AckReceipt:
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
                    str(state["main_terminal"]),
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

    def verify(self, request: TaskVerify) -> TaskStatusReceipt:
        from .task_verification import verify_approved_task

        with self.transaction() as state:
            self.verify_remote(state)
            return verify_approved_task(
                state, request.task_id, save=lambda: self.save(state)
            )
