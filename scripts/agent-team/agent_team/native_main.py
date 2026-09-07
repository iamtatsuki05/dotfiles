"""Run and supervise the native Main process inside an owned terminal."""

from __future__ import annotations

import errno
import os
import secrets
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Final

from .adapters import _process_group_exited, _wait_for_process_group_exit
from .contracts import RuntimeFailure
from .locking import _LifecycleReservation
from .native_terminal import is_native_runtime
from .runtime import RuntimeValidationError
from .runtime import read_state as runtime_read_state
from .runtime import write_state as runtime_write_state

NATIVE_READY_TIMEOUT_SECONDS: Final = 5.0
NATIVE_POLL_SECONDS: Final = 0.05
NATIVE_GROUP_TERM_TIMEOUT_SECONDS: Final = 2.0
NATIVE_GROUP_KILL_TIMEOUT_SECONDS: Final = 2.0
_NATIVE_PHASES: Final = frozenset({"starting", "running", "stopping"})


class NativeMainError(RuntimeError):
    """Raised for a local native Main lifecycle failure."""


@dataclass(frozen=True, slots=True)
class _ChildProcess:
    process: subprocess.Popen[bytes]
    agent_pid: int
    process_group_id: int | None
    launch_nonce: str


def _required_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise NativeMainError(f"native Main {context} must be a non-empty string")
    if "\x00" in value:
        raise NativeMainError(f"native Main {context} must not contain NUL")
    return value


def _run_id(state: dict[str, object], expected: str) -> None:
    if state.get("version") != 3:
        raise NativeMainError("agent-team native Main requires state version 3")
    if state.get("run_id") != expected:
        raise NativeMainError("agent-team state run identity changed")
    if not is_native_runtime(state.get("runtime")):
        raise NativeMainError("agent-team native Main requires a native runtime")


def _native_state(state: dict[str, object]) -> dict[str, object]:
    native = state.get("native")
    if not isinstance(native, dict):
        raise NativeMainError("agent-team state is missing native lifecycle metadata")
    phase = native.get("phase")
    if phase not in _NATIVE_PHASES:
        raise NativeMainError("agent-team native lifecycle phase is invalid")
    _required_text(native.get("run_nonce"), "run nonce")
    return native


def _frozen_argv(native: dict[str, object]) -> tuple[str, ...]:
    raw_argv = native.get("main_argv")
    if not isinstance(raw_argv, list) or not raw_argv:
        raise NativeMainError("agent-team native Main argv is invalid")
    argv: list[str] = []
    for index, raw in enumerate(raw_argv):
        argv.append(_required_text(raw, f"argv[{index}]"))
    if not Path(argv[0]).is_absolute():
        raise NativeMainError("agent-team native Main executable path must be absolute")
    return tuple(argv)


def _workspace(state: dict[str, object]) -> Path:
    raw_workspace = _required_text(state.get("workspace"), "workspace")
    workspace = Path(raw_workspace)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise NativeMainError("agent-team native Main workspace is invalid")
    return workspace


def _load_ready_state(state_path: Path, run_id: str) -> dict[str, object] | None:
    deadline = time.monotonic() + NATIVE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            state = runtime_read_state(state_path)
            _run_id(state, run_id)
            native = _native_state(state)
        except (
            NativeMainError,
            RuntimeValidationError,
            OSError,
            TypeError,
            ValueError,
        ):
            return None
        if native["phase"] == "stopping":
            return None
        if native["phase"] == "running":
            return state
        time.sleep(NATIVE_POLL_SECONDS)
    return None


def _read_current_state(state_path: Path, run_id: str) -> dict[str, object]:
    state = runtime_read_state(state_path)
    _run_id(state, run_id)
    _native_state(state)
    return state


def _new_launch_nonce() -> str:
    return secrets.token_hex(16)


def _running_record(supervisor_pid: int, child: _ChildProcess) -> dict[str, object]:
    return {
        "supervisor_pid": supervisor_pid,
        "agent_pid": child.agent_pid,
        "process_group_id": child.process_group_id,
        "launch_nonce": child.launch_nonce,
        "phase": "running",
    }


def _same_running_record(current: object, expected: dict[str, object]) -> bool:
    if not isinstance(current, dict):
        return False
    return all(current.get(key) == expected[key] for key in expected)


def _stop_known_process(process: subprocess.Popen[bytes]) -> bool:
    """Terminate and reap one known child without making group assumptions."""

    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=NATIVE_GROUP_TERM_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=NATIVE_GROUP_KILL_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return False
    except OSError:
        return False
    return process.poll() is not None


def _replace_native(
    state: dict[str, object], native: dict[str, object]
) -> dict[str, object]:
    next_state = dict(state)
    next_state["native"] = native
    return next_state


def _launch_if_ready(
    state_path: Path,
    run_id: str,
    supervisor_pid: int,
    *,
    is_cancelled: Callable[[], bool],
) -> tuple[_ChildProcess | None, bool]:
    """Launch and record Main while the lifecycle reservation is held."""

    if is_cancelled():
        return None, False
    reservation = _LifecycleReservation(state_path, create_parent=False)
    try:
        reservation.acquire_for_publication()
    except RuntimeFailure:
        return None, False

    child: _ChildProcess | None = None
    published = False
    try:
        state = _read_current_state(state_path, run_id)
        native = _native_state(state)
        if native["phase"] != "running":
            return None, False
        if native.get("main_process") is not None:
            return None, False
        argv = _frozen_argv(native)
        workspace = _workspace(state)
        launch_nonce = _new_launch_nonce()
        if is_cancelled():
            return None, False
        try:
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                stdin=None,
                stdout=None,
                stderr=None,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise NativeMainError("native Main process could not start") from exc
        try:
            process_group_id = os.getpgid(process.pid)
        except OSError as exc:
            child = _ChildProcess(
                process=process,
                agent_pid=process.pid,
                process_group_id=None,
                launch_nonce=launch_nonce,
            )
            raise NativeMainError(
                "native Main process group identity is unavailable"
            ) from exc
        if process_group_id != process.pid:
            process.terminate()
            try:
                process.wait(timeout=NATIVE_GROUP_KILL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=NATIVE_GROUP_KILL_TIMEOUT_SECONDS)
            raise NativeMainError("native Main process group is not privately owned")
        child = _ChildProcess(
            process=process,
            agent_pid=process.pid,
            process_group_id=process_group_id,
            launch_nonce=launch_nonce,
        )
        next_native = dict(native)
        next_native["main_process"] = _running_record(supervisor_pid, child)
        runtime_write_state(
            state_path,
            _replace_native(state, next_native),
            require_existing=True,
            reservation_held=True,
        )
        published = True
    except (
        NativeMainError,
        RuntimeFailure,
        RuntimeValidationError,
        OSError,
        TypeError,
        ValueError,
    ):
        published = False
    finally:
        reservation.release()

    if child is not None and not published:
        if child.process_group_id is None:
            _stop_known_process(child.process)
            returncode = child.process.poll()
            if returncode is None:
                returncode = 1
            _publish_exited(
                state_path,
                run_id,
                supervisor_pid,
                child,
                returncode,
                False,
                allow_missing_running_record=True,
            )
            return None, False
        group_stopped = _stop_process_group(child, signal.SIGTERM)
        returncode = child.process.poll()
        if returncode is None:
            returncode = 1
        _publish_exited(
            state_path,
            run_id,
            supervisor_pid,
            child,
            returncode,
            group_stopped,
        )
        return None, False
    return child, published


def _signal_process_group(child: _ChildProcess, signum: int) -> bool:
    if child.process_group_id != child.agent_pid:
        return False
    if child.process.poll() is None:
        try:
            if os.getpgid(child.agent_pid) != child.process_group_id:
                return False
        except (PermissionError, ProcessLookupError):
            return False
    try:
        os.killpg(child.process_group_id, signum)
    except ProcessLookupError:
        return _process_group_exited(child.process_group_id)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return _process_group_exited(child.process_group_id)
        return False
    return True


def _wait_for_child(child: _ChildProcess) -> int:
    while True:
        returncode = child.process.poll()
        if returncode is not None:
            return returncode
        time.sleep(NATIVE_POLL_SECONDS)


def _stop_process_group(child: _ChildProcess, initial_signal: int) -> bool:
    """Stop the recorded child group and return proof of group termination."""

    if child.process_group_id != child.agent_pid:
        return False
    if not _signal_process_group(child, initial_signal):
        return child.process.poll() is not None and _process_group_exited(
            child.process_group_id
        )
    stopped = _wait_for_process_group_exit(
        child.process_group_id,
        timeout_seconds=NATIVE_GROUP_TERM_TIMEOUT_SECONDS,
        process=child.process,
    )
    if not stopped:
        if not _signal_process_group(child, signal.SIGKILL):
            return False
        stopped = _wait_for_process_group_exit(
            child.process_group_id,
            timeout_seconds=NATIVE_GROUP_KILL_TIMEOUT_SECONDS,
            process=child.process,
        )
    try:
        child.process.wait(timeout=NATIVE_GROUP_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return stopped and _process_group_exited(child.process_group_id)


def _publish_exited(
    state_path: Path,
    run_id: str,
    supervisor_pid: int,
    child: _ChildProcess,
    returncode: int,
    group_stopped: bool,
    *,
    allow_missing_running_record: bool = False,
) -> bool:
    expected = _running_record(supervisor_pid, child)
    reservation = _LifecycleReservation(state_path, create_parent=False)
    try:
        reservation.acquire_for_publication()
    except RuntimeFailure:
        return False
    try:
        state = _read_current_state(state_path, run_id)
        native = _native_state(state)
        if allow_missing_running_record:
            if native.get("main_process") is not None:
                return False
        elif not _same_running_record(native.get("main_process"), expected):
            return False
        exited = dict(expected)
        exited["phase"] = "exited"
        exited["returncode"] = returncode
        exited["group_stopped"] = group_stopped
        next_native = dict(native)
        next_native["main_process"] = exited
        runtime_write_state(
            state_path,
            _replace_native(state, next_native),
            require_existing=True,
            reservation_held=True,
        )
        return True
    except (
        NativeMainError,
        RuntimeFailure,
        RuntimeValidationError,
        OSError,
        TypeError,
        ValueError,
    ):
        return False
    finally:
        reservation.release()


def run(state_path: Path, run_id: str) -> int:
    """Wait for a ready native run, then supervise its frozen Main argv."""

    if os.name == "nt" or not isinstance(state_path, Path):
        return 1
    try:
        run_id = _required_text(run_id, "run ID")
    except NativeMainError:
        return 1

    pending_signal: int | None = None

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        nonlocal pending_signal
        pending_signal = signum

    signal_numbers: tuple[int, ...] = tuple(
        int(signum)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        if signum is not None
    )
    previous_handlers: dict[
        int, signal.Handlers | int | None | Callable[[int, FrameType | None], Any]
    ] = {}
    for signum in signal_numbers:
        previous_handlers[signum] = signal.signal(signum, handle_signal)
    child: _ChildProcess | None = None
    published = False
    group_stopped = False
    cleanup_attempted = False
    returncode = 1
    try:
        ready = _load_ready_state(state_path, run_id)
        if ready is None or pending_signal is not None:
            return 1
        child, published = _launch_if_ready(
            state_path,
            run_id,
            os.getpid(),
            is_cancelled=lambda: pending_signal is not None,
        )
        if child is None or not published:
            return 1

        termination_signal: int | None = None
        while child.process.poll() is None:
            observed_signal = pending_signal
            pending_signal = None
            if observed_signal == signal.SIGINT:
                if not _signal_process_group(child, signal.SIGINT):
                    termination_signal = int(signal.SIGTERM)
                    break
            elif observed_signal in {signal.SIGTERM, signal.SIGHUP}:
                termination_signal = int(observed_signal)
                break
            try:
                current = _read_current_state(state_path, run_id)
                if _native_state(current)["phase"] == "stopping":
                    termination_signal = int(signal.SIGTERM)
                    break
            except (
                NativeMainError,
                RuntimeValidationError,
                OSError,
                TypeError,
                ValueError,
            ):
                termination_signal = int(signal.SIGTERM)
                break
            time.sleep(NATIVE_POLL_SECONDS)

        if termination_signal is not None:
            cleanup_attempted = True
            group_stopped = _stop_process_group(child, termination_signal)
            try:
                returncode = child.process.wait(
                    timeout=NATIVE_GROUP_KILL_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                polled = child.process.poll()
                returncode = polled if polled is not None else 1
        else:
            returncode = _wait_for_child(child)
            process_group_id = child.process_group_id
            if process_group_id is None:
                group_stopped = False
            elif not _process_group_exited(process_group_id):
                cleanup_attempted = True
                group_stopped = _stop_process_group(child, signal.SIGTERM)
            else:
                group_stopped = True
        published_exit = _publish_exited(
            state_path,
            run_id,
            os.getpid(),
            child,
            returncode,
            group_stopped,
        )
        if not published_exit or not group_stopped:
            return 1
        return returncode
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        if child is not None and not group_stopped and not cleanup_attempted:
            # A failed state publication must not leave a launched group behind.
            _stop_process_group(child, signal.SIGTERM)


__all__ = ("NativeMainError", "run")
