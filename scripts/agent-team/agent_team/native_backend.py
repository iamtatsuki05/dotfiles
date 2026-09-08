"""Native task progression independent of the Main terminal host.

The terminal process is only a private host for Main.  ACP roles communicate
completion through :func:`publish_completion`; pane output is deliberately
never interpreted as a lifecycle message.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Final, Generic, NoReturn, cast

from . import native_acp_dependencies, native_delivery, native_questions
from .adapters import (
    ExecutionError,
    _process_group_exited,
    _wait_for_process_group_exit,
    remove_owned_tree,
)
from .contracts import (
    AckReceipt,
    Assignment,
    Attach,
    AttachCoordinator,
    AttachReceipt,
    BackendPort,
    BackendRequest,
    BackendResult,
    CompletionIdentity,
    CoordinatorAttachReceipt,
    DeliveryAck,
    DeliveryRef,
    DispatchRef,
    ErrorCode,
    LaunchMode,
    MessageRef,
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
    StartResult,
    StartSpec,
    Status,
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
from .harness_launch import LaunchValidationError, build_claude_argv
from .locking import _LifecycleReservation
from .named_graph import GraphSpec, validate_graph
from .native_controller import controller_keys
from .native_question_channel import validate_question_request
from .native_terminal import (
    NativeTerminalDriver,
    NativeTerminalInspection,
    NativeTerminalReceipt,
    ReceiptT,
    is_native_runtime,
)
from .parallel_admission import admission_blocker
from .process_identity import python_process_argv, read_process_argv
from .runtime import (
    MAX_PROMPT_CHARS,
    MAX_RESULT_BODY_CHARS,
    NAMED_STATE_VERSION,
    PARALLEL_STATE_VERSION,
    RuntimeValidationError,
    StatePublishError,
    acp_environment,
    build_acp_agent_command,
    build_acp_session_name,
    create_prompt_file,
    remove_prompt_file,
    resolve_state_role,
    validate_native_controller_process,
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
from .scoped_acp import (
    SCOPED_AGENT,
    SCOPED_CLIENT,
    SCOPED_POLICY,
    SCOPED_QUESTIONS,
    checked_digest,
    create_write_policy,
    native_profile,
)
from .task_execution import (
    acknowledge_task,
    answer_task_consultation,
    is_plan_only,
    new_program_wave,
    parse_review,
    prepare_dispatch,
    program_wave,
    task_consultation,
    transition_program_wave,
    validate_task_assignment,
    verification_revision,
)
from .task_spec import TaskSpec, parse_task_specs
from .workspace_revision import snapshot_revision

NATIVE_PHASES: Final = frozenset({"starting", "running", "stopping"})
ACP_ROLES: Final = frozenset({Role.PLANNER, Role.WORKER, Role.REVIEWER})
PROCESS_WAIT_SECONDS: Final = 5.0
ACP_CANCEL_GRACE_SECONDS: Final = 45.0
PROCESS_POLL_SECONDS: Final = 0.05
PACKAGE_ROOT: Final = Path(__file__).resolve().parent.parent
PENDING_DELIVERY_ID: Final = "pending_delivery_id"
PENDING_DELIVERY_KIND: Final = "pending_delivery_kind"
PENDING_DELIVERY_STAGE: Final = "pending_delivery_stage"
PENDING_QUESTION_IDS: Final = "pending_question_ids"
REPLIED_QUESTION_IDS: Final = "replied_question_ids"
_RUNNER_GATE_SCRIPT: Final = (
    "import os,sys\n"
    "fd=int(sys.argv[1])\n"
    "if os.read(fd, 1) != b'1':\n"
    "    raise SystemExit(1)\n"
    "os.close(fd)\n"
    "os.execvpe(sys.argv[2], sys.argv[2:], os.environ)\n"
)


class VerificationCancelled(BaseException):
    pass


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
    _require_codex_inspection_cleanup(state)
    if _native(state).get("phase") != "running":
        raise RuntimeFailure(ErrorCode.BUSY, "native team is not running")


def _require_codex_inspection_cleanup(state: Mapping[str, object]) -> None:
    if _native(state).get("codex_inspection_cleanup") is not None:
        raise RuntimeFailure(
            ErrorCode.BUSY,
            "Codex inspection process cleanup is unconfirmed; state is retained",
        )


def _assignment(state: Mapping[str, object], role: RoleTarget) -> dict[str, object]:
    _require_target(state, role)
    roles = _required_mapping(state.get("roles"), "roles")
    raw = roles.get(role_id(role))
    if not isinstance(raw, dict):
        raise RuntimeFailure(
            ErrorCode.TEAM_NOT_RUNNING,
            "role has no active native ACP assignment",
        )
    return raw


def _require_target(state: Mapping[str, object], role: RoleTarget) -> None:
    try:
        selected = resolve_state_role(state, role_id(role))
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "native node is not selected"
        ) from exc
    if selected != role:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "native node identity or kind does not match"
        )


def _named_identity(state: Mapping[str, object], node_id: str) -> dict[str, str]:
    target = resolve_state_role(state, node_id)
    return {"role_kind": target.kind.value} if isinstance(target, NodeRef) else {}


def _role_specs(state: Mapping[str, object]) -> dict[str, object]:
    return _required_mapping(state.get("role_specs"), "role_specs")


def _spec_dict(spec: object, role: RoleTarget) -> dict[str, object]:
    if not hasattr(spec, "provider"):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            f"role spec is invalid: {role_id(role)}",
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
            f"role spec is incomplete: {role_id(role)}",
        )
    result: dict[str, object] = dict(values)
    adapter_id = getattr(spec, "adapter_id", None)
    if adapter_id is not None:
        if not isinstance(adapter_id, str) or not adapter_id:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                f"role spec adapter is invalid: {role_id(role)}",
            )
        result["adapter_id"] = adapter_id
    raw_executables = getattr(spec, "acp_executables", None)
    if raw_executables is not None:
        if not isinstance(raw_executables, Mapping):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                f"role spec ACP bindings are invalid: {role_id(role)}",
            )
        result["acp_executables"] = dict(raw_executables)
    wrapper_digest = getattr(spec, "scoped_wrapper_sha256", None)
    if wrapper_digest is not None:
        result["scoped_wrapper_sha256"] = wrapper_digest
    client_digest = getattr(spec, "scoped_client_sha256", None)
    if client_digest is not None:
        result["scoped_client_sha256"] = client_digest
    policy_digest = getattr(spec, "scoped_policy_sha256", None)
    if policy_digest is not None:
        result["scoped_policy_sha256"] = policy_digest
    question_digest = getattr(spec, "scoped_question_client_sha256", None)
    if question_digest is not None:
        result["scoped_question_client_sha256"] = question_digest
    provider_snapshot = getattr(spec, "provider_snapshot", None)
    if provider_snapshot is not None:
        if not isinstance(provider_snapshot, Mapping):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "provider snapshot must be a mapping"
            )
        result["provider_snapshot"] = copy.deepcopy(dict(provider_snapshot))
    return result


def _validate_profile(
    spec: StartSpec, launcher_path: Path, *, preflight: bool = True
) -> tuple[dict[str, dict[str, object]], dict[str, object], list[str]]:
    """Validate the explicitly selected native profiles before any state write."""

    raw_specs = spec.role_specs
    if not isinstance(raw_specs, Mapping):
        raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role specs are required")
    main: RoleTarget | None
    if spec.graph is not None:
        try:
            validate_graph(spec.graph, spec.task_specs)
        except (TypeError, ValueError) as exc:
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        if set(raw_specs) != set(spec.graph.nodes):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "node specs must match the selected graph"
            )
        if (
            spec.graph.coordination.dispatch_mode == "parallel"
            and spec.graph.coordination.mode != "program"
        ):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "parallel native execution requires program coordination",
            )
        main = spec.graph.main_node
        if spec.graph.coordination.mode == "program" and not spec.task_specs:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "program coordination requires declared TaskSpecs",
            )
    else:
        if any(not isinstance(role, Role) for role in raw_specs):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "named nodes require an explicit graph"
            )
        main = Role.MAIN if Role.MAIN in raw_specs else None
    if main is None and spec.graph is None:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "native runtime requires a Main role",
        )
    if any(role_kind(role) is Role.WORKER for role in raw_specs) and not any(
        role_kind(role) is Role.REVIEWER for role in raw_specs
    ):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "native Worker requires a selected Reviewer"
        )
    allowed = {Role.MAIN, *ACP_ROLES}
    unknown = [
        role_id(role) if isinstance(role, (Role, NodeRef)) else str(role)
        for role in raw_specs
        if not isinstance(role, (Role, NodeRef)) or role_kind(role) not in allowed
    ]
    if unknown:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "native runtime does not support selected role: " + ", ".join(unknown),
        )

    role_specs: dict[str, dict[str, object]] = {}
    acp_bindings: dict[str, object] = {}
    for role, role_spec in raw_specs.items():
        if not isinstance(role, (Role, NodeRef)):
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role spec key is invalid")
        normalized = _spec_dict(role_spec, role)
        if isinstance(role, NodeRef):
            normalized["kind"] = role.kind.value
        if role_kind(role) is Role.MAIN:
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
            try:
                expected = native_profile(
                    cast(str, normalized.get("provider")), role_kind(role).value
                )
            except RuntimeValidationError as exc:
                raise RuntimeFailure(ErrorCode.INVALID_REQUEST, str(exc)) from exc
            if any(normalized.get(key) != value for key, value in expected.items()):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    f"native {role_id(role)} does not match its scoped ACP profile",
                )
            raw = normalized.get("acp_executables")
            if not isinstance(raw, Mapping):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    f"native {role_id(role)} is missing ACP executable bindings",
                )
            if preflight:
                try:
                    executables: (
                        native_acp_dependencies.CodexAcpExecutables
                        | native_acp_dependencies.NativeAcpExecutables
                    )
                    if normalized["provider"] == "codex":
                        from . import codex_acp

                        executables = (
                            native_acp_dependencies.CodexAcpExecutables.from_dict(raw)
                        )
                        executables.verify()
                        provider_snapshot = normalized.get("provider_snapshot")
                        if not isinstance(provider_snapshot, Mapping):
                            raise RuntimeValidationError(
                                "Codex provider snapshot is missing"
                            )
                        codex_acp.verify_snapshot(provider_snapshot, spec.workspace)
                    else:
                        executables = (
                            native_acp_dependencies.NativeAcpExecutables.from_dict(raw)
                        )
                        executables.verify()
                    # The snapshot helper is part of the explicit ACP boundary.
                    # It is called before state/task/process creation so a bad
                    # binding cannot leave a native assignment behind.
                    snapshot = (
                        native_acp_dependencies.codex_adapter_snapshot(executables)
                        if isinstance(
                            executables, native_acp_dependencies.CodexAcpExecutables
                        )
                        else native_acp_dependencies.adapter_snapshot(executables)
                    )
                except Exception as exc:
                    raise _runtime_error(
                        exc,
                        f"selected {role_id(role)} ACP dependencies are unavailable",
                        code=ErrorCode.INVALID_REQUEST,
                    ) from exc
                if not isinstance(snapshot, Mapping):
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "ACP adapter snapshot is invalid",
                    )
                acp_bindings[role_id(role)] = executables
                normalized["acp_executables"] = executables.as_dict()
                if normalized["provider"] == "claude":
                    normalized["scoped_wrapper_sha256"] = checked_digest(SCOPED_AGENT)
                    normalized["scoped_client_sha256"] = checked_digest(SCOPED_CLIENT)
                    normalized["scoped_policy_sha256"] = checked_digest(SCOPED_POLICY)
                    normalized["scoped_question_client_sha256"] = checked_digest(
                        SCOPED_QUESTIONS
                    )
        role_specs[role_id(role)] = normalized

    if not preflight or main is None:
        return role_specs, acp_bindings, []
    try:
        argv = list(
            build_claude_argv(
                role="main",
                model=cast(str, role_specs[role_id(main)]["model"]),
                effort=cast(str, role_specs[role_id(main)]["effort"]),
                permission="orchestrator",
                instructions=cast(str, role_specs[role_id(main)]["instructions"]),
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


def _inspection_dict(observed: NativeTerminalInspection) -> dict[str, object]:
    return {
        "presence": observed.presence,
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


def _terminal_stop_is_owned(
    receipt: NativeTerminalReceipt,
    observed: NativeTerminalInspection,
    main_process: Mapping[str, object],
    *,
    pid_key: str = "agent_pid",
) -> bool:
    try:
        validate_native_controller_process(main_process, pid_key=pid_key)
    except RuntimeValidationError:
        return False
    if (
        observed.identity_verified is not True
        or main_process.get("supervisor_pid") != receipt.pane_pid
    ):
        return False
    if observed.presence == "present":
        return observed.pane_present is True and observed.pane_pid == receipt.pane_pid
    if observed.presence == "absent":
        return (
            observed.pane_present is False
            and observed.pane_pid is None
            and observed.running is False
            and observed.session_present is True
            and observed.server_pid == receipt.server_pid
            and observed.observed_nonce == receipt.run_nonce
            and main_process.get("phase") == "exited"
            and main_process.get("group_stopped") is True
        )
    return False


def _validated_supervisor_argv(state: Mapping[str, object]) -> tuple[str, ...]:
    raw = _native(state).get("supervisor_argv")
    if not isinstance(raw, list) or len(raw) != 8:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native supervisor argv snapshot is unavailable; state retained",
        )
    argv = tuple(_required_string(value, "supervisor argv") for value in raw)
    expected_arguments = (
        "-m",
        "agent_team",
        "_native-main",
        "--state",
        str(_state_path(state)),
        "--run-id",
        _required_string(state.get("run_id"), "run_id"),
    )
    if not Path(argv[0]).is_absolute() or argv[1:] != expected_arguments:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native supervisor argv snapshot does not match this run",
        )
    return argv


def _native_result_fields(
    value: object,
    *,
    role: RoleTarget,
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
        "role": role_id(role),
        "run_id": run_id,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "terminal_handle": terminal_handle,
        "launch_nonce": launch_nonce,
        **({"role_kind": role.kind.value} if isinstance(role, NodeRef) else {}),
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
    assignment: Mapping[str, object], *, role: RoleTarget, state: Mapping[str, object]
) -> tuple[str, str, str, str]:
    _require_target(state, role)
    if isinstance(role, NodeRef) and (
        assignment.get("role") != role.node_id
        or assignment.get("role_kind") != role.kind.value
    ):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "native assignment node identity changed"
        )
    values = tuple(
        assignment.get(key)
        for key in ("task_id", "dispatch_id", "terminal_handle", "launch_nonce")
    )
    if not all(isinstance(value, str) and value for value in values):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            f"native {role_id(role)} assignment identity is incomplete",
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


def _remove_socket_root(root: Path) -> None:
    """Remove only the empty private directory allocated for this receipt."""

    if root.parent != Path("/tmp"):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native socket root does not match its receipt",
        )
    try:
        info = root.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _runtime_error(exc, "native socket root cannot be inspected") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native socket root ownership is unproven",
        )
    try:
        root.rmdir()
    except OSError as exc:
        raise _runtime_error(exc, "native socket root cleanup failed") from exc


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


def _runner_gate_argv(
    launch_argv: list[str], release_fd: int
) -> tuple[list[str], tuple[str, ...]]:
    gate_argv = [
        sys.executable,
        "-c",
        _RUNNER_GATE_SCRIPT,
        str(release_fd),
        *launch_argv,
    ]
    try:
        identity = python_process_argv(gate_argv)
    except (ValueError, RuntimeError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native Python runner gate identity is unproven",
        ) from exc
    return gate_argv, identity


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
    accepted_argvs: tuple[tuple[str, ...], ...] = (tuple(argv),)
    startup_argv = assignment.get("runner_startup_argv")
    if startup_argv is not None:
        if (
            not isinstance(startup_argv, list)
            or not startup_argv
            or any(not isinstance(item, str) or not item for item in startup_argv)
        ):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native ACP runner startup argv identity is unavailable",
            )
        startup_tuple = tuple(startup_argv)
        if (
            len(startup_tuple) < 5
            or startup_tuple[0] != argv[0]
            or startup_tuple[1:3] != ("-c", _RUNNER_GATE_SCRIPT)
            or startup_tuple[4:]
            not in {tuple(argv), (sys.executable, *tuple(argv[1:]))}
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native ACP runner startup argv does not match state",
            )
        try:
            if int(startup_tuple[3]) < 0:
                raise ValueError
        except ValueError as exc:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native ACP runner startup fd is invalid",
            ) from exc
        accepted_argvs += (startup_tuple,)
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
            if _process_group_alive(pgid):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native ACP runner argv ownership is unproven",
                )
        elif observed_argv not in accepted_argvs:
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
    state_path: Path, role: RoleTarget, assignment: Mapping[str, object]
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
        role_id(role),
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
        if not _process_group_alive(pgid):
            return
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


def _publisher_assignment(
    state_path: Path,
    state: dict[str, object],
    *,
    role: str,
    run_id: str,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    launch_nonce: str,
) -> dict[str, object]:
    if not is_native_runtime(state.get("runtime")):
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
    if not isinstance(selected_spec, Mapping):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "native completion role spec is missing"
        )
    try:
        expected = native_profile(
            cast(str, selected_spec.get("provider")),
            role_kind(resolve_state_role(state, role)).value,
        )
    except RuntimeValidationError as exc:
        raise RuntimeFailure(ErrorCode.IDENTITY_MISMATCH, str(exc)) from exc
    if any(
        selected_spec.get(key) != expected_value
        for key, expected_value in expected.items()
    ):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native completion role spec does not match its ACP profile",
        )
    assignment = _assignment(state, resolve_state_role(state, role))
    saved_task, saved_dispatch, saved_terminal, saved_nonce = _assignment_identity(
        assignment, role=resolve_state_role(state, role), state=state
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
    _assert_publisher(
        _absolute(state_path), resolve_state_role(state, role), assignment
    )
    return assignment


def _delivery_state(
    state: Mapping[str, object], node_id: str | None = None
) -> dict[str, object]:
    if state.get("version") == PARALLEL_STATE_VERSION:
        if not isinstance(node_id, str):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "parallel delivery requires a node identity",
            )
        try:
            target = resolve_state_role(state, node_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native node is not selected"
            ) from exc
        _assignment(state, target)
    return _required_mapping(native_delivery.container(state, node_id), "delivery")


def _delivery_for_id(
    state: Mapping[str, object], identity: str, *, message: bool = False
) -> tuple[str | None, dict[str, object]]:
    matches = [
        (node, _required_mapping(value, "delivery"))
        for node, value in native_delivery.containers(state)
        if (
            identity in cast(list[str], value.get(PENDING_QUESTION_IDS, []))
            if message
            else value.get(PENDING_DELIVERY_ID) == identity
        )
    ]
    if len(matches) != 1:
        raise RuntimeFailure(
            ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
            "identity does not match one native pending observation",
        )
    return matches[0]


def _state_version(spec: StartSpec) -> int:
    if spec.graph is None:
        return 3
    if spec.graph.coordination.dispatch_mode == "parallel":
        return PARALLEL_STATE_VERSION
    return NAMED_STATE_VERSION


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
    task_evidence: Mapping[str, object] | None = None,
) -> str:
    """Publish one trusted ACP completion after the runner cleaned its session."""

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
    reservation.acquire_for_publication()
    try:
        state = _read_state(_absolute(state_path))
        assignment = _publisher_assignment(
            state_path,
            state,
            role=role,
            run_id=run_id,
            task_id=task_id,
            dispatch_id=dispatch_id,
            terminal_handle=terminal_handle,
            launch_nonce=launch_nonce,
        )
        task = validate_task_assignment(state, assignment)
        if _native(state)["phase"] == "stopping":
            outcome = "failed"
            task_evidence = None
        delivery = _delivery_state(state, role)
        question = native_questions.outbox(state, role)
        if question is not None:
            if question["phase"] == "recorded" and cleanup_confirmed:
                delivery.pop("native_question")
            elif outcome == "succeeded":
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "native question must be answered, acknowledged, and received before completion",
                )
            elif not (
                state["version"] == PARALLEL_STATE_VERSION
                and question["phase"] == "failed"
            ):
                stopping = _native(state)["phase"] == "stopping"
                question["phase"] = "cancelling" if stopping else "failed"
                question["error"] = (
                    None
                    if stopping
                    else "ACP ended before question delivery cleanup was confirmed"
                )
        evidence = None
        target = resolve_state_role(state, role)
        if (
            task is not None
            and role_kind(target) is Role.REVIEWER
            and outcome == "succeeded"
        ):
            stage = assignment.get("task_stage")
            revision = assignment.get("task_revision")
            if (
                not isinstance(stage, str)
                or not isinstance(revision, str)
                or task_evidence is None
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH, "trusted review evidence is missing"
                )
            evidence = parse_review(
                json.dumps(dict(task_evidence)),
                task=task,
                stage=stage,
                revision=revision,
            )
            workspace_revision = (
                assignment.get("task_workspace_revision")
                if is_plan_only(state, task.task_id)
                else revision
                if stage == "implementation"
                else None
            )
            if (
                workspace_revision is not None
                and snapshot_revision(Path(str(state["workspace"])))
                != workspace_revision
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH, "reviewed workspace revision changed"
                )
        elif task_evidence is not None:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "task evidence requires a successful structured review",
            )
        existing = delivery.get("native_result")
        if existing is not None:
            existing_fields = _native_result_fields(
                existing,
                role=target,
                run_id=run_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                terminal_handle=terminal_handle,
                launch_nonce=launch_nonce,
            )
            if (
                existing_fields[1:] != (outcome, cleanup_confirmed, body)
                or cast(Mapping[str, object], existing).get("task_evidence") != evidence
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native completion was already published",
                )
            return outcome
        delivery["native_result"] = {
            "role": role,
            **_named_identity(state, role),
            "run_id": run_id,
            "task_id": task_id,
            "dispatch_id": dispatch_id,
            "terminal_handle": terminal_handle,
            "launch_nonce": launch_nonce,
            "outcome": outcome,
            "body": body,
            "cleanup_confirmed": cleanup_confirmed,
            "delivery_id": _new_id(),
            **({"logical_task_id": task.task_id} if task is not None else {}),
            **({"task_evidence": evidence} if evidence is not None else {}),
            **(
                {"question_receipts": copy.deepcopy(assignment["question_receipts"])}
                if "question_receipts" in assignment
                else {}
            ),
        }
        _save_state(
            _absolute(state_path),
            state,
            require_existing=True,
            reservation_held=True,
        )
        return outcome
    finally:
        reservation.release()


def _assert_publisher(
    path: Path, role: RoleTarget, assignment: Mapping[str, object]
) -> None:
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


def _read_question_state(
    path: Path, identity: Mapping[str, str]
) -> tuple[dict[str, object], dict[str, object]]:
    state = _read_state(_absolute(path))
    named = state["version"] in {NAMED_STATE_VERSION, PARALLEL_STATE_VERSION}
    fields = (
        native_questions.NAMED_IDENTITY_FIELDS
        if named
        else native_questions.IDENTITY_FIELDS
    )
    if set(identity) != set(fields):
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native question publisher identity is incomplete",
        )
    try:
        target = resolve_state_role(state, identity["role"])
    except RuntimeValidationError as exc:
        raise RuntimeFailure(ErrorCode.IDENTITY_MISMATCH, str(exc)) from exc
    if named and identity["role_kind"] != role_kind(target).value:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native question publisher kind does not match its graph",
        )
    assignment = _publisher_assignment(
        path,
        state,
        **{field: identity[field] for field in native_questions.IDENTITY_FIELDS},
    )
    validate_task_assignment(state, assignment)
    spec = _required_mapping(_role_specs(state)[identity["role"]], "question role spec")
    if spec.get("provider") != "claude":
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "native questions require the selected Claude ACP profile",
        )
    return state, assignment


@contextmanager
def _question_state(
    path: Path, identity: Mapping[str, str]
) -> Iterator[tuple[dict[str, object], dict[str, object]]]:
    reservation = _LifecycleReservation(_absolute(path), create_parent=False)
    reservation.acquire_for_publication()
    try:
        yield _read_question_state(path, identity)
    finally:
        reservation.release()


def _matching_question(
    state: Mapping[str, object], request: Mapping[str, object], node_id: str
) -> dict[str, object]:
    question = native_questions.outbox(state, node_id)
    if question is None or question["request"] != request:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "native question does not match the active client request",
        )
    return question


def publish_question(
    state_path: Path, *, request: Mapping[str, object], **identity: str
) -> None:
    """Publish a client request through the same ownership gate as completion."""
    with _question_state(state_path, identity) as (state, _assignment_value):
        _require_running(state)
        delivery = _delivery_state(state, identity["role"])
        previous_question = native_questions.outbox(state, identity["role"])
        if (
            (previous_question is not None and previous_question["phase"] != "recorded")
            or delivery.get("native_result") is not None
            or delivery.get(PENDING_DELIVERY_ID) is not None
        ):
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "another native question or completion is pending",
            )
        try:
            parsed = validate_question_request(dict(request))
        except ValueError as exc:
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        delivery["native_question"] = {
            **identity,
            **_named_identity(state, identity["role"]),
            "phase": "published",
            "request": parsed.as_dict(),
            "delivery_id": _new_id(),
            "message_ids": [_new_id() for _field in parsed.questions],
            "answers": {},
            "error": None,
        }
        _save_state(state_path, state, require_existing=True, reservation_held=True)


def question_answers(
    state_path: Path, *, request: Mapping[str, object], **identity: str
) -> dict[str, str] | None:
    """Release saved answers only after Main acknowledged the question Delivery."""
    # Atomic state replacement lets polling read without contending with Main.
    state, _assignment_value = _read_question_state(state_path, identity)
    _require_running(state)
    question = _matching_question(state, request, identity["role"])
    if question["phase"] in {"failed", "cancelling"}:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "native question channel is unavailable",
        )
    if question["phase"] != "acknowledged":
        return None
    parsed = validate_question_request(question["request"])
    ids = cast(list[str], question["message_ids"])
    answers = cast(dict[str, str], question["answers"])
    return {
        field.field: answers[message_id]
        for field, message_id in zip(parsed.questions, ids, strict=True)
    }


def confirm_question(
    state_path: Path, *, request: Mapping[str, object], **identity: str
) -> None:
    """Record client receipt while retaining the outbox across publication failure."""
    with _question_state(state_path, identity) as (state, assignment):
        _require_running(state)
        question = _matching_question(state, request, identity["role"])
        if question["phase"] != "acknowledged":
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION, "native question was not acknowledged"
            )
        previous = cast(
            list[dict[str, object]], assignment.get("question_receipts", [])
        )
        assignment["question_receipts"] = [
            *previous,
            native_questions.receipt(question),
        ]
        question["phase"] = "received"
        _save_state(state_path, state, require_existing=True, reservation_held=True)


def record_question_sent(
    state_path: Path, *, request: Mapping[str, object], **identity: str
) -> None:
    """Confirm that the channel sent the recorded frame before releasing its outbox."""
    with _question_state(state_path, identity) as (state, _assignment_value):
        _require_running(state)
        question = _matching_question(state, request, identity["role"])
        if question["phase"] != "received":
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION, "native question client receipt is missing"
            )
        question["phase"] = "recorded"
        _save_state(state_path, state, require_existing=True, reservation_held=True)


def fail_question(
    state_path: Path, *, request: Mapping[str, object] | None, **identity: str
) -> None:
    """Keep an interrupted outbox inspectable; this does not acknowledge it."""
    with _question_state(state_path, identity) as (state, _assignment_value):
        question = native_questions.outbox(state, identity["role"])
        if question is None:
            return
        if request is not None and question["request"] != request:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "failed native question identity changed"
            )
        if state["version"] == PARALLEL_STATE_VERSION and question["phase"] == "failed":
            return
        if _native(state)["phase"] == "stopping":
            question["phase"] = "cancelling"
            question["error"] = None
        else:
            question["phase"] = "failed"
            question["error"] = (
                "native question channel failed; assignment and unanswered delivery are retained"
            )
        _save_state(state_path, state, require_existing=True, reservation_held=True)


class NativeBackend(BackendPort, ABC, Generic[ReceiptT]):
    """Enforce task progression using a concrete terminal ownership contract."""

    def __init__(
        self,
        *,
        launcher_path: Path | None = None,
        resume_existing: bool = False,
    ) -> None:
        self._launcher_path = _absolute(launcher_path or Path(sys.argv[0]))
        self._resume_existing = resume_existing
        self._state: dict[str, object] | None = None
        self._driver: NativeTerminalDriver[ReceiptT] | None = None
        self._runners: dict[str, subprocess.Popen[bytes]] = {}
        self._program_owner: dict[str, object] | None = None
        self.last_start_response: dict[str, object] | None = None
        self.last_status_response: dict[str, object] | None = None
        self.last_attach_response: dict[str, object] | None = None
        self.last_stop_response: dict[str, object] | None = None

    @property
    @abstractmethod
    def runtime(self) -> str: ...

    @property
    def _receipt_key(self) -> str:
        return f"{self.runtime}_receipt"

    @abstractmethod
    def _new_driver(
        self, socket_root: Path, run_nonce: str, session_name: str
    ) -> NativeTerminalDriver[ReceiptT]: ...

    @abstractmethod
    def _startup_socket_path(self, socket_root: Path, session_name: str) -> Path: ...

    @abstractmethod
    def _parse_receipt(self, value: object) -> ReceiptT: ...

    @abstractmethod
    def _restore_driver(self, receipt: ReceiptT) -> NativeTerminalDriver[ReceiptT]: ...

    @abstractmethod
    def _socket_root(self, receipt: ReceiptT) -> Path: ...

    def _receipt_from_state(self, native: Mapping[str, object]) -> ReceiptT:
        try:
            return self._parse_receipt(native.get(self._receipt_key))
        except Exception as exc:
            raise _runtime_error(exc, "native terminal receipt is invalid") from exc

    def _driver_from_receipt(self, receipt: ReceiptT) -> NativeTerminalDriver[ReceiptT]:
        try:
            return self._restore_driver(receipt)
        except Exception as exc:
            raise _runtime_error(exc, "native terminal cannot be resumed") from exc

    def start(self, spec: StartSpec) -> StartResult:
        self._ensure_supported_platform()
        if spec.max_review_rounds is not None and (
            type(spec.max_review_rounds) is not int or spec.max_review_rounds < 1
        ):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "max_review_rounds must be a positive integer",
            )
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
        if not isinstance(spec.task_specs, tuple) or any(
            not isinstance(task, TaskSpec) for task in spec.task_specs
        ):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "declared task specifications are invalid"
            )
        try:
            parse_task_specs([task.as_dict() for task in spec.task_specs])
        except ValueError as exc:
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        if spec.task_specs and spec.max_review_rounds is None:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "declared tasks require max_review_rounds"
            )
        role_specs, _acp_bindings, main_argv = _validate_profile(
            spec, self._launcher_path, preflight=not self._resume_existing
        )
        state_path = _absolute(spec.state_path)
        if self._resume_existing:
            result = self._resume(spec, role_specs)
        else:
            reservation = _LifecycleReservation(state_path, create_parent=True)
            reservation.acquire()
            try:
                result = self._start_new_locked(
                    spec, role_specs=role_specs, main_argv=main_argv
                )
            finally:
                reservation.release()
        if spec.attach:
            main = spec.graph.main_node if spec.graph is not None else Role.MAIN
            self.request(Attach(main) if main is not None else AttachCoordinator())
        return result

    def request(self, request: BackendRequest) -> BackendResult:
        if isinstance(
            request,
            (
                RolePrompt,
                TaskDispatch,
                TaskVerify,
                RoleWait,
                RoleRead,
                RoleRelease,
                DeliveryAck,
            ),
        ):
            state = self._require_state()
            self._require_program_owner(_read_state(_state_path(state)))
        if isinstance(request, Status):
            return self._status()
        if isinstance(request, (Attach, AttachCoordinator)):
            return self._attach(request)
        if isinstance(request, (RolePrompt, TaskDispatch)):
            return self._prompt(request)
        if isinstance(request, TaskGet):
            return self._task_get(request)
        if isinstance(request, TaskConsultationReply):
            return self._task_consultation_reply(request)
        if isinstance(request, TaskVerify):
            return self._task_verify(request)
        if isinstance(request, RoleWait):
            return self._wait(request)
        if isinstance(request, RoleRead):
            return self._read(request)
        if isinstance(request, RoleRelease):
            return self._release(request)
        if isinstance(request, DeliveryAck):
            return self._ack(request)
        if isinstance(request, MessageReply):
            return self._reply(request)
        if isinstance(request, RoleGet):
            return self._role_get(request)
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "unsupported native runtime request"
        )

    def _require_program_owner(self, state: Mapping[str, object]) -> None:
        keys = controller_keys(state)
        if keys.pid != "coordinator_pid":
            return
        process = _native(state).get(keys.process)
        if not isinstance(process, dict):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "program coordinator process receipt is unavailable",
            )
        pid = os.getpid()
        native = _native(state)
        argv = native.get(keys.argv)
        expected = (
            "-m",
            "agent_team",
            "_program-run",
            "--state",
            str(_state_path(state)),
            "--run-id",
            _required_string(state.get("run_id"), "run_id"),
        )
        if not isinstance(argv, list) or tuple(argv[1:]) != expected:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "program coordinator command identity changed",
            )
        try:
            owned = (
                process.get("phase") == "running"
                and process.get(keys.pid) == pid
                and process.get("process_group_id") == pid
                and os.getpgid(pid) == pid
                and process.get("supervisor_pid") == os.getppid()
                and process.get("supervisor_pid")
                == self._receipt_from_state(_native(state)).pane_pid
                and read_process_argv(pid) == tuple(argv)
                and read_process_argv(os.getppid()) == _validated_supervisor_argv(state)
            )
        except OSError:
            owned = False
        identity = {
            key: process.get(key)
            for key in ("supervisor_pid", keys.pid, "process_group_id", "launch_nonce")
        }
        if not owned or (
            self._program_owner is not None and identity != self._program_owner
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "only the recorded program coordinator may advance tasks",
            )
        self._program_owner = identity

    def _progress_state(self, path: Path) -> dict[str, object]:
        state = self._reload_state(path)
        self._require_program_owner(state)
        return state

    @staticmethod
    def _require_delivery_phase(state: Mapping[str, object], *, stopping: bool) -> None:
        if stopping:
            if (
                state.get("version") != PARALLEL_STATE_VERSION
                or _native(state)["phase"] != "stopping"
            ):
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "Stop delivery drain requires a stopping parallel state",
                )
        else:
            _require_running(state)

    def _delivery_progress_state(
        self, path: Path, *, stopping: bool
    ) -> dict[str, object]:
        state = self._reload_state(path) if stopping else self._progress_state(path)
        self._require_delivery_phase(state, stopping=stopping)
        return state

    def program_transition(self, transition: str) -> None:
        path = _state_path(self._require_state())
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._progress_state(path)
            _require_running(state)
            revision = (
                snapshot_revision(Path(str(state["workspace"])))
                if transition == "seal_wave"
                else None
            )
            transition_program_wave(state, transition, revision=revision)
            _save_state(path, state, require_existing=True, reservation_held=True)
        finally:
            reservation.release()

    def program_snapshot(self) -> dict[str, object]:
        state = self._progress_state(_state_path(self._require_state()))
        _require_running(state)
        if controller_keys(state).pid != "coordinator_pid":
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "this team has no program coordinator"
            )
        return copy.deepcopy(state)

    @staticmethod
    def _task_record(state: dict[str, object], task_id: str) -> dict[str, object]:
        tasks = state.get("tasks")
        record = tasks.get(task_id) if isinstance(tasks, dict) else None
        if not isinstance(record, dict):
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "task is unknown")
        return record

    def _task_get(self, request: TaskGet) -> TaskStatusReceipt:
        path = _state_path(self._require_state())
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_state(path)
            record = self._task_record(state, request.task_id)
            consultation = task_consultation(state, record)
            return TaskStatusReceipt(
                request.task_id,
                str(record["status"]),
                {
                    **record,
                    **(
                        {"consultation": consultation}
                        if consultation is not None
                        else {}
                    ),
                },
            )
        finally:
            reservation.release()

    def _task_consultation_reply(
        self, request: TaskConsultationReply
    ) -> TaskStatusReceipt:
        path = _state_path(self._require_state())
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_state(path)
            _require_running(state)
            record = answer_task_consultation(
                state, request.consultation_id, request.body
            )
            _save_state(path, state, require_existing=True, reservation_held=True)
            task = TaskSpec.from_dict(record["spec"])
            return TaskStatusReceipt(task.task_id, str(record["status"]), dict(record))
        finally:
            reservation.release()

    def _task_verify(self, request: TaskVerify) -> TaskStatusReceipt:
        from .task_verification import run_verification

        path = _state_path(self._require_state())
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._progress_state(path)
            _require_running(state)
            if _verification_pending(state):
                raise RuntimeFailure(
                    ErrorCode.BUSY,
                    "verification cleanup must be confirmed before executing another task",
                )
            record = self._task_record(state, request.task_id)
            final_stage = (
                "plan" if is_plan_only(state, request.task_id) else "implementation"
            )
            if controller_keys(state).pid == "coordinator_pid":
                wave = program_wave(state)
                if (
                    wave["phase"] != "verification"
                    or request.task_id not in cast(list[str], wave["task_ids"])
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
            if state.get("roles") or state.get(PENDING_DELIVERY_ID) is not None:
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
                record["status"] = "verifying"
                _save_state(path, state, require_existing=True, reservation_held=True)
                try:
                    evidence = run_verification(task, workspace, revision)
                except BaseException as exc:
                    cleanup_confirmed = isinstance(
                        exc, (VerificationCancelled, RuntimeFailure)
                    )
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
                    _save_state(
                        path, state, require_existing=True, reservation_held=True
                    )
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
                _save_state(path, state, require_existing=True, reservation_held=True)
            return TaskStatusReceipt(task.task_id, str(record["status"]), dict(record))
        finally:
            reservation.release()

    def stop(self) -> StopResult:
        self._ensure_supported_platform()
        previous = self._require_state()
        path = _state_path(previous)
        _require_codex_inspection_cleanup(_read_state(path))
        self._wait_for_main_process_receipt(path)
        inspected_state = self._reload_state(path)
        inspected_receipt = self._receipt_from_state(_native(inspected_state))
        driver = self._driver or self._driver_from_receipt(inspected_receipt)
        self._driver = driver
        inspection = driver.inspect(inspected_receipt)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state, receipt, main_process, active_roles = self._prepare_stop_locked(
                path, inspected_state, inspection
            )
        finally:
            reservation.release()

        # Cancellation and supervisor waits intentionally happen outside the
        # lifecycle reservation.  ACP/native publishers must be able to finish
        # their final state write while a stop is waiting for a process group.
        if state["version"] == PARALLEL_STATE_VERSION:
            retained: list[dict[str, object]] = []
            drained: list[dict[str, object]] = []
            for target in active_roles:
                try:
                    drained.append(self._drain_stopping_assignment(path, target))
                except (RuntimeFailure, OSError) as exc:
                    retained.append({"role": role_id(target), "error": str(exc)})
            try:
                self._stop_supervisor(state, receipt, main_process)
            except (RuntimeFailure, OSError) as exc:
                retained.append({"role": "coordinator", "error": str(exc)})
            self.last_stop_response = {
                "status": "cleanup_pending" if retained else "drained",
                "drained": drained,
                "retained": retained,
            }
            if retained:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "parallel native cleanup remains unconfirmed: "
                    + ", ".join(str(item["role"]) for item in retained),
                )
        else:
            if active_roles:
                self._cancel_runner(state, active_roles[0])
            self._stop_supervisor(state, receipt, main_process)

        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            current = self._reload_state(path)
            native = _native(current)
            if native.get("phase") != "stopping":
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native stop phase changed during cleanup",
                )
            driver = self._driver or self._driver_from_receipt(
                self._receipt_from_state(native)
            )
            current_receipt = self._receipt_from_state(native)
            if current_receipt != receipt:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native receipt changed during stop",
                )
            inspected = driver.inspect(current_receipt)
            stopped_main = native.get(controller_keys(state).process)
            if not isinstance(stopped_main, dict) or not _terminal_stop_is_owned(
                current_receipt,
                inspected,
                stopped_main,
                pid_key=controller_keys(current).pid,
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native pane ownership is unproven during stop",
                )
            if (
                stopped_main.get("phase") != "exited"
                or stopped_main.get("group_stopped") is not True
            ):
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native Main process cleanup is unconfirmed during stop",
                )
            closed = driver.close(current_receipt)
            if (
                not closed.ownership_verified
                or not closed.session_terminated
                or not closed.server_terminated
                or closed.evidence
                not in {
                    "server-terminated",
                    "session-terminated",
                }
                or not closed.socket_removed
            ):
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native server termination is unproven",
                )
            _remove_socket_root(self._socket_root(current_receipt))
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
                **(
                    {"drained": drained, "retained": []}
                    if state["version"] == PARALLEL_STATE_VERSION
                    else {}
                ),
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
        controller_terminal = _new_id()
        run_nonce = secrets.token_hex(16)
        keys = controller_keys(
            {
                "version": _state_version(spec),
                **({"graph": spec.graph.as_dict()} if spec.graph is not None else {}),
            }
        )
        program = keys.pid == "coordinator_pid"
        controller_name = "coordinator" if program else "main"
        session_name = f"agent-team-{controller_name}-{run_nonce[:16]}"
        if program:
            main_argv = list(
                python_process_argv(
                    (
                        sys.executable,
                        "-m",
                        "agent_team",
                        "_program-run",
                        "--state",
                        str(state_path),
                        "--run-id",
                        run_id,
                    )
                )
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
        socket_root = Path(tempfile.mkdtemp(prefix="at-n", dir="/tmp"))
        try:
            os.chmod(socket_root, 0o700)
        except OSError as exc:
            raise _runtime_error(exc, "native socket root setup failed") from exc
        state: dict[str, object] = {
            "version": _state_version(spec),
            **({"graph": spec.graph.as_dict()} if spec.graph is not None else {}),
            "runtime": self.runtime,
            "team_id": spec.team_id,
            "workspace": str(_canonical(spec.workspace)),
            "config_path": str(_canonical(spec.config_path)),
            "state_path": str(state_path),
            "launcher_path": str(self._launcher_path),
            "run_id": run_id,
            keys.terminal: controller_terminal,
            "role_specs": role_specs,
            "task_specs": [task.as_dict() for task in spec.task_specs],
            "roles": {},
            **(
                {"max_review_rounds": spec.max_review_rounds, "tasks": {}}
                if spec.max_review_rounds is not None
                else {}
            ),
            "native": {
                "phase": "starting",
                "run_nonce": run_nonce,
                keys.argv: list(main_argv),
                "supervisor_argv": list(python_process_argv(supervisor_argv)),
                "startup_socket_path": str(
                    self._startup_socket_path(socket_root, session_name)
                ),
            },
        }
        if program:
            state["program_wave"] = new_program_wave(state)
        _save_state(
            state_path,
            state,
            require_existing=False,
            reservation_held=True,
        )
        try:
            driver = self._new_driver(socket_root, run_nonce, session_name)
            receipt = driver.create(
                supervisor_argv,
                cwd=PACKAGE_ROOT,
                env=acp_environment(),
                title=f"{spec.team_id}-{controller_name}",
            )
            if receipt.run_nonce != run_nonce or receipt.session_name != session_name:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native receipt identity does not match startup",
                )
        except Exception as exc:
            # The state intentionally remains in ``starting``.  A lost terminal
            # create effect must not be converted into a clean restart.
            raise _runtime_error(exc, "native Main startup failed") from exc
        native = _native(state)
        native[self._receipt_key] = receipt.as_dict()
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
            main_terminal_id=None if program else TerminalRef(controller_terminal),
            state_path=spec.state_path,
            coordinator_terminal_id=TerminalRef(controller_terminal)
            if program
            else None,
        )
        self.last_start_response = {
            "status": "running",
            "team_id": spec.team_id,
            "workspace": str(_canonical(spec.workspace)),
            "run_id": run_id,
            keys.terminal: controller_terminal,
            "state_path": str(spec.state_path),
        }
        return result

    def _resume(
        self, spec: StartSpec, expected_role_specs: dict[str, dict[str, object]]
    ) -> StartResult:
        state = _read_state(_absolute(spec.state_path))
        _require_codex_inspection_cleanup(state)
        self._assert_state_matches_spec(state, spec, expected_role_specs)
        native = _native(state)
        phase = native.get("phase")
        if phase not in {"starting", "running", "stopping"}:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "native phase is unknown"
            )
        if self._receipt_key not in native and phase != "starting":
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native Main startup is unresolved",
            )
        if self._receipt_key in native:
            receipt = self._receipt_from_state(native)
            driver = self._driver_from_receipt(receipt)
            inspected = driver.inspect(receipt)
            current = _read_state(_absolute(spec.state_path))
            self._assert_state_matches_spec(current, spec, expected_role_specs)
            self._assert_inspection_current(state, current)
            _require_codex_inspection_cleanup(current)
            state = current
            native = _native(state)
            if inspected.presence == "absent":
                main_process = native.get(controller_keys(state).process)
                owned = isinstance(main_process, dict) and _terminal_stop_is_owned(
                    receipt,
                    inspected,
                    main_process,
                    pid_key=controller_keys(state).pid,
                )
            else:
                owned = (
                    inspected.identity_verified is True
                    and inspected.presence == "present"
                    and inspected.pane_present is True
                    and inspected.pane_pid == receipt.pane_pid
                )
            if not owned:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native pane ownership is unproven",
                )
            self._driver = driver
        else:
            self._driver = None
        self._state = state
        keys = controller_keys(state)
        terminal = TerminalRef(
            _required_string(state.get(keys.terminal), keys.terminal)
        )
        program = keys.pid == "coordinator_pid"
        result = StartResult(
            team_id=spec.team_id,
            run_id=RunRef(_required_string(state.get("run_id"), "run_id")),
            main_terminal_id=None if program else terminal,
            state_path=spec.state_path,
            coordinator_terminal_id=terminal if program else None,
        )
        self.last_start_response = {
            "status": "running" if phase == "running" else "starting",
            "team_id": spec.team_id,
            "workspace": _required_string(state.get("workspace"), "workspace"),
            "run_id": result.run_id._value,
            keys.terminal: terminal._value,
            "state_path": str(spec.state_path),
        }
        return result

    def _assert_inspection_current(
        self, before: Mapping[str, object], current: Mapping[str, object]
    ) -> None:
        old_native = _native(before)
        new_native = _native(current)
        if (
            any(
                before.get(key) != current.get(key)
                for key in ("run_id", "main_terminal", "coordinator_terminal")
            )
            or old_native.get("phase") != new_native.get("phase")
            or old_native.get(controller_keys(before).process)
            != new_native.get(controller_keys(current).process)
            or old_native.get(self._receipt_key) != new_native.get(self._receipt_key)
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native terminal identity or phase changed during inspection",
            )

    def _reload_state(self, path: Path) -> dict[str, object]:
        previous = self._require_state()
        current = _read_state(path)
        for key in (
            "version",
            "runtime",
            "team_id",
            "run_id",
            "main_terminal",
            "coordinator_terminal",
            "task_specs",
            "graph",
            "role_specs",
            "max_review_rounds",
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
        if state.get("runtime") != self.runtime:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native state runtime does not match"
            )
        expected_graph = spec.graph.as_dict() if spec.graph is not None else None
        expected_version = _state_version(spec)
        if (
            state.get("graph") != expected_graph
            or state.get("version") != expected_version
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native graph snapshot does not match requested configuration",
            )
        if _role_specs(state) != expected_role_specs:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native role launch snapshot does not match requested config",
            )
        declared = [task.as_dict() for task in spec.task_specs]
        if (state.get("task_specs", [])) != declared:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native declared TaskSpec snapshot does not match requested config",
            )

    def _status(self) -> StatusReceipt:
        previous = self._require_state()
        path = _state_path(previous)
        state = self._reload_state(path)
        native = _native(state)
        phase = native.get("phase")
        status = phase if phase in NATIVE_PHASES else "unknown"
        inspection: NativeTerminalInspection | None = None
        receipt: ReceiptT | None = None
        if self._receipt_key in native:
            receipt = self._receipt_from_state(native)
            driver = self._driver or self._driver_from_receipt(receipt)
            self._driver = driver
            inspection = driver.inspect(receipt)
            current = self._reload_state(path)
            self._assert_inspection_current(state, current)
            state = current
            native = _native(state)
            if not inspection.identity_verified:
                status = "unknown"
            elif phase == "running" and inspection.running is False:
                process = native.get(controller_keys(state).process)
                if (
                    isinstance(process, Mapping)
                    and process.get("phase") == "exited"
                    and process.get("group_stopped") is True
                ):
                    status = "exited"
                else:
                    status = "unknown"
        team_id = _required_string(state.get("team_id"), "team_id")
        if native.get("codex_inspection_cleanup") is not None:
            status = "unknown"
        questions = [
            question
            for node_id, _value in native_delivery.containers(state)
            if (question := native_questions.outbox(state, node_id)) is not None
        ]
        if any(question["phase"] == "failed" for question in questions):
            status = "unknown"
        run_id = _required_string(state.get("run_id"), "run_id")
        response: dict[str, object] = {
            "status": status,
            "team_id": team_id,
            "run_id": run_id,
            controller_keys(state).terminal: _required_string(
                state.get(controller_keys(state).terminal),
                controller_keys(state).terminal,
            ),
            "native": dict(native),
            "roles": dict(_required_mapping(state.get("roles"), "roles")),
        }
        summaries = [
            {
                "phase": question["phase"],
                "role": question["role"],
                "delivery_id": question["delivery_id"],
                "answered": len(cast(dict[str, str], question["answers"])),
                "total": len(cast(list[str], question["message_ids"])),
                "messages": [
                    {
                        "message_id": message_id,
                        "body": field.body,
                        "answered": message_id
                        in cast(dict[str, str], question["answers"]),
                    }
                    for message_id, field in zip(
                        cast(list[str], question["message_ids"]),
                        validate_question_request(question["request"]).questions,
                        strict=True,
                    )
                ],
            }
            for question in questions
        ]
        if state["version"] == PARALLEL_STATE_VERSION:
            response["questions"] = summaries
        elif summaries:
            response["question"] = summaries[0]
        if inspection is not None:
            response[self.runtime] = _inspection_dict(inspection)
        if state.get("version") in {NAMED_STATE_VERSION, PARALLEL_STATE_VERSION}:
            response["consultations"] = [
                {"task_id": task_id, **pending}
                for task_id, record in _required_mapping(
                    state.get("tasks"), "tasks"
                ).items()
                if isinstance(record, Mapping)
                and (pending := task_consultation(state, record)) is not None
            ]
        self.last_status_response = response
        return StatusReceipt(status, team_id, RunRef(run_id))

    def _attach(
        self, request: Attach | AttachCoordinator
    ) -> AttachReceipt | CoordinatorAttachReceipt:
        if isinstance(request, Attach) and role_kind(request.role) is not Role.MAIN:
            state = self._require_state()
            if role_id(request.role) in _role_specs(state):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    "native ACP roles have no TTY to attach",
                )
            raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role is not selected")
        previous = self._require_state()
        path = _state_path(previous)
        try:
            state = self._reload_state(path)
            keys = controller_keys(state)
            if isinstance(request, AttachCoordinator):
                if keys.pid != "coordinator_pid":
                    raise RuntimeFailure(
                        ErrorCode.INVALID_REQUEST,
                        "this team has no program coordinator",
                    )
            else:
                _require_target(state, request.role)
            native = _native(state)
            receipt = self._receipt_from_state(native)
            driver = self._driver or self._driver_from_receipt(receipt)
            self._driver = driver
            inspected = driver.inspect(receipt)
            current = self._reload_state(path)
            self._assert_inspection_current(state, current)
            state = current
            if (
                not inspected.identity_verified
                or inspected.presence != "present"
                or inspected.pane_pid != receipt.pane_pid
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native Main terminal ownership is unproven",
                )
            argv = driver.attach_argv(receipt)
        except RuntimeFailure:
            raise
        except Exception as exc:
            raise _runtime_error(exc, "native Main attach preparation failed") from exc
        try:
            attached = subprocess.run(list(argv), check=False)
        except OSError as exc:
            raise _runtime_error(exc, "native Main attach failed") from exc
        if attached.returncode != 0:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "native Main attach failed"
            )
        run_id = _required_string(state.get("run_id"), "run_id")
        terminal = _required_string(state.get(keys.terminal), keys.terminal)
        self.last_attach_response = {
            "status": "focused",
            **(
                {"coordinator_terminal": terminal}
                if isinstance(request, AttachCoordinator)
                else {
                    "role": role_id(request.role),
                    **_named_identity(state, role_id(request.role)),
                }
            ),
            "terminal": terminal,
            "argv": list(argv),
        }
        if isinstance(request, AttachCoordinator):
            return CoordinatorAttachReceipt(TerminalRef(terminal), RunRef(run_id))
        return AttachReceipt(request.role, TerminalRef(terminal), RunRef(run_id))

    def _prompt(self, request: RolePrompt | TaskDispatch) -> Assignment:
        if role_kind(request.role) is Role.WORKER and not isinstance(
            request, TaskDispatch
        ):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "Worker requires task_dispatch with a TaskSpec",
            )
        if role_kind(request.role) not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not an ACP role"
            )
        text = request.message if isinstance(request, TaskDispatch) else request.text
        if not isinstance(text, str) or not text.strip():
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "role prompt must be a non-empty string"
            )
        if len(text) > MAX_PROMPT_CHARS:
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
        release_read: int | None = None
        release_write: int | None = None
        try:
            state = self._progress_state(path)
            _require_running(state)
            if state["version"] in {
                NAMED_STATE_VERSION,
                PARALLEL_STATE_VERSION,
            } or isinstance(request.role, NodeRef):
                _require_target(state, request.role)
            if _verification_pending(state):
                raise RuntimeFailure(
                    ErrorCode.BUSY,
                    "verification cleanup must be confirmed before starting a role",
                )
            roles = _required_mapping(state.get("roles"), "roles")
            if state["version"] == PARALLEL_STATE_VERSION:
                if not isinstance(request, TaskDispatch) or not isinstance(
                    request.role, NodeRef
                ):
                    raise RuntimeFailure(
                        ErrorCode.INVALID_REQUEST,
                        "parallel execution requires a named TaskDispatch",
                    )
                graph = GraphSpec.from_dict(state["graph"])
                blocker = admission_blocker(
                    graph,
                    parse_task_specs(state["task_specs"]),
                    cast(Mapping[str, Mapping[str, object]], roles),
                    request.role,
                    request.task,
                )
                if blocker is not None:
                    raise RuntimeFailure(ErrorCode.BUSY, blocker)
            elif roles:
                raise RuntimeFailure(
                    ErrorCode.BUSY, "another native role is already active"
                )
            if state.get(PENDING_DELIVERY_ID) is not None:
                raise RuntimeFailure(
                    ErrorCode.BUSY,
                    "acknowledge the pending Delivery before starting a role",
                )
            task_record = None
            if isinstance(request, TaskDispatch):
                revision = None
                workspace_revision = None
                tasks = state.get("tasks")
                prior_task = (
                    tasks.get(request.task.task_id)
                    if isinstance(tasks, Mapping)
                    else None
                )
                if (
                    role_kind(request.role) is Role.REVIEWER
                    and isinstance(prior_task, Mapping)
                    and prior_task.get("status") == "awaiting_implementation_review"
                ):
                    revision = snapshot_revision(Path(str(state["workspace"])))
                elif role_kind(request.role) is Role.REVIEWER and is_plan_only(
                    state, request.task.task_id
                ):
                    workspace_revision = snapshot_revision(
                        Path(str(state["workspace"]))
                    )
                task_record, text = prepare_dispatch(
                    state,
                    request,
                    revision=revision,
                    workspace_revision=workspace_revision,
                )
                if len(text) > MAX_PROMPT_CHARS:
                    raise RuntimeFailure(
                        ErrorCode.INVALID_REQUEST,
                        "TaskSpec prompt exceeds character limit",
                    )
            specs = _role_specs(state)
            raw_spec = specs.get(role_id(request.role))
            if not isinstance(raw_spec, Mapping):
                raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "role is not selected")
            try:
                expected_spec = native_profile(
                    cast(str, raw_spec.get("provider")), role_kind(request.role).value
                )
            except RuntimeValidationError as exc:
                raise RuntimeFailure(ErrorCode.INVALID_REQUEST, str(exc)) from exc
            if any(
                raw_spec.get(key) != expected_value
                for key, expected_value in expected_spec.items()
            ):
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    "native role does not match its scoped ACP profile",
                )
            try:
                executables: (
                    native_acp_dependencies.NativeAcpExecutables
                    | native_acp_dependencies.CodexAcpExecutables
                )
                if raw_spec["provider"] == "codex":
                    from . import codex_acp

                    executables = native_acp_dependencies.CodexAcpExecutables.from_dict(
                        raw_spec.get("acp_executables")
                    )
                    executables.verify()
                    provider_snapshot = raw_spec.get("provider_snapshot")
                    if not isinstance(provider_snapshot, Mapping):
                        raise RuntimeValidationError(
                            "Codex provider snapshot is missing"
                        )
                    codex_acp.verify_snapshot(
                        provider_snapshot, Path(str(state["workspace"]))
                    )
                else:
                    executables = (
                        native_acp_dependencies.NativeAcpExecutables.from_dict(
                            raw_spec.get("acp_executables")
                        )
                    )
                    executables.verify()
                snapshot = (
                    native_acp_dependencies.codex_adapter_snapshot(executables)
                    if isinstance(
                        executables, native_acp_dependencies.CodexAcpExecutables
                    )
                    else native_acp_dependencies.adapter_snapshot(executables)
                )
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
            if raw_spec["provider"] == "claude" and (
                checked_digest(SCOPED_AGENT) != raw_spec.get("scoped_wrapper_sha256")
                or checked_digest(SCOPED_CLIENT) != raw_spec.get("scoped_client_sha256")
                or checked_digest(SCOPED_POLICY) != raw_spec.get("scoped_policy_sha256")
                or checked_digest(SCOPED_QUESTIONS)
                != raw_spec.get("scoped_question_client_sha256")
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "scoped ACP runtime changed since team start",
                )
            launch_nonce = secrets.token_hex(16)
            # Unix socket paths must fit even when the caller's TMPDIR is deeply nested.
            private_root = Path(
                tempfile.mkdtemp(
                    prefix="agent-team-provider-",
                    dir="/tmp" if raw_spec["provider"] == "claude" else None,
                )
            ).resolve(strict=True)
            codex_fields: dict[str, object] = {}
            if isinstance(executables, native_acp_dependencies.CodexAcpExecutables):
                from . import codex_acp

                # The journal must be durable before inspection can spawn a child.
                inspection_state = self._progress_state(path)
                _native(inspection_state)["codex_inspection_cleanup"] = {
                    "role": role_id(request.role),
                    "provider_private_root": str(private_root),
                    "cleanup_confirmed": False,
                }
                _save_state(
                    path, inspection_state, require_existing=True, reservation_held=True
                )
                inspection_root, private_root = private_root, None
                inspection_confirmed = False
                try:
                    codex_fields = codex_acp.prepare_assignment(
                        private_root=inspection_root,
                        workspace=Path(str(state["workspace"])),
                        state_path=path,
                        task=request.task
                        if isinstance(request, TaskDispatch)
                        else None,
                        permission=str(raw_spec["permission"]),
                        executables=executables,
                        model=str(raw_spec["model"]),
                        effort=str(raw_spec["effort"]),
                        instructions=str(raw_spec["instructions"]),
                        provider_snapshot=cast(
                            Mapping[str, object], raw_spec["provider_snapshot"]
                        ),
                    )
                    inspection_confirmed = True
                except ExecutionError as exc:
                    inspection_confirmed = exc.cleanup_confirmed
                    if inspection_confirmed:
                        raise
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "Codex inspection process cleanup is unconfirmed; "
                        f"private root retained: {inspection_root}",
                    ) from exc
                except RuntimeValidationError:
                    inspection_confirmed = True
                    raise
                finally:
                    if inspection_confirmed:
                        private_root = inspection_root
                        _native(inspection_state).pop("codex_inspection_cleanup")
                        _save_state(
                            path,
                            inspection_state,
                            require_existing=True,
                            reservation_held=True,
                        )
                write_policy = Path(str(codex_fields["write_policy_path"]))
                policy_digest = str(codex_fields["write_policy_sha256"])
                agent_command = codex_acp.agent_command(executables)
            else:
                assert private_root is not None
                write_policy, policy_digest = create_write_policy(
                    private_root,
                    Path(str(state["workspace"])),
                    path,
                    request.task if isinstance(request, TaskDispatch) else None,
                    executables.agent,
                    permission=str(raw_spec["permission"]),
                )
                agent_command = build_acp_agent_command(
                    _required_string(state.get("team_id"), "team_id"),
                    request.role,
                    launch_nonce,
                    executables=executables,
                    write_policy=write_policy,
                    questions=True,
                )
            session_name = build_acp_session_name(request.role, launch_nonce)
            task_id = _new_id()
            dispatch_id = _new_id()
            terminal_handle = _new_id()
            prompt_path = create_prompt_file(
                path.parent, request.role, launch_nonce, text
            )
            snapshot_root = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
            launch_argv = [
                sys.executable,
                "-m",
                "agent_team",
                "_acp-run",
                role_id(request.role),
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
            try:
                runner_argv = list(python_process_argv(launch_argv))
            except (ValueError, RuntimeError) as exc:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native Python runner process argv identity is unproven",
                ) from exc
            try:
                release_read, release_write = os.pipe()
                gate_launch_argv, gate_argv = _runner_gate_argv(
                    launch_argv, release_read
                )
            except OSError as exc:
                raise _runtime_error(
                    exc, "native ACP runner gate setup failed"
                ) from exc
            assignment = {
                **(
                    {"role": request.role.node_id, "role_kind": request.role.kind.value}
                    if isinstance(request.role, NodeRef)
                    else {}
                ),
                "task_id": task_id,
                "dispatch_id": dispatch_id,
                "terminal_handle": terminal_handle,
                "completion_observed": False,
                "launcher_owned_runner": True,
                "launch_nonce": launch_nonce,
                "prompt_path": str(prompt_path),
                "execution": "background",
                "adapter_id": raw_spec["adapter_id"],
                "agent_command": agent_command,
                "session_name": session_name,
                "provider_private_root": str(private_root),
                "snapshot_root": str(snapshot_root),
                "adapter_snapshot": dict(snapshot),
                "runner_argv": runner_argv,
                "runner_startup_argv": list(gate_argv),
                **codex_fields,
            }
            if isinstance(request, TaskDispatch):
                assert task_record is not None
                assignment["task_spec"] = request.task.as_dict()
                task_record["dispatch_id"] = dispatch_id
                assignment["task_stage"] = task_record["stage"]
                assignment["task_revision"] = task_record["revision"]
                if role_kind(request.role) is Role.REVIEWER and is_plan_only(
                    state, request.task.task_id
                ):
                    assignment["task_workspace_revision"] = task_record[
                        "workspace_revision"
                    ]
            if write_policy is not None:
                assignment["write_policy_path"] = str(write_policy)
                assignment["write_policy_sha256"] = policy_digest
            if raw_spec["provider"] == "claude":
                assert private_root is not None
                assignment["question_socket"] = str(private_root / "q.sock")
            roles[role_id(request.role)] = assignment
            try:
                _save_state(path, state, require_existing=True, reservation_held=True)
                assignment_persisted = True
            except RuntimeFailure as exc:
                if isinstance(exc.__cause__, StatePublishError):
                    assignment_persisted = True
                raise
            try:
                spawn_attempted = True
                # The gate keeps the ACP runner from spawning its own provider
                # group until this backend has published runner ownership.
                process = subprocess.Popen(
                    gate_launch_argv,
                    cwd=PACKAGE_ROOT,
                    env=acp_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                    pass_fds=(release_read,),
                )
            except Exception as exc:
                # The assignment remains durable, because the process effect is
                # unknown once a spawn call has been attempted.
                raise _runtime_error(exc, "native ACP runner startup failed") from exc
            finally:
                if release_read is not None:
                    try:
                        os.close(release_read)
                    except OSError:
                        pass
                    release_read = None
            pid = process.pid
            assignment["runner_pid"] = pid
            self._runners[role_id(request.role)] = process
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
            identity_error: RuntimeFailure | None = None
            try:
                _runner_identity_is_owned(assignment)
            except RuntimeFailure as exc:
                identity_error = exc
            _save_state(path, state, require_existing=True, reservation_held=True)
            if identity_error is not None:
                raise identity_error
            if release_write is None:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native ACP runner gate release is unavailable",
                )
            try:
                if os.write(release_write, b"1") != 1:
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "native ACP runner gate release was incomplete",
                    )
            finally:
                try:
                    os.close(release_write)
                except OSError:
                    pass
                release_write = None
            return self._assignment_receipt(state, request.role)
        except RuntimeFailure:
            if not spawn_attempted and not assignment_persisted:
                self._cleanup_unpersisted(
                    prompt_path, private_root, snapshot_root, path, request.role
                )
            elif process is not None:
                self._stop_failed_runner(process, assignment)
            raise
        except Exception as exc:
            if not spawn_attempted and not assignment_persisted:
                self._cleanup_unpersisted(
                    prompt_path, private_root, snapshot_root, path, request.role
                )
            elif process is not None:
                self._stop_failed_runner(process, assignment)
            raise _runtime_error(exc, "native role startup failed") from exc
        finally:
            for fd in (release_read, release_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            reservation.release()

    @staticmethod
    def _stop_failed_runner(
        process: subprocess.Popen[bytes],
        assignment: Mapping[str, object] | None = None,
    ) -> None:
        if assignment is not None and process.poll() is None:
            try:
                pid, pgid = _runner_identity_is_owned(assignment)
            except RuntimeFailure:
                pass
            else:
                if pid == process.pid:
                    _terminate_process_group(
                        pid, pgid, grace_seconds=ACP_CANCEL_GRACE_SECONDS
                    )
                    try:
                        process.wait(timeout=PROCESS_WAIT_SECONDS)
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "native failed runner exit is unconfirmed; assignment retained",
                        ) from exc
                    return
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
        role: RoleTarget,
    ) -> None:
        if prompt_path is not None and (
            prompt_path.exists() or prompt_path.is_symlink()
        ):
            try:
                remove_prompt_file(
                    prompt_path,
                    state_path.parent,
                    role=role,
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
        self, state: Mapping[str, object], role: RoleTarget
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

    @staticmethod
    def _reserve_wait(reservation: _LifecycleReservation, deadline: float) -> bool:
        while True:
            try:
                reservation.acquire()
                if time.monotonic() >= deadline:
                    reservation.release()
                    return False
                return True
            except RuntimeFailure as exc:
                if exc.code is not ErrorCode.TEAM_ALREADY_RUNNING:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(PROCESS_POLL_SECONDS, remaining))

    def _wait(self, request: RoleWait, *, stopping: bool = False) -> WaitReceipt:
        if role_kind(request.role) not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not an ACP role"
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
            delivery = _delivery_state(state, role_id(request.role))
            self._require_delivery_phase(state, stopping=stopping)
            if delivery.get(PENDING_DELIVERY_ID) is not None:
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "acknowledge the pending Delivery before waiting again",
                )
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            run_id = _required_string(state.get("run_id"), "run_id")
            question = native_questions.outbox(state, role_id(request.role))
            if question is not None:
                if question["phase"] == "failed":
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "native question channel failed; stop is required and state is retained",
                    )
                if question["phase"] == "published":
                    reservation = _LifecycleReservation(path, create_parent=False)
                    if not self._reserve_wait(reservation, deadline):
                        self._require_delivery_phase(
                            _read_state(path), stopping=stopping
                        )
                        return WaitReceipt(None, ())
                    try:
                        current = self._delivery_progress_state(path, stopping=stopping)
                        current_delivery_state = _delivery_state(
                            current, role_id(request.role)
                        )
                        self._require_delivery_phase(current, stopping=stopping)
                        current_question = native_questions.outbox(
                            current, role_id(request.role)
                        )
                        if current_question is None or current_question != question:
                            raise RuntimeFailure(
                                ErrorCode.IDENTITY_MISMATCH,
                                "native question changed while waiting",
                            )
                        current_question["phase"] = "observed"
                        delivery_id = cast(str, current_question["delivery_id"])
                        ids = cast(list[str], current_question["message_ids"])
                        current_delivery_state[PENDING_DELIVERY_ID] = delivery_id
                        current_delivery_state[PENDING_DELIVERY_KIND] = "question"
                        current_delivery_state[PENDING_DELIVERY_STAGE] = "observed"
                        current_delivery_state[PENDING_QUESTION_IDS] = list(ids)
                        current_delivery_state[REPLIED_QUESTION_IDS] = []
                        _save_state(
                            path, current, require_existing=True, reservation_held=True
                        )
                        identity = CompletionIdentity(
                            run_id=RunRef(run_id),
                            task_id=TaskRef(task_id),
                            dispatch_id=DispatchRef(dispatch_id),
                            sender_terminal_id=TerminalRef(terminal),
                        )
                        parsed = validate_question_request(current_question["request"])
                        events = tuple(
                            NormalizedEvent.question(
                                identity=identity,
                                message_id=MessageRef(message_id),
                                delivery_id=DeliveryRef(delivery_id),
                                body=field.body,
                            )
                            for message_id, field in zip(
                                ids, parsed.questions, strict=True
                            )
                        )
                        return WaitReceipt(DeliveryRef(delivery_id), events)
                    finally:
                        reservation.release()
            result = delivery.get("native_result")
            if result is not None:
                _delivery_id, _outcome, _cleanup, _body = _native_result_fields(
                    result,
                    role=request.role,
                    run_id=run_id,
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    terminal_handle=terminal,
                    launch_nonce=launch_nonce,
                )
                reservation = _LifecycleReservation(path, create_parent=False)
                if not self._reserve_wait(reservation, deadline):
                    self._require_delivery_phase(_read_state(path), stopping=stopping)
                    return WaitReceipt(None, ())
                try:
                    current = self._delivery_progress_state(path, stopping=stopping)
                    current_delivery_state = _delivery_state(
                        current, role_id(request.role)
                    )
                    self._require_delivery_phase(current, stopping=stopping)
                    if current_delivery_state.get(PENDING_DELIVERY_ID) is not None:
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
                    current_result = current_delivery_state.get("native_result")
                    (
                        current_delivery,
                        current_outcome,
                        _current_cleanup,
                        current_body,
                    ) = _native_result_fields(
                        current_result,
                        role=request.role,
                        run_id=run_id,
                        task_id=task_id,
                        dispatch_id=dispatch_id,
                        terminal_handle=terminal,
                        launch_nonce=launch_nonce,
                    )
                    current_assignment["completion_observed"] = True
                    current_delivery_state[PENDING_DELIVERY_ID] = current_delivery
                    current_delivery_state[PENDING_DELIVERY_KIND] = "worker_done"
                    current_delivery_state[PENDING_DELIVERY_STAGE] = "observed"
                    current_delivery_state[PENDING_QUESTION_IDS] = []
                    current_delivery_state[REPLIED_QUESTION_IDS] = []
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
            runner = self._runners.get(role_id(request.role))
            if (runner is not None and runner.poll() is not None) or (
                runner is None and self._saved_runner_status(assignment) == "exited"
            ):
                # The runner may publish after the lock-free read and before exit.
                refreshed = _read_state(path)
                self._require_delivery_phase(refreshed, stopping=stopping)
                refreshed_result = _delivery_state(
                    refreshed, role_id(request.role)
                ).get("native_result")
                if refreshed_result is not None:
                    _native_result_fields(
                        refreshed_result,
                        role=request.role,
                        run_id=run_id,
                        task_id=task_id,
                        dispatch_id=dispatch_id,
                        terminal_handle=terminal,
                        launch_nonce=launch_nonce,
                    )
                    continue
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native ACP runner exited without publishing completion",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return WaitReceipt(None, ())
            time.sleep(min(PROCESS_POLL_SECONDS, remaining))

    def _read(self, request: RoleRead, *, stopping: bool = False) -> ReadReceipt:
        if role_kind(request.role) not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not an ACP role"
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
            state = self._delivery_progress_state(path, stopping=stopping)
            delivery = _delivery_state(state, role_id(request.role))
            self._require_delivery_phase(state, stopping=stopping)
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            _validate_runner_argv(path, request.role, assignment)
            if (
                assignment.get("completion_observed") is not True
                or delivery.get(PENDING_DELIVERY_KIND) != "worker_done"
                or delivery.get(PENDING_DELIVERY_STAGE) != "observed"
            ):
                raise RuntimeFailure(
                    ErrorCode.COMPLETION_NOT_OBSERVED,
                    "matching native worker_done has not been observed",
                )
            run_id = _required_string(state.get("run_id"), "run_id")
            result = delivery.get("native_result")
            delivery_id, _outcome, _cleanup, body = _native_result_fields(
                result,
                role=request.role,
                run_id=run_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                terminal_handle=terminal,
                launch_nonce=launch_nonce,
            )
            if delivery.get(PENDING_DELIVERY_ID) != delivery_id:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native Delivery identity does not match result",
                )
            lines = body.splitlines()
            output = "\n".join(lines[: request.lines])
            delivery[PENDING_DELIVERY_STAGE] = "read"
            _save_state(path, state, require_existing=True, reservation_held=True)
            return ReadReceipt(output)
        finally:
            reservation.release()

    def _release(
        self, request: RoleRelease, *, stopping: bool = False
    ) -> ReleaseReceipt:
        if role_kind(request.role) not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not an ACP role"
            )
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._delivery_progress_state(path, stopping=stopping)
            delivery = _delivery_state(state, role_id(request.role))
            self._require_delivery_phase(state, stopping=stopping)
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            _validate_runner_argv(path, request.role, assignment)
            if (
                assignment.get("completion_observed") is not True
                or delivery.get(PENDING_DELIVERY_KIND) != "worker_done"
                or delivery.get(PENDING_DELIVERY_STAGE) != "read"
            ):
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "native role release requires read after worker_done",
                )
            run_id = _required_string(state.get("run_id"), "run_id")
            result = delivery.get("native_result")
            _delivery, _outcome, cleanup_confirmed, _body = _native_result_fields(
                result,
                role=request.role,
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

        self._wait_runner_exit(assignment, request.role)

        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._delivery_progress_state(path, stopping=stopping)
            delivery = _delivery_state(state, role_id(request.role))
            self._require_delivery_phase(state, stopping=stopping)
            assignment = _assignment(state, request.role)
            task_id, dispatch_id, terminal, launch_nonce = _assignment_identity(
                assignment, role=request.role, state=state
            )
            if delivery.get(PENDING_DELIVERY_STAGE) != "read":
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "native release order changed during cleanup",
                )
            run_id = _required_string(state.get("run_id"), "run_id")
            _delivery, _outcome, cleanup_confirmed, _body = _native_result_fields(
                delivery.get("native_result"),
                role=request.role,
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
            if state["version"] != PARALLEL_STATE_VERSION:
                del roles[role_id(request.role)]
            delivery[PENDING_DELIVERY_STAGE] = "released"
            _save_state(path, state, require_existing=True, reservation_held=True)
            self._runners.pop(role_id(request.role), None)
            self._state = state
            return ReleaseReceipt("released")
        finally:
            reservation.release()

    def _wait_runner_exit(
        self, assignment: Mapping[str, object], role: RoleTarget
    ) -> None:
        process = self._runners.get(role_id(role))
        if process is not None and process.pid != assignment.get("runner_pid"):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native ACP runner PID changed"
            )
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

    def _cleanup_assignment(
        self, assignment: Mapping[str, object], path: Path, role: RoleTarget
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
                remove_prompt_file(prompt, path.parent, role=role, launch_nonce=nonce)
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

    def _reply(self, request: MessageReply) -> ReplyReceipt:
        path = _state_path(self._require_state())
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_state(path)
            _require_running(state)
            node_id, delivery = _delivery_for_id(
                state, request.message_id._value, message=True
            )
            question = native_questions.outbox(state, node_id)
            if question is None or question["phase"] != "observed":
                raise RuntimeFailure(
                    ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                    "message does not match an observed native question",
                )
            message_id = request.message_id._value
            ids = cast(list[str], question["message_ids"])
            if message_id not in ids:
                raise RuntimeFailure(
                    ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                    "message does not match an observed native question",
                )
            try:
                body = native_questions.text(request.body, "answer")
            except ValueError as exc:
                raise RuntimeFailure(ErrorCode.INVALID_REQUEST, str(exc)) from exc
            answers = cast(dict[str, str], question["answers"])
            if message_id in answers:
                if answers[message_id] != body:
                    raise RuntimeFailure(
                        ErrorCode.IDENTITY_MISMATCH,
                        "native question already has a different saved answer",
                    )
                return ReplyReceipt(True)
            answers[message_id] = body
            delivery[REPLIED_QUESTION_IDS] = [item for item in ids if item in answers]
            _save_state(path, state, require_existing=True, reservation_held=True)
            return ReplyReceipt(True)
        finally:
            reservation.release()

    def _ack(self, request: DeliveryAck, *, stopping: bool = False) -> AckReceipt:
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._delivery_progress_state(path, stopping=stopping)
            self._require_delivery_phase(state, stopping=stopping)
            node_id, delivery = _delivery_for_id(state, request.delivery_id._value)
            pending = delivery.get(PENDING_DELIVERY_ID)
            if pending != request.delivery_id._value:
                raise RuntimeFailure(
                    ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                    "Delivery does not match native pending observation",
                )
            if delivery.get(PENDING_DELIVERY_KIND) == "question":
                question = native_questions.outbox(state, node_id)
                if (
                    question is None
                    or question["phase"] != "observed"
                    or set(cast(dict[str, str], question["answers"]))
                    != set(cast(list[str], question["message_ids"]))
                ):
                    raise RuntimeFailure(
                        ErrorCode.ORDER_VIOLATION,
                        "reply to every native question before acknowledging its Delivery",
                    )
                question["phase"] = "acknowledged"
                _native(state)["last_ack"] = request.delivery_id._value
                _clear_pending(delivery)
                _save_state(path, state, require_existing=True, reservation_held=True)
                return AckReceipt(True)
            if (
                delivery.get(PENDING_DELIVERY_KIND) != "worker_done"
                or delivery.get(PENDING_DELIVERY_STAGE) != "released"
            ):
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "release the completed native role before acknowledging its Delivery",
                )
            if state["version"] == PARALLEL_STATE_VERSION:
                assert node_id is not None
                role = resolve_state_role(state, node_id)
                assignment = _assignment(state, role)
                _assignment_identity(assignment, role=role, state=state)
                _validate_runner_argv(path, role, assignment)
                # Released resources no longer belong to a possibly recycled PID.
                _runner_identity_is_owned(assignment, verify_process=False)
            roles = _required_mapping(state.get("roles"), "roles")
            if roles and state["version"] != PARALLEL_STATE_VERSION:
                raise RuntimeFailure(
                    ErrorCode.BUSY, "native role cleanup is still pending"
                )
            _native(state)["last_ack"] = request.delivery_id._value
            result = delivery.get("native_result")
            if isinstance(result, Mapping) and "logical_task_id" in result:
                acknowledge_task(state, result)
            _clear_pending(delivery)
            delivery.pop("native_result", None)
            if state["version"] == PARALLEL_STATE_VERSION:
                assert node_id is not None
                del roles[node_id]
            _save_state(path, state, require_existing=True, reservation_held=True)
            self._state = state
            return AckReceipt(True)
        finally:
            reservation.release()

    def _role_get(self, request: RoleGet) -> RoleStatusReceipt:
        if role_kind(request.role) not in ACP_ROLES:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "native role is not an ACP role"
            )
        previous = self._require_state()
        path = _state_path(previous)
        reservation = _LifecycleReservation(path, create_parent=False)
        reservation.acquire()
        try:
            state = self._reload_state(path)
            delivery = _delivery_state(state, role_id(request.role))
            assignment = _assignment(state, request.role)
            status = (
                "completed" if delivery.get("native_result") is not None else "running"
            )
            runner = self._runners.get(role_id(request.role))
            if (
                runner is not None
                and runner.poll() is not None
                and delivery.get("native_result") is None
            ):
                status = "exited"
            elif runner is None and delivery.get("native_result") is None:
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
        self,
        path: Path,
        inspected_state: Mapping[str, object],
        inspected: NativeTerminalInspection,
    ) -> tuple[dict[str, object], ReceiptT, dict[str, object], tuple[RoleTarget, ...]]:
        state = self._reload_state(path)
        self._assert_inspection_current(inspected_state, state)
        _require_codex_inspection_cleanup(state)
        if _verification_pending(state):
            raise RuntimeFailure(
                ErrorCode.BUSY,
                "verification process cleanup is unconfirmed; task state is retained",
            )
        questions = [
            question
            for node_id, _value in native_delivery.containers(state)
            if (question := native_questions.outbox(state, node_id)) is not None
        ]
        if state["version"] != PARALLEL_STATE_VERSION:
            pending = state.get(PENDING_DELIVERY_ID)
            if pending is not None and (
                state.get(PENDING_DELIVERY_KIND) != "question" or not questions
            ):
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
        receipt = self._receipt_from_state(native)
        process = native.get(controller_keys(state).process)
        if not isinstance(process, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native Main process receipt is unknown",
            )
        if not _terminal_stop_is_owned(
            receipt, inspected, process, pid_key=controller_keys(state).pid
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "native terminal ownership is unproven"
            )
        if process.get("phase") == "running" and inspected.running is not True:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native supervisor liveness is unconfirmed",
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
        for question in questions:
            if (
                state["version"] == PARALLEL_STATE_VERSION
                and question["phase"] == "failed"
            ):
                continue
            question["phase"] = "cancelling"
            question["error"] = None
        _save_state(path, state, require_existing=True, reservation_held=True)
        roles = _required_mapping(state.get("roles"), "roles")
        active_roles: list[RoleTarget] = []
        if len(roles) > 1 and state["version"] != PARALLEL_STATE_VERSION:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "native role ownership is ambiguous"
            )
        for raw_role in roles:
            target = resolve_state_role(state, raw_role)
            if role_kind(target) not in ACP_ROLES:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native role ownership is invalid",
                )
            active_roles.append(target)
        return state, receipt, process, tuple(active_roles)

    def _wait_for_main_process_receipt(self, path: Path) -> None:
        """Wait outside the lifecycle reservation for native_main to publish."""

        deadline = time.monotonic() + PROCESS_WAIT_SECONDS
        while True:
            state = _read_state(path)
            native = _native(state)
            if self._receipt_key not in native:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "native Main startup is unresolved; state and socket path retained",
                )
            if isinstance(native.get(controller_keys(state).process), dict):
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

    def _drain_stopping_assignment(
        self, path: Path, role: RoleTarget
    ) -> dict[str, object]:
        state = self._delivery_progress_state(path, stopping=True)
        assignment = _assignment(state, role)
        identity = _assignment_identity(assignment, role=role, state=state)
        container = _delivery_state(state, role_id(role))
        _validate_runner_argv(path, role, assignment)
        _runner_identity_is_owned(
            assignment,
            verify_process=container.get(PENDING_DELIVERY_STAGE) != "released",
        )
        cancelled = container.get("native_result") is None
        if cancelled:
            self._cancel_runner(state, role, retain_delivery=True)
            state = self._delivery_progress_state(path, stopping=True)
            container = _delivery_state(state, role_id(role))
        result = _required_mapping(container.get("native_result"), "stopping result")
        if native_questions.outbox(state, role_id(role)) is not None:
            if (
                result.get("outcome") != "failed"
                or result.get("cleanup_confirmed") is not True
            ):
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "cancelled question cleanup is unconfirmed; assignment retained",
                )
            _validate_runner_argv(path, role, assignment)
            self._wait_runner_exit(assignment, role)
            _pid, pgid = _runner_identity_is_owned(assignment, verify_process=False)
            if _process_group_alive(pgid):
                raise RuntimeFailure(
                    ErrorCode.BUSY, "cancelled question runner group remains live"
                )
            reservation = _LifecycleReservation(path, create_parent=False)
            reservation.acquire()
            try:
                current = self._delivery_progress_state(path, stopping=True)
                current_assignment = _assignment(current, role)
                current_delivery = _delivery_state(current, role_id(role))
                if (
                    _assignment_identity(current_assignment, role=role, state=current)
                    != identity
                    or current_delivery.get("native_result") != result
                ):
                    raise RuntimeFailure(
                        ErrorCode.IDENTITY_MISMATCH,
                        "cancelled question assignment changed before cleanup",
                    )
                current_delivery.pop("native_question", None)
                if current_delivery.get(PENDING_DELIVERY_KIND) == "question":
                    _clear_pending(current_delivery)
                _save_state(path, current, require_existing=True, reservation_held=True)
            finally:
                reservation.release()
        state = self._delivery_progress_state(path, stopping=True)
        delivery = _delivery_state(state, role_id(role))
        if delivery.get(PENDING_DELIVERY_ID) is None:
            self._wait(RoleWait(role, 1000), stopping=True)
            state = self._delivery_progress_state(path, stopping=True)
            delivery = _delivery_state(state, role_id(role))
        if delivery.get(PENDING_DELIVERY_STAGE) == "observed":
            self._read(RoleRead(role, MAX_RESULT_BODY_CHARS), stopping=True)
            state = self._delivery_progress_state(path, stopping=True)
            delivery = _delivery_state(state, role_id(role))
        if delivery.get(PENDING_DELIVERY_STAGE) == "read":
            self._release(RoleRelease(role), stopping=True)
            state = self._delivery_progress_state(path, stopping=True)
            delivery = _delivery_state(state, role_id(role))
        delivery_id = _required_string(
            delivery.get(PENDING_DELIVERY_ID), "stopping delivery identity"
        )
        self._ack(DeliveryAck(DeliveryRef(delivery_id)), stopping=True)
        return {
            "role": role_id(role),
            "task_id": identity[0],
            "dispatch_id": identity[1],
            "delivery_id": delivery_id,
            "outcome": result["outcome"],
            "cancelled": cancelled,
        }

    def _cancel_runner(
        self,
        state: dict[str, object],
        role: RoleTarget,
        *,
        retain_delivery: bool = False,
    ) -> None:
        path = _state_path(state)
        if retain_delivery:
            self._require_delivery_phase(state, stopping=True)
        assignment = _assignment(state, role)
        _validate_runner_argv(path, role, assignment)
        process = self._runners.get(role_id(role))
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
            _runner_identity_is_owned(assignment)
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
        result = _delivery_state(current, role_id(role)).get("native_result")
        if result is None:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native ACP cancellation cleanup is unknown; assignment retained",
            )
        run_id = _required_string(current.get("run_id"), "run_id")
        _delivery, _outcome, cleanup_confirmed, _body = _native_result_fields(
            result,
            role=role,
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
        if retain_delivery:
            return
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
            roles.pop(role_id(role), None)
            current.pop("native_result", None)
            current.pop("native_question", None)
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
        receipt: ReceiptT,
        process: Mapping[str, object],
    ) -> None:
        try:
            validate_native_controller_process(
                process, pid_key=controller_keys(state).pid
            )
        except RuntimeValidationError as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "native Main process receipt is invalid; state retained",
            ) from exc
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
        expected_argv = _validated_supervisor_argv(state)
        if read_process_argv(pid) != expected_argv:
            if self._supervisor_cleanup_confirmed(state, process):
                return
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "native supervisor process identity is unproven; state retained",
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
        saved = _native(current).get(controller_keys(current).process)
        if not isinstance(saved, Mapping):
            return False
        if any(
            saved.get(key) != process.get(key)
            for key in (
                "supervisor_pid",
                controller_keys(state).pid,
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
                "native runtime requires Linux or macOS process identity support",
            )


__all__ = ("NativeBackend", "publish_completion")
