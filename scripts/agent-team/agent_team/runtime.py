"""Small, shared safety helpers for the agent-team launch paths."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .acp_dependencies import AcpDependencyError, AcpExecutables
from .contracts import ErrorCode, RuntimeFailure
from .locking import _LifecycleReservation
from .native_acp_dependencies import NativeAcpDependencyError, NativeAcpExecutables
from .native_controller import ControllerKeys, controller_keys
from .native_terminal import NATIVE_RUNTIMES, is_native_runtime
from .task_execution import validate_saved_tasks, validate_task_assignment
from .task_spec import parse_task_specs

if TYPE_CHECKING:
    from .contracts import NodeRef, RoleTarget
    from .named_graph import GraphSpec

ACP_ROLES: Final = frozenset({"planner", "worker", "reviewer"})
ACP_PACKAGE: Final = "acpx@0.13.2"
CLAUDE_ACP_PACKAGE: Final = "@agentclientprotocol/claude-agent-acp@0.70.0"
STATE_VERSION: Final = 3
NAMED_STATE_VERSION: Final = 4
MAX_STATE_BYTES: Final = 2_000_000
MAX_PROMPT_CHARS: Final = 100_000
MAX_PROMPT_BYTES: Final = 400_000
MAX_RESULT_BODY_CHARS: Final = 100_000
ACP_ENV_KEYS: Final = frozenset(
    {"PATH", "HOME", "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG"}
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

    Version 3 keeps the fixed ``Role`` namespace.  Version 4 stores a parsed
    graph and returns its ``NodeRef`` so the node ID and its fixed kind remain
    coupled at every caller boundary.
    """

    if not isinstance(state, Mapping) or not isinstance(node_id, str) or not node_id:
        raise RuntimeValidationError("agent-team state role identity is invalid")
    version = state.get("version")
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
    if version != NAMED_STATE_VERSION:
        raise RuntimeValidationError("agent-team state has an unsupported version")
    if state.get("runtime") == "orca":
        raise RuntimeValidationError("version-4 Orca state is not supported")
    graph = _parse_named_graph(state)
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
    required = (
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
) -> None:
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

    assignment = roles.get(result_role)
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

    pending_id = state.get("pending_delivery_id")
    if pending_id is None and any(
        field in state for field in ("pending_delivery_kind", "pending_delivery_stage")
    ):
        raise RuntimeValidationError("named native pending delivery is incomplete")
    if pending_id is not None:
        pending_kind = state.get("pending_delivery_kind")
        pending_stage = state.get("pending_delivery_stage")
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


def _validate_named_state(path: Path, state: object) -> dict[str, object]:
    if not isinstance(state, dict):
        raise RuntimeValidationError("agent-team state must be an object")
    if state.get("version") != NAMED_STATE_VERSION:
        raise RuntimeValidationError(
            "agent-team state has an unsupported named version"
        )
    runtime = state.get("runtime")
    if not is_native_runtime(runtime):
        raise RuntimeValidationError("version-4 state requires a native runtime")
    if "worktree_id" in state or "orca_socket" in state:
        raise RuntimeValidationError("native state must not contain Orca metadata")

    required = (
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
    for key in required:
        value = state.get(key)
        if key in {"graph", "role_specs", "roles"}:
            if not isinstance(value, dict):
                raise RuntimeValidationError(f"agent-team state is missing {key}")
        elif key != "version" and (not isinstance(value, str) or not value):
            raise RuntimeValidationError(f"agent-team state is missing {key}")
    if _canonical(Path(str(state["state_path"]))) != _canonical(path):
        raise RuntimeValidationError("agent-team state path does not match its file")

    graph = _parse_named_graph(state)
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

    try:
        keys = controller_keys(state)
    except (TypeError, ValueError) as exc:
        raise RuntimeValidationError(
            f"agent-team native controller identity is invalid: {exc}"
        ) from exc
    _validate_native_controller_state(state, keys)

    roles = state["roles"]
    assert isinstance(roles, dict)
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
        _validate_assignment_common(
            state,
            node_id,
            assignment,
            ownership_key="launcher_owned_runner",
        )
        assignment_spec = role_specs[node_id]
        assert isinstance(assignment_spec, Mapping)
        _validate_background_assignment(
            node_id,
            assignment_spec,
            assignment,
        )

    try:
        validate_saved_tasks(state)
    except (ValueError, RuntimeFailure) as exc:
        raise RuntimeValidationError(str(exc)) from exc

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
        raise RuntimeValidationError("named native pending completion has no result")
    if "native_result" in state:
        _validate_named_native_result(
            state,
            native_result,
            node_by_id,
            role_specs,
            roles,
        )

    from .native_questions import validate_state as validate_questions

    try:
        validate_questions(state)
    except (TypeError, ValueError) as exc:
        raise RuntimeValidationError(str(exc)) from exc
    return state


def validate_state_object(path: Path, state: object) -> dict[str, object]:
    if isinstance(state, dict) and state.get("version") == NAMED_STATE_VERSION:
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
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise RuntimeValidationError(f"agent-team is not running: {path}") from exc
    if stat.S_ISLNK(file_stat.st_mode):
        raise RuntimeValidationError(f"agent-team state must not be a symlink: {path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeValidationError(f"agent-team state must be a regular file: {path}")
    if file_stat.st_uid != os.getuid():
        raise RuntimeValidationError(
            f"agent-team state owner is not the current user: {path}"
        )
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise RuntimeValidationError(f"agent-team state must have mode 0600: {path}")
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
            raise RuntimeValidationError("agent-team state changed during open")
        payload = _read_bounded_fd(fd, MAX_STATE_BYTES)
    except OSError as exc:
        raise RuntimeValidationError(
            f"agent-team state is unavailable: {path}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
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
