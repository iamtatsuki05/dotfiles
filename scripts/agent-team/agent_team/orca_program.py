"""Run one fixed Orca program coordinator without a Main model."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FrameType

from .adapters import (
    ExecutionError,
    ProcessResult,
    ProcessRunner,
    _process_group_exited,
)
from .contracts import ErrorCode, RuntimeFailure
from .locking import _LifecycleReservation
from .orca import MAX_ORCA_OUTPUT_BYTES, OrcaClient
from .process_identity import python_process_argv, read_process_argv
from .runtime import RuntimeValidationError, acp_environment, read_state, write_state

START_WAIT_SECONDS = 10.0
_parent_ready = False


class CoordinatorCliRunner(ProcessRunner):
    def __init__(self) -> None:
        super().__init__(max_output_bytes=MAX_ORCA_OUTPUT_BYTES)
        self.cli_cleanup_confirmed = True

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 900.0,
    ) -> ProcessResult:
        try:
            return super().run(
                argv,
                cwd=cwd,
                env=env,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
            )
        except ExecutionError as exc:
            self.cli_cleanup_confirmed = (
                self.cli_cleanup_confirmed and exc.cleanup_confirmed
            )
            raise
        except BaseException:
            if self.process_attempted and self.completed_returncode is None:
                self.cli_cleanup_confirmed = False
            raise


def _receive_ready(_number: int, _frame: FrameType | None) -> None:
    global _parent_ready
    _parent_ready = True


def notify_ready(state: Mapping[str, object]) -> None:
    if not process_running(state):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "Orca coordinator disappeared before readiness"
        )
    process = state["coordinator_process"]
    pid = process.get("pid") if isinstance(process, Mapping) else None
    if type(pid) is not int:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "Orca coordinator process identity is invalid"
        )
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError as exc:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "Orca coordinator readiness could not be delivered",
        ) from exc


def launch_argv(path: Path, run_id: str, nonce: str) -> list[str]:
    if not path.is_absolute() or not run_id or not nonce:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "program launch identity is incomplete"
        )
    return [
        sys.executable,
        "-P",
        "-m",
        "agent_team",
        "_orca-program-run",
        "--state",
        str(path),
        "--run-id",
        run_id,
        "--launch-nonce",
        nonce,
    ]


def argv_digest(argv: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def launch_environment() -> dict[str, str]:
    environment = acp_environment()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    environment["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
    )
    if not Path(sys.executable).is_absolute() or not os.access(sys.executable, os.X_OK):
        raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "program Python is unavailable")
    if not os.access("/usr/bin/env", os.X_OK):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "program environment launcher is unavailable"
        )
    return environment


def launch_command(argv: list[str], environment: Mapping[str, str]) -> str:
    return shlex.join(
        [
            "exec",
            "/usr/bin/env",
            "-i",
            *(f"{key}={value}" for key, value in sorted(environment.items())),
            *argv,
        ]
    )


def startup_marker(
    state: Mapping[str, object], phase: str, nonce: str
) -> dict[str, object]:
    argv = state["coordinator_argv"]
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise RuntimeFailure(ErrorCode.IDENTITY_MISMATCH, "program argv is invalid")
    return {
        "phase": phase,
        "run_id": state["run_id"],
        "coordinator_terminal": state["coordinator_terminal"],
        "argv_sha256": argv_digest(argv),
        "launch_nonce": nonce,
    }


def require_owner(state: Mapping[str, object]) -> None:
    from .orca_controller import is_program

    if not is_program(state):
        return
    from .cleanup import startup_recovery_path

    recovery = startup_recovery_path(Path(str(state["state_path"])))
    if (
        state.get("pending_coordinator_start") is not None
        or recovery.exists()
        or recovery.is_symlink()
    ):
        raise RuntimeFailure(ErrorCode.BUSY, "Orca coordinator startup is pending")
    process = state.get("coordinator_process")
    argv = state.get("coordinator_argv")
    if (
        not isinstance(process, Mapping)
        or not isinstance(argv, list)
        or process.get("phase") != "running"
        or process.get("pid") != os.getpid()
        or process.get("process_group_id") != os.getpgrp()
        or process.get("launch_nonce") != argv[-1]
        or process.get("argv") != list(read_process_argv(os.getpid()) or ())
    ):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "only the recorded Orca program coordinator may advance tasks",
        )


def process_running(state: Mapping[str, object]) -> bool:
    process = state.get("coordinator_process")
    if not isinstance(process, Mapping):
        return False
    pid, pgid, argv = (
        process.get("pid"),
        process.get("process_group_id"),
        process.get("argv"),
    )
    if type(pid) is not int or type(pgid) is not int or not isinstance(argv, list):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "program process identity is invalid"
        )
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return False
    if group != pgid or read_process_argv(pid) != tuple(argv):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "program process identity changed"
        )
    return True


def _register(path: Path, run_id: str, nonce: str) -> None:
    deadline = time.monotonic() + START_WAIT_SECONDS
    while True:
        reservation = _LifecycleReservation(path, create_parent=False)
        try:
            reservation.acquire_for_publication()
            state = read_state(path)
            marker = state.get("pending_coordinator_start")
            argv = state.get("coordinator_argv")
            if (
                state.get("run_id") != run_id
                or state.get("orca_stop_requested") is True
                or not isinstance(argv, list)
                or argv != launch_argv(path, run_id, nonce)
                or not isinstance(marker, Mapping)
                or marker.get("launch_nonce") != nonce
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH, "Orca program startup identity changed"
                )
            if marker.get("phase") != "sent":
                raise RuntimeFailure(ErrorCode.BUSY, "Orca program send is unconfirmed")
            if state.get("coordinator_process") is not None:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH, "Orca program is already registered"
                )
            expected = python_process_argv(argv)
            if read_process_argv(os.getpid()) != expected:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca program kernel argv does not match launch",
                )
            state["coordinator_process"] = {
                "pid": os.getpid(),
                "process_group_id": os.getpgrp(),
                "launch_nonce": nonce,
                "argv": list(expected),
                "phase": "running",
                "exit_code": None,
                "cli_cleanup_confirmed": None,
            }
            write_state(path, state, require_existing=True, reservation_held=True)
            return
        except RuntimeFailure as exc:
            if (
                exc.code not in {ErrorCode.BUSY, ErrorCode.TEAM_ALREADY_RUNNING}
                or time.monotonic() >= deadline
            ):
                raise
        finally:
            reservation.release()
        time.sleep(0.05)


def _await_parent(path: Path, run_id: str) -> None:
    deadline = time.monotonic() + START_WAIT_SECONDS
    while True:
        state = read_state(path)
        if state.get("run_id") != run_id or state.get("orca_stop_requested") is True:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "Orca program startup was fenced"
            )
        from .cleanup import startup_recovery_path

        recovery = startup_recovery_path(path)
        if (
            _parent_ready
            and state.get("pending_coordinator_start") is None
            and not recovery.exists()
            and not recovery.is_symlink()
        ):
            require_owner(state)
            return
        if time.monotonic() >= deadline:
            raise RuntimeFailure(
                ErrorCode.BUSY, "Orca program parent readiness is unconfirmed"
            )
        time.sleep(0.05)


def _record_exit(
    path: Path, run_id: str, nonce: str, code: int, *, cli_cleanup_confirmed: bool
) -> None:
    deadline = time.monotonic() + START_WAIT_SECONDS
    while True:
        reservation = _LifecycleReservation(path, create_parent=False)
        try:
            if not path.exists():
                return
            reservation.acquire_for_publication()
            if not path.exists():
                return
            state = read_state(path)
            process = state.get("coordinator_process")
            if (
                state.get("run_id") != run_id
                or not isinstance(process, dict)
                or process.get("pid") != os.getpid()
                or process.get("launch_nonce") != nonce
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH, "Orca program exit identity changed"
                )
            process.update(
                {
                    "phase": "exited",
                    "exit_code": code,
                    "cli_cleanup_confirmed": cli_cleanup_confirmed,
                }
            )
            write_state(path, state, require_existing=True, reservation_held=True)
            return
        except RuntimeFailure as exc:
            if (
                exc.code not in {ErrorCode.BUSY, ErrorCode.TEAM_ALREADY_RUNNING}
                or time.monotonic() >= deadline
            ):
                raise
        finally:
            reservation.release()
        time.sleep(0.05)


def exit_cleanup_confirmed(state: Mapping[str, object]) -> bool:
    process = state.get("coordinator_process")
    if (
        not isinstance(process, Mapping)
        or process.get("phase") != "exited"
        or process.get("cli_cleanup_confirmed") is not True
    ):
        return False
    group = process.get("process_group_id")
    pid = process.get("pid")
    argv = process.get("argv")
    if (
        type(group) is not int
        or group <= 0
        or type(pid) is not int
        or pid <= 0
        or not isinstance(argv, list)
    ):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "Orca coordinator process group is invalid"
        )
    try:
        observed_group = os.getpgid(pid)
    except ProcessLookupError:
        return _process_group_exited(group)
    if observed_group != group:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "Orca coordinator process group changed"
        )
    observed_argv = read_process_argv(pid)
    if observed_argv is not None and observed_argv != tuple(argv):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "Orca coordinator process argv changed"
        )
    return False


def wait_for_exit(path: Path, expected: Mapping[str, object]) -> None:
    deadline = time.monotonic() + START_WAIT_SECONDS
    previous = expected.get("coordinator_process")
    if not isinstance(previous, Mapping):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "Orca coordinator process receipt is missing"
        )
    while True:
        state = read_state(path)
        current = state.get("coordinator_process")
        if (
            not isinstance(current, Mapping)
            or any(
                state.get(key) != expected.get(key)
                for key in ("run_id", "coordinator_terminal", "coordinator_argv")
            )
            or any(
                current.get(key) != previous.get(key)
                for key in ("pid", "process_group_id", "launch_nonce", "argv")
            )
            or state.get("orca_stop_requested") is not True
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "Orca coordinator Stop identity changed"
            )
        if exit_cleanup_confirmed(state):
            return
        if (
            current.get("phase") == "exited"
            and current.get("cli_cleanup_confirmed") is not True
        ):
            raise RuntimeFailure(
                ErrorCode.BUSY, "Orca coordinator CLI process cleanup is unconfirmed"
            )
        if current.get("phase") != "exited" and not process_running(state):
            raise RuntimeFailure(
                ErrorCode.BUSY, "Orca coordinator disappeared without a cleanup receipt"
            )
        if time.monotonic() >= deadline:
            raise RuntimeFailure(
                ErrorCode.BUSY,
                "Orca coordinator exit and process cleanup are unconfirmed",
            )
        time.sleep(0.05)


def run(path: Path, run_id: str, nonce: str) -> int:
    from .backend import OrcaBackend
    from .cli import _management_plan_from_state, _start_spec
    from .program_driver import drive

    code = 1
    registered = False
    cli_runner = CoordinatorCliRunner()
    global _parent_ready
    _parent_ready = False
    previous_handler = signal.signal(signal.SIGUSR1, _receive_ready)
    try:
        _register(path, run_id, nonce)
        registered = True
        _await_parent(path, run_id)
        state = read_state(path)
        plan = _management_plan_from_state(state)
        backend = OrcaBackend(OrcaClient(runner=cli_runner), resume_existing=True)
        backend.start(_start_spec(plan, attach=False))
        code = drive(backend)
    except (
        RuntimeFailure,
        RuntimeValidationError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "unresolved", "reason": str(exc)[:240]}, ensure_ascii=False
            ),
            flush=True,
        )
    finally:
        try:
            if registered:
                _record_exit(
                    path,
                    run_id,
                    nonce,
                    code,
                    cli_cleanup_confirmed=cli_runner.cli_cleanup_confirmed,
                )
        finally:
            signal.signal(signal.SIGUSR1, previous_handler)
    return code
