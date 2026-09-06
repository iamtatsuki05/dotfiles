"""Native tmux backend for the small typed agent-team runtime.

The tmux process is only a private host for Main.  ACP roles communicate
completion through :func:`publish_completion`; pane output is deliberately
never interpreted as a lifecycle message.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from . import acp_dependencies
from .adapters import (
    _process_group_exited,
    _wait_for_process_group_exit,
    remove_owned_tree,
)
from .contracts import (
    AckReceipt,
    Assignment,
    Attach,
    AttachReceipt,
    BackendPort,
    BackendRequest,
    BackendResult,
    CompletionIdentity,
    DeliveryAck,
    DeliveryRef,
    DispatchRef,
    ErrorCode,
    LaunchMode,
    MessageReply,
    NormalizedEvent,
    Outcome,
    ReadReceipt,
    ReleaseReceipt,
    Role,
    RoleGet,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleStatusReceipt,
    RoleWait,
    RunRef,
    RuntimeFailure,
    StartResult,
    StartSpec,
    Status,
    StatusReceipt,
    StopResult,
    TaskRef,
    TerminalRef,
    WaitReceipt,
)
from .harness_launch import LaunchValidationError, build_claude_argv
from .locking import _LifecycleReservation
from .process_identity import read_process_argv
from .runtime import (
    MAX_PROMPT_CHARS,
    RuntimeValidationError,
    StatePublishError,
    acp_environment,
    build_acp_agent_command,
    build_acp_session_name,
    create_prompt_file,
    remove_prompt_file,
)
from .runtime import (
    read_state as runtime_read_state,
)
from .runtime import (
    remove_state_tree as runtime_remove_state_tree,
)
from .runtime import (
    write_state as runtime_write_state,
)
from .tmux import (
    CloseEvidence,
    TmuxDriver,
    TmuxInspection,
    TmuxReceipt,
)

NATIVE_RUNTIME: Final = "tmux"
NATIVE_PHASES: Final = frozenset({"starting", "running", "stopping"})
ACP_ADAPTER_ID: Final = "claude-acp-0.70.0"
ACP_ROLES: Final = frozenset({Role.PLANNER, Role.REVIEWER})
MAX_RESULT_BODY_CHARS: Final = 100_000
PROCESS_WAIT_SECONDS: Final = 5.0
ACP_CANCEL_GRACE_SECONDS: Final = 45.0
PROCESS_POLL_SECONDS: Final = 0.05
PACKAGE_ROOT: Final = Path(__file__).resolve().parent.parent
PENDING_DELIVERY_ID: Final = "pending_delivery_id"
PENDING_DELIVERY_KIND: Final = "pending_delivery_kind"
PENDING_DELIVERY_STAGE: Final = "pending_delivery_stage"
PENDING_QUESTION_IDS: Final = "pending_question_ids"
REPLIED_QUESTION_IDS: Final = "replied_question_ids"


def _new_id() -> str:
    """Return an opaque backend-owned identity."""

    return str(uuid.uuid4())


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def _canonical(path: Path) -> Path:
    return _absolute(path).resolve(strict=False)


def _runtime_error(
    exc: BaseException,
    context: str,
    *,
    code: ErrorCode = ErrorCode.BACKEND_PROTOCOL_FAILURE,
) -> RuntimeFailure:
    if isinstance(exc, RuntimeFailure):
        return exc
    return RuntimeFailure(code, f"{context}: {type(exc).__name__}"[:240])


def _required_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            f"agent-team state is missing {context}",
        )
    return value


def _required_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            f"agent-team state has invalid {context}",
        )
    return value


def _state_path(state: Mapping[str, object]) -> Path:
    return Path(_required_string(state.get("state_path"), "state_path"))


def _native(state: Mapping[str, object]) -> dict[str, object]:
    return _required_mapping(state.get("native"), "native metadata")


def _require_running(state: Mapping[str, object]) -> None:
    if _native(state).get("phase") != "running":
        raise RuntimeFailure(ErrorCode.BUSY, "native team is not running")


def _assignment(state: Mapping[str, object], role: Role) -> dict[str, object]:
    roles = _required_mapping(state.get("roles"), "roles")
    raw = roles.get(role.value)
    if not isinstance(raw, dict):
        raise RuntimeFailure(
            ErrorCode.TEAM_NOT_RUNNING,
            "role has no active native ACP assignment",
        )
    return raw


def _role_specs(state: Mapping[str, object]) -> dict[str, object]:
    return _required_mapping(state.get("role_specs"), "role_specs")


def _spec_dict(spec: object, role: Role) -> dict[str, object]:
    if not hasattr(spec, "provider"):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            f"role spec is invalid: {role.value}",
        )
    # StartSpec is typed, but the backend still validates the boundary before
    # writing durable state so a caller cannot smuggle arbitrary role data in.
    values = {
        key: getattr(spec, key, None)
        for key in (
            "provider",
            "transport",
            "model",
            "effort",
            "permission",
            "instructions",
            "execution",
        )
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            f"role spec is incomplete: {role.value}",
        )
    result: dict[str, object] = dict(values)
    adapter_id = getattr(spec, "adapter_id", None)
    if adapter_id is not None:
        if not isinstance(adapter_id, str) or not adapter_id:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                f"role spec adapter is invalid: {role.value}",
            )
        result["adapter_id"] = adapter_id
    raw_executables = getattr(spec, "acp_executables", None)
    if raw_executables is not None:
        if not isinstance(raw_executables, Mapping):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                f"role spec ACP bindings are invalid: {role.value}",
            )
        result["acp_executables"] = dict(raw_executables)
    return result


def _validate_profile(
    spec: StartSpec, launcher_path: Path, *, preflight: bool = True
) -> tuple[dict[str, dict[str, object]], dict[str, object], list[str]]:
    """Validate the explicitly selected native profiles before any state write."""

    raw_specs = spec.role_specs
    if not isinstance(raw_specs, Mapping):
        raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role specs are required")
    if Role.MAIN not in raw_specs:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "native runtime requires a Main role",
        )
    allowed = {Role.MAIN, *ACP_ROLES}
    unknown = [
        role.value if isinstance(role, Role) else str(role)
        for role in raw_specs
        if not isinstance(role, Role) or role not in allowed
    ]
    if unknown:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "native runtime does not support selected role: " + ", ".join(unknown),
        )

    role_specs: dict[str, dict[str, object]] = {}
    acp_bindings: dict[str, object] = {}
    for role, role_spec in raw_specs.items():
        if not isinstance(role, Role):
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role spec key is invalid")
        normalized = _spec_dict(role_spec, role)
        if role is Role.MAIN:
            expected = {
                "provider": "claude",
                "transport": "direct",
                "permission": "orchestrator",
                "execution": "tui_direct",
            }
            if any(normalized.get(key) != value for key, value in expected.items()):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    "native Main must be direct Claude with orchestrator permission",
                )
            if "adapter_id" in normalized or "acp_executables" in normalized:
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    "native Main cannot carry an ACP adapter",
                )
        else:
            expected = {
                "provider": "claude",
                "transport": "acp",
                "permission": "read-only",
                "execution": "background",
                "adapter_id": ACP_ADAPTER_ID,
            }
            if any(normalized.get(key) != value for key, value in expected.items()):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    f"native {role.value} must be verified Claude ACP read-only",
                )
            raw = normalized.get("acp_executables")
            if not isinstance(raw, Mapping):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    f"native {role.value} is missing ACP executable bindings",
                )
            if preflight:
                try:
                    from_dict = acp_dependencies.AcpExecutables.from_dict
                    executables = from_dict(raw)
                    executables.verify()
                    # The snapshot helper is part of the explicit ACP boundary.
                    # It is called before state/task/process creation so a bad
                    # binding cannot leave a native assignment behind.
                    snapshot = acp_dependencies.adapter_snapshot(executables)
                except Exception as exc:
                    raise _runtime_error(
                        exc,
                        f"selected {role.value} ACP dependencies are unavailable",
                        code=ErrorCode.INVALID_REQUEST,
                    ) from exc
                if not isinstance(snapshot, Mapping):
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "ACP adapter snapshot is invalid",
                    )
                acp_bindings[role.value] = executables
                normalized["acp_executables"] = executables.as_dict()
        role_specs[role.value] = normalized

    if not preflight:
        return role_specs, acp_bindings, []
    try:
        argv = list(
            build_claude_argv(
                role="main",
                model=cast(str, role_specs[Role.MAIN.value]["model"]),
                effort=cast(str, role_specs[Role.MAIN.value]["effort"]),
                permission="orchestrator",
                instructions=cast(str, role_specs[Role.MAIN.value]["instructions"]),
                state_path=_absolute(spec.state_path),
                mcp_server_path=launcher_path,
            )
        )
    except (LaunchValidationError, RuntimeValidationError) as exc:
        raise _runtime_error(
            exc, "native Main launch plan is invalid", code=ErrorCode.INVALID_REQUEST
        ) from exc
    selected = shutil.which("claude")
    if selected is None:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "selected Claude executable is unavailable",
        )
    try:
        claude_executable = Path(selected).expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "selected Claude executable is unavailable",
        ) from exc
    if not claude_executable.is_file() or not os.access(claude_executable, os.X_OK):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "selected Claude executable is not executable",
        )
    argv[0] = str(claude_executable)
    return role_specs, acp_bindings, argv


def _receipt_from_state(native: Mapping[str, object]) -> TmuxReceipt:
    raw = native.get("tmux_receipt")
    try:
        return TmuxReceipt.from_dict(raw)
    except Exception as exc:
        raise _runtime_error(exc, "native tmux receipt is invalid") from exc


def _driver_from_receipt(receipt: TmuxReceipt) -> TmuxDriver:
    try:
        return TmuxDriver.from_receipt(receipt)
    except Exception as exc:
        raise _runtime_error(exc, "native tmux receipt cannot be resumed") from exc


def _inspection_dict(observed: TmuxInspection) -> dict[str, object]:
    return {
        "running": observed.running,
        "exit_status": observed.exit_status,
        "identity_verified": observed.identity_verified,
        "pane_present": observed.pane_present,
        "session_present": observed.session_present,
        "pane_pid": observed.pane_pid,
        "server_pid": observed.server_pid,
        "observed_nonce": observed.observed_nonce,
        "reason": observed.reason,
    }


def _native_result_fields(
    value: object,
    *,
    role: str,
    run_id: str,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    launch_nonce: str,
) -> tuple[str, str, bool, str]:
    if not isinstance(value, Mapping):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native completion result is invalid",
        )
    expected = {
        "role": role,
        "run_id": run_id,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "terminal_handle": terminal_handle,
        "launch_nonce": launch_nonce,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native completion identity does not match assignment",
            )
    delivery_id = value.get("delivery_id")
    outcome = value.get("outcome")
    body = value.get("body")
    cleanup_confirmed = value.get("cleanup_confirmed")
    if (
        not isinstance(delivery_id, str)
        or not delivery_id
        or outcome not in {Outcome.SUCCEEDED.value, Outcome.FAILED.value}
        or not isinstance(cleanup_confirmed, bool)
        or not isinstance(body, str)
        or len(body) > MAX_RESULT_BODY_CHARS
    ):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native completion result is invalid",
        )
    return delivery_id, cast(str, outcome), cleanup_confirmed, body


def _assignment_identity(
    assignment: Mapping[str, object], *, role: Role, state: Mapping[str, object]
) -> tuple[str, str, str, str]:
    values = tuple(
        assignment.get(key)
        for key in ("task_id", "dispatch_id", "terminal_handle", "launch_nonce")
    )
    if not all(isinstance(value, str) and value for value in values):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            f"native {role.value} assignment identity is incomplete",
        )
    if assignment.get("launcher_owned_runner") is not True:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native assignment runner ownership is unknown",
        )
    return (
        cast(str, values[0]),
        cast(str, values[1]),
        cast(str, values[2]),
        cast(str, values[3]),
    )


def _clear_pending(state: dict[str, object]) -> None:
    for key in (
        PENDING_DELIVERY_ID,
        PENDING_DELIVERY_KIND,
        PENDING_DELIVERY_STAGE,
        PENDING_QUESTION_IDS,
        REPLIED_QUESTION_IDS,
    ):
        state.pop(key, None)


def _remove_socket_root(receipt: TmuxReceipt) -> None:
    """Remove only the empty private directory allocated for this receipt."""

    root = receipt.socket_path.parent
    if root.parent != Path("/tmp"):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native tmux socket root does not match its receipt",
        )
    try:
        info = root.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _runtime_error(
            exc, "native tmux socket root cannot be inspected"
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native tmux socket root ownership is unproven",
        )
    try:
        root.rmdir()
    except OSError as exc:
        raise _runtime_error(exc, "native tmux socket root cleanup failed") from exc


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_group_alive(pgid: int) -> bool:
    return not _process_group_exited(pgid)


def _runner_identity_is_owned(
    assignment: Mapping[str, object], *, verify_process: bool = True
) -> tuple[int, int]:
    pid = assignment.get("runner_pid")
    pgid = assignment.get("runner_process_group_id")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 1
        or not isinstance(pgid, int)
        or isinstance(pgid, bool)
        or pgid < 1
    ):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native ACP runner process identity is unavailable",
        )
    if pid != pgid:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native ACP runner process group is not private",
        )
    argv = assignment.get("runner_argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native ACP runner argv identity is unavailable",
        )
    if verify_process and _process_group_alive(pgid):
        try:
            observed_group = os.getpgid(pid)
        except ProcessLookupError:
            observed_group = None
        except OSError as exc:
            raise _runtime_error(
                exc, "native ACP runner identity is unavailable"
            ) from exc
        if observed_group is not None and observed_group != pgid:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native ACP runner process group does not match state",
            )
        observed_argv = read_process_argv(pid)
        if observed_argv is None:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native ACP runner argv ownership is unproven",
            )
        if tuple(argv) != observed_argv:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native ACP runner argv does not match state",
            )
    if pgid == os.getpgrp():
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native ACP runner process group is not private",
        )
    return pid, pgid


def _validate_runner_argv(
    state_path: Path, role: Role, assignment: Mapping[str, object]
) -> tuple[str, ...]:
    """Validate the persisted runner command against its assignment identity."""

    values = (
        assignment.get("task_id"),
        assignment.get("dispatch_id"),
        assignment.get("terminal_handle"),
        assignment.get("prompt_path"),
        assignment.get("launch_nonce"),
    )
    if not all(isinstance(value, str) and value for value in values):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native ACP runner command identity is incomplete",
        )
    raw_argv = assignment.get("runner_argv")
    if (
        not isinstance(raw_argv, list)
        or not raw_argv
        or any(not isinstance(item, str) or not item for item in raw_argv)
    ):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native ACP runner command identity is unavailable",
        )
    expected = (
        str(raw_argv[0]),
        "-m",
        "agent_team",
        "_acp-run",
        role.value,
        "--state",
        str(state_path),
        "--task-id",
        cast(str, values[0]),
        "--dispatch-id",
        cast(str, values[1]),
        "--terminal",
        cast(str, values[2]),
        "--prompt",
        cast(str, values[3]),
        "--launch-nonce",
        cast(str, values[4]),
    )
    if tuple(raw_argv) != expected or not Path(expected[0]).is_absolute():
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native ACP runner command does not match assignment",
        )
    return expected


def _terminate_process_group(
    pid: int, pgid: int, *, grace_seconds: float = PROCESS_WAIT_SECONDS
) -> None:
    if pgid == os.getpgrp() or pgid <= 1:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "refusing to signal the current process group",
        )
    try:
        if _process_group_alive(pgid):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while _process_group_alive(pgid):
            if time.monotonic() >= deadline:
                break
            time.sleep(PROCESS_POLL_SECONDS)
        if _process_group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)
            deadline = time.monotonic() + PROCESS_WAIT_SECONDS
            while _process_group_alive(pgid):
                if time.monotonic() >= deadline:
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "owned process group did not stop",
                    )
                time.sleep(PROCESS_POLL_SECONDS)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "owned process group cannot be signaled",
        ) from exc
    except OSError as exc:
        raise _runtime_error(exc, "owned process group stop failed") from exc


def _save_state(
    path: Path,
    state: dict[str, object],
    *,
    require_existing: bool,
    reservation_held: bool,
) -> None:
    try:
        runtime_write_state(
            path,
            state,
            require_existing=require_existing,
            reservation_held=reservation_held,
        )
    except RuntimeFailure:
        raise
    except Exception as exc:
        raise _runtime_error(exc, "native state save failed") from exc


def _read_state(path: Path) -> dict[str, object]:
    try:
        return runtime_read_state(path)
    except RuntimeValidationError as exc:
        if str(exc).startswith("agent-team is not running:"):
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING, "agent-team is not running"
            ) from exc
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE, "agent-team state is invalid"
        ) from exc
    except Exception as exc:
        raise _runtime_error(exc, "native state read failed") from exc


def publish_completion(
    state_path: Path,
    *,
    role: str,
    run_id: str,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    launch_nonce: str,
    outcome: str,
    body: str,
    cleanup_confirmed: bool,
) -> None:
    """Publish one trusted ACP completion after the runner cleaned its session."""

    if role not in {item.value for item in ACP_ROLES}:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "native completion role is unsupported"
        )
    if outcome not in {Outcome.SUCCEEDED.value, Outcome.FAILED.value}:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "native completion outcome is invalid"
        )
    if not isinstance(body, str) or len(body) > MAX_RESULT_BODY_CHARS:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "native completion body is too large"
        )
    if not isinstance(cleanup_confirmed, bool):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "native cleanup confirmation is invalid"
        )
    reservation = _LifecycleReservation(_absolute(state_path), create_parent=False)
    reservation.acquire()
    try:
        state = _read_state(_absolute(state_path))
        if state.get("runtime") != NATIVE_RUNTIME:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native state runtime does not match"
            )
        saved_run = _required_string(state.get("run_id"), "run_id")
        if saved_run != run_id:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native completion Run does not match state",
            )
        selected = _role_specs(state)
        if role not in selected:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native completion role is not selected"
            )
        selected_spec = selected.get(role)
        if not isinstance(selected_spec, Mapping) or any(
            selected_spec.get(key) != expected_value
            for key, expected_value in {
                "provider": "claude",
                "transport": "acp",
                "permission": "read-only",
                "execution": "background",
                "adapter_id": ACP_ADAPTER_ID,
            }.items()
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native completion role spec does not match Claude ACP",
            )
        assignment = _assignment(state, Role(role))
        saved_task, saved_dispatch, saved_terminal, saved_nonce = _assignment_identity(
            assignment, role=Role(role), state=state
        )
        if (saved_task, saved_dispatch, saved_terminal, saved_nonce) != (
            task_id,
            dispatch_id,
            terminal_handle,
            launch_nonce,
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native completion identity does not match assignment",
            )
        _assert_publisher(_absolute(state_path), Role(role), assignment)
        existing = state.get("native_result")
        if existing is not None:
            existing_fields = _native_result_fields(
                existing,
                role=role,
                run_id=run_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                terminal_handle=terminal_handle,
                launch_nonce=launch_nonce,
            )
            if existing_fields[1:] != (outcome, cleanup_confirmed, body):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native completion was already published",
                )
            return
        state["native_result"] = {
            "role": role,
            "run_id": run_id,
            "task_id": task_id,
            "dispatch_id": dispatch_id,
            "terminal_handle": terminal_handle,
            "launch_nonce": launch_nonce,
            "outcome": outcome,
            "body": body,
            "cleanup_confirmed": cleanup_confirmed,
            "delivery_id": _new_id(),
        }
        _save_state(
            _absolute(state_path),
            state,
            require_existing=True,
            reservation_held=True,
        )
    finally:
        reservation.release()


def _assert_publisher(path: Path, role: Role, assignment: Mapping[str, object]) -> None:
    expected = _validate_runner_argv(path, role, assignment)
    pid = os.getpid()
    if (
        assignment.get("runner_pid") != pid
        or assignment.get("runner_process_group_id") != pid
        or os.getpgrp() != pid
        or read_process_argv(pid) != expected
    ):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native completion publisher ownership is unproven",
        )


class TmuxBackend(BackendPort):
    """Bind the typed team runtime contract to a private tmux server."""

    def __init__(
        self,
        *,
        tmux_executable: str | Path = "tmux",
        launcher_path: Path | None = None,
        resume_existing: bool = False,
    ) -> None:
        self._tmux_executable = tmux_executable
        self._launcher_path = _absolute(launcher_path or Path(sys.argv[0]))
        self._resume_existing = resume_existing
        self._state: dict[str, object] | None = None
        self._driver: TmuxDriver | None = None
        self._runners: dict[str, subprocess.Popen[bytes]] = {}
        self.last_start_response: dict[str, object] | None = None
        self.last_status_response: dict[str, object] | None = None
        self.last_attach_response: dict[str, object] | None = None
        self.last_stop_response: dict[str, object] | None = None

    def start(self, spec: StartSpec) -> StartResult:
        self._ensure_supported_platform()
        if (
            not isinstance(spec.team_id, str)
            or not spec.team_id
            or not isinstance(spec.workspace, Path)
            or not isinstance(spec.config_path, Path)
            or not isinstance(spec.state_path, Path)
        ):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native start specification is incomplete"
            )
        if not _canonical(spec.workspace).is_dir():
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native workspace is not a directory"
            )
        role_specs, _acp_bindings, main_argv = _validate_profile(
            spec, self._launcher_path, preflight=not self._resume_existing
        )
        state_path = _absolute(spec.state_path)
        reservation = _LifecycleReservation(
            state_path, create_parent=not self._resume_existing
        )
        reservation.acquire()
        try:
            if self._resume_existing:
                result = self._resume_locked(spec, role_specs)
            else:
                result = self._start_new_locked(
                    spec, role_specs=role_specs, main_argv=main_argv
                )
        finally:
            reservation.release()
        if spec.attach:
            self.request(Attach(Role.MAIN))
        return result

    def request(self, request: BackendRequest) -> BackendResult:
        if isinstance(request, Status):
            return self._status()
        if isinstance(request, Attach):
            return self._attach(request)
        if isinstance(request, RolePrompt):
            return self._prompt(request)
        if isinstance(request, RoleWait):
            return self._wait(request)
        if isinstance(request, RoleRead):
            return self._read(request)
        if isinstance(request, RoleRelease):
            return self._release(request)
        if isinstance(request, DeliveryAck):
            return self._ack(request)
        if isinstance(request, MessageReply):
            raise RuntimeFailure(
                ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                "native ACP does not publish question messages",
            )
        if isinstance(request, RoleGet):
            return self._role_get(request)
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "unsupported native runtime request"
        )

    def stop(self) -> StopResult:
        self._ensure_supported_platform()
        previous = self._require_state()
        path = _state_path(previous)
        self._wait_for_main_process_receipt(path)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state, receipt, main_process, active_role = self._prepare_stop_locked(path)
        finally:
            reservation.release()

        # Cancellation and supervisor waits intentionally happen outside the
        # lifecycle reservation.  ACP/native publishers must be able to finish
        # their final state write while a stop is waiting for a process group.
        if active_role is not None:
            self._cancel_runner(state, active_role)
        self._stop_supervisor(state, receipt, main_process)

        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            current = self._reload_locked(path)
            native = _native(current)
            if native.get("phase") != "stopping":
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native stop phase changed during cleanup",
                )
            driver = self._driver or _driver_from_receipt(_receipt_from_state(native))
            current_receipt = _receipt_from_state(native)
            if current_receipt != receipt:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native tmux receipt changed during stop",
                )
            inspected = driver.inspect(current_receipt)
            if (
                not inspected.identity_verified
                or inspected.pane_pid != current_receipt.pane_pid
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native tmux pane ownership is unproven during stop",
                )
            closed = driver.close(current_receipt)
            if (
                not closed.ownership_verified
                or not closed.session_terminated
                or not closed.server_terminated
                or closed.evidence
                not in {
                    CloseEvidence.SERVER_TERMINATED,
                    CloseEvidence.SESSION_TERMINATED,
                }
                or not closed.socket_removed
            ):
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native tmux server termination is unproven",
                )
            _remove_socket_root(current_receipt)
            roles = _required_mapping(current.get("roles"), "roles")
            if roles:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native role cleanup remains unconfirmed",
                )
            try:
                runtime_remove_state_tree(path, current)
            except Exception as exc:
                raise _runtime_error(exc, "native state cleanup failed") from exc
            result = StopResult(
                team_id=_required_string(current.get("team_id"), "team_id"),
                run_id=RunRef(_required_string(current.get("run_id"), "run_id")),
            )
            self.last_stop_response = {
                "status": "stopped",
                "team_id": result.team_id,
                "run_id": result.run_id._value,
            }
            self._state = None
            self._driver = None
            self._runners.clear()
            return result
        finally:
            reservation.release()

    def _start_new_locked(
        self,
        spec: StartSpec,
        *,
        role_specs: dict[str, dict[str, object]],
        main_argv: list[str],
    ) -> StartResult:
        state_path = _absolute(spec.state_path)
        if state_path.exists() or state_path.is_symlink():
            raise RuntimeFailure(
                ErrorCode.TEAM_ALREADY_RUNNING,
                "agent-team state already exists; use attach or stop",
            )
        if state_path.parent.exists() and any(state_path.parent.iterdir()):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native agent-team state directory contains unknown entries",
            )
        run_id = _new_id()
        main_terminal = _new_id()
        run_nonce = secrets.token_hex(16)
        socket_root = Path(tempfile.mkdtemp(prefix="at-", dir="/tmp"))
        try:
            os.chmod(socket_root, 0o700)
        except OSError as exc:
            raise _runtime_error(exc, "native tmux socket root setup failed") from exc
        state: dict[str, object] = {
            "version": 3,
            "runtime": NATIVE_RUNTIME,
            "team_id": spec.team_id,
            "workspace": str(_canonical(spec.workspace)),
            "config_path": str(_canonical(spec.config_path)),
            "state_path": str(state_path),
            "launcher_path": str(self._launcher_path),
            "run_id": run_id,
            "main_terminal": main_terminal,
            "role_specs": role_specs,
            "roles": {},
            "native": {
                "phase": "starting",
                "run_nonce": run_nonce,
                "main_argv": list(main_argv),
                "startup_socket_path": str(socket_root / "s"),
            },
        }
        _save_state(
            state_path,
            state,
            require_existing=False,
            reservation_held=True,
        )
        session_name = f"agent-team-main-{run_nonce[:16]}"
        try:
            driver = TmuxDriver(
                self._tmux_executable,
                socket_root / "s",
                run_nonce,
                session_name,
            )
            supervisor_argv = (
                sys.executable,
                "-m",
                "agent_team",
                "_native-main",
                "--state",
                str(state_path),
                "--run-id",
                run_id,
            )
            receipt = driver.create(
                supervisor_argv,
                cwd=PACKAGE_ROOT,
                env=acp_environment(),
                title=f"{spec.team_id}-main",
            )
            if receipt.run_nonce != run_nonce or receipt.session_name != session_name:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native tmux receipt identity does not match startup",
                )
        except Exception as exc:
            # The state intentionally remains in ``starting``.  A lost tmux
            # create effect must not be converted into a clean restart.
            raise _runtime_error(exc, "native Main startup failed") from exc
        native = _native(state)
        native["tmux_receipt"] = receipt.as_dict()
        native["phase"] = "running"
        _save_state(
            state_path,
            state,
            require_existing=True,
            reservation_held=True,
        )
        self._state = state
        self._driver = driver
        result = StartResult(
            team_id=spec.team_id,
            run_id=RunRef(run_id),
            main_terminal_id=TerminalRef(main_terminal),
            state_path=spec.state_path,
        )
        self.last_start_response = {
            "status": "running",
            "team_id": spec.team_id,
            "workspace": str(_canonical(spec.workspace)),
            "run_id": run_id,
            "main_terminal": main_terminal,
            "state_path": str(spec.state_path),
        }
        return result

    def _resume_locked(
        self, spec: StartSpec, expected_role_specs: dict[str, dict[str, object]]
    ) -> StartResult:
        state = _read_state(_absolute(spec.state_path))
        self._assert_state_matches_spec(state, spec, expected_role_specs)
        native = _native(state)
        phase = native.get("phase")
        if phase not in {"starting", "running", "stopping"}:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "native phase is unknown"
            )
        if "tmux_receipt" not in native and phase != "starting":
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native Main startup is unresolved",
            )
        if "tmux_receipt" in native:
            receipt = _receipt_from_state(native)
            driver = _driver_from_receipt(receipt)
            inspected = driver.inspect(receipt)
            if not inspected.identity_verified:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native tmux pane ownership is unproven",
                )
            self._driver = driver
        else:
            self._driver = None
        self._state = state
        result = StartResult(
            team_id=spec.team_id,
            run_id=RunRef(_required_string(state.get("run_id"), "run_id")),
            main_terminal_id=TerminalRef(
                _required_string(state.get("main_terminal"), "main_terminal")
            ),
            state_path=spec.state_path,
        )
        self.last_start_response = {
            "status": "running" if phase == "running" else "starting",
            "team_id": spec.team_id,
            "workspace": _required_string(state.get("workspace"), "workspace"),
            "run_id": result.run_id._value,
            "main_terminal": result.main_terminal_id._value,
            "state_path": str(spec.state_path),
        }
        return result

    def _reload_locked(self, path: Path) -> dict[str, object]:
        previous = self._require_state()
        current = _read_state(path)
        for key in (
            "runtime",
            "team_id",
            "run_id",
            "main_terminal",
        ):
            if current.get(key) != previous.get(key):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native state identity changed during operation",
                )
        for key in ("workspace", "config_path", "state_path", "launcher_path"):
            old = previous.get(key)
            new = current.get(key)
            if (
                not isinstance(old, str)
                or not isinstance(new, str)
                or _canonical(Path(old)) != _canonical(Path(new))
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native state path identity changed during operation",
                )
        self._state = current
        return current

    def _assert_state_matches_spec(
        self,
        state: Mapping[str, object],
        spec: StartSpec,
        expected_role_specs: dict[str, dict[str, object]],
    ) -> None:
        expected = {
            "team_id": spec.team_id,
            "workspace": _canonical(spec.workspace),
            "config_path": _canonical(spec.config_path),
            "state_path": _canonical(spec.state_path),
        }
        actual = {
            "team_id": state.get("team_id"),
            "workspace": _canonical(
                Path(_required_string(state.get("workspace"), "workspace"))
            ),
            "config_path": _canonical(
                Path(_required_string(state.get("config_path"), "config_path"))
            ),
            "state_path": _canonical(
                Path(_required_string(state.get("state_path"), "state_path"))
            ),
        }
        if any(actual[key] != expected[key] for key in expected):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native state does not match requested team identity",
            )
        if state.get("runtime") != NATIVE_RUNTIME:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native state runtime does not match"
            )
        if _role_specs(state) != expected_role_specs:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native role launch snapshot does not match requested config",
            )

    def _status(self) -> StatusReceipt:
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_locked(path)
            native = _native(state)
            phase = native.get("phase")
            status = phase if phase in NATIVE_PHASES else "unknown"
            inspection: TmuxInspection | None = None
            receipt: TmuxReceipt | None = None
            if "tmux_receipt" in native:
                receipt = _receipt_from_state(native)
                driver = self._driver or _driver_from_receipt(receipt)
                self._driver = driver
                inspection = driver.inspect(receipt)
                if not inspection.identity_verified:
                    status = "unknown"
                elif phase == "running" and inspection.running is False:
                    process = native.get("main_process")
                    if (
                        isinstance(process, Mapping)
                        and process.get("phase") == "exited"
                        and process.get("group_stopped") is True
                    ):
                        status = "exited"
                    else:
                        status = "unknown"
            team_id = _required_string(state.get("team_id"), "team_id")
            run_id = _required_string(state.get("run_id"), "run_id")
            response: dict[str, object] = {
                "status": status,
                "team_id": team_id,
                "run_id": run_id,
                "main_terminal": _required_string(
                    state.get("main_terminal"), "main_terminal"
                ),
                "native": dict(native),
                "roles": dict(_required_mapping(state.get("roles"), "roles")),
            }
            if inspection is not None:
                response["tmux"] = _inspection_dict(inspection)
            self.last_status_response = response
            return StatusReceipt(status, team_id, RunRef(run_id))
        finally:
            reservation.release()

    def _attach(self, request: Attach) -> AttachReceipt:
        if request.role is not Role.MAIN:
            state = self._require_state()
            if request.role.value in _role_specs(state):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    "native ACP roles have no TTY to attach",
                )
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role is not selected")
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_locked(path)
            native = _native(state)
            receipt = _receipt_from_state(native)
            driver = self._driver or _driver_from_receipt(receipt)
            self._driver = driver
            inspected = driver.inspect(receipt)
            if not inspected.identity_verified:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native Main tmux ownership is unproven",
                )
            argv = driver.attach_argv(receipt)
        except RuntimeFailure:
            raise
        except Exception as exc:
            raise _runtime_error(exc, "native Main attach preparation failed") from exc
        finally:
            reservation.release()
        try:
            attached = subprocess.run(list(argv), check=False)
        except OSError as exc:
            raise _runtime_error(exc, "native Main attach failed") from exc
        if attached.returncode != 0:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "native Main attach failed"
            )
        run_id = _required_string(state.get("run_id"), "run_id")
        terminal = _required_string(state.get("main_terminal"), "main_terminal")
        self.last_attach_response = {
            "status": "focused",
            "role": Role.MAIN.value,
            "terminal": terminal,
            "argv": list(argv),
        }
        return AttachReceipt(Role.MAIN, TerminalRef(terminal), RunRef(run_id))

    def _prompt(self, request: RolePrompt) -> Assignment:
        if request.role not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not a Claude ACP role"
            )
        if not isinstance(request.text, str) or not request.text.strip():
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "role prompt must be a non-empty string"
            )
        if len(request.text) > MAX_PROMPT_CHARS:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "role prompt exceeds character limit"
            )
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        prompt_path: Path | None = None
        private_root: Path | None = None
        snapshot_root: Path | None = None
        assignment: dict[str, object] | None = None
        process: subprocess.Popen[bytes] | None = None
        spawn_attempted = False
        assignment_persisted = False
        try:
            state = self._reload_locked(path)
            native = _native(state)
            if native.get("phase") != "running":
                raise RuntimeFailure(ErrorCode.BUSY, "native team is not running")
            roles = _required_mapping(state.get("roles"), "roles")
            if roles:
                raise RuntimeFailure(
                    ErrorCode.BUSY, "another native role is already active"
                )
            if state.get(PENDING_DELIVERY_ID) is not None:
                raise RuntimeFailure(
                    ErrorCode.BUSY,
                    "acknowledge the pending Delivery before starting a role",
                )
            specs = _role_specs(state)
            raw_spec = specs.get(request.role.value)
            if not isinstance(raw_spec, Mapping):
                raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role is not selected")
            expected_spec = {
                "provider": "claude",
                "transport": "acp",
                "permission": "read-only",
                "execution": "background",
                "adapter_id": ACP_ADAPTER_ID,
            }
            if any(
                raw_spec.get(key) != expected_value
                for key, expected_value in expected_spec.items()
            ):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    "native role does not satisfy Claude ACP read-only capability",
                )
            try:
                executables = acp_dependencies.AcpExecutables.from_dict(
                    raw_spec.get("acp_executables")
                )
                executables.verify()
                snapshot = acp_dependencies.adapter_snapshot(executables)
                if not isinstance(snapshot, Mapping):
                    raise TypeError("ACP adapter snapshot is invalid")
            except Exception as exc:
                # The nonce is generated below after the dependency check.  No
                # state, Task, or process effect has happened on this path.
                raise _runtime_error(
                    exc,
                    "selected ACP dependencies are unavailable",
                    code=ErrorCode.INVALID_REQUEST,
                ) from exc
            launch_nonce = secrets.token_hex(16)
            agent_command = build_acp_agent_command(
                _required_string(state.get("team_id"), "team_id"),
                request.role.value,
                launch_nonce,
                executables=executables,
            )
            session_name = build_acp_session_name(request.role.value, launch_nonce)
            task_id = _new_id()
            dispatch_id = _new_id()
            terminal_handle = _new_id()
            prompt_path = create_prompt_file(
                path.parent, request.role.value, launch_nonce, request.text
            )
            private_root = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
            snapshot_root = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
            runner_argv = [
                sys.executable,
                "-m",
                "agent_team",
                "_acp-run",
                request.role.value,
                "--state",
                str(path),
                "--task-id",
                task_id,
                "--dispatch-id",
                dispatch_id,
                "--terminal",
                terminal_handle,
                "--prompt",
                str(prompt_path),
                "--launch-nonce",
                launch_nonce,
            ]
            assignment = {
                "task_id": task_id,
                "dispatch_id": dispatch_id,
                "terminal_handle": terminal_handle,
                "completion_observed": False,
                "launcher_owned_runner": True,
                "launch_nonce": launch_nonce,
                "prompt_path": str(prompt_path),
                "execution": "background",
                "adapter_id": ACP_ADAPTER_ID,
                "agent_command": agent_command,
                "session_name": session_name,
                "provider_private_root": str(private_root),
                "snapshot_root": str(snapshot_root),
                "adapter_snapshot": dict(snapshot),
                "runner_argv": runner_argv,
            }
            roles[request.role.value] = assignment
            try:
                _save_state(path, state, require_existing=True, reservation_held=True)
                assignment_persisted = True
            except RuntimeFailure as exc:
                if isinstance(exc.__cause__, StatePublishError):
                    assignment_persisted = True
                raise
            try:
                spawn_attempted = True
                process = subprocess.Popen(
                    runner_argv,
                    cwd=PACKAGE_ROOT,
                    env=acp_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                )
            except Exception as exc:
                # The assignment remains durable, because the process effect is
                # unknown once a spawn call has been attempted.
                raise _runtime_error(exc, "native ACP runner startup failed") from exc
            pid = process.pid
            assignment["runner_pid"] = pid
            self._runners[request.role.value] = process
            try:
                pgid = os.getpgid(pid)
            except OSError as exc:
                assignment["runner_process_group_id"] = None
                _save_state(path, state, require_existing=True, reservation_held=True)
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native ACP runner process group is unproven",
                ) from exc
            assignment["runner_process_group_id"] = pgid
            _save_state(path, state, require_existing=True, reservation_held=True)
            if pgid != pid:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native ACP runner process group is not private",
                )
            return self._assignment_receipt(state, request.role)
        except RuntimeFailure:
            if not spawn_attempted and not assignment_persisted:
                self._cleanup_unpersisted(
                    prompt_path, private_root, snapshot_root, path, request.role
                )
            elif process is not None:
                self._stop_failed_runner(process)
            raise
        except Exception as exc:
            if not spawn_attempted and not assignment_persisted:
                self._cleanup_unpersisted(
                    prompt_path, private_root, snapshot_root, path, request.role
                )
            elif process is not None:
                self._stop_failed_runner(process)
            raise _runtime_error(exc, "native role startup failed") from exc
        finally:
            reservation.release()

    @staticmethod
    def _stop_failed_runner(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=PROCESS_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=PROCESS_WAIT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native failed runner exit is unconfirmed; assignment retained",
                ) from exc

    def _cleanup_unpersisted(
        self,
        prompt_path: Path | None,
        private_root: Path | None,
        snapshot_root: Path | None,
        state_path: Path,
        role: Role,
    ) -> None:
        if prompt_path is not None and (
            prompt_path.exists() or prompt_path.is_symlink()
        ):
            try:
                remove_prompt_file(
                    prompt_path,
                    state_path.parent,
                    role=role.value,
                    launch_nonce=prompt_path.stem.rsplit("-", 1)[-1],
                )
            except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
                del cleanup_error
        for root in (private_root, snapshot_root):
            if root is not None:
                try:
                    remove_owned_tree(root)
                except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
                    del cleanup_error

    def _assignment_receipt(
        self, state: Mapping[str, object], role: Role
    ) -> Assignment:
        assignment = _assignment(state, role)
        task_id, dispatch_id, terminal, _nonce = _assignment_identity(
            assignment, role=role, state=state
        )
        run_id = _required_string(state.get("run_id"), "run_id")
        identity = CompletionIdentity(
            run_id=RunRef(run_id),
            task_id=TaskRef(task_id),
            dispatch_id=DispatchRef(dispatch_id),
            sender_terminal_id=TerminalRef(terminal),
        )
        return Assignment(
            role=role,
            launch_mode=LaunchMode.BARE_BACKGROUND,
            task_id=TaskRef(task_id),
            dispatch_id=DispatchRef(dispatch_id),
            terminal_id=TerminalRef(terminal),
            completion_identity=identity,
        )

    def _wait(self, request: RoleWait) -> WaitReceipt:
        if request.role not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not a Claude ACP role"
            )
        if request.timeout_ms < 1:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "wait timeout must be positive"
            )
        previous = self._require_state()
        path = _state_path(previous)
        deadline = time.monotonic() + request.timeout_ms / 1_000
        while True:
            state = _read_state(path)
            _require_running(state)
            if state.get(PENDING_DELIVERY_ID) is not None:
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "acknowledge the pending Delivery before waiting again",
                )
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            run_id = _required_string(state.get("run_id"), "run_id")
            result = state.get("native_result")
            if result is not None:
                _delivery_id, _outcome, _cleanup, _body = _native_result_fields(
                    result,
                    role=request.role.value,
                    run_id=run_id,
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    terminal_handle=terminal,
                    launch_nonce=launch_nonce,
                )
                reservation = _LifecycleReservation(path, create_parent=False)
                reservation.acquire()
                try:
                    current = self._reload_locked(path)
                    _require_running(current)
                    if current.get(PENDING_DELIVERY_ID) is not None:
                        raise RuntimeFailure(
                            ErrorCode.ORDER_VIOLATION,
                            "acknowledge the pending Delivery before waiting again",
                        )
                    current_assignment = _assignment(current, request.role)
                    current_task, current_dispatch, current_terminal, current_nonce = (
                        _assignment_identity(
                            current_assignment, role=request.role, state=current
                        )
                    )
                    if (
                        current_task,
                        current_dispatch,
                        current_terminal,
                        current_nonce,
                    ) != (task_id, dispatch_id, terminal, launch_nonce):
                        raise RuntimeFailure(
                            ErrorCode.IDENTITY_MISMATCH,
                            "native assignment changed while waiting",
                        )
                    current_result = current.get("native_result")
                    (
                        current_delivery,
                        current_outcome,
                        _current_cleanup,
                        current_body,
                    ) = _native_result_fields(
                        current_result,
                        role=request.role.value,
                        run_id=run_id,
                        task_id=task_id,
                        dispatch_id=dispatch_id,
                        terminal_handle=terminal,
                        launch_nonce=launch_nonce,
                    )
                    current_assignment["completion_observed"] = True
                    current[PENDING_DELIVERY_ID] = current_delivery
                    current[PENDING_DELIVERY_KIND] = "worker_done"
                    current[PENDING_DELIVERY_STAGE] = "observed"
                    current[PENDING_QUESTION_IDS] = []
                    current[REPLIED_QUESTION_IDS] = []
                    _save_state(
                        path, current, require_existing=True, reservation_held=True
                    )
                    identity = CompletionIdentity(
                        run_id=RunRef(run_id),
                        task_id=TaskRef(task_id),
                        dispatch_id=DispatchRef(dispatch_id),
                        sender_terminal_id=TerminalRef(terminal),
                    )
                    event = NormalizedEvent.worker_done(
                        identity=identity,
                        outcome=Outcome(current_outcome),
                        body=current_body,
                        delivery_id=DeliveryRef(current_delivery),
                    )
                    return WaitReceipt(DeliveryRef(current_delivery), (event,))
                finally:
                    reservation.release()
            runner = self._runners.get(request.role.value)
            if (runner is not None and runner.poll() is not None) or (
                runner is None and self._saved_runner_status(assignment) == "exited"
            ):
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native ACP runner exited without publishing completion",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return WaitReceipt(None, ())
            time.sleep(min(PROCESS_POLL_SECONDS, remaining))

    def _read(self, request: RoleRead) -> ReadReceipt:
        if request.role not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not a Claude ACP role"
            )
        if request.lines < 1:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "read lines must be positive"
            )
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_locked(path)
            _require_running(state)
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            _validate_runner_argv(path, request.role, assignment)
            if (
                assignment.get("completion_observed") is not True
                or state.get(PENDING_DELIVERY_KIND) != "worker_done"
                or state.get(PENDING_DELIVERY_STAGE) != "observed"
            ):
                raise RuntimeFailure(
                    ErrorCode.COMPLETION_NOT_OBSERVED,
                    "matching native worker_done has not been observed",
                )
            run_id = _required_string(state.get("run_id"), "run_id")
            result = state.get("native_result")
            delivery_id, _outcome, _cleanup, body = _native_result_fields(
                result,
                role=request.role.value,
                run_id=run_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                terminal_handle=terminal,
                launch_nonce=launch_nonce,
            )
            if state.get(PENDING_DELIVERY_ID) != delivery_id:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native Delivery identity does not match result",
                )
            lines = body.splitlines()
            output = "\n".join(lines[: request.lines])
            state[PENDING_DELIVERY_STAGE] = "read"
            _save_state(path, state, require_existing=True, reservation_held=True)
            return ReadReceipt(output)
        finally:
            reservation.release()

    def _release(self, request: RoleRelease) -> ReleaseReceipt:
        if request.role not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not a Claude ACP role"
            )
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_locked(path)
            _require_running(state)
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            _validate_runner_argv(path, request.role, assignment)
            if (
                assignment.get("completion_observed") is not True
                or state.get(PENDING_DELIVERY_KIND) != "worker_done"
                or state.get(PENDING_DELIVERY_STAGE) != "read"
            ):
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "native role release requires read after worker_done",
                )
            run_id = _required_string(state.get("run_id"), "run_id")
            result = state.get("native_result")
            _delivery, _outcome, cleanup_confirmed, _body = _native_result_fields(
                result,
                role=request.role.value,
                run_id=run_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                terminal_handle=terminal,
                launch_nonce=launch_nonce,
            )
            if not cleanup_confirmed:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native ACP cleanup is unconfirmed; assignment is retained",
                )
        finally:
            reservation.release()

        process = self._runners.get(request.role.value)
        if process is not None:
            try:
                process.wait(timeout=PROCESS_WAIT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeFailure(
                    ErrorCode.BUSY, "native ACP runner has not exited"
                ) from exc
            except Exception as exc:
                raise _runtime_error(exc, "native ACP runner reap failed") from exc
        else:
            _pid, pgid = _runner_identity_is_owned(assignment, verify_process=False)
            if not _wait_for_process_group_exit(
                pgid, timeout_seconds=PROCESS_WAIT_SECONDS
            ):
                raise RuntimeFailure(ErrorCode.BUSY, "native ACP runner has not exited")

        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_locked(path)
            _require_running(state)
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            if state.get(PENDING_DELIVERY_STAGE) != "read":
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native release order changed during cleanup",
                )
            run_id = _required_string(state.get("run_id"), "run_id")
            _delivery, _outcome, cleanup_confirmed, _body = _native_result_fields(
                state.get("native_result"),
                role=request.role.value,
                run_id=run_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                terminal_handle=terminal,
                launch_nonce=launch_nonce,
            )
            if not cleanup_confirmed:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native ACP cleanup is unconfirmed; assignment is retained",
                )
            self._cleanup_assignment(assignment, path, request.role)
            roles = _required_mapping(state.get("roles"), "roles")
            del roles[request.role.value]
            state[PENDING_DELIVERY_STAGE] = "released"
            _save_state(path, state, require_existing=True, reservation_held=True)
            self._runners.pop(request.role.value, None)
            self._state = state
            return ReleaseReceipt("released")
        finally:
            reservation.release()

    def _cleanup_assignment(
        self, assignment: Mapping[str, object], path: Path, role: Role
    ) -> None:
        raw_prompt = assignment.get("prompt_path")
        nonce = assignment.get("launch_nonce")
        if not isinstance(raw_prompt, str) or not isinstance(nonce, str):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native ACP prompt cleanup identity is missing",
            )
        prompt = Path(raw_prompt)
        if prompt.exists() or prompt.is_symlink():
            try:
                remove_prompt_file(
                    prompt, path.parent, role=role.value, launch_nonce=nonce
                )
            except Exception as exc:
                raise _runtime_error(exc, "native ACP prompt cleanup failed") from exc
        for key in ("provider_private_root", "snapshot_root"):
            raw_root = assignment.get(key)
            if not isinstance(raw_root, str) or not raw_root:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native ACP cleanup root is missing",
                )
            try:
                remove_owned_tree(Path(raw_root))
            except Exception as exc:
                raise _runtime_error(exc, "native ACP private cleanup failed") from exc

    def _ack(self, request: DeliveryAck) -> AckReceipt:
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_locked(path)
            _require_running(state)
            pending = state.get(PENDING_DELIVERY_ID)
            if pending != request.delivery_id._value:
                raise RuntimeFailure(
                    ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                    "Delivery does not match native pending observation",
                )
            if (
                state.get(PENDING_DELIVERY_KIND) != "worker_done"
                or state.get(PENDING_DELIVERY_STAGE) != "released"
            ):
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "release the completed native role before acknowledging its Delivery",
                )
            roles = _required_mapping(state.get("roles"), "roles")
            if roles:
                raise RuntimeFailure(
                    ErrorCode.BUSY, "native role cleanup is still pending"
                )
            _native(state)["last_ack"] = request.delivery_id._value
            _clear_pending(state)
            state.pop("native_result", None)
            _save_state(path, state, require_existing=True, reservation_held=True)
            self._state = state
            return AckReceipt(True)
        finally:
            reservation.release()

    def _role_get(self, request: RoleGet) -> RoleStatusReceipt:
        if request.role not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not a Claude ACP role"
            )
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_locked(path)
            assignment = _assignment(state, request.role)
            status = (
                "completed" if state.get("native_result") is not None else "running"
            )
            runner = self._runners.get(request.role.value)
            if (
                runner is not None
                and runner.poll() is not None
                and state.get("native_result") is None
            ):
                status = "exited"
            elif runner is None and state.get("native_result") is None:
                status = self._saved_runner_status(assignment)
            return RoleStatusReceipt(request.role, status)
        finally:
            reservation.release()

    @staticmethod
    def _saved_runner_status(assignment: Mapping[str, object]) -> str:
        try:
            _pid, pgid = _runner_identity_is_owned(assignment)
        except RuntimeFailure:
            return "unknown"
        return "running" if _process_group_alive(pgid) else "exited"

    def _prepare_stop_locked(
        self, path: Path
    ) -> tuple[dict[str, object], TmuxReceipt, dict[str, object], Role | None]:
        state = self._reload_locked(path)
        pending = state.get(PENDING_DELIVERY_ID)
        if pending is not None:
            raise RuntimeFailure(
                ErrorCode.BUSY,
                "acknowledge the pending Delivery before stopping native team",
            )
        native = _native(state)
        phase = native.get("phase")
        if phase not in {"starting", "running", "stopping"}:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "native phase is unknown"
            )
        receipt = _receipt_from_state(native)
        driver = self._driver or _driver_from_receipt(receipt)
        self._driver = driver
        inspected = driver.inspect(receipt)
        if not inspected.identity_verified or inspected.pane_pid != receipt.pane_pid:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native tmux pane ownership is unproven"
            )
        process = native.get("main_process")
        if not isinstance(process, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native Main process receipt is unknown",
            )
        if process.get("phase") == "running" and inspected.running is not True:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native supervisor liveness is unconfirmed",
            )
        supervisor_pid = process.get("supervisor_pid")
        if supervisor_pid != receipt.pane_pid:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native supervisor PID does not match tmux pane",
            )
        if (
            process.get("phase") == "exited"
            and process.get("group_stopped") is not True
        ):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native Main process cleanup is unconfirmed",
            )
        native["phase"] = "stopping"
        _save_state(path, state, require_existing=True, reservation_held=True)
        roles = _required_mapping(state.get("roles"), "roles")
        active_role: Role | None = None
        if roles:
            if len(roles) != 1:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native role ownership is ambiguous",
                )
            raw_role = next(iter(roles))
            try:
                active_role = Role(raw_role)
            except ValueError as exc:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native role ownership is invalid",
                ) from exc
            if active_role not in ACP_ROLES:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native role ownership is invalid",
                )
        return state, receipt, process, active_role

    def _wait_for_main_process_receipt(self, path: Path) -> None:
        """Wait outside the lifecycle reservation for native_main to publish."""

        deadline = time.monotonic() + PROCESS_WAIT_SECONDS
        while True:
            state = _read_state(path)
            native = _native(state)
            if "tmux_receipt" not in native:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native Main startup is unresolved; state and socket path retained",
                )
            if isinstance(native.get("main_process"), dict):
                self._state = state
                return
            if native.get("phase") not in {"running", "stopping"}:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native Main process receipt is unknown",
                )
            if time.monotonic() >= deadline:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native Main process receipt is unavailable",
                )
            time.sleep(PROCESS_POLL_SECONDS)

    def _cancel_runner(self, state: dict[str, object], role: Role) -> None:
        path = _state_path(state)
        assignment = _assignment(state, role)
        _validate_runner_argv(path, role, assignment)
        process = self._runners.get(role.value)
        if process is not None and process.pid != assignment.get("runner_pid"):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native ACP runner PID changed"
            )
        pid, pgid = _runner_identity_is_owned(assignment)
        if (
            process is not None
            and process.poll() is not None
            and _process_group_alive(pgid)
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native ACP runner exited but its saved group is live; assignment retained",
            )
        runner_alive = _process_group_alive(pgid)
        if runner_alive:
            # The ACP runner must finish both bounded session cleanup commands
            # before escalation can safely terminate its supervisor.
            _terminate_process_group(pid, pgid, grace_seconds=ACP_CANCEL_GRACE_SECONDS)
        if process is not None:
            if process.poll() is None:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native ACP runner exit is unconfirmed",
                )
            process.wait()
        # A cancelled ACP session has no trusted completion cleanup receipt.  A
        # publisher-created result with cleanup_confirmed=True is the only safe
        # evidence that its private session can be reclaimed.
        current = _read_state(path)
        current_assignment = _assignment(current, role)
        task_id, dispatch_id, terminal, nonce = _assignment_identity(
            current_assignment, role=role, state=current
        )
        if _assignment_identity(assignment, role=role, state=state) != (
            task_id,
            dispatch_id,
            terminal,
            nonce,
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native ACP assignment changed during cancellation",
            )
        _validate_runner_argv(path, role, current_assignment)
        result = current.get("native_result")
        if result is None:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native ACP cancellation cleanup is unknown; assignment retained",
            )
        run_id = _required_string(current.get("run_id"), "run_id")
        _delivery, _outcome, cleanup_confirmed, _body = _native_result_fields(
            result,
            role=role.value,
            run_id=run_id,
            task_id=task_id,
            dispatch_id=dispatch_id,
            terminal_handle=terminal,
            launch_nonce=nonce,
        )
        if not cleanup_confirmed:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native ACP cancellation cleanup is unconfirmed; assignment retained",
            )
        self._cleanup_assignment(current_assignment, path, role)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            current = _read_state(path)
            current_assignment = _assignment(current, role)
            current_task, current_dispatch, current_terminal, current_nonce = (
                _assignment_identity(current_assignment, role=role, state=current)
            )
            if (current_task, current_dispatch, current_terminal, current_nonce) != (
                task_id,
                dispatch_id,
                terminal,
                nonce,
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native ACP assignment changed during cancellation",
                )
            roles = _required_mapping(current.get("roles"), "roles")
            roles.pop(role.value, None)
            current.pop("native_result", None)
            _clear_pending(current)
            _save_state(
                path,
                current,
                require_existing=True,
                reservation_held=True,
            )
            self._state = current
        finally:
            reservation.release()

    def _stop_supervisor(
        self,
        state: dict[str, object],
        receipt: TmuxReceipt,
        process: Mapping[str, object],
    ) -> None:
        if self._supervisor_cleanup_confirmed(state, process):
            return
        phase = process.get("phase")
        if phase == "exited":
            if process.get("group_stopped") is not True:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native Main process cleanup is unconfirmed",
                )
            return
        if phase != "running":
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native Main process phase is unknown",
            )
        pid = process.get("supervisor_pid")
        if not isinstance(pid, int) or pid != receipt.pane_pid:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native supervisor process identity is invalid",
            )
        # native_main owns the Main child process group and handles this exact
        # supervisor signal.  Its receipt's process_group_id belongs to the
        # Claude child, so it must never be used to signal the supervisor.
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError as exc:
            if self._supervisor_cleanup_confirmed(state, process):
                return
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native supervisor exit effect is unknown",
            ) from exc
        except PermissionError as exc:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native supervisor cannot be signaled"
            ) from exc
        except OSError as exc:
            raise _runtime_error(exc, "native supervisor stop failed") from exc
        deadline = time.monotonic() + PROCESS_WAIT_SECONDS
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(PROCESS_POLL_SECONDS)
        if _pid_alive(pid):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native supervisor exit is unconfirmed",
            )
        proof_deadline = time.monotonic() + PROCESS_WAIT_SECONDS
        while time.monotonic() < proof_deadline:
            if self._supervisor_cleanup_confirmed(state, process):
                return
            time.sleep(PROCESS_POLL_SECONDS)
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native supervisor cleanup proof is unavailable",
        )

    def _supervisor_cleanup_confirmed(
        self, state: Mapping[str, object], process: Mapping[str, object]
    ) -> bool:
        current = _read_state(_state_path(state))
        saved = _native(current).get("main_process")
        if not isinstance(saved, Mapping):
            return False
        if any(
            saved.get(key) != process.get(key)
            for key in (
                "supervisor_pid",
                "agent_pid",
                "process_group_id",
                "launch_nonce",
            )
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native supervisor identity changed during stop",
            )
        if saved.get("phase") == "exited" and saved.get("group_stopped") is True:
            self._state = current
            return True
        return False

    def _require_state(self) -> dict[str, object]:
        if self._state is None:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING, "agent-team runtime has not been started"
            )
        return self._state

    @staticmethod
    def _ensure_supported_platform() -> None:
        if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "native tmux runtime requires Linux or macOS process identity support",
            )


__all__ = ("TmuxBackend", "publish_completion")
