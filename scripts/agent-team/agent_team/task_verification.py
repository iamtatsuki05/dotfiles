"""Run trusted, fixed-argv verification against one workspace revision."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import NoReturn

from .adapters import ExecutionError, ProcessRunner
from .contracts import ErrorCode, RuntimeFailure
from .runtime import acp_environment
from .task_spec import TaskSpec, VerificationSpec
from .workspace_revision import WorkspaceRevisionError, snapshot_revision

_MAX_ERROR_CHARS = 512
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


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
