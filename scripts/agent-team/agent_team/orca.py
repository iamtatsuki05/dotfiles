"""Fixed Orca CLI transport and response decoding."""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeAlias

from .adapters import ExecutionError, ProcessRunner

ORCA_TIMEOUT_SECONDS: Final = 900.0
ORCA_CLEANUP_TIMEOUT_SECONDS: Final = 15.0
ORCA_STARTUP_TIMEOUT_MS: Final = 180_000
MAX_ORCA_OUTPUT_BYTES: Final = 100_000
TERMINAL_ABSENCE_ERRORS: Final = frozenset(
    {"terminal_not_found", "terminal_handle_stale", "terminal_gone"}
)
DISPATCH_ABSENCE_ERRORS: Final = frozenset({"dispatch_not_found"})
RUN_ABSENCE_ERRORS: Final = frozenset({"run_not_found"})
TASK_ABSENCE_ERRORS: Final = frozenset({"task_not_found"})


def _absence_code(operation: tuple[str, ...], error_code: object) -> str | None:
    if not isinstance(error_code, str):
        return None
    operation_kind = operation[:2]
    if operation_kind[0] == "terminal":
        allowed = TERMINAL_ABSENCE_ERRORS
    elif operation_kind == ("orchestration", "run-show"):
        allowed = RUN_ABSENCE_ERRORS
    elif operation_kind in {
        ("orchestration", "worker-show"),
        ("orchestration", "worker-stop"),
    }:
        allowed = (
            DISPATCH_ABSENCE_ERRORS
            | RUN_ABSENCE_ERRORS
            | TASK_ABSENCE_ERRORS
            | TERMINAL_ABSENCE_ERRORS
        )
    elif operation_kind == ("orchestration", "worker-list"):
        allowed = RUN_ABSENCE_ERRORS
    else:
        return None
    return error_code if error_code in allowed else None


def _operation_name(operation: tuple[str, ...]) -> str:
    if operation[:2] == ("status", "--json"):
        return "status"
    if operation[:2] == ("worktree", "show"):
        return "worktree show"
    if operation[:2] == ("terminal", "create"):
        return "terminal create"
    if operation[:2] == ("terminal", "wait"):
        return "terminal wait"
    if operation[:2] == ("terminal", "switch"):
        return "terminal switch"
    if operation[:2] == ("terminal", "show"):
        return "terminal show"
    if operation[:2] == ("terminal", "close"):
        return "terminal close"
    if operation[:2] == ("orchestration", "run-create"):
        return "orchestration run-create"
    if operation[:2] == ("orchestration", "run-show"):
        return "orchestration run-show"
    if operation[:2] == ("orchestration", "worker-list"):
        return "orchestration worker-list"
    if operation[:2] == ("orchestration", "worker-show"):
        return "orchestration worker-show"
    if operation[:2] == ("orchestration", "worker-stop"):
        return "orchestration worker-stop"
    return "Orca operation"


def _required_string(
    payload: Mapping[str, object], keys: tuple[str, ...], context: str
) -> str:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise OrcaProtocolError(f"{context} response is missing {'.'.join(keys)}")
        current = current[key]
    if not isinstance(current, str) or not current:
        raise OrcaProtocolError(f"{context} response has invalid {'.'.join(keys)}")
    return current


def _required_bool(
    payload: Mapping[str, object], keys: tuple[str, ...], context: str
) -> bool:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise OrcaProtocolError(f"{context} response is missing {'.'.join(keys)}")
        current = current[key]
    if not isinstance(current, bool):
        raise OrcaProtocolError(f"{context} response has invalid {'.'.join(keys)}")
    return current


class OrcaError(RuntimeError):
    """Base class for bounded Orca client failures."""


class OrcaCommandError(OrcaError):
    """The Orca process returned a known command failure."""

    def __init__(
        self,
        operation: str = "Orca command",
        *,
        result: dict[str, object] | None = None,
        not_found: bool = False,
        absence_code: str | None = None,
    ) -> None:
        del operation
        self._result = result
        self._absence_code = absence_code
        self._not_found = not_found or absence_code is not None
        super().__init__("Orca command failed")

    @property
    def result(self) -> dict[str, object] | None:
        return self._result

    @property
    def not_found(self) -> bool:
        return self._not_found

    @property
    def absence_code(self) -> str | None:
        return self._absence_code


class OrcaTransportError(OrcaError):
    """The Orca process could not complete, so the effect is unknown."""

    def __init__(self, operation: str = "Orca operation") -> None:
        del operation
        super().__init__("Orca transport failed; effect is unknown")


class OrcaProtocolError(OrcaError):
    """Orca returned an invalid or unsuccessful JSON envelope."""

    def __init__(self, operation: str = "Orca operation") -> None:
        del operation
        super().__init__("Orca response was invalid")


class OrcaPlatformError(OrcaError):
    """The current platform has no supported Orca CLI lifecycle."""

    def __init__(self) -> None:
        super().__init__("agent-team Orca lifecycle requires a POSIX runtime")


def orca_executable() -> str:
    if sys.platform == "darwin":
        return "orca"
    if sys.platform.startswith("linux"):
        return "orca-ide"
    raise OrcaPlatformError()


@dataclass(frozen=True, slots=True)
class _WorkerStopBase:
    dispatch_id: str
    state: str
    process_action: str

    @property
    def closed_agent_terminal(self) -> bool:
        return False

    @property
    def pty_killed(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class WorkerStopOwnedVerdict(_WorkerStopBase):
    already_settled: bool
    pty_killed: Literal[True] = True

    @property
    def closed_agent_terminal(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class WorkerStopContextOnlyVerdict(_WorkerStopBase):
    already_settled: bool
    pty_killed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class WorkerStopAlreadySettledVerdict(_WorkerStopBase):
    already_settled: Literal[True] = True
    pty_killed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class WorkerStopUnknownVerdict(_WorkerStopBase):
    already_settled: Literal[False] = False
    pty_killed: Literal[False] = False


WorkerStopVerdict: TypeAlias = (
    WorkerStopOwnedVerdict
    | WorkerStopContextOnlyVerdict
    | WorkerStopAlreadySettledVerdict
    | WorkerStopUnknownVerdict
)


@dataclass(frozen=True, slots=True)
class TerminalCloseVerdict:
    handle: str
    close_mode: str | None
    pty_killed: bool
    pty_stop_verdict: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalSwitchVerdict:
    handle: str
    navigated: bool


class OrcaClient:
    """Run the small, fixed Orca command set required by CLI lifecycle."""

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        timeout_seconds: float = ORCA_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("Orca timeout must be finite and positive")
        self._runner = runner or ProcessRunner(max_output_bytes=MAX_ORCA_OUTPUT_BYTES)
        self._executable = orca_executable()
        self._timeout_seconds = timeout_seconds

    @property
    def executable(self) -> str:
        return self._executable

    def _call(
        self,
        operation: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        if not operation:
            raise OrcaProtocolError("Orca operation must not be empty")
        operation_name = _operation_name(operation)
        timeout = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            result = self._runner.run(
                (self._executable, *operation),
                cwd=cwd,
                env=os.environ.copy(),
                timeout_seconds=timeout,
            )
        except (ExecutionError, OSError, TimeoutError) as exc:
            raise OrcaTransportError(operation_name) from exc
        except Exception as exc:
            raise OrcaTransportError(operation_name) from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OrcaProtocolError(operation_name) from exc
        if not isinstance(payload, dict):
            raise OrcaProtocolError(operation_name)
        result_payload = payload.get("result")
        if result.returncode != 0:
            error_payload = payload.get("error")
            error_code = (
                error_payload.get("code") if isinstance(error_payload, dict) else None
            )
            normalized_absence = _absence_code(operation, error_code)
            raise OrcaCommandError(
                operation_name,
                result=result_payload if isinstance(result_payload, dict) else None,
                not_found=normalized_absence is not None,
                absence_code=normalized_absence,
            )
        if payload.get("ok") is not True:
            raise OrcaProtocolError(operation_name)
        if not isinstance(result_payload, dict):
            raise OrcaProtocolError(operation_name)
        return result_payload

    def status(self, cwd: Path) -> dict[str, object]:
        return self._call(("status", "--json"), cwd=cwd)

    def worktree_show(self, cwd: Path) -> dict[str, object]:
        worktree_path = cwd.expanduser().resolve(strict=False)
        return self._call(
            (
                "worktree",
                "show",
                "--worktree",
                f"path:{worktree_path}",
                "--json",
            ),
            cwd=cwd,
        )

    def terminal_create(
        self,
        *,
        worktree_id: str,
        title: str,
        command: str,
        cwd: Path,
    ) -> dict[str, object]:
        return self._call(
            (
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                title,
                "--command",
                command,
                "--json",
            ),
            cwd=cwd,
        )

    def terminal_wait(self, *, terminal_id: str, cwd: Path) -> None:
        result = self._call(
            (
                "terminal",
                "wait",
                "--terminal",
                terminal_id,
                "--for",
                "tui-idle",
                "--timeout-ms",
                str(ORCA_STARTUP_TIMEOUT_MS),
                "--json",
            ),
            cwd=cwd,
        )
        wait = result.get("wait")
        if not isinstance(wait, dict):
            raise OrcaProtocolError("Orca terminal wait response was invalid")
        handle = _required_string(wait, ("handle",), "orca terminal wait")
        condition = _required_string(wait, ("condition",), "orca terminal wait")
        if (
            handle != terminal_id
            or condition != "tui-idle"
            or wait.get("satisfied") is not True
        ):
            raise OrcaProtocolError("Orca terminal wait response was invalid")

    def run_create(self, *, objective: str, terminal_id: str, cwd: Path) -> str:
        result = self._call(
            (
                "orchestration",
                "run-create",
                "--objective",
                objective,
                "--from",
                terminal_id,
                "--json",
            ),
            cwd=cwd,
        )
        run_id = _required_string(
            result, ("run", "id"), "orca orchestration run-create"
        )
        observed_objective = _required_string(
            result, ("run", "objective"), "orca orchestration run-create"
        )
        coordinator = _required_string(
            result,
            ("run", "coordinator_handle"),
            "orca orchestration run-create",
        )
        if observed_objective != objective or coordinator != terminal_id:
            raise OrcaProtocolError(
                "Orca orchestration run-create response was invalid"
            )
        return run_id

    def terminal_switch(self, *, terminal_id: str, cwd: Path) -> TerminalSwitchVerdict:
        result = self._call(
            ("terminal", "switch", "--terminal", terminal_id, "--json"), cwd=cwd
        )
        focus = result.get("focus")
        if not isinstance(focus, dict):
            raise OrcaProtocolError("Orca terminal switch response was invalid")
        handle = _required_string(focus, ("handle",), "orca terminal switch")
        navigated = _required_bool(focus, ("navigated",), "orca terminal switch")
        if handle != terminal_id or not navigated:
            raise OrcaProtocolError("Orca terminal switch response was invalid")
        return TerminalSwitchVerdict(handle=handle, navigated=navigated)

    def run_show(self, *, run_id: str, cwd: Path) -> dict[str, object]:
        return self._call(
            ("orchestration", "run-show", "--id", run_id, "--json"), cwd=cwd
        )

    def terminal_show(self, *, terminal_id: str, cwd: Path) -> dict[str, object]:
        return self._call(
            ("terminal", "show", "--terminal", terminal_id, "--json"), cwd=cwd
        )

    def worker_list(self, *, run_id: str, cwd: Path) -> dict[str, object]:
        return self._call(
            ("orchestration", "worker-list", "--run", run_id, "--json"), cwd=cwd
        )

    def worker_show(self, *, dispatch_id: str, cwd: Path) -> dict[str, object]:
        return self._call(
            (
                "orchestration",
                "worker-show",
                "--dispatch",
                dispatch_id,
                "--json",
            ),
            cwd=cwd,
        )

    def worker_stop(self, *, dispatch_id: str, cwd: Path) -> WorkerStopVerdict:
        command_error: OrcaCommandError | None = None
        try:
            result = self._call(
                (
                    "orchestration",
                    "worker-stop",
                    "--dispatch",
                    dispatch_id,
                    "--json",
                ),
                cwd=cwd,
            )
        except OrcaCommandError as exc:
            if exc.not_found:
                raise
            if exc.result is None:
                raise OrcaProtocolError(
                    "Orca orchestration worker-stop verdict was missing"
                ) from exc
            command_error = exc
            result = exc.result
        observed_dispatch = _required_string(
            result, ("dispatchId",), "orca orchestration worker-stop"
        )
        state = _required_string(result, ("state",), "orca orchestration worker-stop")
        process_action = _required_string(
            result, ("processAction",), "orca orchestration worker-stop"
        )
        if observed_dispatch != dispatch_id:
            raise OrcaProtocolError(
                "Orca orchestration worker-stop response was invalid"
            )
        if state == "stop_unknown":
            if process_action not in {
                "none",
                "unknown",
                "closed_agent_terminal",
            }:
                raise OrcaProtocolError(
                    "Orca orchestration worker-stop response was invalid"
                )
            already_settled = _required_bool(
                result, ("alreadySettled",), "orca orchestration worker-stop"
            )
            if already_settled or "close" in result:
                raise OrcaProtocolError(
                    "Orca orchestration worker-stop response was invalid"
                )
            return WorkerStopUnknownVerdict(
                dispatch_id=observed_dispatch,
                state=state,
                process_action=process_action,
                already_settled=already_settled,
            )
        if process_action == "closed_agent_terminal":
            if state != "stopped":
                raise OrcaProtocolError(
                    "Orca orchestration worker-stop response was invalid"
                )
            already_settled = _required_bool(
                result, ("alreadySettled",), "orca orchestration worker-stop"
            )
            close = result.get("close")
            if not isinstance(close, dict):
                raise OrcaProtocolError(
                    "Orca orchestration worker-stop response was invalid"
                )
            pty_killed = _required_bool(
                close, ("ptyKilled",), "orca orchestration worker-stop"
            )
            if not pty_killed:
                raise OrcaProtocolError(
                    "Orca orchestration worker-stop response was invalid"
                )
            if command_error is not None:
                raise command_error
            return WorkerStopOwnedVerdict(
                dispatch_id=observed_dispatch,
                state=state,
                process_action=process_action,
                already_settled=already_settled,
            )
        if process_action != "none":
            raise OrcaProtocolError(
                "Orca orchestration worker-stop response was invalid"
            )
        if "close" in result:
            raise OrcaProtocolError(
                "Orca orchestration worker-stop response was invalid"
            )
        already_settled_value = result.get("alreadySettled")
        if not isinstance(already_settled_value, bool):
            raise OrcaProtocolError(
                "Orca orchestration worker-stop response was invalid"
            )
        if state == "stopped" and not already_settled_value:
            if command_error is not None:
                raise command_error
            return WorkerStopContextOnlyVerdict(
                dispatch_id=observed_dispatch,
                state=state,
                process_action=process_action,
                already_settled=already_settled_value,
            )
        if state in {
            "succeeded",
            "failed",
            "stopped",
            "abandoned",
            "completed",
            "circuit_broken",
        } and (already_settled_value):
            if command_error is not None:
                raise command_error
            return WorkerStopAlreadySettledVerdict(
                dispatch_id=observed_dispatch,
                state=state,
                process_action=process_action,
            )
        raise OrcaProtocolError("Orca orchestration worker-stop response was invalid")

    def terminal_close(self, *, terminal_id: str, cwd: Path) -> TerminalCloseVerdict:
        command_error: OrcaCommandError | None = None
        try:
            result = self._call(
                (
                    "terminal",
                    "close",
                    "--terminal",
                    terminal_id,
                    "--json",
                ),
                cwd=cwd,
                timeout_seconds=ORCA_CLEANUP_TIMEOUT_SECONDS,
            )
        except OrcaCommandError as exc:
            if exc.not_found:
                raise
            if exc.result is None:
                raise
            command_error = exc
            result = exc.result
        verdict = self._decode_terminal_close(result, terminal_id=terminal_id)
        if command_error is not None and verdict.pty_killed:
            raise command_error
        return verdict

    @staticmethod
    def _decode_terminal_close(
        result: Mapping[str, object], *, terminal_id: str
    ) -> TerminalCloseVerdict:
        observed_handle = _required_string(
            result, ("close", "handle"), "orca terminal close"
        )
        close = result.get("close")
        if not isinstance(close, dict):
            raise OrcaProtocolError("Orca terminal close response was invalid")
        close_mode = close.get("closeMode")
        if close_mode is not None and (
            not isinstance(close_mode, str) or not close_mode
        ):
            raise OrcaProtocolError("Orca terminal close response was invalid")
        pty_killed = _required_bool(close, ("ptyKilled",), "orca terminal close")
        pty_stop_verdict = close.get("ptyStopVerdict")
        if pty_stop_verdict is not None and (
            not isinstance(pty_stop_verdict, str)
            or pty_stop_verdict not in {"live", "unverifiable"}
        ):
            raise OrcaProtocolError("Orca terminal close response was invalid")
        if pty_killed and pty_stop_verdict in {"live", "unverifiable"}:
            raise OrcaProtocolError("Orca terminal close response was invalid")
        if observed_handle != terminal_id:
            raise OrcaProtocolError("Orca terminal close response was invalid")
        return TerminalCloseVerdict(
            handle=observed_handle,
            close_mode=close_mode if isinstance(close_mode, str) else None,
            pty_killed=pty_killed,
            pty_stop_verdict=(
                pty_stop_verdict if isinstance(pty_stop_verdict, str) else None
            ),
        )
