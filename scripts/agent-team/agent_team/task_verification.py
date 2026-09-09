"""Run trusted, fixed-argv verification against one workspace revision."""

from __future__ import annotations

import hashlib
import json
import signal
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn, cast

from .adapters import ExecutionError, ProcessRunner
from .contracts import ErrorCode, RuntimeFailure, TaskStatusReceipt
from .named_graph import GraphSpec
from .runtime import acp_environment
from .task_execution import (
    is_agent_parallel,
    is_plan_only,
    parse_review,
    prepare_agent_batch_verification,
    program_wave,
    verification_revision,
)
from .task_spec import TaskSpec, VerificationSpec
from .workspace_revision import WorkspaceRevisionError, snapshot_revision

_MAX_ERROR_CHARS = 512
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class VerificationCancelled(BaseException):
    pass


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise RuntimeFailure(code, message)


def _bounded_error(value: object) -> str:
    text = "".join(character for character in str(value) if character.isprintable())
    return text[:_MAX_ERROR_CHARS] or "verification failed"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_or_raise(workspace: Path) -> str:
    try:
        return snapshot_revision(workspace)
    except WorkspaceRevisionError as exc:
        _fail(ErrorCode.INVALID_REQUEST, _bounded_error(exc))


def _empty_command(spec: VerificationSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "argv": list(spec.argv),
        "timeout_seconds": spec.timeout_seconds,
        "returncode": None,
        "stdout_sha256": _EMPTY_SHA256,
        "stderr_sha256": _EMPTY_SHA256,
        "error": None,
    }


def _result(
    revision: str,
    commands: list[dict[str, object]],
    error: str | None,
    *,
    cleanup_confirmed: bool,
) -> dict[str, object]:
    return {
        "revision": revision,
        "passed": cleanup_confirmed
        and error is None
        and all(command["error"] is None for command in commands),
        "commands": commands,
        "error": error,
        "cleanup_confirmed": cleanup_confirmed,
    }


def _verification_pending(state: Mapping[str, object]) -> bool:
    tasks = state.get("tasks")
    return isinstance(tasks, Mapping) and any(
        isinstance(task, Mapping) and task.get("status") == "verifying"
        for task in tasks.values()
    )


@contextmanager
def _verification_signals() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "verification requires the runtime process main thread",
        )

    def cancel(_signal: int, _frame: FrameType | None) -> NoReturn:
        raise VerificationCancelled("verification interrupted")

    previous = {
        number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for number in previous:
            signal.signal(number, cancel)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _task_record(state: dict[str, object], task_id: str) -> dict[str, object]:
    tasks = state.get("tasks")
    record = tasks.get(task_id) if isinstance(tasks, dict) else None
    if not isinstance(record, dict):
        raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "task is unknown")
    return record


def _program_coordination(state: Mapping[str, object]) -> bool:
    raw_graph = state.get("graph")
    if not isinstance(raw_graph, Mapping):
        return False
    try:
        graph = GraphSpec.from_dict(raw_graph)
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "saved graph is invalid"
        ) from exc
    return graph.coordination.mode == "program"


def verify_approved_task(
    state: dict[str, object], task_id: str, *, save: Callable[[], None]
) -> TaskStatusReceipt:
    """Verify one review-approved task while retaining cleanup evidence."""

    if _verification_pending(state):
        raise RuntimeFailure(
            ErrorCode.BUSY,
            "verification cleanup must be confirmed before executing another task",
        )
    record = _task_record(state, task_id)
    final_stage = "plan" if is_plan_only(state, task_id) else "implementation"
    if _program_coordination(state):
        wave = program_wave(state)
        if (
            wave["phase"] != "verification"
            or task_id not in cast(list[str], wave["task_ids"])
            or verification_revision(record) != wave["revision"]
        ):
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "verification requires the sealed, approved program wave",
            )
    if record.get("status") != f"{final_stage}_approved":
        raise RuntimeFailure(
            ErrorCode.ORDER_VIOLATION,
            f"verification requires {final_stage} review approval",
        )
    if state.get("roles") or state.get("pending_delivery_id") is not None:
        raise RuntimeFailure(
            ErrorCode.BUSY,
            "consume the active role and Delivery before verification",
        )
    task = TaskSpec.from_dict(record["spec"])
    revision = verification_revision(record)
    verdict = parse_review(
        json.dumps(record.get("task_evidence")),
        task=task,
        stage=final_stage,
        revision=str(record["revision"]),
    )
    if verdict["decision"] != "approve":
        raise RuntimeFailure(
            ErrorCode.ORDER_VIOLATION, "final review approval is missing"
        )
    workspace = Path(str(state["workspace"]))
    if snapshot_revision(workspace) != revision:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "approved workspace revision changed"
        )
    with _verification_signals():
        if is_agent_parallel(state):
            prepare_agent_batch_verification(state, task_id, revision)
        record["status"] = "verifying"
        save()
        try:
            evidence = run_verification(task, workspace, revision)
        except BaseException as exc:
            cleanup_confirmed = isinstance(exc, (VerificationCancelled, RuntimeFailure))
            record["status"] = (
                "verification_failed" if cleanup_confirmed else "verifying"
            )
            record["verification"] = {
                "revision": revision,
                "passed": False,
                "commands": [],
                "error": "verification interrupted or failed",
                "cleanup_confirmed": cleanup_confirmed,
            }
            save()
            if not isinstance(exc, Exception):
                raise
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "verification execution failed",
            ) from exc
        record["verification"] = evidence
        record["status"] = (
            "verifying"
            if evidence["cleanup_confirmed"] is not True
            else "completed"
            if evidence["passed"] is True
            else "verification_failed"
        )
        save()
    return TaskStatusReceipt(task.task_id, str(record["status"]), dict(record))


def run_verification(
    task: TaskSpec, workspace: Path, revision: str
) -> dict[str, object]:
    """Run every declared command while preserving one workspace revision."""

    if not isinstance(task, TaskSpec):
        _fail(ErrorCode.INVALID_REQUEST, "TaskSpec is required")
    if not isinstance(workspace, Path):
        _fail(ErrorCode.INVALID_REQUEST, "verification workspace is invalid")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.INVALID_REQUEST, "verification revision is required")

    initial = _snapshot_or_raise(workspace)
    if initial != revision:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "verification revision does not match the workspace",
        )

    runner = ProcessRunner()
    environment = acp_environment()
    commands: list[dict[str, object]] = []
    command_error: str | None = None
    cleanup_confirmed = True
    for spec in task.verification:
        try:
            before = _snapshot_or_raise(workspace)
        except RuntimeFailure as exc:
            return _result(
                revision,
                commands,
                _bounded_error(exc),
                cleanup_confirmed=cleanup_confirmed,
            )
        if before != revision:
            return _result(
                revision,
                commands,
                "workspace revision changed before a verification command",
                cleanup_confirmed=cleanup_confirmed,
            )

        command = _empty_command(spec)
        try:
            process = runner.run(
                spec.argv,
                cwd=workspace,
                env=environment,
                timeout_seconds=spec.timeout_seconds,
            )
            command["returncode"] = process.returncode
            command["stdout_sha256"] = _digest(process.stdout)
            command["stderr_sha256"] = _digest(process.stderr)
            if process.returncode != 0:
                command["error"] = (
                    f"verification command exited with return code {process.returncode}"
                )
                if command_error is None:
                    command_error = str(command["error"])
        except ExecutionError as exc:
            command["error"] = _bounded_error(exc)
            cleanup_confirmed = exc.cleanup_confirmed
            if command_error is None:
                command_error = str(command["error"])
        commands.append(command)
        if not cleanup_confirmed:
            return _result(revision, commands, command_error, cleanup_confirmed=False)

        try:
            after = _snapshot_or_raise(workspace)
        except RuntimeFailure as exc:
            return _result(
                revision,
                commands,
                _bounded_error(exc),
                cleanup_confirmed=cleanup_confirmed,
            )
        if after != revision:
            return _result(
                revision,
                commands,
                "workspace revision changed during verification",
                cleanup_confirmed=cleanup_confirmed,
            )

    try:
        final = _snapshot_or_raise(workspace)
    except RuntimeFailure as exc:
        return _result(
            revision, commands, _bounded_error(exc), cleanup_confirmed=cleanup_confirmed
        )
    if final != revision:
        return _result(
            revision,
            commands,
            "workspace revision changed after verification",
            cleanup_confirmed=cleanup_confirmed,
        )
    return _result(
        revision, commands, command_error, cleanup_confirmed=cleanup_confirmed
    )
