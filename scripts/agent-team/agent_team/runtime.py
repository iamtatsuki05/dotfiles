"""Small, shared safety helpers for the agent-team launch paths."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from .acp_dependencies import AcpDependencyError, AcpExecutables
from .contracts import ErrorCode, RuntimeFailure
from .locking import _LifecycleReservation
from .native_acp_dependencies import NativeAcpDependencyError, NativeAcpExecutables
from .native_controller import ControllerKeys, controller_keys
from .native_terminal import NATIVE_RUNTIMES, is_native_runtime
from .orca_controller import (
    _validate_controller_state,
    controller_key,
    is_program,
)
from .task_execution import (
    parse_review,
    validate_saved_tasks,
    validate_task_assignment,
)
from .task_spec import TaskSpec, parse_task_specs

if TYPE_CHECKING:
    from .contracts import NodeRef, RoleTarget
    from .named_graph import GraphSpec

ACP_ROLES: Final = frozenset({"planner", "worker", "reviewer"})
ACP_PACKAGE: Final = "acpx@0.13.2"
CLAUDE_ACP_PACKAGE: Final = "@agentclientprotocol/claude-agent-acp@0.70.0"
STATE_VERSION: Final = 3
NAMED_STATE_VERSION: Final = 4
PARALLEL_STATE_VERSION: Final = 5
MAX_STATE_BYTES: Final = 2_000_000
MAX_PROMPT_CHARS: Final = 100_000
MAX_PROMPT_BYTES: Final = 400_000
MAX_RESULT_BODY_CHARS: Final = 100_000
MAX_RUNTIME_FAILURE_CHARS: Final = 240
ACP_ENV_KEYS: Final = frozenset(
    {"PATH", "HOME", "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG"}
)

_ORCA_DELIVERY_BATCH_FIELDS: Final = frozenset(
    {
        "delivery_id",
        "phase",
        "members",
        "message_count",
        "messages_sha256",
        "error",
    }
)
_ORCA_DELIVERY_BATCH_MEMBER_FIELDS: Final = frozenset(
    {
        "role",
        "role_kind",
        "message_id",
        "kind",
        "task_id",
        "dispatch_id",
        "terminal_handle",
        "launch_nonce",
    }
)
_ORCA_DELIVERY_BATCH_KINDS: Final = frozenset({"worker_done", "question", "escalation"})
_ORCA_DELIVERY_BATCH_PHASES: Final = frozenset({"observed", "invalid", "acknowledging"})
_ORCA_DELIVERY_BATCH_ID_LIMIT: Final = 256
_ORCA_DELIVERY_BATCH_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_ORCA_DELIVERY_ASSIGNMENT_FIELDS: Final = frozenset(
    {
        "pending_orca_effect",
        "pending_delivery_id",
        "pending_delivery_kind",
        "pending_delivery_stage",
        "pending_question_ids",
        "replied_question_ids",
    }
)

_TEAM_ID_RE: Final = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_LAUNCH_NONCE_RE: Final = re.compile(r"[a-z0-9]{8,64}\Z")
_STATE_REQUIRED_KEYS: Final = (
    "version",
    "runtime",
    "team_id",
    "workspace",
    "config_path",
    "state_path",
    "launcher_path",
    "worktree_id",
    "orca_socket",
    "run_id",
    "main_terminal",
    "role_specs",
    "roles",
)


class RuntimeValidationError(ValueError):
    """Raised when a shared runtime artifact fails its safety contract."""


class StatePublishError(RuntimeValidationError):
    """State replacement succeeded but directory durability is unconfirmed."""


def _role_target_parts(role: str | RoleTarget) -> tuple[str, str | None]:
    """Return the path identity and fixed kind for an ACP role target.

    String callers intentionally retain the version-3 fixed-role contract.  A
    named node must be represented by ``NodeRef`` so an arbitrary string cannot
    silently become a new role identity.
    """

    from .contracts import NodeRef, Role

    if isinstance(role, NodeRef):
        role_id = role.node_id
        kind = role.kind.value
        if kind not in ACP_ROLES:
            raise RuntimeValidationError("ACP role target kind is invalid")
        return role_id, kind
    if isinstance(role, Role):
        role_id = role.value
    elif isinstance(role, str):
        role_id = role
    else:
        raise RuntimeValidationError("ACP role target is invalid")
    if role_id not in ACP_ROLES:
        raise RuntimeValidationError("ACP role target is invalid")
    return role_id, None


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def _canonical(path: Path) -> Path:
    return _absolute(path).resolve(strict=False)


def _require_role_nonce(role: str | RoleTarget, launch_nonce: str, context: str) -> str:
    try:
        role_id, _kind = _role_target_parts(role)
    except RuntimeValidationError as exc:
        raise RuntimeValidationError(f"{context} is invalid") from exc
    if not _LAUNCH_NONCE_RE.fullmatch(launch_nonce):
        raise RuntimeValidationError(f"{context} is invalid")
    return role_id


def _require_identity(team_id: str, role: str | RoleTarget, launch_nonce: str) -> str:
    if not _TEAM_ID_RE.fullmatch(team_id):
        raise RuntimeValidationError("ACP launch identity is invalid")
    return _require_role_nonce(role, launch_nonce, "ACP launch identity")


def _validated_acp_executables(executables: AcpExecutables) -> AcpExecutables:
    if not isinstance(executables, AcpExecutables):
        raise RuntimeValidationError("resolved ACP executables are required")
    paths = (executables.node, executables.client, executables.agent)
    if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
        raise RuntimeValidationError("resolved ACP executable paths must be absolute")
    try:
        executables.verify()
    except AcpDependencyError as exc:
        raise RuntimeValidationError(str(exc)) from exc
    return executables


def build_acp_agent_command(
    team_id: str,
    role: str | RoleTarget,
    launch_nonce: str,
    *,
    executables: AcpExecutables | NativeAcpExecutables,
    write_policy: Path | None = None,
    questions: bool = False,
) -> str:
    role_id = _require_identity(team_id, role, launch_nonce)
    if type(questions) is not bool or (
        questions and not isinstance(executables, NativeAcpExecutables)
    ):
        raise RuntimeValidationError(
            "native questions require selected native Claude ACP"
        )
    if isinstance(executables, NativeAcpExecutables):
        if write_policy is None:
            raise RuntimeValidationError("native ACP requires a scoped policy")
        try:
            executables.verify()
        except NativeAcpDependencyError as exc:
            raise RuntimeValidationError(str(exc)) from exc
    else:
        executables = _validated_acp_executables(executables)
    marker = f"agent-team/{team_id}/{role_id}/{launch_nonce}"
    argv = [
        "env",
        f"AGENT_TEAM_ACP_MARKER={marker}",
        str(executables.node),
        str(executables.agent),
    ]
    if write_policy is not None:
        from .scoped_acp import SCOPED_AGENT

        if not write_policy.is_absolute():
            raise RuntimeValidationError(
                "scoped ACP policy requires an absolute policy path"
            )
        argv[3:] = [
            str(SCOPED_AGENT),
            "--agent-entry",
            str(executables.agent),
            "--policy",
            str(write_policy),
            *(["--questions"] if questions else []),
        ]
    return shlex.join(argv)


def build_acp_session_name(role: str | RoleTarget, launch_nonce: str) -> str:
    role_id = _require_role_nonce(role, launch_nonce, "ACP session identity")
    return f"agent-team-{role_id}-{launch_nonce}"


def _state_dir_stat(state_dir: Path) -> os.stat_result:
    state_dir = _absolute(state_dir)
    try:
        directory_stat = state_dir.lstat()
    except OSError as exc:
        raise RuntimeValidationError(
            f"state directory is unavailable: {state_dir}"
        ) from exc
    if stat.S_ISLNK(directory_stat.st_mode):
        raise RuntimeValidationError(
            f"state directory must not be a symlink: {state_dir}"
        )
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise RuntimeValidationError(
            f"state directory must be a directory: {state_dir}"
        )
    if directory_stat.st_uid != os.getuid():
        raise RuntimeValidationError(
            f"state directory owner is not the current user: {state_dir}"
        )
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise RuntimeValidationError(
            f"state directory must have mode 0700: {state_dir}"
        )
    return directory_stat


def _prompt_path(state_dir: Path, role: str | RoleTarget, launch_nonce: str) -> Path:
    role_id = _require_role_nonce(role, launch_nonce, "prompt identity")
    return _absolute(state_dir) / f"prompt-{role_id}-{launch_nonce}.md"


def _validate_prompt_stat(
    path: Path, file_stat: os.stat_result, *, context: str = "ACP prompt"
) -> None:
    if stat.S_ISLNK(file_stat.st_mode):
        raise RuntimeValidationError(f"{context} must not be a symlink: {path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeValidationError(f"{context} must be a regular file: {path}")
    if file_stat.st_uid != os.getuid():
        raise RuntimeValidationError(f"{context} owner is not the current user: {path}")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise RuntimeValidationError(f"{context} must have mode 0600: {path}")


def validate_prompt_file(
    path: Path,
    state_dir: Path,
    *,
    role: str | RoleTarget | None = None,
    launch_nonce: str | None = None,
) -> Path:
    state_dir = _absolute(state_dir)
    _state_dir_stat(state_dir)
    state_dir = _canonical(state_dir)
    raw_candidate = _absolute(path)
    try:
        raw_stat = raw_candidate.lstat()
    except OSError as exc:
        raise RuntimeValidationError(
            f"ACP prompt is unavailable: {raw_candidate}"
        ) from exc
    if stat.S_ISLNK(raw_stat.st_mode):
        raise RuntimeValidationError(
            f"ACP prompt must not be a symlink: {raw_candidate}"
        )
    candidate = _canonical(raw_candidate)
    try:
        candidate.relative_to(state_dir)
    except ValueError as exc:
        raise RuntimeValidationError(
            "ACP prompt must stay directly in the state directory"
        ) from exc
    if candidate.parent != state_dir:
        raise RuntimeValidationError(
            "ACP prompt must stay directly in the state directory"
        )
    if (role is None) != (launch_nonce is None):
        raise RuntimeValidationError("prompt identity must be complete")
    if role is not None and launch_nonce is not None:
        expected = _prompt_path(state_dir, role, launch_nonce)
        if candidate != expected:
            raise RuntimeValidationError(
                "ACP prompt path does not match its launch identity"
            )
    try:
        file_stat = candidate.lstat()
    except OSError as exc:
        raise RuntimeValidationError(f"ACP prompt is unavailable: {candidate}") from exc
    _validate_prompt_stat(candidate, file_stat)
    return candidate


def create_prompt_file(
    state_dir: Path, role: str | RoleTarget, launch_nonce: str, text: str
) -> Path:
    if not text:
        raise RuntimeValidationError("prompt must not be empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise RuntimeValidationError("prompt exceeds character limit")
    state_dir = _absolute(state_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise RuntimeValidationError(
            f"could not create ACP state directory: {state_dir}"
        ) from exc
    _state_dir_stat(state_dir)
    state_dir = _canonical(state_dir)
    path = _prompt_path(state_dir, role, launch_nonce)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_PROMPT_BYTES:
        raise RuntimeValidationError("prompt exceeds byte limit")
    fd: int | None = None
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as prompt_file:
            fd = None
            prompt_file.write(encoded)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise RuntimeValidationError(
            f"could not create ACP prompt file: {path}"
        ) from exc
    validate_prompt_file(path, state_dir, role=role, launch_nonce=launch_nonce)
    return path


def read_prompt_file(
    path: Path,
    state_dir: Path,
    *,
    role: str | RoleTarget,
    launch_nonce: str,
) -> str:
    state_dir = _absolute(state_dir)
    candidate = validate_prompt_file(
        path, state_dir, role=role, launch_nonce=launch_nonce
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(candidate, flags)
        file_stat = os.fstat(fd)
        _validate_prompt_stat(candidate, file_stat)
        data = bytearray()
        while len(data) <= MAX_PROMPT_BYTES:
            chunk = os.read(fd, min(65_536, MAX_PROMPT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_PROMPT_BYTES:
            raise RuntimeValidationError("ACP prompt exceeds byte limit")
    except OSError as exc:
        raise RuntimeValidationError(
            f"could not read ACP prompt file: {candidate}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeValidationError("ACP prompt is not valid UTF-8") from exc
    if len(text) > MAX_PROMPT_CHARS:
        raise RuntimeValidationError("ACP prompt exceeds character limit")
    return text


def remove_prompt_file(
    path: Path,
    state_dir: Path,
    *,
    role: str | RoleTarget,
    launch_nonce: str,
) -> None:
    candidate = validate_prompt_file(
        path, state_dir, role=role, launch_nonce=launch_nonce
    )
    try:
        candidate.unlink()
    except OSError as exc:
        raise RuntimeValidationError(
            f"could not remove ACP prompt file: {candidate}"
        ) from exc


def build_acp_runner_command(
    state: dict[str, object],
    role: str | RoleTarget,
    *,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    prompt_path: Path,
    launch_nonce: str,
) -> str:
    def state_string(key: str) -> str:
        value = state.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(f"agent-team state is missing {key}")
        return value

    launcher_path = state_string("launcher_path")
    state_path_value = state_string("state_path")
    role_id = _require_role_nonce(role, launch_nonce, "ACP runner identity")
    return shlex.join(
        [
            launcher_path,
            "_acp-run",
            role_id,
            "--state",
            state_path_value,
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
    )


def build_background_runner_command(
    state: dict[str, object],
    role: str | RoleTarget,
    *,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    prompt_path: Path,
    launch_nonce: str,
) -> str:
    """Build the trusted command for a non-TUI background role."""

    def state_string(key: str) -> str:
        value = state.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(f"agent-team state is missing {key}")
        return value

    role_id = _require_role_nonce(role, launch_nonce, "background runner identity")
    return shlex.join(
        [
            state_string("launcher_path"),
            "_background-run",
            role_id,
            "--state",
            state_string("state_path"),
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
    )


def build_acp_argv(
    *,
    workspace: Path,
    agent_command: str,
    executables: AcpExecutables,
    model: str,
    instructions: str,
    operation: tuple[str, ...],
    timeout_seconds: int,
    write_policy: Path | None = None,
) -> list[str]:
    if not operation or operation[0] not in {"sessions", "set", "prompt"}:
        raise RuntimeValidationError("ACP operation is not supported")
    executables = _validated_acp_executables(executables)
    if not isinstance(agent_command, str) or not agent_command:
        raise RuntimeValidationError("ACP agent command is invalid")
    try:
        agent_tokens = shlex.split(agent_command)
    except ValueError as exc:
        raise RuntimeValidationError("ACP agent command is invalid") from exc
    expected_agent_tokens = [str(executables.agent)]
    if write_policy is not None:
        from .scoped_acp import SCOPED_AGENT

        expected_agent_tokens = [
            str(SCOPED_AGENT),
            "--agent-entry",
            str(executables.agent),
            "--policy",
            str(write_policy),
        ]
    if (
        len(agent_tokens) != 3 + len(expected_agent_tokens)
        or agent_tokens[0] != "env"
        or not agent_tokens[1].startswith("AGENT_TEAM_ACP_MARKER=")
        or agent_tokens[1] == "AGENT_TEAM_ACP_MARKER="
        or agent_tokens[2] != str(executables.node)
        or agent_tokens[3:] != expected_agent_tokens
    ):
        raise RuntimeValidationError(
            "ACP agent command does not match resolved executable bindings"
        )
    return [
        str(executables.node),
        str(executables.client),
        "--agent",
        agent_command,
        "--cwd",
        str(workspace),
        "--auth-policy",
        "fail",
        "--approve-reads",
        "--non-interactive-permissions",
        "fail",
        "--no-fs",
        "--no-terminal",
        "--format",
        "quiet",
        "--model",
        model,
        "--append-system-prompt",
        instructions,
        "--allowed-tools",
        "Read,Grep,Glob,Write,Edit" if write_policy is not None else "Read,Grep,Glob",
        "--timeout",
        str(timeout_seconds),
        "--ttl",
        "0",
        *operation,
    ]


def acp_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {
        key: value
        for key, value in source.items()
        if key in ACP_ENV_KEYS or key.startswith("LC_")
    }


def _read_bounded_fd(fd: int, maximum: int) -> bytes:
    data = bytearray()
    while len(data) <= maximum:
        chunk = os.read(fd, min(65_536, maximum + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > maximum:
        raise RuntimeValidationError("agent-team state exceeds size limit")
    return bytes(data)


def validate_native_controller_process(value: object, *, pid_key: str) -> None:
    """Validate one native supervisor child receipt for its exact controller.

    The receipt shape is shared by agent and program modes, but the child PID
    field is deliberately mode-specific so a Main receipt cannot be reused as
    a program coordinator receipt (or vice versa).
    """

    if not isinstance(pid_key, str) or pid_key not in {
        "agent_pid",
        "coordinator_pid",
    }:
        raise RuntimeValidationError("native controller process PID key is invalid")
    if not isinstance(value, Mapping):
        raise RuntimeValidationError(
            "native controller process receipt must be an object"
        )
    fields = {
        "supervisor_pid",
        pid_key,
        "process_group_id",
        "launch_nonce",
        "phase",
    }
    phase = value.get("phase")
    if phase == "exited":
        fields.update({"returncode", "group_stopped"})
    elif phase != "running":
        raise RuntimeValidationError("native controller process phase is invalid")
    if set(value) != fields:
        raise RuntimeValidationError(
            "native controller process receipt fields are invalid"
        )
    for key in ("supervisor_pid", pid_key):
        pid = value[key]
        if type(pid) is not int or pid <= 1:
            raise RuntimeValidationError(f"native controller process {key} is invalid")
    if value["supervisor_pid"] == value[pid_key]:
        raise RuntimeValidationError(
            "native controller process must be a separate child"
        )
    nonce = value["launch_nonce"]
    if not isinstance(nonce, str) or not _LAUNCH_NONCE_RE.fullmatch(nonce):
        raise RuntimeValidationError(
            "native controller process launch nonce is invalid"
        )
    group = value["process_group_id"]
    if group is not None and (type(group) is not int or group != value[pid_key]):
        raise RuntimeValidationError(
            "native controller process group identity is invalid"
        )
    if phase == "running":
        if group is None:
            raise RuntimeValidationError(
                "running native controller process group is unproven"
            )
    elif (
        type(value["returncode"]) is not int
        or type(value["group_stopped"]) is not bool
        or (value["group_stopped"] and group is None)
    ):
        raise RuntimeValidationError(
            "native controller process exit evidence is invalid"
        )


def validate_native_main_process(value: object) -> None:
    """Preserve the public agent/Main receipt validator contract."""

    try:
        validate_native_controller_process(value, pid_key="agent_pid")
    except RuntimeValidationError as exc:
        # Existing callers and tests use the Main-specific diagnostic.
        message = str(exc).replace("native controller", "native Main")
        raise RuntimeValidationError(message) from exc


def _validate_native_controller_state(
    state: Mapping[str, object], keys: ControllerKeys
) -> None:
    """Validate lifecycle metadata after mode-specific key selection."""

    if keys.terminal == "main_terminal":
        if "coordinator_terminal" in state:
            raise RuntimeValidationError(
                "agent state must not contain coordinator_terminal"
            )
    else:
        if "main_terminal" in state:
            raise RuntimeValidationError("program state must not contain main_terminal")

    terminal = state.get(keys.terminal)
    if not isinstance(terminal, str) or not terminal:
        raise RuntimeValidationError(f"native state is missing {keys.terminal}")
    native = state.get("native")
    if not isinstance(native, dict):
        raise RuntimeValidationError("native state metadata is invalid")
    opposite_argv = (
        "main_argv" if keys.argv == "coordinator_argv" else "coordinator_argv"
    )
    opposite_process = (
        "main_process"
        if keys.process == "coordinator_process"
        else "coordinator_process"
    )
    if opposite_argv in native or opposite_process in native:
        raise RuntimeValidationError(
            "native state contains lifecycle keys for another coordination mode"
        )
    if not isinstance(native.get("phase"), str) or native.get("phase") not in {
        "starting",
        "running",
        "stopping",
        "stopped",
    }:
        raise RuntimeValidationError("native state has an invalid lifecycle phase")
    nonce = native.get("run_nonce")
    if not isinstance(nonce, str) or not _LAUNCH_NONCE_RE.fullmatch(nonce):
        raise RuntimeValidationError("native state has an invalid run nonce")
    argv = native.get(keys.argv)
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        or not Path(argv[0]).is_absolute()
    ):
        raise RuntimeValidationError(f"native state has an invalid {keys.argv} command")
    if keys.process in native:
        if keys.pid == "agent_pid":
            validate_native_main_process(native[keys.process])
        else:
            validate_native_controller_process(native[keys.process], pid_key=keys.pid)


def _parse_named_graph(state: Mapping[str, object]) -> GraphSpec:
    from .named_graph import GraphSpec, validate_graph

    raw_graph = state.get("graph")
    try:
        graph = GraphSpec.from_dict(raw_graph)
        raw_tasks = state.get("task_specs", [])
        task_specs = parse_task_specs(raw_tasks)
        validate_graph(graph, task_specs)
    except (TypeError, ValueError) as exc:
        raise RuntimeValidationError(
            f"agent-team named graph is invalid: {exc}"
        ) from exc
    return graph


def resolve_state_role(state: Mapping[str, object], node_id: str) -> RoleTarget:
    """Resolve one exact state identity without converting v3 into v4.

    Version 3 keeps the fixed ``Role`` namespace.  Versions 4 and 5 store a
    parsed graph and return its ``NodeRef`` so the node ID and its fixed kind
    remain coupled at every caller boundary.  Version 5 is accepted only for
    an explicit parallel graph; it is never projected into v4.
    """

    if not isinstance(state, Mapping) or not isinstance(node_id, str) or not node_id:
        raise RuntimeValidationError("agent-team state role identity is invalid")
    version = state.get("version")
    if "orca_delivery_batch" in state and not (
        version == PARALLEL_STATE_VERSION and state.get("runtime") == "orca"
    ):
        raise RuntimeValidationError(
            "orca_delivery_batch is only valid for Orca version-5 state"
        )
    if version == STATE_VERSION:
        if "graph" in state:
            raise RuntimeValidationError("version-3 state must not contain a graph")
        from .contracts import Role

        try:
            role = Role(node_id)
        except ValueError as exc:
            raise RuntimeValidationError(
                "agent-team state role is not selected"
            ) from exc
        specs = state.get("role_specs")
        if not isinstance(specs, Mapping) or node_id not in specs:
            raise RuntimeValidationError("agent-team state role is not selected")
        spec = specs[node_id]
        if not isinstance(spec, Mapping) or "kind" in spec:
            raise RuntimeValidationError(
                "version-3 role spec is mixed with named state"
            )
        return role
    if version not in {NAMED_STATE_VERSION, PARALLEL_STATE_VERSION}:
        raise RuntimeValidationError("agent-team state has an unsupported version")
    graph = _parse_named_graph(state)
    if version == NAMED_STATE_VERSION and graph.coordination.dispatch_mode != "serial":
        raise RuntimeValidationError("version-4 named state requires serial dispatch")
    if (
        version == PARALLEL_STATE_VERSION
        and graph.coordination.dispatch_mode != "parallel"
    ):
        raise RuntimeValidationError("parallel state requires parallel coordination")
    if state.get("runtime") == "orca":
        # Keep Orca's exact controller identity coupled to the graph mode.  A
        # program node must not be silently routed through the Main alias.
        controller_key(state)
    try:
        target = graph.node(node_id)
    except KeyError as exc:
        raise RuntimeValidationError("agent-team state role is not selected") from exc
    specs = state.get("role_specs")
    if not isinstance(specs, Mapping) or node_id not in specs:
        raise RuntimeValidationError("agent-team state role is not selected")
    spec = specs[node_id]
    if not isinstance(spec, Mapping) or spec.get("kind") != target.kind.value:
        raise RuntimeValidationError("named state role kind does not match its graph")
    return target


def _validate_named_role_spec(
    node_id: str, node_kind: str, spec: Mapping[str, object]
) -> None:
    required: tuple[str, ...] = (
        "kind",
        "provider",
        "transport",
        "model",
        "effort",
        "permission",
        "instructions",
        "execution",
    )
    for key in required:
        value = spec.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(
                f"agent-team state role spec is missing {node_id}.{key}"
            )
    if spec["kind"] != node_kind:
        raise RuntimeValidationError(
            f"agent-team state role spec kind does not match {node_id}"
        )
    if node_kind == "main":
        expected = {
            "provider": "claude",
            "transport": "direct",
            "permission": "orchestrator",
            "execution": "tui_direct",
        }
        if any(spec.get(key) != value for key, value in expected.items()):
            raise RuntimeValidationError(
                f"agent-team state role spec profile does not match {node_id}"
            )
        if "adapter_id" in spec or "acp_executables" in spec:
            raise RuntimeValidationError(
                f"agent-team state Main role spec has an ACP binding: {node_id}"
            )
        return
    try:
        from .scoped_acp import native_profile

        expected = native_profile(str(spec["provider"]), node_kind)
    except RuntimeValidationError as exc:
        raise RuntimeValidationError(str(exc)) from exc
    if any(spec.get(key) != value for key, value in expected.items()):
        raise RuntimeValidationError(
            f"agent-team state role spec profile does not match {node_id}"
        )


def _validate_assignment_common(
    state: Mapping[str, object],
    role: str,
    assignment: Mapping[str, object],
    *,
    ownership_key: str,
) -> None:
    try:
        task = validate_task_assignment(state, assignment)
        if task is not None:
            tasks = state.get("tasks")
            if not isinstance(tasks, Mapping) or task.task_id not in tasks:
                raise RuntimeValidationError("TaskSpec assignment is not saved")
            task_record = tasks[task.task_id]
            if not isinstance(task_record, Mapping) or (
                assignment.get("task_stage") != task_record.get("stage")
                or assignment.get("task_revision") != task_record.get("revision")
            ):
                raise RuntimeValidationError(
                    "TaskSpec stage or revision differs from assignment"
                )
    except (ValueError, RuntimeFailure) as exc:
        raise RuntimeValidationError(str(exc)) from exc
    for key in ("task_id", "dispatch_id", "terminal_handle"):
        value = assignment.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(
                f"agent-team state role assignment is missing {role}.{key}"
            )
    if assignment.get(ownership_key) is not True:
        raise RuntimeValidationError(
            f"agent-team state role assignment has unknown ownership: {role}"
        )
    if not isinstance(assignment.get("completion_observed"), bool):
        raise RuntimeValidationError(
            f"agent-team state role assignment has invalid completion state: {role}"
        )


_PARALLEL_ASSIGNMENT_IDENTITY_FIELDS: Final = (
    "task_id",
    "dispatch_id",
    "terminal_handle",
    "launch_nonce",
    "session_name",
    "prompt_path",
    "provider_private_root",
    "snapshot_root",
    "question_socket",
    "write_policy_path",
)
_PARALLEL_ASSIGNMENT_PROCESS_FIELDS: Final = (
    "runner_pid",
    "runner_process_group_id",
)
_PARALLEL_RESOURCE_PATH_FIELDS: Final = (
    "provider_private_root",
    "snapshot_root",
    "prompt_path",
    "question_socket",
    "write_policy_path",
)


def _validate_parallel_assignment_ownership(
    roles: Mapping[str, object],
) -> None:
    """Reject duplicate claims without inspecting or resolving filesystem paths."""

    seen: dict[str, set[str | int]] = {
        field: set()
        for field in (
            *_PARALLEL_ASSIGNMENT_IDENTITY_FIELDS,
            *_PARALLEL_ASSIGNMENT_PROCESS_FIELDS,
        )
    }
    for node_id, raw_assignment in roles.items():
        if not isinstance(raw_assignment, Mapping):
            continue
        for field in _PARALLEL_ASSIGNMENT_IDENTITY_FIELDS:
            value = raw_assignment.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise RuntimeValidationError(
                    f"parallel assignment {node_id}.{field} is invalid"
                )
            if value in seen[field]:
                raise RuntimeValidationError(
                    f"parallel assignment {node_id}.{field} is already owned"
                )
            seen[field].add(value)
        for field in _PARALLEL_ASSIGNMENT_PROCESS_FIELDS:
            value = raw_assignment.get(field)
            if value is None:
                continue
            if type(value) is not int or value <= 1:
                raise RuntimeValidationError(
                    f"parallel assignment {node_id}.{field} is invalid"
                )
            if value in seen[field]:
                raise RuntimeValidationError(
                    f"parallel assignment {node_id}.{field} is already owned"
                )
            seen[field].add(value)


def _parallel_resource_path_parts(
    value: object, *, node_id: str, field: str
) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or not os.path.isabs(value)
    ):
        raise RuntimeValidationError(
            f"parallel assignment {node_id}.{field} path is invalid"
        )
    try:
        canonical = Path(value).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeValidationError(
            f"parallel assignment {node_id}.{field} path cannot be resolved"
        ) from exc
    normalized = unicodedata.normalize("NFC", str(canonical))
    parts = tuple(part.casefold() for part in normalized.split("/") if part)
    if not parts:
        raise RuntimeValidationError(
            f"parallel assignment {node_id}.{field} path is invalid"
        )
    return parts


def _parallel_resource_paths_overlap(
    left: tuple[str, ...], right: tuple[str, ...]
) -> bool:
    shortest = min(len(left), len(right))
    return left[:shortest] == right[:shortest]


def _validate_parallel_resource_paths(roles: Mapping[str, object]) -> None:
    claims: list[tuple[str, str, tuple[str, ...]]] = []
    for node_id, raw_assignment in roles.items():
        if not isinstance(raw_assignment, Mapping):
            continue
        for field in _PARALLEL_RESOURCE_PATH_FIELDS:
            if field not in raw_assignment:
                continue
            parts = _parallel_resource_path_parts(
                raw_assignment[field], node_id=node_id, field=field
            )
            for prior_node, prior_field, prior_parts in claims:
                if prior_node != node_id and _parallel_resource_paths_overlap(
                    prior_parts, parts
                ):
                    raise RuntimeValidationError(
                        f"parallel assignment {node_id}.{field} resource overlaps "
                        f"{prior_node}.{prior_field}"
                    )
            claims.append((node_id, field, parts))


def _validate_background_assignment(
    role: str,
    spec: Mapping[str, object],
    assignment: Mapping[str, object],
) -> None:
    if spec.get("execution") != "background":
        return
    for key in (
        "execution",
        "adapter_id",
        "launch_nonce",
        "prompt_path",
        "provider_private_root",
        "snapshot_root",
    ):
        value = assignment.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(
                f"agent-team state background assignment is missing {role}.{key}"
            )
    if assignment["execution"] != "background" or assignment["adapter_id"] != spec.get(
        "adapter_id"
    ):
        raise RuntimeValidationError(
            f"agent-team state background assignment identity does not match {role}"
        )
    snapshot = assignment.get("adapter_snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeValidationError(
            f"agent-team state background assignment is missing {role}.adapter_snapshot"
        )
    for key in ("adapter_id", "revision", "executable", "version"):
        value = snapshot.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(
                f"agent-team state adapter snapshot is missing {role}.{key}"
            )
    identity = snapshot.get("identity")
    if not isinstance(identity, dict):
        raise RuntimeValidationError(
            f"agent-team state adapter snapshot has invalid {role}.identity"
        )
    for key in ("device", "inode", "size", "mtime_ns"):
        if not isinstance(identity.get(key), int):
            raise RuntimeValidationError(
                f"agent-team state adapter snapshot has invalid {role}.{key}"
            )
    if not isinstance(identity.get("sha256"), str) or not identity["sha256"]:
        raise RuntimeValidationError(
            f"agent-team state adapter snapshot has invalid {role}.sha256"
        )


def _validate_named_native_result(
    state: Mapping[str, object],
    result: object,
    node_by_id: Mapping[str, NodeRef],
    role_specs: Mapping[str, object],
    roles: Mapping[str, object],
    *,
    delivery_container: Mapping[str, object] | None = None,
    containing_node_id: str | None = None,
) -> None:
    if (delivery_container is None) != (containing_node_id is None):
        raise RuntimeValidationError(
            "native result delivery container and node must be supplied together"
        )
    if not isinstance(result, dict):
        raise RuntimeValidationError("named native result must be an object")
    required = {
        "role",
        "role_kind",
        "run_id",
        "task_id",
        "dispatch_id",
        "terminal_handle",
        "launch_nonce",
        "outcome",
        "body",
        "cleanup_confirmed",
        "delivery_id",
    }
    optional = {"logical_task_id", "task_evidence", "question_receipts"}
    if set(result) - required - optional or not required.issubset(result):
        raise RuntimeValidationError("named native result fields are invalid")

    def result_string(field: str) -> str:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(f"named native result {field} is invalid")
        return value

    result_role = result_string("role")
    if containing_node_id is not None and result_role != containing_node_id:
        raise RuntimeValidationError(
            "named native result does not belong to its containing assignment"
        )
    result_kind = result_string("role_kind")
    result_node = node_by_id.get(result_role)
    result_spec = role_specs.get(result_role)
    if (
        result_node is None
        or not isinstance(result_spec, Mapping)
        or result_kind != result_node.kind.value
        or result_spec.get("kind") != result_kind
        or result.get("run_id") != state.get("run_id")
    ):
        raise RuntimeValidationError(
            "named native result identity does not match its graph"
        )
    if result_kind not in ACP_ROLES:
        raise RuntimeValidationError("named native result kind is invalid")
    result_string("run_id")
    result_string("task_id")
    dispatch_id = result_string("dispatch_id")
    result_string("terminal_handle")
    launch_nonce = result_string("launch_nonce")
    if not _LAUNCH_NONCE_RE.fullmatch(launch_nonce):
        raise RuntimeValidationError("named native result launch nonce is invalid")
    delivery_id = result_string("delivery_id")
    outcome = result.get("outcome")
    if not isinstance(outcome, str) or outcome not in {"succeeded", "failed"}:
        raise RuntimeValidationError("named native result outcome is invalid")
    body = result.get("body")
    if not isinstance(body, str) or len(body) > MAX_RESULT_BODY_CHARS:
        raise RuntimeValidationError("named native result body is invalid")
    if type(result.get("cleanup_confirmed")) is not bool:
        raise RuntimeValidationError(
            "named native result cleanup confirmation is invalid"
        )
    logical_task_id = result.get("logical_task_id")
    if logical_task_id is not None and (
        not isinstance(logical_task_id, str) or not logical_task_id
    ):
        raise RuntimeValidationError(
            "named native result logical TaskSpec ID is invalid"
        )
    if "task_evidence" in result and not isinstance(result["task_evidence"], Mapping):
        raise RuntimeValidationError("named native result task evidence is invalid")

    assignment = (
        delivery_container if delivery_container is not None else roles.get(result_role)
    )
    if roles and not isinstance(assignment, Mapping):
        raise RuntimeValidationError(
            "named native result does not belong to the active assignment"
        )
    if isinstance(assignment, Mapping) and (
        assignment.get("role") != result_role
        or assignment.get("role_kind") != result_kind
        or any(
            result.get(field) != assignment.get(field)
            for field in (
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
            )
        )
    ):
        raise RuntimeValidationError(
            "named native result does not match its active assignment"
        )

    task_spec = assignment.get("task_spec") if isinstance(assignment, Mapping) else None
    task_spec_id = task_spec.get("task_id") if isinstance(task_spec, Mapping) else None
    if (
        delivery_container is not None
        and isinstance(task_spec_id, str)
        and (not isinstance(logical_task_id, str) or logical_task_id != task_spec_id)
    ):
        raise RuntimeValidationError(
            "parallel native result TaskSpec identity is missing or changed"
        )
    if (
        isinstance(task_spec_id, str)
        and isinstance(logical_task_id, str)
        and logical_task_id != task_spec_id
    ):
        raise RuntimeValidationError("named native result TaskSpec identity changed")
    if isinstance(logical_task_id, str):
        tasks = state.get("tasks")
        if not isinstance(tasks, Mapping):
            raise RuntimeValidationError("named native result TaskSpec is not saved")
        task_record = tasks.get(logical_task_id)
        if not isinstance(task_record, Mapping):
            raise RuntimeValidationError("named native result TaskSpec is not saved")
        if (
            task_record.get("role") != result_role
            or task_record.get("role_kind") != result_kind
            or task_record.get("dispatch_id") != dispatch_id
        ):
            raise RuntimeValidationError(
                "named native result TaskSpec identity changed"
            )

    delivery_owner = delivery_container if delivery_container is not None else state
    pending_id = delivery_owner.get("pending_delivery_id")
    if pending_id is None and any(
        field in delivery_owner
        for field in ("pending_delivery_kind", "pending_delivery_stage")
    ):
        raise RuntimeValidationError("named native pending delivery is incomplete")
    if pending_id is not None:
        pending_kind = delivery_owner.get("pending_delivery_kind")
        pending_stage = delivery_owner.get("pending_delivery_stage")
        if not isinstance(pending_id, str) or not pending_id:
            raise RuntimeValidationError("named native pending delivery is invalid")
        if not isinstance(pending_kind, str) or pending_kind not in {
            "worker_done",
            "question",
        }:
            raise RuntimeValidationError(
                "named native pending delivery kind is invalid"
            )
        if pending_kind == "worker_done" and (
            pending_id != delivery_id
            or not isinstance(pending_stage, str)
            or pending_stage not in {"observed", "read", "released"}
        ):
            raise RuntimeValidationError(
                "named native result does not match its pending delivery"
            )
        if (
            pending_kind == "worker_done"
            and pending_stage == "released"
            and delivery_container is not None
            and (
                result.get("cleanup_confirmed") is not True
                or not isinstance(assignment, Mapping)
                or assignment.get("completion_observed") is not True
            )
        ):
            raise RuntimeValidationError(
                "released native result lacks cleanup or completion evidence"
            )


def _validate_orca_result(
    state: Mapping[str, object],
    result: object,
    node_by_id: Mapping[str, NodeRef],
    role_specs: Mapping[str, object],
    roles: Mapping[str, object],
    *,
    delivery_container: Mapping[str, object] | None = None,
    containing_node_id: str | None = None,
) -> None:
    """Validate one trusted Orca completion publication.

    Orca publishes a structured runner result before its Delivery is observed.
    It therefore cannot reuse the native result validator: native process and
    question receipts are deliberately absent from this state shape.
    """

    if not isinstance(result, dict):
        raise RuntimeValidationError("Orca result must be an object")
    required = {
        "role",
        "role_kind",
        "run_id",
        "task_id",
        "dispatch_id",
        "terminal_handle",
        "launch_nonce",
        "outcome",
        "body",
        "cleanup_confirmed",
        "notification_expected",
    }
    optional = {"delivery_id", "logical_task_id", "task_evidence"}
    if set(result) - required - optional or not required.issubset(result):
        raise RuntimeValidationError("Orca result fields are invalid")

    def result_string(field: str) -> str:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(f"Orca result {field} is invalid")
        return value

    if (delivery_container is None) != (containing_node_id is None):
        raise RuntimeValidationError(
            "Orca result delivery container and node must be supplied together"
        )

    role = result_string("role")
    if containing_node_id is not None and role != containing_node_id:
        raise RuntimeValidationError(
            "Orca result does not belong to its containing assignment"
        )
    role_kind = result_string("role_kind")
    node = node_by_id.get(role)
    spec = role_specs.get(role)
    if (
        node is None
        or not isinstance(spec, Mapping)
        or node.kind.value != role_kind
        or spec.get("kind") != role_kind
        or role_kind not in ACP_ROLES
        or result.get("run_id") != state.get("run_id")
    ):
        raise RuntimeValidationError("Orca result identity does not match its graph")

    result_string("run_id")
    result_string("task_id")
    dispatch_id = result_string("dispatch_id")
    result_string("terminal_handle")
    launch_nonce = result_string("launch_nonce")
    if not _LAUNCH_NONCE_RE.fullmatch(launch_nonce):
        raise RuntimeValidationError("Orca result launch nonce is invalid")
    outcome = result.get("outcome")
    if not isinstance(outcome, str) or outcome not in {"succeeded", "failed"}:
        raise RuntimeValidationError("Orca result outcome is invalid")
    body = result.get("body")
    if not isinstance(body, str) or len(body) > MAX_RESULT_BODY_CHARS:
        raise RuntimeValidationError("Orca result body is invalid")
    if type(result.get("cleanup_confirmed")) is not bool:
        raise RuntimeValidationError("Orca result cleanup confirmation is invalid")
    if type(result.get("notification_expected")) is not bool:
        raise RuntimeValidationError("Orca notification expectation is invalid")
    if (
        result["notification_expected"] is True
        and result["cleanup_confirmed"] is not True
    ):
        raise RuntimeValidationError("Orca notification requires confirmed cleanup")
    if result["notification_expected"] is False and outcome != "failed":
        raise RuntimeValidationError("suppressed Orca result must be failed")

    delivery_id = result.get("delivery_id")
    if delivery_id is not None and result["notification_expected"] is not True:
        raise RuntimeValidationError("suppressed Orca result cannot have a Delivery")
    if delivery_id is not None and (
        not isinstance(delivery_id, str) or not delivery_id
    ):
        raise RuntimeValidationError("Orca result delivery identity is invalid")
    logical_task_id = result.get("logical_task_id")
    if logical_task_id is not None and (
        not isinstance(logical_task_id, str) or not logical_task_id
    ):
        raise RuntimeValidationError("Orca result logical TaskSpec ID is invalid")
    if "task_evidence" in result and not isinstance(result["task_evidence"], Mapping):
        raise RuntimeValidationError("Orca result task evidence is invalid")

    assignment = (
        delivery_container if delivery_container is not None else roles.get(role)
    )
    if (
        delivery_container is None
        and not roles
        and state.get("pending_delivery_stage") != "released"
    ):
        raise RuntimeValidationError(
            "Orca result without an assignment requires released Delivery"
        )
    if roles and not isinstance(assignment, Mapping):
        raise RuntimeValidationError("Orca result does not belong to an active role")
    if isinstance(assignment, Mapping):
        if assignment.get("role") != role or assignment.get("role_kind") != role_kind:
            raise RuntimeValidationError("Orca result assignment identity is invalid")
        if any(
            result.get(field) != assignment.get(field)
            for field in ("task_id", "dispatch_id", "terminal_handle", "launch_nonce")
        ):
            raise RuntimeValidationError("Orca result does not match its assignment")
        task_spec = assignment.get("task_spec")
        if isinstance(task_spec, Mapping):
            if not isinstance(logical_task_id, str):
                raise RuntimeValidationError(
                    "Orca TaskSpec assignment requires logical_task_id"
                )
            if task_spec.get("task_id") != logical_task_id:
                raise RuntimeValidationError("Orca result TaskSpec identity changed")
        elif logical_task_id is not None:
            raise RuntimeValidationError(
                "taskless Orca result must not contain logical_task_id"
            )

    tasks = state.get("tasks")
    task_record: Mapping[str, object] | None = None
    if isinstance(tasks, Mapping):
        if isinstance(logical_task_id, str):
            candidate = tasks.get(logical_task_id)
            if not isinstance(candidate, Mapping):
                raise RuntimeValidationError("Orca result TaskSpec is not saved")
            task_record = candidate
        elif not roles:
            matches = tuple(
                candidate
                for candidate in tasks.values()
                if isinstance(candidate, Mapping)
                and candidate.get("dispatch_id") == dispatch_id
            )
            if len(matches) > 1:
                raise RuntimeValidationError(
                    "Orca result TaskSpec identity is ambiguous"
                )
            if matches:
                raise RuntimeValidationError(
                    "Orca released TaskSpec result requires logical_task_id"
                )
    if task_record is not None and (
        task_record.get("role") != role
        or task_record.get("role_kind") != role_kind
        or task_record.get("dispatch_id") != dispatch_id
    ):
        raise RuntimeValidationError("Orca result TaskSpec identity changed")

    if (
        outcome == "succeeded"
        and role_kind == "reviewer"
        and task_record is not None
        and "task_evidence" not in result
    ):
        raise RuntimeValidationError("successful Orca reviewer result lacks evidence")

    if "task_evidence" in result:
        evidence = result["task_evidence"]
        if (
            outcome != "succeeded"
            or role_kind != "reviewer"
            or not isinstance(logical_task_id, str)
            or not isinstance(evidence, Mapping)
            or task_record is None
        ):
            raise RuntimeValidationError("Orca task evidence is not allowed here")
        stage = task_record.get("stage")
        revision = task_record.get("revision")
        try:
            task = TaskSpec.from_dict(task_record.get("spec"))
            if not isinstance(stage, str) or not isinstance(revision, str):
                raise TypeError("saved Orca review binding is incomplete")
            verdict = parse_review(
                json.dumps(dict(evidence), ensure_ascii=False),
                task=task,
                stage=stage,
                revision=revision,
            )
        except (TypeError, ValueError, RuntimeFailure) as exc:
            raise RuntimeValidationError("Orca task evidence is invalid") from exc
        if verdict != dict(evidence):
            raise RuntimeValidationError("Orca task evidence is not canonical")


def _validate_orca_effect(
    state: Mapping[str, object],
    *,
    delivery_container: Mapping[str, object] | None = None,
) -> None:
    owner = delivery_container if delivery_container is not None else state
    if "pending_orca_effect" not in owner:
        return
    effect = owner["pending_orca_effect"]
    controller_field = controller_key(state)
    required_fields = {
        "operation",
        "run_id",
        controller_field,
        "delivery_id",
        "message_id",
        "body_sha256",
    }
    if not isinstance(effect, Mapping) or set(effect) != required_fields:
        raise RuntimeValidationError("Orca pending effect fields are invalid")
    if any(effect[key] != state.get(key) for key in ("run_id", controller_field)):
        raise RuntimeValidationError("Orca pending effect Run identity changed")
    delivery_id = effect["delivery_id"]
    if (
        not isinstance(delivery_id, str)
        or not delivery_id
        or delivery_id != owner.get("pending_delivery_id")
    ):
        raise RuntimeValidationError("Orca pending effect Delivery identity changed")
    kind = owner.get("pending_delivery_kind")
    stage = owner.get("pending_delivery_stage")
    question_ids = owner.get("pending_question_ids")
    replied_ids = owner.get("replied_question_ids")
    if not isinstance(question_ids, list) or not isinstance(replied_ids, list):
        raise RuntimeValidationError("Orca pending effect question IDs are invalid")
    if effect["operation"] == "reply":
        message_id, digest = effect["message_id"], effect["body_sha256"]
        if (
            kind != "question"
            or stage != "observed"
            or not isinstance(message_id, str)
            or message_id not in cast(list[object], question_ids)
            or message_id in cast(list[object], replied_ids)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise RuntimeValidationError("Orca pending reply identity is invalid")
    elif effect["operation"] == "ack":
        if delivery_container is not None:
            raise RuntimeValidationError(
                "Orca parallel acknowledgment belongs to the Delivery batch"
            )
        if effect["message_id"] is not None or effect["body_sha256"] is not None:
            raise RuntimeValidationError("Orca pending ACK contains reply data")
        if not (
            kind == "worker_done"
            and stage == "released"
            or kind == "question"
            and stage == "observed"
            and owner.get("pending_question_ids") == owner.get("replied_question_ids")
        ):
            raise RuntimeValidationError("Orca pending ACK is premature")
    else:
        raise RuntimeValidationError("Orca pending effect operation is invalid")


def _validate_orca_release(
    state: Mapping[str, object],
    result: Mapping[str, object] | None,
    roles: Mapping[str, object],
    *,
    delivery_container: Mapping[str, object] | None = None,
    containing_node_id: str | None = None,
) -> None:
    """Validate the durable external-terminal release journal."""

    if (delivery_container is None) != (containing_node_id is None):
        raise RuntimeValidationError(
            "Orca release delivery container and node must be supplied together"
        )
    owner = delivery_container if delivery_container is not None else state
    parallel = delivery_container is not None
    release = owner.get("orca_release")
    pending_stage = owner.get("pending_delivery_stage")
    if release is None:
        if pending_stage == "released":
            raise RuntimeValidationError(
                "released Orca state is missing release journal"
            )
        return
    if not isinstance(release, Mapping) or set(release) != {
        "phase",
        "identity",
        "terminal_close",
    }:
        raise RuntimeValidationError("Orca release journal fields are invalid")
    phase = release.get("phase")
    if phase not in {"closing", "closed", "released"}:
        raise RuntimeValidationError("Orca release journal phase is invalid")
    identity = release.get("identity")
    identity_fields = {
        "role",
        "role_kind",
        "run_id",
        "task_id",
        "dispatch_id",
        "terminal_handle",
        "launch_nonce",
        "delivery_id",
    }
    if not isinstance(identity, Mapping) or set(identity) != identity_fields:
        raise RuntimeValidationError("Orca release identity fields are invalid")
    if any(
        not isinstance(identity.get(field), str) or not identity.get(field)
        for field in identity_fields
    ):
        raise RuntimeValidationError("Orca release identity is invalid")
    if result is None:
        raise RuntimeValidationError("Orca release journal has no result")
    if containing_node_id is not None and identity.get("role") != containing_node_id:
        raise RuntimeValidationError("Orca release identity does not match assignment")
    for field in identity_fields:
        if result.get(field) != identity.get(field):
            raise RuntimeValidationError("Orca release identity does not match result")

    if parallel:
        assert containing_node_id is not None
        assignment = roles.get(containing_node_id)
        if not isinstance(assignment, Mapping):
            raise RuntimeValidationError(
                "Orca parallel release has no active assignment"
            )
        if assignment.get("role") != containing_node_id or assignment.get(
            "role_kind"
        ) != identity.get("role_kind"):
            raise RuntimeValidationError("Orca release assignment identity changed")

    close = release.get("terminal_close")
    if phase == "closing":
        if close is not None or pending_stage != "read" or not roles:
            raise RuntimeValidationError("Orca closing release journal is invalid")
        return

    if not isinstance(close, Mapping) or set(close) != {
        "handle",
        "close_mode",
        "pty_killed",
        "pty_stop_verdict",
    }:
        raise RuntimeValidationError("Orca terminal close receipt is invalid")
    if close.get("handle") != identity.get("terminal_handle"):
        raise RuntimeValidationError("Orca terminal close handle changed")
    close_mode = close.get("close_mode")
    if close_mode is not None and (not isinstance(close_mode, str) or not close_mode):
        raise RuntimeValidationError("Orca terminal close mode is invalid")
    if close.get("pty_killed") is not True:
        raise RuntimeValidationError("Orca terminal close did not kill the PTY")
    pty_stop_verdict = close.get("pty_stop_verdict")
    if pty_stop_verdict is not None and (
        not isinstance(pty_stop_verdict, str)
        or pty_stop_verdict in {"live", "unverifiable"}
    ):
        raise RuntimeValidationError("Orca terminal stop verdict is invalid")
    if phase == "closed":
        if pending_stage != "read" or not roles:
            raise RuntimeValidationError("Orca closed release journal is invalid")
        return
    if pending_stage != "released" or result.get("cleanup_confirmed") is not True:
        raise RuntimeValidationError("Orca released release journal is invalid")
    if not parallel and roles:
        raise RuntimeValidationError("Orca released release journal is invalid")
    if parallel:
        assignment = roles.get(cast(str, containing_node_id))
        if (
            not isinstance(assignment, Mapping)
            or assignment.get("completion_observed") is not True
        ):
            raise RuntimeValidationError(
                "Orca released assignment lacks completion evidence"
            )


def _validate_orca_delivery(
    state: Mapping[str, object],
    result: Mapping[str, object] | None,
    roles: Mapping[str, object],
    *,
    delivery_container: Mapping[str, object] | None = None,
) -> None:
    """Validate the serial Orca Delivery state around an optional result."""

    owner = delivery_container if delivery_container is not None else state
    pending_id = owner.get("pending_delivery_id")
    pending_fields = (
        "pending_delivery_kind",
        "pending_delivery_stage",
        "pending_question_ids",
        "replied_question_ids",
    )
    if pending_id is None:
        if any(field in owner for field in pending_fields):
            raise RuntimeValidationError("Orca pending Delivery is incomplete")
        if isinstance(result, Mapping) and "delivery_id" in result:
            raise RuntimeValidationError(
                "Orca result delivery requires an observed pending Delivery"
            )
        return

    if not isinstance(pending_id, str) or not pending_id:
        raise RuntimeValidationError("Orca pending Delivery identity is invalid")
    kind = owner.get("pending_delivery_kind")
    stage = owner.get("pending_delivery_stage")
    if kind not in {"worker_done", "question", "escalation"}:
        raise RuntimeValidationError("Orca pending Delivery kind is invalid")
    if stage not in {"invalid", "observed", "read", "released"}:
        raise RuntimeValidationError("Orca pending Delivery stage is invalid")

    question_ids = owner.get("pending_question_ids")
    replied_ids = owner.get("replied_question_ids")
    if (
        not isinstance(question_ids, list)
        or not isinstance(replied_ids, list)
        or any(not isinstance(item, str) or not item for item in question_ids)
        or any(not isinstance(item, str) or not item for item in replied_ids)
        or len(question_ids) != len(set(question_ids))
        or len(replied_ids) != len(set(replied_ids))
    ):
        raise RuntimeValidationError("Orca pending Delivery question IDs are invalid")

    def failed_terminal_result_allowed() -> bool:
        if result is None or result.get("outcome") != "failed":
            return False
        if "delivery_id" in result:
            return False
        role = result.get("role")
        assignment = roles.get(role) if isinstance(role, str) else None
        if not isinstance(assignment, Mapping):
            return False
        question = assignment.get("orca_question")
        question_failed = isinstance(question, Mapping) and question.get("phase") in {
            "failed",
            "cancelling",
        }
        return question_failed or state.get("orca_stop_requested") is True

    if stage == "invalid":
        if result is not None:
            if "delivery_id" in result:
                raise RuntimeValidationError(
                    "invalid Orca Delivery cannot contain delivery_id"
                )
            if (
                kind in {"question", "escalation"}
                and not failed_terminal_result_allowed()
            ):
                raise RuntimeValidationError(
                    "successful Orca result cannot coexist with unresolved Delivery"
                )
        if kind == "question" and not set(replied_ids).issubset(question_ids):
            raise RuntimeValidationError("Orca invalid question IDs are inconsistent")
        return

    if kind == "question":
        if stage != "observed" or (
            result is not None and not failed_terminal_result_allowed()
        ):
            raise RuntimeValidationError(
                "Orca question Delivery has an unexpected result or stage"
            )
        if not set(replied_ids).issubset(question_ids):
            raise RuntimeValidationError("Orca question reply identity is invalid")
        return

    if question_ids or replied_ids:
        raise RuntimeValidationError("Orca non-question Delivery has question IDs")
    if kind == "escalation":
        if result is not None and not failed_terminal_result_allowed():
            raise RuntimeValidationError("Orca escalation cannot contain a result")
        return

    # A malformed worker_done Delivery may be retained at the invalid stage,
    # but any trusted result must bind to an actually observed Delivery.
    if result is None:
        if stage == "invalid":
            return
        raise RuntimeValidationError("Orca worker_done Delivery has no result")
    delivery_id = result.get("delivery_id")
    if stage in {"observed", "read", "released"} and delivery_id != pending_id:
        raise RuntimeValidationError("observed Orca result lacks delivery identity")
    if stage == "invalid" and delivery_id is not None:
        raise RuntimeValidationError("Orca result does not match its pending Delivery")
    role = result.get("role")
    assignment = roles.get(role) if isinstance(role, str) else None
    if stage in {"observed", "read"} and (
        not isinstance(assignment, Mapping)
        or assignment.get("completion_observed") is not True
    ):
        raise RuntimeValidationError(
            "Orca observed completion has no active assignment"
        )
    if stage == "released":
        if result.get("cleanup_confirmed") is not True:
            raise RuntimeValidationError("Orca released result lacks cleanup evidence")
        if delivery_container is None:
            if roles:
                raise RuntimeValidationError(
                    "Orca released result still has an active assignment"
                )
        else:
            result_role = result.get("role")
            if not isinstance(result_role, str):
                raise RuntimeValidationError("Orca released result role is invalid")
            assignment = roles.get(result_role)
            if (
                not isinstance(assignment, Mapping)
                or assignment.get("completion_observed") is not True
            ):
                raise RuntimeValidationError(
                    "Orca released result lacks completion evidence"
                )


def _validate_parallel_delivery_container(
    assignment: Mapping[str, object],
    *,
    node_id: str,
) -> None:
    if "native_result" in assignment and assignment["native_result"] is None:
        raise RuntimeValidationError(
            f"parallel assignment {node_id} has a null native result"
        )
    pending_id = assignment.get("pending_delivery_id")
    pending_fields = (
        "pending_delivery_kind",
        "pending_delivery_stage",
        "pending_question_ids",
        "replied_question_ids",
    )
    if pending_id is None and any(field in assignment for field in pending_fields):
        raise RuntimeValidationError(
            f"parallel assignment {node_id} has incomplete pending delivery"
        )
    result = assignment.get("native_result")
    if (
        pending_id is not None
        and result is None
        and assignment.get("pending_delivery_kind") != "question"
    ):
        raise RuntimeValidationError(
            f"parallel assignment {node_id} pending completion has no result"
        )
    if (
        assignment.get("pending_delivery_kind") == "worker_done"
        and assignment.get("completion_observed") is not True
    ):
        raise RuntimeValidationError(
            f"parallel assignment {node_id} pending completion is not observed"
        )
    if assignment.get("completion_observed") is True:
        if result is None:
            raise RuntimeValidationError(
                f"parallel assignment {node_id} completion has no result"
            )
        if (
            pending_id is None
            or assignment.get("pending_delivery_kind") != "worker_done"
            or assignment.get("pending_delivery_stage")
            not in {"observed", "read", "released"}
        ):
            raise RuntimeValidationError(
                f"parallel assignment {node_id} completion pending delivery is invalid"
            )


def _batch_identity(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _ORCA_DELIVERY_BATCH_ID_LIMIT
        or "\x00" in value
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or 0xD800 <= ord(character) <= 0xDFFF
            or unicodedata.category(character) == "Cc"
            for character in value
        )
    ):
        raise RuntimeValidationError(f"Orca Delivery batch {field} is invalid")
    return value


def _is_suppressed_orca_result(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("outcome") == "failed"
        and value.get("notification_expected") is False
        and "delivery_id" not in value
    )


def _validate_orca_delivery_batch(
    state: Mapping[str, object], roles: Mapping[str, object]
) -> tuple[str, str, dict[str, Mapping[str, object]]] | None:
    """Validate the one Run-level FIFO envelope used by named Orca v5."""

    if "orca_delivery_batch" not in state:
        return None
    value = state["orca_delivery_batch"]
    if not isinstance(value, dict) or set(value) != _ORCA_DELIVERY_BATCH_FIELDS:
        raise RuntimeValidationError("Orca Delivery batch fields are invalid")

    delivery_id = _batch_identity(value.get("delivery_id"), "delivery_id")
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in _ORCA_DELIVERY_BATCH_PHASES:
        raise RuntimeValidationError("Orca Delivery batch phase is invalid")
    messages_sha256 = value.get("messages_sha256")
    if (
        not isinstance(messages_sha256, str)
        or _ORCA_DELIVERY_BATCH_HASH_RE.fullmatch(messages_sha256) is None
    ):
        raise RuntimeValidationError("Orca Delivery batch message digest is invalid")
    members = value.get("members")
    if not isinstance(members, list):
        raise RuntimeValidationError("Orca Delivery batch members are invalid")
    message_count = value.get("message_count")
    if phase == "invalid":
        if message_count is not None and (
            type(message_count) is not int or message_count < 0
        ):
            raise RuntimeValidationError("invalid Orca Delivery batch count is invalid")
        if members:
            raise RuntimeValidationError(
                "invalid Orca Delivery batch must not retain members"
            )
        error = value.get("error")
        if (
            not isinstance(error, str)
            or not error
            or len(error) > MAX_RUNTIME_FAILURE_CHARS
            or "\x00" in error
        ):
            raise RuntimeValidationError("invalid Orca Delivery batch error is invalid")
        for assignment in roles.values():
            if not isinstance(assignment, Mapping):
                continue
            if _ORCA_DELIVERY_ASSIGNMENT_FIELDS.intersection(assignment):
                raise RuntimeValidationError(
                    "invalid Orca Delivery batch has partial assignment state"
                )
            result = assignment.get("orca_result")
            if isinstance(result, Mapping) and result.get("delivery_id") is not None:
                raise RuntimeValidationError(
                    "invalid Orca Delivery batch has a partial result Delivery"
                )
        return delivery_id, phase, {}

    if type(message_count) is not int or message_count <= 0:
        raise RuntimeValidationError("Orca Delivery batch count is invalid")
    if value.get("error") is not None:
        raise RuntimeValidationError("Orca Delivery batch error is invalid")
    if len(members) != message_count:
        raise RuntimeValidationError("Orca Delivery batch count does not match members")

    members_by_role: dict[str, Mapping[str, object]] = {}
    message_ids: set[str] = set()
    for raw_member in members:
        if not isinstance(raw_member, dict) or set(raw_member) != (
            _ORCA_DELIVERY_BATCH_MEMBER_FIELDS
        ):
            raise RuntimeValidationError(
                "Orca Delivery batch member fields are invalid"
            )
        member = cast(dict[str, object], raw_member)
        role = _batch_identity(member.get("role"), "member role")
        if role in members_by_role:
            raise RuntimeValidationError("Orca Delivery batch contains duplicate roles")
        message_id = _batch_identity(member.get("message_id"), "member message_id")
        if message_id in message_ids:
            raise RuntimeValidationError(
                "Orca Delivery batch contains duplicate message IDs"
            )
        message_ids.add(message_id)
        role_kind = _batch_identity(member.get("role_kind"), "member role_kind")
        kind = member.get("kind")
        if not isinstance(kind, str) or kind not in _ORCA_DELIVERY_BATCH_KINDS:
            raise RuntimeValidationError("Orca Delivery batch member kind is invalid")
        for field in ("task_id", "dispatch_id", "terminal_handle", "launch_nonce"):
            _batch_identity(member.get(field), f"member {field}")
        assignment = roles.get(role)
        if not isinstance(assignment, Mapping):
            raise RuntimeValidationError(
                "Orca Delivery batch member has no active assignment"
            )
        if any(
            member.get(field) != assignment.get(field)
            for field in (
                "role",
                "role_kind",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
            )
        ):
            raise RuntimeValidationError(
                "Orca Delivery batch member identity does not match assignment"
            )
        if role_kind != assignment.get("role_kind"):
            raise RuntimeValidationError("Orca Delivery batch member role kind changed")
        if assignment.get("pending_delivery_id") != delivery_id:
            raise RuntimeValidationError(
                "Orca Delivery batch member Delivery identity is invalid"
            )
        if assignment.get("pending_delivery_kind") != kind:
            raise RuntimeValidationError(
                "Orca Delivery batch member kind does not match assignment"
            )
        pending_stage = assignment.get("pending_delivery_stage")
        if pending_stage not in {"observed", "read", "released"}:
            raise RuntimeValidationError("Orca Delivery batch member stage is invalid")

        result = assignment.get("orca_result")
        question = assignment.get("orca_question")
        if kind == "worker_done":
            if (
                assignment.get("completion_observed") is not True
                or not isinstance(result, Mapping)
                or question is not None
                or result.get("delivery_id") != delivery_id
            ):
                raise RuntimeValidationError(
                    "Orca completion member does not match its batch"
                )
        elif kind == "question":
            if (
                not isinstance(question, Mapping)
                or question.get("message_id") != message_id
                or pending_stage != "observed"
                or result is not None
                and not _is_suppressed_orca_result(result)
            ):
                raise RuntimeValidationError(
                    "Orca question member does not match its batch"
                )
        elif question is not None or (
            result is not None and not _is_suppressed_orca_result(result)
        ):
            raise RuntimeValidationError(
                "Orca escalation member has a typed assignment journal"
            )
        members_by_role[role] = member

    return delivery_id, phase, members_by_role


def _validate_orca_batch_acknowledging(
    batch: tuple[str, str, dict[str, Mapping[str, object]]] | None,
    roles: Mapping[str, object],
) -> None:
    if batch is None or batch[1] != "acknowledging":
        return
    _delivery_id, _phase, members_by_role = batch
    for role, member in members_by_role.items():
        assignment = roles.get(role)
        if not isinstance(assignment, Mapping):
            raise RuntimeValidationError(
                "acknowledging Orca Delivery batch member is not assigned"
            )
        if "pending_orca_effect" in assignment:
            raise RuntimeValidationError(
                "acknowledging Orca Delivery batch has a pending effect"
            )
        kind = member["kind"]
        stage = assignment.get("pending_delivery_stage")
        if kind == "worker_done":
            release = assignment.get("orca_release")
            if (
                stage != "released"
                or not isinstance(release, Mapping)
                or release.get("phase") != "released"
            ):
                raise RuntimeValidationError(
                    "acknowledging Orca completion is not ready"
                )
        elif kind == "question":
            question = assignment.get("orca_question")
            if (
                stage != "observed"
                or not isinstance(question, Mapping)
                or question.get("phase") != "replied"
                or not isinstance(question.get("message_id"), str)
                or assignment.get("pending_question_ids")
                != [question.get("message_id")]
                or assignment.get("replied_question_ids")
                != [question.get("message_id")]
                or not isinstance(question.get("answer_message_id"), str)
                or not question.get("answer_message_id")
            ):
                raise RuntimeValidationError("acknowledging Orca question is not ready")
        else:
            raise RuntimeValidationError(
                "acknowledging Orca batch contains an escalation"
            )


def _validate_orca_parallel_deliveries(
    state: Mapping[str, object],
    node_by_id: Mapping[str, NodeRef],
    role_specs: Mapping[str, object],
    roles: Mapping[str, object],
) -> None:
    """Validate one exact Orca Delivery container for every active node."""

    from .orca_delivery import containers

    try:
        records = containers(state)
    except (TypeError, ValueError) as exc:
        raise RuntimeValidationError(str(exc)) from exc

    batch = _validate_orca_delivery_batch(state, roles)
    batch_delivery_id = (
        batch[0] if batch is not None and batch[1] != "invalid" else None
    )
    batch_members = batch[2] if batch is not None and batch[1] != "invalid" else {}
    delivery_owners: dict[str, str] = {}
    question_owners: dict[str, str] = {}

    def claim(
        owners: dict[str, str], value: object, *, node_id: str, field: str
    ) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not value:
            raise RuntimeValidationError(f"Orca parallel {field} identity is invalid")
        previous = owners.get(value)
        shared_batch_id = (
            batch_delivery_id is not None
            and value == batch_delivery_id
            and node_id in batch_members
            and previous in batch_members
        )
        if previous is not None and previous != node_id and not shared_batch_id:
            raise RuntimeValidationError(f"Orca parallel {field} identity was reused")
        owners[value] = node_id

    for selected, raw_assignment in records:
        if selected is None or not isinstance(raw_assignment, Mapping):
            raise RuntimeValidationError(
                "Orca parallel Delivery requires role assignments"
            )
        node_id = selected
        assignment = raw_assignment
        result_value = assignment.get("orca_result")
        if "orca_result" in assignment and result_value is None:
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} has a null result"
            )
        if any(
            field in assignment and assignment[field] is None
            for field in ("orca_release", "pending_orca_effect", "orca_question")
        ):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} has a null journal"
            )

        pending_id = assignment.get("pending_delivery_id")
        batch_member = batch_members.get(node_id)
        if pending_id is not None and batch_delivery_id is None:
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} is missing a Delivery batch"
            )
        if (
            batch_delivery_id is not None
            and pending_id is not None
            and (batch_member is None or pending_id != batch_delivery_id)
        ):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} is outside its Delivery batch"
            )
        claim(delivery_owners, pending_id, node_id=node_id, field="Delivery")
        pending_fields = (
            "pending_delivery_kind",
            "pending_delivery_stage",
            "pending_question_ids",
            "replied_question_ids",
        )
        if pending_id is None and any(field in assignment for field in pending_fields):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} has incomplete Delivery"
            )

        result = result_value if isinstance(result_value, Mapping) else None
        if result_value is not None and not isinstance(result_value, Mapping):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} result is invalid"
            )
        question = assignment.get("orca_question")
        if (
            isinstance(question, Mapping)
            and question.get("phase")
            in {"asking", "observed", "replied", "failed", "cancelling"}
            and isinstance(result, Mapping)
            and result.get("notification_expected") is True
        ):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} active question conflicts with a notification result"
            )
        if (
            assignment.get("pending_delivery_kind") == "worker_done"
            and assignment.get("pending_delivery_stage") != "invalid"
            and assignment.get("completion_observed") is not True
        ):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} pending completion is not observed"
            )
        if assignment.get("completion_observed") is True:
            if result is None:
                raise RuntimeValidationError(
                    f"Orca parallel assignment {node_id} completion has no result"
                )
            if (
                pending_id is None
                or assignment.get("pending_delivery_kind") != "worker_done"
                or assignment.get("pending_delivery_stage")
                not in {"observed", "read", "released"}
            ):
                raise RuntimeValidationError(
                    f"Orca parallel assignment {node_id} completion Delivery is invalid"
                )

        if isinstance(result, Mapping):
            result_delivery_id = result.get("delivery_id")
            if result_delivery_id is not None and batch_delivery_id is None:
                raise RuntimeValidationError(
                    f"Orca parallel assignment {node_id} result is missing a Delivery batch"
                )
            if (
                batch_delivery_id is not None
                and result_delivery_id is not None
                and (batch_member is None or result_delivery_id != batch_delivery_id)
            ):
                raise RuntimeValidationError(
                    f"Orca parallel assignment {node_id} result is outside its Delivery batch"
                )
            claim(
                delivery_owners,
                result_delivery_id,
                node_id=node_id,
                field="result Delivery",
            )
            _validate_orca_result(
                state,
                result,
                node_by_id,
                role_specs,
                roles,
                delivery_container=assignment,
                containing_node_id=node_id,
            )

        question_has_delivery = isinstance(question, Mapping) and (
            question.get("phase") in {"observed", "replied"}
            or assignment.get("pending_delivery_id") is not None
        )
        if (
            question_has_delivery
            and isinstance(question, Mapping)
            and question.get("message_id") is not None
            and batch_delivery_id is None
        ):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} question is missing a Delivery batch"
            )
        if (
            question_has_delivery
            and isinstance(question, Mapping)
            and question.get("message_id") is not None
            and batch_delivery_id is not None
            and batch_member is None
        ):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} question is outside its Delivery batch"
            )
        if assignment.get("pending_delivery_kind") == "question":
            message_id = (
                question.get("message_id") if isinstance(question, Mapping) else None
            )
            if not isinstance(message_id, str) or assignment.get(
                "pending_question_ids"
            ) != [message_id]:
                raise RuntimeValidationError(
                    f"Orca parallel assignment {node_id} question Delivery is unpaired"
                )
        if question is not None:
            from .orca_questions import validate_outbox

            try:
                validate_outbox(state, assignment)
            except (TypeError, ValueError) as exc:
                raise RuntimeValidationError(str(exc)) from exc
            if isinstance(question, Mapping):
                claim(
                    question_owners,
                    question.get("message_id"),
                    node_id=node_id,
                    field="question message",
                )

        _validate_orca_delivery(
            state,
            result,
            roles,
            delivery_container=assignment,
        )
        _validate_orca_release(
            state,
            result,
            roles,
            delivery_container=assignment,
            containing_node_id=node_id,
        )
        _validate_orca_effect(state, delivery_container=assignment)

        release = assignment.get("orca_release")
        if (
            release is not None
            and assignment.get("pending_delivery_kind") != "worker_done"
        ):
            raise RuntimeValidationError(
                f"Orca parallel assignment {node_id} release is not a completion Delivery"
            )

    _validate_orca_batch_acknowledging(batch, roles)


def _validate_named_state(path: Path, state: object) -> dict[str, object]:
    if not isinstance(state, dict):
        raise RuntimeValidationError("agent-team state must be an object")
    version = state.get("version")
    if version not in {NAMED_STATE_VERSION, PARALLEL_STATE_VERSION}:
        raise RuntimeValidationError(
            "agent-team state has an unsupported named version"
        )
    runtime = state.get("runtime")
    orca = runtime == "orca"
    if not orca and not is_native_runtime(runtime):
        raise RuntimeValidationError("named state requires a native runtime")
    if "orca_delivery_batch" in state and (
        not orca or version != PARALLEL_STATE_VERSION
    ):
        raise RuntimeValidationError(
            "orca_delivery_batch is only valid for Orca version-5 state"
        )
    if orca:
        if not isinstance(state.get("tasks"), dict):
            raise RuntimeValidationError("Orca named state is missing tasks")
        if (
            "orca_stop_requested" in state
            and state.get("orca_stop_requested") is not True
        ):
            raise RuntimeValidationError("orca_stop_requested must be true")
        if any(
            field in state
            for field in (
                "native",
                "native_result",
                "native_question",
                "coordinator_pid",
                "main_argv",
                "main_process",
                "agent_pid",
                "pending_delivery",
            )
        ):
            raise RuntimeValidationError("Orca named state contains native metadata")
        if version == PARALLEL_STATE_VERSION:
            from .orca_delivery import DELIVERY_FIELDS

            if DELIVERY_FIELDS.intersection(state) or "orca_question" in state:
                raise RuntimeValidationError(
                    "Orca parallel state contains a legacy root Delivery"
                )
            if "pending_role_starts" in state:
                raise RuntimeValidationError(
                    "Orca parallel state contains a startup marker map"
                )
    elif "worktree_id" in state or "orca_socket" in state:
        raise RuntimeValidationError("native state must not contain Orca metadata")

    required: tuple[str, ...] = (
        "version",
        "runtime",
        "team_id",
        "workspace",
        "config_path",
        "state_path",
        "launcher_path",
        "run_id",
        "graph",
        "role_specs",
        "roles",
    )
    if orca:
        required = (*required, "worktree_id", "orca_socket")
    for key in required:
        value = state.get(key)
        if key in {"graph", "role_specs", "roles"}:
            if not isinstance(value, dict):
                raise RuntimeValidationError(f"agent-team state is missing {key}")
        elif key != "version" and (not isinstance(value, str) or not value):
            raise RuntimeValidationError(f"agent-team state is missing {key}")
    if _canonical(Path(str(state["state_path"]))) != _canonical(path):
        raise RuntimeValidationError("agent-team state path does not match its file")

    if orca and not isinstance(state.get("task_specs"), list):
        raise RuntimeValidationError("Orca named state is missing task_specs")

    graph = _parse_named_graph(state)
    if version == NAMED_STATE_VERSION and graph.coordination.dispatch_mode != "serial":
        raise RuntimeValidationError("version-4 named state requires serial dispatch")
    if (
        version == PARALLEL_STATE_VERSION
        and graph.coordination.dispatch_mode != "parallel"
    ):
        raise RuntimeValidationError("version-5 state requires parallel coordination")
    if orca:
        is_program(state)
        _validate_controller_state(state)
    node_by_id = {node.node_id: node for node in graph.nodes}
    role_specs = state["role_specs"]
    assert isinstance(role_specs, dict)
    if set(role_specs) != set(node_by_id):
        raise RuntimeValidationError(
            "agent-team state role_specs do not match its graph"
        )
    for node_id, node in node_by_id.items():
        spec = role_specs.get(node_id)
        if not isinstance(spec, dict):
            raise RuntimeValidationError(
                f"agent-team state role spec is invalid: {node_id}"
            )
        _validate_named_role_spec(node_id, node.kind.value, spec)

    if not orca:
        try:
            keys = controller_keys(state)
        except (TypeError, ValueError) as exc:
            raise RuntimeValidationError(
                f"agent-team native controller identity is invalid: {exc}"
            ) from exc
        _validate_native_controller_state(state, keys)

    roles = state["roles"]
    assert isinstance(roles, dict)
    if orca and version == NAMED_STATE_VERSION and len(roles) > 1:
        raise RuntimeValidationError(
            "Orca version-4 state allows at most one active role"
        )
    for node_id, assignment in roles.items():
        assignment_node = node_by_id.get(node_id)
        if assignment_node is None or not isinstance(assignment, dict):
            raise RuntimeValidationError("agent-team state has invalid role assignment")
        if (
            assignment.get("role") != node_id
            or assignment.get("role_kind") != assignment_node.kind.value
        ):
            raise RuntimeValidationError(
                f"agent-team state role assignment identity does not match {node_id}"
            )
        if orca and assignment_node.kind.value not in ACP_ROLES:
            raise RuntimeValidationError("Orca state cannot assign its Main node")
        if (
            orca
            and version == PARALLEL_STATE_VERSION
            and any(
                field in assignment
                for field in ("orca_stop_requested", "pending_role_start")
            )
        ):
            raise RuntimeValidationError(
                "Orca parallel stop and startup markers must remain at the root"
            )
        if orca and any(
            field in assignment
            for field in (
                "runner_pid",
                "runner_process_group_id",
                "runner_argv",
                "runner_startup_argv",
                "launcher_owned_runner",
                "native_result",
                "native_question",
                "question_receipts",
            )
        ):
            raise RuntimeValidationError(
                "Orca state contains native assignment metadata"
            )
        _validate_assignment_common(
            state,
            node_id,
            assignment,
            ownership_key="launcher_owned_terminal"
            if orca
            else "launcher_owned_runner",
        )
        assignment_spec = role_specs[node_id]
        assert isinstance(assignment_spec, Mapping)
        _validate_background_assignment(
            node_id,
            assignment_spec,
            assignment,
        )
    if version == PARALLEL_STATE_VERSION:
        if len(roles) > graph.coordination.max_active:
            raise RuntimeValidationError(
                "parallel assignments exceed coordination.max_active"
            )
        _validate_parallel_assignment_ownership(roles)
        _validate_parallel_resource_paths(roles)

    try:
        validate_saved_tasks(state)
    except (ValueError, RuntimeFailure) as exc:
        raise RuntimeValidationError(str(exc)) from exc

    if version == PARALLEL_STATE_VERSION and orca:
        _validate_orca_parallel_deliveries(
            state,
            node_by_id,
            role_specs,
            roles,
        )
    elif version == PARALLEL_STATE_VERSION:
        delivery_ids: set[str] = set()
        for node_id, assignment in roles.items():
            _validate_parallel_delivery_container(assignment, node_id=node_id)
            result = assignment.get("native_result")
            if result is not None:
                _validate_named_native_result(
                    state,
                    result,
                    node_by_id,
                    role_specs,
                    roles,
                    delivery_container=assignment,
                    containing_node_id=node_id,
                )
                if isinstance(result, Mapping) and isinstance(
                    result.get("delivery_id"), str
                ):
                    delivery_id = result["delivery_id"]
                    if delivery_id in delivery_ids:
                        raise RuntimeValidationError(
                            "parallel native delivery identity was reused"
                        )
                    delivery_ids.add(delivery_id)
            question = assignment.get("native_question")
            if isinstance(question, Mapping) and isinstance(
                question.get("delivery_id"), str
            ):
                delivery_id = question["delivery_id"]
                if delivery_id in delivery_ids:
                    raise RuntimeValidationError(
                        "parallel native delivery identity was reused"
                    )
                delivery_ids.add(delivery_id)
    elif orca:
        orca_result = state.get("orca_result")
        if "orca_result" in state:
            _validate_orca_result(
                state,
                orca_result,
                node_by_id,
                role_specs,
                roles,
            )
        for assignment in roles.values():
            if isinstance(assignment, Mapping) and "orca_question" in assignment:
                from .orca_questions import validate_outbox

                try:
                    validate_outbox(state, assignment)
                except (TypeError, ValueError) as exc:
                    raise RuntimeValidationError(str(exc)) from exc
        _validate_orca_delivery(
            state,
            orca_result if isinstance(orca_result, Mapping) else None,
            roles,
        )
        _validate_orca_release(
            state,
            orca_result if isinstance(orca_result, Mapping) else None,
            roles,
        )
        _validate_orca_effect(state)
    else:
        native_result = state.get("native_result")
        pending_id = state.get("pending_delivery_id")
        pending_fields = ("pending_delivery_kind", "pending_delivery_stage")
        if pending_id is None and any(field in state for field in pending_fields):
            raise RuntimeValidationError("named native pending delivery is incomplete")
        if (
            pending_id is not None
            and native_result is None
            and state.get("pending_delivery_kind") != "question"
        ):
            raise RuntimeValidationError(
                "named native pending completion has no result"
            )
        if "native_result" in state:
            _validate_named_native_result(
                state,
                native_result,
                node_by_id,
                role_specs,
                roles,
            )

    if not orca:
        from .native_questions import validate_state as validate_questions

        try:
            validate_questions(state)
        except (TypeError, ValueError) as exc:
            raise RuntimeValidationError(str(exc)) from exc
    return state


def validate_state_object(path: Path, state: object) -> dict[str, object]:
    if isinstance(state, dict) and state.get("version") in {
        NAMED_STATE_VERSION,
        PARALLEL_STATE_VERSION,
    }:
        return _validate_named_state(path, state)
    return _validate_state_v3(path, state)


def _validate_state_v3(path: Path, state: object) -> dict[str, object]:
    path = _canonical(path)
    if not isinstance(state, dict):
        raise RuntimeValidationError("agent-team state must be an object")
    if state.get("version") != STATE_VERSION:
        raise RuntimeValidationError(
            f"agent-team state has an unsupported format (expected version {STATE_VERSION})"
        )
    runtime = state.get("runtime")
    if runtime != "orca" and not is_native_runtime(runtime):
        raise RuntimeValidationError(
            "agent-team state runtime must be one of: "
            + ", ".join(sorted({"orca", *NATIVE_RUNTIMES}))
        )
    if "orca_delivery_batch" in state:
        raise RuntimeValidationError(
            "orca_delivery_batch is only valid for Orca version-5 state"
        )
    required: tuple[str, ...] = _STATE_REQUIRED_KEYS
    if is_native_runtime(runtime):
        if "worktree_id" in state or "orca_socket" in state:
            raise RuntimeValidationError("native state must not contain Orca metadata")
        required = tuple(
            key for key in required if key not in {"worktree_id", "orca_socket"}
        )
        native = state.get("native")
        if (
            not isinstance(native, dict)
            or not isinstance(native.get("phase"), str)
            or native.get("phase")
            not in {
                "starting",
                "running",
                "stopping",
                "stopped",
            }
        ):
            raise RuntimeValidationError("native state has an invalid lifecycle phase")
        if "coordinator_terminal" in state:
            raise RuntimeValidationError(
                "version-3 agent state must not contain coordinator_terminal"
            )
        nonce = native.get("run_nonce")
        if not isinstance(nonce, str) or not _LAUNCH_NONCE_RE.fullmatch(nonce):
            raise RuntimeValidationError("native state has an invalid run nonce")
        main_argv = native.get("main_argv")
        if (
            not isinstance(main_argv, list)
            or not main_argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in main_argv)
            or not Path(main_argv[0]).is_absolute()
        ):
            raise RuntimeValidationError("native state has an invalid Main command")
        if "coordinator_argv" in native or "coordinator_process" in native:
            raise RuntimeValidationError(
                "version-3 agent state contains program lifecycle keys"
            )
        if "main_process" in native:
            validate_native_main_process(native["main_process"])
    for key in required:
        value = state.get(key)
        if key in {"role_specs", "roles"}:
            if not isinstance(value, dict):
                raise RuntimeValidationError(f"agent-team state is missing {key}")
        elif key != "version" and (not isinstance(value, str) or not value):
            raise RuntimeValidationError(f"agent-team state is missing {key}")
    state_path = state.get("state_path")
    if not isinstance(state_path, str) or _canonical(Path(state_path)) != path:
        raise RuntimeValidationError("agent-team state path does not match its file")
    role_specs = state["role_specs"]
    assert isinstance(role_specs, dict)
    for role, spec in role_specs.items():
        if not isinstance(role, str) or not isinstance(spec, dict):
            raise RuntimeValidationError("agent-team state has invalid role_specs")
        if "kind" in spec:
            raise RuntimeValidationError(
                "version-3 role spec is mixed with named state"
            )
        for key in (
            "provider",
            "transport",
            "model",
            "effort",
            "permission",
            "instructions",
            "execution",
        ):
            value = spec.get(key)
            if not isinstance(value, str) or not value:
                raise RuntimeValidationError(
                    f"agent-team state role spec is missing {role}.{key}"
                )
        execution = spec["execution"]
        if execution not in {"tui_direct", "background"}:
            raise RuntimeValidationError(
                f"agent-team state role spec has unsupported {role}.execution"
            )
        adapter_id = spec.get("adapter_id")
        if execution == "background":
            if not isinstance(adapter_id, str) or not adapter_id:
                raise RuntimeValidationError(
                    f"agent-team state role spec is missing {role}.adapter_id"
                )
        elif adapter_id is not None:
            raise RuntimeValidationError(
                f"agent-team state role spec has unexpected {role}.adapter_id"
            )
    roles = state["roles"]
    assert isinstance(roles, dict)
    try:
        validate_saved_tasks(state)
    except (ValueError, RuntimeFailure) as exc:
        raise RuntimeValidationError(str(exc)) from exc
    for role, assignment in roles.items():
        if role not in role_specs or not isinstance(assignment, dict):
            raise RuntimeValidationError("agent-team state has invalid role assignment")
        if "role_kind" in assignment:
            raise RuntimeValidationError(
                "version-3 role assignment is mixed with named state"
            )
        _validate_assignment_common(
            state,
            role,
            assignment,
            ownership_key=(
                "launcher_owned_runner"
                if is_native_runtime(runtime)
                else "launcher_owned_terminal"
            ),
        )
        spec = role_specs[role]
        assert isinstance(spec, dict)
        _validate_background_assignment(role, spec, assignment)
    if is_native_runtime(runtime):
        from .native_questions import validate_state as validate_questions

        try:
            validate_questions(state)
        except (TypeError, ValueError) as exc:
            raise RuntimeValidationError(str(exc)) from exc
    elif "native_question" in state:
        raise RuntimeValidationError("Orca state must not contain a native question")
    return state


def write_state(
    path: Path,
    state: dict[str, object],
    *,
    require_existing: bool = False,
    reservation_held: bool = False,
) -> None:
    if not reservation_held:
        reservation = _LifecycleReservation(path, create_parent=not require_existing)
        try:
            reservation.acquire()
        except RuntimeFailure as exc:
            message = (
                "agent-team state disappeared before save"
                if exc.code is ErrorCode.TEAM_NOT_RUNNING
                else "agent-team state reservation is unavailable"
            )
            raise RuntimeValidationError(message) from exc
        try:
            write_state(
                path,
                state,
                require_existing=require_existing,
                reservation_held=True,
            )
        finally:
            reservation.release()
        return
    path = _absolute(path)
    if require_existing:
        try:
            _state_dir_stat(path.parent)
        except RuntimeValidationError as exc:
            raise RuntimeValidationError(
                f"agent-team state disappeared before save: {path}"
            ) from exc
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _state_dir_stat(path.parent)
    validate_state_object(path, state)
    existing: tuple[int, int] | None = None
    if require_existing:
        try:
            existing_stat = path.lstat()
        except OSError as exc:
            raise RuntimeValidationError(
                f"agent-team state disappeared before save: {path}"
            ) from exc
        if not stat.S_ISREG(existing_stat.st_mode) or stat.S_ISLNK(
            existing_stat.st_mode
        ):
            raise RuntimeValidationError(
                f"agent-team state is not a regular file: {path}"
            )
        existing = (existing_stat.st_dev, existing_stat.st_ino)
    temporary = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise RuntimeValidationError("agent-team state exceeds size limit")
    fd: int | None = None
    state_published = False
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as state_file:
            fd = None
            state_file.write(payload)
            state_file.flush()
            os.fsync(state_file.fileno())
        if require_existing:
            try:
                current_stat = path.lstat()
            except OSError as exc:
                raise RuntimeValidationError(
                    f"agent-team state disappeared before save: {path}"
                ) from exc
            if existing != (current_stat.st_dev, current_stat.st_ino):
                raise RuntimeValidationError(
                    f"agent-team state changed before save: {path}"
                )
        os.replace(temporary, path)
        state_published = True
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, RuntimeValidationError) as exc:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(exc, RuntimeValidationError):
            raise
        if state_published:
            raise StatePublishError(
                "agent-team state was published but durability is unconfirmed"
            ) from exc
        raise RuntimeValidationError(
            f"could not write agent-team state: {path}"
        ) from exc


def read_state(path: Path) -> dict[str, object]:
    path = _absolute(path)
    # Atomic publication can replace the inode between lstat and open.
    for attempt in range(3):
        try:
            file_stat = path.lstat()
        except OSError as exc:
            raise RuntimeValidationError(f"agent-team is not running: {path}") from exc
        if stat.S_ISLNK(file_stat.st_mode):
            raise RuntimeValidationError(
                f"agent-team state must not be a symlink: {path}"
            )
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeValidationError(
                f"agent-team state must be a regular file: {path}"
            )
        if file_stat.st_uid != os.getuid():
            raise RuntimeValidationError(
                f"agent-team state owner is not the current user: {path}"
            )
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise RuntimeValidationError(
                f"agent-team state must have mode 0600: {path}"
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        try:
            fd = os.open(path, flags)
            opened_stat = os.fstat(fd)
            if (
                opened_stat.st_dev != file_stat.st_dev
                or opened_stat.st_ino != file_stat.st_ino
            ):
                if attempt < 2:
                    continue
                raise RuntimeValidationError("agent-team state changed during open")
            payload = _read_bounded_fd(fd, MAX_STATE_BYTES)
        except OSError as exc:
            raise RuntimeValidationError(
                f"agent-team state is unavailable: {path}"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)
        break
    try:
        state = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeValidationError(f"agent-team state is invalid: {path}") from exc
    return validate_state_object(path, state)


def _state_tree_root(state_path: Path, state: dict[str, object]) -> Path:
    raw_state_path = _absolute(state_path)
    if raw_state_path.name != "state.json":
        raise RuntimeValidationError("agent-team state path must be state.json")
    state_value = state.get("state_path")
    team_id = state.get("team_id")
    if not isinstance(state_value, str) or not isinstance(team_id, str) or not team_id:
        raise RuntimeValidationError("agent-team state is missing cleanup identity")
    if _canonical(Path(state_value)) != _canonical(raw_state_path):
        raise RuntimeValidationError(
            "agent-team state path does not match cleanup target"
        )
    root = raw_state_path.parent
    _state_dir_stat(root)
    if root.name != team_id:
        raise RuntimeValidationError("agent-team state root does not match team_id")
    try:
        state_stat = raw_state_path.lstat()
    except OSError as exc:
        raise RuntimeValidationError(
            f"agent-team state is unavailable: {raw_state_path}"
        ) from exc
    if stat.S_ISLNK(state_stat.st_mode) or not stat.S_ISREG(state_stat.st_mode):
        raise RuntimeValidationError("agent-team state must be a regular file")
    if state_stat.st_uid != os.getuid() or stat.S_IMODE(state_stat.st_mode) != 0o600:
        raise RuntimeValidationError("agent-team state is not private")
    return _canonical(root)


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(path, flags)
        directory_stat = os.fstat(directory_fd)
    except OSError as exc:
        raise RuntimeValidationError(f"could not open state directory: {path}") from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        os.close(directory_fd)
        raise RuntimeValidationError(f"state path is not a directory: {path}")
    if directory_stat.st_uid != os.getuid():
        os.close(directory_fd)
        raise RuntimeValidationError(
            f"state directory owner is not the current user: {path}"
        )
    return directory_fd


def _open_child_directory(parent_fd: int, name: str, expected: os.stat_result) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        child_stat = os.fstat(child_fd)
    except OSError as exc:
        raise RuntimeValidationError(f"state tree directory changed: {name}") from exc
    if (
        not stat.S_ISDIR(child_stat.st_mode)
        or child_stat.st_uid != os.getuid()
        or child_stat.st_dev != expected.st_dev
        or child_stat.st_ino != expected.st_ino
    ):
        os.close(child_fd)
        raise RuntimeValidationError(f"state tree directory changed: {name}")
    if stat.S_IMODE(child_stat.st_mode) not in {0o700, 0o755}:
        os.close(child_fd)
        raise RuntimeValidationError(f"state tree directory has an unsafe mode: {name}")
    return child_fd


def _scandir_fd(directory_fd: int) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(os.dup(directory_fd)) as entries:
            return list(entries)
    except OSError as exc:
        raise RuntimeValidationError("could not inspect state tree") from exc


def _validate_state_file_fd(root_fd: int) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        state_fd = os.open("state.json", flags, dir_fd=root_fd)
        state_stat = os.fstat(state_fd)
    except OSError as exc:
        raise RuntimeValidationError("agent-team state is unavailable") from exc
    finally:
        if "state_fd" in locals():
            os.close(state_fd)
    if (
        stat.S_ISLNK(state_stat.st_mode)
        or not stat.S_ISREG(state_stat.st_mode)
        or state_stat.st_uid != os.getuid()
        or stat.S_IMODE(state_stat.st_mode) != 0o600
    ):
        raise RuntimeValidationError("agent-team state is not private")


def _validate_state_tree_fd(directory_fd: int, relative: str = "") -> None:
    for entry in _scandir_fd(directory_fd):
        entry_stat = entry.stat(follow_symlinks=False)
        entry_name = f"{relative}/{entry.name}" if relative else entry.name
        if entry_stat.st_uid != os.getuid():
            raise RuntimeValidationError(
                f"state tree entry owner is not the current user: {entry_name}"
            )
        if stat.S_ISLNK(entry_stat.st_mode) or stat.S_ISREG(entry_stat.st_mode):
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise RuntimeValidationError(
                f"unsupported special file in state tree: {entry_name}"
            )
        child_fd = _open_child_directory(directory_fd, entry.name, entry_stat)
        try:
            _validate_state_tree_fd(child_fd, entry_name)
        finally:
            os.close(child_fd)


def _open_validated_state_root(
    state_path: Path, state: dict[str, object]
) -> tuple[Path, int, os.stat_result]:
    root = _state_tree_root(state_path, state)
    root_fd = _open_directory(root)
    try:
        root_stat = os.fstat(root_fd)
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise RuntimeValidationError("state root must have mode 0700")
        _validate_state_file_fd(root_fd)
        _validate_state_tree_fd(root_fd)
    except BaseException:
        os.close(root_fd)
        raise
    return root, root_fd, root_stat


def validate_state_tree(state_path: Path, state: dict[str, object]) -> Path:
    root, root_fd, _ = _open_validated_state_root(state_path, state)
    os.close(root_fd)
    return root


def _remove_state_tree_fd(directory_fd: int, relative: str = "") -> None:
    for entry in _scandir_fd(directory_fd):
        entry_stat = entry.stat(follow_symlinks=False)
        entry_name = f"{relative}/{entry.name}" if relative else entry.name
        if entry_stat.st_uid != os.getuid():
            raise RuntimeValidationError(
                f"state tree entry owner is not the current user: {entry_name}"
            )
        if stat.S_ISLNK(entry_stat.st_mode) or stat.S_ISREG(entry_stat.st_mode):
            try:
                os.unlink(entry.name, dir_fd=directory_fd)
            except OSError as exc:
                raise RuntimeValidationError(
                    f"could not remove state tree entry: {entry_name}"
                ) from exc
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise RuntimeValidationError(
                f"unsupported special file in state tree: {entry_name}"
            )
        child_fd = _open_child_directory(directory_fd, entry.name, entry_stat)
        try:
            _remove_state_tree_fd(child_fd, entry_name)
        finally:
            os.close(child_fd)
        try:
            os.rmdir(entry.name, dir_fd=directory_fd)
        except OSError as exc:
            raise RuntimeValidationError(
                f"could not remove state tree directory: {entry_name}"
            ) from exc


def remove_state_tree(state_path: Path, state: dict[str, object]) -> None:
    root, root_fd, root_stat = _open_validated_state_root(state_path, state)
    try:
        _remove_state_tree_fd(root_fd)
    finally:
        os.close(root_fd)
    parent_fd = _open_directory(root.parent)
    try:
        current_stat = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_stat.st_mode)
            or current_stat.st_dev != root_stat.st_dev
            or current_stat.st_ino != root_stat.st_ino
        ):
            raise RuntimeValidationError("state root changed before removal")
        os.rmdir(root.name, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeValidationError("could not remove state root") from exc
    finally:
        os.close(parent_fd)
