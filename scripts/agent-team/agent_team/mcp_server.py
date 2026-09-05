from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Final, cast

from .adapters import (
    AdapterContext,
    AdapterSnapshot,
    ReadSnapshot,
    background_adapter,
    create_read_snapshot,
    remove_owned_tree,
)
from .contracts import ErrorCode, RuntimeFailure
from .orca import (
    OrcaCommandError,
    OrcaProtocolError,
    OrcaTransportError,
    _LifecycleReservation,
    orca_executable,
)
from .runtime import (
    MAX_PROMPT_CHARS,
    RuntimeValidationError,
    build_acp_agent_command,
    build_acp_runner_command,
    build_acp_session_name,
    build_background_runner_command,
    build_role_command,
)
from .runtime import (
    create_prompt_file as runtime_create_prompt_file,
)
from .runtime import (
    read_state as runtime_read_state,
)
from .runtime import (
    remove_prompt_file as runtime_remove_prompt_file,
)
from .runtime import (
    write_state as runtime_write_state,
)

ROLES: Final = ("planner", "worker", "reviewer")
StateGeneration = tuple[str, str, str, str, str, str, str]
MAX_REPLY_CHARS: Final = 20_000
MIN_TIMEOUT_MS: Final = 1_000
MAX_TIMEOUT_MS: Final = 900_000
MAX_READ_LINES: Final = 2_000


class ToolInputError(ValueError):
    pass


def role_schema() -> dict[str, object]:
    return {"type": "string", "enum": list(ROLES)}


def tools() -> list[dict[str, object]]:
    role_only = {
        "type": "object",
        "properties": {"role": role_schema()},
        "required": ["role"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "role_get",
            "description": "Orcaで監督中の1 roleのTaskとDispatch状態を取得します。",
            "inputSchema": role_only,
        },
        {
            "name": "role_prompt",
            "description": "1 role用のOrca Taskを作り、専用terminalをsupervised Dispatchとして起動します。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role": role_schema(),
                    "text": {"type": "string", "minLength": 1},
                },
                "required": ["role", "text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "role_wait",
            "description": "指定roleを含むOrca Runの完了、質問、escalation通知を待ちます。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role": role_schema(),
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": MIN_TIMEOUT_MS,
                        "maximum": MAX_TIMEOUT_MS,
                        "default": 300_000,
                    },
                },
                "required": ["role"],
                "additionalProperties": False,
            },
        },
        {
            "name": "role_read",
            "description": "指定roleのOrca worker出力を読みます。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role": role_schema(),
                    "lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_READ_LINES,
                        "default": 400,
                    },
                },
                "required": ["role"],
                "additionalProperties": False,
            },
        },
        {
            "name": "role_release",
            "description": "完了確認済みのOrca worker terminalをarchive後に解放します。",
            "inputSchema": role_only,
        },
        {
            "name": "delivery_ack",
            "description": "処理済みのOrca Delivery全体をacknowledgeします。",
            "inputSchema": {
                "type": "object",
                "properties": {"delivery_id": {"type": "string", "minLength": 1}},
                "required": ["delivery_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "message_reply",
            "description": "Orca workerから届いたquestion messageへ回答します。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "minLength": 1},
                    "body": {"type": "string", "minLength": 1},
                },
                "required": ["message_id", "body"],
                "additionalProperties": False,
            },
        },
    ]


def require_role(arguments: dict[str, object]) -> str:
    role = arguments.get("role")
    if not isinstance(role, str) or role not in ROLES:
        raise ToolInputError(f"role must be one of: {', '.join(ROLES)}")
    return role


def bounded_integer(
    arguments: dict[str, object],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolInputError(f"{key} must be an integer")
    if value < minimum or value > maximum:
        raise ToolInputError(f"{key} must be between {minimum} and {maximum}")
    return value


def bounded_text(arguments: dict[str, object], key: str, *, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{key} must be a non-empty string")
    if len(value) > maximum:
        raise ToolInputError(f"{key} must be at most {maximum} characters")
    return value


def state_path() -> Path:
    raw_path = os.environ.get("AGENT_TEAM_STATE_PATH")
    if not raw_path:
        raise ToolInputError("AGENT_TEAM_STATE_PATH is required")
    return Path(raw_path)


def load_state() -> tuple[Path, dict[str, object]]:
    path = state_path()
    try:
        return path, runtime_read_state(path)
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def _state_generation(state: dict[str, object]) -> StateGeneration:
    generation: dict[str, str] = {}
    for key in ("team_id", "run_id", "worktree_id", "main_terminal"):
        value = state.get(key)
        if not isinstance(value, str) or not value:
            raise ToolInputError(f"agent-team state is missing {key}")
        generation[key] = value
    for key in ("workspace", "config_path", "state_path"):
        value = state.get(key)
        if not isinstance(value, str) or not value:
            raise ToolInputError(f"agent-team state is missing {key}")
        generation[key] = str(Path(value).expanduser().resolve(strict=False))
    return (
        generation["team_id"],
        generation["workspace"],
        generation["config_path"],
        generation["state_path"],
        generation["run_id"],
        generation["worktree_id"],
        generation["main_terminal"],
    )


def _assert_state_generation(path: Path, expected: StateGeneration) -> None:
    try:
        current = runtime_read_state(path)
    except RuntimeValidationError as exc:
        if str(exc).startswith("agent-team is not running:"):
            raise ToolInputError(
                "agent-team state disappeared during operation"
            ) from exc
        raise ToolInputError("agent-team state is invalid during operation") from exc
    if _state_generation(current) != expected:
        raise ToolInputError("agent-team state generation changed during operation")


def _reservation_error(exc: RuntimeFailure) -> ToolInputError:
    message = (
        "agent-team state disappeared before save"
        if exc.code is ErrorCode.TEAM_NOT_RUNNING
        else "agent-team state reservation is unavailable"
    )
    return ToolInputError(message)


def save_state(
    path: Path,
    state: dict[str, object],
    *,
    expected_generation: StateGeneration | None = None,
    reservation_held: bool = False,
) -> None:
    if reservation_held:
        if expected_generation is not None:
            _assert_state_generation(path, expected_generation)
        try:
            runtime_write_state(
                path, state, require_existing=True, reservation_held=True
            )
        except RuntimeValidationError as exc:
            raise ToolInputError(str(exc)) from exc
        return

    reservation = _LifecycleReservation(path, create_parent=False)
    try:
        reservation.acquire()
    except RuntimeFailure as exc:
        raise _reservation_error(exc) from exc
    try:
        if expected_generation is not None:
            _assert_state_generation(path, expected_generation)
        runtime_write_state(path, state, require_existing=True, reservation_held=True)
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc
    finally:
        reservation.release()


def require_state_string(state: dict[str, object], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        raise ToolInputError(f"agent-team state is missing {key}")
    return value


def role_assignment(state: dict[str, object], role: str) -> dict[str, object]:
    roles = state.get("roles")
    assignment = roles.get(role) if isinstance(roles, dict) else None
    if not isinstance(assignment, dict):
        raise ToolInputError(f"role has no active Orca Dispatch: {role}")
    return assignment


def run_orca(
    state: dict[str, object], args: list[str], *, timeout_ms: int = 30_000
) -> dict[str, object]:
    workspace = Path(require_state_string(state, "workspace"))
    executable = orca_executable()
    try:
        result = subprocess.run(
            [executable, *args],
            check=False,
            capture_output=True,
            cwd=workspace,
            text=True,
            timeout=timeout_ms / 1_000,
        )
    except Exception as exc:
        raise OrcaTransportError() from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OrcaProtocolError() from exc
    returncode = result.returncode
    if not isinstance(payload, dict):
        raise OrcaProtocolError()
    if returncode != 0 or payload.get("ok") is not True:
        raise OrcaCommandError()
    response = payload.get("result")
    if not isinstance(response, dict):
        raise OrcaProtocolError()
    return response


def require_nested_string(
    payload: dict[str, object], keys: tuple[str, ...], context: str
) -> str:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise RuntimeError(f"{context} response is missing {'.'.join(keys)}")
        current = current[key]
    if not isinstance(current, str) or not current:
        raise RuntimeError(f"{context} response has invalid {'.'.join(keys)}")
    return current


def _observed_value(
    payload: dict[str, object], paths: tuple[tuple[str, ...], ...]
) -> object:
    for path in paths:
        current: object = payload
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            return current
    return None


def validate_dispatch_response(
    response: dict[str, object],
    *,
    task_id: str,
    terminal_handle: str,
    run_id: str,
    context: str,
) -> str:
    dispatch_id = require_nested_string(response, ("dispatchId",), context)
    checks = (
        (("taskId",), ("task_id",), task_id),
        (("assigneeHandle",), ("assignee_handle",), terminal_handle),
        (("assignee", "handle"), ("assignee", "terminalHandle"), terminal_handle),
        (("runId",), ("run_id",), run_id),
        (("run", "id"), ("run",), run_id),
    )
    for first, second, expected in checks:
        observed = _observed_value(response, (first, second))
        if observed is not None and observed != expected:
            raise RuntimeError(
                f"{context} response identity does not match {first[-1]}"
            )
    return dispatch_id


def validate_acp_dispatch_response(
    response: dict[str, object],
    *,
    task_id: str,
    terminal_handle: str,
    run_id: str,
    context: str,
) -> str:
    if response.get("injected") is not False:
        raise RuntimeError(f"{context} response must have injected=false")
    dispatch = response.get("dispatch")
    if not isinstance(dispatch, dict):
        raise TypeError(f"{context} response is missing dispatch")
    dispatch_id = require_nested_string(dispatch, ("id",), context)
    for key, expected in (
        ("task_id", task_id),
        ("assignee_handle", terminal_handle),
        ("run_id", run_id),
    ):
        observed = require_nested_string(dispatch, (key,), context)
        if observed != expected:
            raise RuntimeError(f"{context} response identity does not match {key}")
    return dispatch_id


def worker_done_sender(message: dict[str, object]) -> str | None:
    for key in ("from_handle", "fromHandle", "from", "senderHandle", "sender"):
        sender = message.get(key)
        if isinstance(sender, str) and sender:
            return sender
    payload = message.get("payload")
    if isinstance(payload, dict):
        for key in ("from_handle", "fromHandle", "from", "senderHandle", "sender"):
            sender = payload.get(key)
            if isinstance(sender, str) and sender:
                return sender
    return None


def role_command(state: dict[str, object], role: str) -> str:
    try:
        return build_role_command(
            require_state_string(state, "launcher_path"),
            role,
            require_state_string(state, "config_path"),
            require_state_string(state, "workspace"),
            require_state_string(state, "orca_socket"),
        )
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def role_transport(state: dict[str, object], role: str) -> str:
    spec = role_spec(state, role)
    transport = spec.get("transport")
    if transport not in {"direct", "acp"}:
        raise ToolInputError(f"role {role} has an unsupported transport")
    return str(transport)


def role_execution(state: dict[str, object], role: str) -> str:
    execution = role_spec(state, role).get("execution")
    if execution not in {"tui_direct", "background"}:
        raise ToolInputError(f"role {role} has an unsupported execution")
    return str(execution)


def acp_agent_command(team_id: str, role: str, launch_nonce: str) -> str:
    try:
        return build_acp_agent_command(team_id, role, launch_nonce)
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def acp_session_name(role: str, launch_nonce: str) -> str:
    try:
        return build_acp_session_name(role, launch_nonce)
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def create_prompt_file(
    state_dir: Path, role: str, launch_nonce: str, text: str
) -> Path:
    try:
        return runtime_create_prompt_file(state_dir, role, launch_nonce, text)
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def remove_prompt_file(
    path: Path,
    state_dir: Path,
    *,
    role: str,
    launch_nonce: str,
) -> None:
    try:
        runtime_remove_prompt_file(
            path, state_dir, role=role, launch_nonce=launch_nonce
        )
    except RuntimeValidationError as exc:
        raise RuntimeError(str(exc)) from exc


def acp_runner_command(
    state: dict[str, object],
    role: str,
    *,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    prompt_path: Path,
    launch_nonce: str,
) -> str:
    try:
        return build_acp_runner_command(
            state,
            role,
            task_id=task_id,
            dispatch_id=dispatch_id,
            terminal_handle=terminal_handle,
            prompt_path=prompt_path,
            launch_nonce=launch_nonce,
        )
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def background_runner_command(
    state: dict[str, object],
    role: str,
    *,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    prompt_path: Path,
    launch_nonce: str,
) -> str:
    try:
        return build_background_runner_command(
            state,
            role,
            task_id=task_id,
            dispatch_id=dispatch_id,
            terminal_handle=terminal_handle,
            prompt_path=prompt_path,
            launch_nonce=launch_nonce,
        )
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def _adapter_snapshot_dict(snapshot: AdapterSnapshot) -> dict[str, object]:
    return {
        "adapter_id": snapshot.adapter_id,
        "revision": snapshot.revision,
        "executable": str(snapshot.executable),
        "version": snapshot.version,
        "identity": {
            "device": snapshot.identity.device,
            "inode": snapshot.identity.inode,
            "size": snapshot.identity.size,
            "mtime_ns": snapshot.identity.mtime_ns,
            "sha256": snapshot.identity.sha256,
        },
    }


def cleanup_background_resources(
    assignment: dict[str, object], state_path: Path, *, role: str
) -> None:
    """Clean only the exact resources recorded for one background turn."""

    raw_prompt = assignment.get("prompt_path")
    nonce = assignment.get("launch_nonce")
    if not isinstance(raw_prompt, str) or not isinstance(nonce, str):
        raise ToolInputError("background assignment is missing prompt cleanup identity")
    prompt = Path(raw_prompt)
    if prompt.exists() or prompt.is_symlink():
        remove_prompt_file(prompt, state_path.parent, role=role, launch_nonce=nonce)
    state_root = state_path.parent.resolve(strict=False)
    for key in ("provider_private_root", "snapshot_root"):
        raw_root = assignment.get(key)
        if not isinstance(raw_root, str) or not raw_root:
            raise ToolInputError(f"background assignment is missing {key}")
        root = Path(raw_root).resolve(strict=False)
        try:
            root.relative_to(state_root)
        except ValueError:
            pass
        else:
            raise ToolInputError(f"background {key} must stay outside agent-team state")
        remove_owned_tree(root)


def role_spec(state: dict[str, object], role: str) -> dict[str, object]:
    specs = state.get("role_specs")
    spec = specs.get(role) if isinstance(specs, dict) else None
    if not isinstance(spec, dict):
        raise ToolInputError(f"agent-team state is missing role spec: {role}")
    return spec


def agent_ready(preview: str, spec: dict[str, object]) -> bool:
    provider = spec.get("provider")
    model = spec.get("model")
    effort = spec.get("effort")
    if (
        not isinstance(provider, str)
        or not isinstance(model, str)
        or not isinstance(effort, str)
    ):
        raise ToolInputError("agent-team role spec is invalid")
    normalized = " ".join(preview.lower().split())
    if re.search(r"model\s*:\s*loading", normalized):
        return False
    if provider == "codex":
        footer = re.compile(
            rf"{re.escape(model.lower())}\s+{re.escape(effort.lower())}\s+·\s+~?/"
        )
        return footer.search(normalized) is not None
    if provider == "claude":
        footer = re.compile(
            rf"{re.escape(model.lower())}(?:\s+\d+)?\s+with\s+"
            rf"{re.escape(effort.lower())}\s+effort\s+·"
        )
        return footer.search(normalized) is not None
    return False


def wait_for_agent_ready(
    state: dict[str, object], role: str, terminal_handle: str, *, timeout_ms: int
) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    spec = role_spec(state, role)
    while True:
        shown = run_orca(
            state,
            ["terminal", "show", "--terminal", terminal_handle, "--json"],
        )
        terminal = shown.get("terminal")
        preview = terminal.get("preview") if isinstance(terminal, dict) else None
        if isinstance(preview, str) and agent_ready(preview, spec):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{role} agent did not finish loading before dispatch")
        time.sleep(0.25)


def _create_task(state: dict[str, object], role: str, text: str) -> str:
    task = run_orca(
        state,
        [
            "orchestration",
            "task-create",
            "--spec",
            text,
            "--task-title",
            f"{require_state_string(state, 'team_id')}-{role}",
            "--display-name",
            f"team-{role}",
            "--run",
            require_state_string(state, "run_id"),
            "--from",
            require_state_string(state, "main_terminal"),
            "--json",
        ],
    )
    return require_nested_string(task, ("task", "id"), "task-create")


def _mark_task_failed(state: dict[str, object], task_id: str, result: str) -> None:
    run_orca(
        state,
        [
            "orchestration",
            "task-update",
            "--id",
            task_id,
            "--status",
            "failed",
            "--result",
            result,
            "--run",
            require_state_string(state, "run_id"),
            "--from",
            require_state_string(state, "main_terminal"),
            "--json",
        ],
    )


def start_background_role(
    path: Path,
    state: dict[str, object],
    role: str,
    text: str,
    *,
    expected_generation: StateGeneration | None = None,
    reservation_held: bool = False,
) -> dict[str, object]:
    """Start a fixed, non-TUI provider on a private turn snapshot."""

    roles = state.get("roles")
    if not isinstance(roles, dict):
        raise ToolInputError("agent-team state has invalid roles")
    spec = role_spec(state, role)
    if spec.get("execution") != "background":
        raise ToolInputError(f"role {role} is not a background role")
    provider = spec.get("provider")
    adapter_id = spec.get("adapter_id")
    if not isinstance(provider, str) or not isinstance(adapter_id, str):
        raise ToolInputError(f"background role {role} has invalid adapter identity")
    if provider != "copilot":
        raise ToolInputError(f"background provider is not enabled: {provider}")
    model = spec.get("model")
    effort = spec.get("effort")
    instructions = spec.get("instructions")
    if not all(
        isinstance(value, str) and value for value in (model, effort, instructions)
    ):
        raise ToolInputError(f"background role {role} has an invalid role spec")
    model = cast(str, model)
    effort = cast(str, effort)
    instructions = cast(str, instructions)
    workspace = Path(require_state_string(state, "workspace"))
    team_id = require_state_string(state, "team_id")
    worktree_id = require_state_string(state, "worktree_id")
    run_id = require_state_string(state, "run_id")
    main_terminal = require_state_string(state, "main_terminal")
    launch_nonce = secrets.token_hex(16)
    private_root = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
    snapshot: ReadSnapshot | None = None
    prompt_path: Path | None = None
    task_id: str | None = None
    terminal_handle: str | None = None
    dispatch_id: str | None = None
    persisted = False
    cleanup_errors: list[str] = []
    try:
        # These checks intentionally precede task creation so a bad provider
        # cannot leave an Orca task that has no executable background runner.
        preflight_context = AdapterContext(
            provider=provider,
            role=role,
            model=model,
            effort=effort,
            workspace=workspace,
            private_root=private_root,
        )
        adapter = background_adapter(adapter_id)
        adapter_snapshot = adapter.preflight(preflight_context)
        snapshot = create_read_snapshot(workspace, state_root=path.parent)
        task_id = _create_task(state, role, text)
        combined_prompt = f"{instructions}\n\n{text}"
        prompt_path = create_prompt_file(
            path.parent, role, launch_nonce, combined_prompt
        )
        terminal = run_orca(
            state,
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                f"{team_id}-{role}",
                "--json",
            ],
        )
        terminal_handle = require_nested_string(
            terminal, ("terminal", "handle"), "terminal create"
        )
        dispatch = run_orca(
            state,
            [
                "orchestration",
                "dispatch",
                "--task",
                task_id,
                "--to",
                terminal_handle,
                "--run",
                run_id,
                "--from",
                main_terminal,
                "--json",
            ],
        )
        try:
            dispatch_id = validate_acp_dispatch_response(
                dispatch,
                task_id=task_id,
                terminal_handle=terminal_handle,
                run_id=run_id,
                context="dispatch",
            )
        except (RuntimeError, TypeError):
            dispatch_record = dispatch.get("dispatch")
            candidate = (
                dispatch_record.get("id") if isinstance(dispatch_record, dict) else None
            )
            if isinstance(candidate, str) and candidate:
                dispatch_id = candidate
            raise
        assignment: dict[str, object] = {
            "task_id": task_id,
            "dispatch_id": dispatch_id,
            "terminal_handle": terminal_handle,
            "completion_observed": False,
            "launcher_owned_terminal": True,
            "execution": "background",
            "adapter_id": adapter_id,
            "launch_nonce": launch_nonce,
            "prompt_path": str(prompt_path),
            "provider_private_root": str(private_root),
            "snapshot_root": str(snapshot.root),
            "adapter_snapshot": _adapter_snapshot_dict(adapter_snapshot),
        }
        roles[role] = assignment
        save_state(
            path,
            state,
            expected_generation=expected_generation,
            reservation_held=reservation_held,
        )
        persisted = True
        run_orca(
            state,
            [
                "terminal",
                "send",
                "--terminal",
                terminal_handle,
                "--text",
                background_runner_command(
                    state,
                    role,
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    terminal_handle=terminal_handle,
                    prompt_path=prompt_path,
                    launch_nonce=launch_nonce,
                ),
                "--enter",
                "--json",
            ],
        )
        return assignment
    except BaseException as exc:
        roles.pop(role, None)
        if persisted:
            try:
                save_state(
                    path,
                    state,
                    expected_generation=expected_generation,
                    reservation_held=reservation_held,
                )
            except (OSError, TypeError, ValueError) as cleanup_error:
                cleanup_errors.append(f"state rollback failed: {cleanup_error}")
        if dispatch_id is not None:
            try:
                run_orca(
                    state,
                    [
                        "orchestration",
                        "worker-stop",
                        "--dispatch",
                        dispatch_id,
                        "--json",
                    ],
                )
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"Dispatch cleanup failed: {cleanup_error}")
        if terminal_handle is not None:
            try:
                run_orca(
                    state,
                    [
                        "terminal",
                        "close",
                        "--terminal",
                        terminal_handle,
                        "--tab",
                        "--json",
                    ],
                )
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"terminal cleanup failed: {cleanup_error}")
        if prompt_path is not None:
            try:
                remove_prompt_file(
                    prompt_path, path.parent, role=role, launch_nonce=launch_nonce
                )
            except (RuntimeError, ToolInputError) as cleanup_error:
                cleanup_errors.append(f"prompt cleanup failed: {cleanup_error}")
        if snapshot is not None:
            try:
                snapshot.cleanup()
            except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
                cleanup_errors.append(f"snapshot cleanup failed: {cleanup_error}")
        try:
            remove_owned_tree(private_root)
        except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
            cleanup_errors.append(f"provider cleanup failed: {cleanup_error}")
        if task_id is not None:
            try:
                _mark_task_failed(
                    state, task_id, "agent-team background role startup failed"
                )
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"task cleanup failed: {cleanup_error}")
        if cleanup_errors:
            raise RuntimeError(
                f"background role startup failed: {exc}; cleanup also failed: "
                + "; ".join(cleanup_errors)
            ) from exc
        raise


def start_acp_role(
    path: Path,
    state: dict[str, object],
    role: str,
    text: str,
    *,
    expected_generation: StateGeneration | None = None,
    reservation_held: bool = False,
) -> dict[str, object]:
    """Create a tracked bare-shell ACP dispatch and then deliver its runner."""

    roles = state.get("roles")
    if not isinstance(roles, dict):
        raise ToolInputError("agent-team state has invalid roles")
    run_id = require_state_string(state, "run_id")
    main_terminal = require_state_string(state, "main_terminal")
    team_id = require_state_string(state, "team_id")
    worktree_id = require_state_string(state, "worktree_id")
    task_id = _create_task(state, role, text)
    launch_nonce = secrets.token_hex(16)
    prompt_path: Path | None = None
    provider_private_root = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
    snapshot_root = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
    terminal_handle: str | None = None
    dispatch_id: str | None = None
    persisted = False
    cleanup_errors: list[str] = []
    try:
        prompt_path = create_prompt_file(Path(path).parent, role, launch_nonce, text)
        terminal = run_orca(
            state,
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                f"{team_id}-{role}",
                "--json",
            ],
        )
        terminal_handle = require_nested_string(
            terminal, ("terminal", "handle"), "terminal create"
        )
        dispatch = run_orca(
            state,
            [
                "orchestration",
                "dispatch",
                "--task",
                task_id,
                "--to",
                terminal_handle,
                "--run",
                run_id,
                "--from",
                main_terminal,
                "--json",
            ],
        )
        try:
            dispatch_id = validate_acp_dispatch_response(
                dispatch,
                task_id=task_id,
                terminal_handle=terminal_handle,
                run_id=run_id,
                context="dispatch",
            )
        except (RuntimeError, TypeError):
            dispatch_record = dispatch.get("dispatch")
            candidate = (
                dispatch_record.get("id") if isinstance(dispatch_record, dict) else None
            )
            if isinstance(candidate, str) and candidate:
                dispatch_id = candidate
            raise
        agent_command = acp_agent_command(team_id, role, launch_nonce)
        assignment: dict[str, object] = {
            "task_id": task_id,
            "dispatch_id": dispatch_id,
            "terminal_handle": terminal_handle,
            "completion_observed": False,
            "launcher_owned_terminal": True,
            "prompt_path": str(prompt_path),
            "launch_nonce": launch_nonce,
            "agent_command": agent_command,
            "session_name": acp_session_name(role, launch_nonce),
            "execution": "background",
            "adapter_id": role_spec(state, role).get("adapter_id"),
            "provider_private_root": str(provider_private_root),
            "snapshot_root": str(snapshot_root),
            "adapter_snapshot": {
                "adapter_id": role_spec(state, role).get("adapter_id"),
                "revision": "acpx@0.13.2",
                "executable": "npx",
                "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
                "identity": {
                    "device": 0,
                    "inode": 0,
                    "size": 0,
                    "mtime_ns": 0,
                    "sha256": "acpx-managed",
                },
            },
        }
        roles[role] = assignment
        save_state(
            path,
            state,
            expected_generation=expected_generation,
            reservation_held=reservation_held,
        )
        persisted = True
        run_orca(
            state,
            [
                "terminal",
                "send",
                "--terminal",
                terminal_handle,
                "--text",
                acp_runner_command(
                    state,
                    role,
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    terminal_handle=terminal_handle,
                    prompt_path=prompt_path,
                    launch_nonce=launch_nonce,
                ),
                "--enter",
                "--json",
            ],
        )
        return assignment
    except BaseException as exc:
        roles.pop(role, None)
        if persisted:
            try:
                save_state(
                    path,
                    state,
                    expected_generation=expected_generation,
                    reservation_held=reservation_held,
                )
            except (OSError, TypeError, ValueError) as cleanup_error:
                cleanup_errors.append(f"state rollback failed: {cleanup_error}")
        if dispatch_id is not None:
            try:
                run_orca(
                    state,
                    [
                        "orchestration",
                        "worker-stop",
                        "--dispatch",
                        dispatch_id,
                        "--json",
                    ],
                )
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"Dispatch cleanup failed: {cleanup_error}")
        if terminal_handle is not None:
            try:
                run_orca(
                    state,
                    [
                        "terminal",
                        "close",
                        "--terminal",
                        terminal_handle,
                        "--tab",
                        "--json",
                    ],
                )
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"terminal cleanup failed: {cleanup_error}")
        if prompt_path is not None:
            try:
                remove_prompt_file(
                    prompt_path,
                    path.parent,
                    role=role,
                    launch_nonce=launch_nonce,
                )
            except (RuntimeError, ToolInputError) as cleanup_error:
                cleanup_errors.append(f"prompt cleanup failed: {cleanup_error}")
        for root in (provider_private_root, snapshot_root):
            try:
                remove_owned_tree(root)
            except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
                cleanup_errors.append(
                    f"background resource cleanup failed: {cleanup_error}"
                )
        try:
            _mark_task_failed(state, task_id, "agent-team ACP role startup failed")
        except (
            RuntimeError,
            TypeError,
            OSError,
            subprocess.TimeoutExpired,
        ) as cleanup_error:
            cleanup_errors.append(f"task cleanup failed: {cleanup_error}")
        if cleanup_errors:
            raise RuntimeError(
                f"ACP role startup failed: {exc}; cleanup also failed: "
                + "; ".join(cleanup_errors)
            ) from exc
        raise


def start_role(
    path: Path,
    state: dict[str, object],
    role: str,
    text: str,
    *,
    expected_generation: StateGeneration | None = None,
    reservation_held: bool = False,
) -> dict[str, object]:
    roles = state.get("roles")
    if not isinstance(roles, dict):
        raise ToolInputError("agent-team state has invalid roles")
    if roles:
        raise ToolInputError(
            "another role already has an active Orca Dispatch; "
            "agent-team processes one role at a time"
        )
    if state.get("pending_delivery_id") is not None:
        raise ToolInputError(
            "acknowledge the pending Orca Delivery before starting a role"
        )
    if role_execution(state, role) == "background":
        if role_spec(state, role).get("provider") == "claude":
            return start_acp_role(
                path,
                state,
                role,
                text,
                expected_generation=expected_generation,
                reservation_held=reservation_held,
            )
        return start_background_role(
            path,
            state,
            role,
            text,
            expected_generation=expected_generation,
            reservation_held=reservation_held,
        )
    if role_transport(state, role) == "acp":
        return start_acp_role(
            path,
            state,
            role,
            text,
            expected_generation=expected_generation,
            reservation_held=reservation_held,
        )
    run_id = require_state_string(state, "run_id")
    main_terminal = require_state_string(state, "main_terminal")
    team_id = require_state_string(state, "team_id")
    worktree_id = require_state_string(state, "worktree_id")
    task_id = _create_task(state, role, text)
    terminal_handle: str | None = None
    dispatch_id: str | None = None
    try:
        terminal = run_orca(
            state,
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                f"{team_id}-{role}",
                "--command",
                role_command(state, role),
                "--json",
            ],
        )
        terminal_handle = require_nested_string(
            terminal, ("terminal", "handle"), "terminal create"
        )
        startup_deadline = time.monotonic() + 180
        run_orca(
            state,
            [
                "terminal",
                "wait",
                "--terminal",
                terminal_handle,
                "--for",
                "tui-idle",
                "--timeout-ms",
                "180000",
                "--json",
            ],
            timeout_ms=185_000,
        )
        remaining_ms = max(0, int((startup_deadline - time.monotonic()) * 1_000))
        if remaining_ms < MIN_TIMEOUT_MS:
            raise RuntimeError(f"{role} agent did not finish loading before dispatch")
        wait_for_agent_ready(state, role, terminal_handle, timeout_ms=remaining_ms)
        worker = run_orca(
            state,
            [
                "orchestration",
                "worker-start",
                "--task",
                task_id,
                "--terminal",
                terminal_handle,
                "--run",
                run_id,
                "--from",
                main_terminal,
                "--timeout-ms",
                "60000",
                "--json",
            ],
            timeout_ms=65_000,
        )
        try:
            dispatch_id = validate_dispatch_response(
                worker,
                task_id=task_id,
                terminal_handle=terminal_handle,
                run_id=run_id,
                context="worker-start",
            )
        except (RuntimeError, TypeError):
            candidate = worker.get("dispatchId")
            if isinstance(candidate, str) and candidate:
                dispatch_id = candidate
            raise
        assignment: dict[str, object] = {
            "task_id": task_id,
            "dispatch_id": dispatch_id,
            "terminal_handle": terminal_handle,
            "completion_observed": False,
            "launcher_owned_terminal": True,
        }
        roles[role] = assignment
        save_state(
            path,
            state,
            expected_generation=expected_generation,
            reservation_held=reservation_held,
        )
    except BaseException as start_error:
        roles.pop(role, None)
        cleanup_errors: list[str] = []
        dispatch_stopped = False
        if dispatch_id is not None:
            try:
                run_orca(
                    state,
                    [
                        "orchestration",
                        "worker-stop",
                        "--dispatch",
                        dispatch_id,
                        "--json",
                    ],
                )
                dispatch_stopped = True
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"Dispatch cleanup failed: {cleanup_error}")
        if terminal_handle is not None and (
            dispatch_id is None or not dispatch_stopped
        ):
            try:
                run_orca(
                    state,
                    [
                        "terminal",
                        "close",
                        "--terminal",
                        terminal_handle,
                        "--tab",
                        "--json",
                    ],
                )
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"terminal cleanup failed: {cleanup_error}")
        try:
            _mark_task_failed(state, task_id, "agent-team role startup failed")
        except (
            RuntimeError,
            TypeError,
            OSError,
            subprocess.TimeoutExpired,
        ) as cleanup_error:
            cleanup_errors.append(f"task cleanup failed: {cleanup_error}")
        if cleanup_errors:
            raise RuntimeError(
                f"role startup failed: {start_error}; cleanup also failed: "
                + "; ".join(cleanup_errors)
            ) from start_error
        raise
    return assignment


def execute_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
    path = state_path()
    reservation = _LifecycleReservation(path, create_parent=False)
    try:
        reservation.acquire()
    except RuntimeFailure as exc:
        raise _reservation_error(exc) from exc
    try:
        path, state = load_state()
        generation = _state_generation(state)
        _assert_state_generation(path, generation)
        return _execute_tool_locked(name, arguments, path, state, generation)
    finally:
        reservation.release()


def _execute_tool_locked(
    name: str,
    arguments: dict[str, object],
    path: Path,
    state: dict[str, object],
    generation: StateGeneration,
) -> dict[str, object]:
    if name == "delivery_ack":
        delivery_id = bounded_text(arguments, "delivery_id", maximum=256)
        pending_delivery_id = state.get("pending_delivery_id")
        if pending_delivery_id != delivery_id:
            raise ToolInputError(
                "delivery_id does not match the observed pending Delivery"
            )
        result = run_orca(
            state,
            [
                "orchestration",
                "check",
                "--terminal",
                require_state_string(state, "main_terminal"),
                "--run",
                require_state_string(state, "run_id"),
                "--ack",
                delivery_id,
                "--peek",
                "--json",
            ],
        )
        state.pop("pending_delivery_id", None)
        save_state(
            path,
            state,
            expected_generation=generation,
            reservation_held=True,
        )
        return result
    if name == "message_reply":
        message_id = bounded_text(arguments, "message_id", maximum=256)
        body = bounded_text(arguments, "body", maximum=MAX_REPLY_CHARS)
        result = run_orca(
            state,
            [
                "orchestration",
                "reply",
                "--id",
                message_id,
                "--body",
                body,
                "--run",
                require_state_string(state, "run_id"),
                "--from",
                require_state_string(state, "main_terminal"),
                "--json",
            ],
        )
        _assert_state_generation(path, generation)
        return result

    role = require_role(arguments)
    if name == "role_prompt":
        text = bounded_text(arguments, "text", maximum=MAX_PROMPT_CHARS)
        return start_role(
            path,
            state,
            role,
            text,
            expected_generation=generation,
            reservation_held=True,
        )
    assignment = role_assignment(state, role)
    dispatch_id = require_nested_string(assignment, ("dispatch_id",), "role assignment")
    if name == "role_get":
        return run_orca(
            state,
            [
                "orchestration",
                "worker-show",
                "--dispatch",
                dispatch_id,
                "--json",
            ],
        )
    if name == "role_wait":
        if state.get("pending_delivery_id") is not None:
            raise ToolInputError(
                "acknowledge the pending Orca Delivery before waiting again"
            )
        timeout_ms = bounded_integer(
            arguments,
            "timeout_ms",
            default=300_000,
            minimum=MIN_TIMEOUT_MS,
            maximum=MAX_TIMEOUT_MS,
        )
        wait_args = [
            "orchestration",
            "check",
            "--terminal",
            require_state_string(state, "main_terminal"),
            "--run",
            require_state_string(state, "run_id"),
            "--wait",
            "--types",
            "worker_done,escalation,question",
            "--timeout-ms",
            str(timeout_ms),
            "--json",
        ]
        result = run_orca(
            state,
            wait_args,
            timeout_ms=timeout_ms + 5_000,
        )
        observed_delivery_id = result.get("deliveryId")
        if isinstance(observed_delivery_id, str) and observed_delivery_id:
            state["pending_delivery_id"] = observed_delivery_id
        messages = result.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if (
                    not isinstance(message, dict)
                    or message.get("type") != "worker_done"
                ):
                    continue
                payload = message.get("payload")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(payload, dict):
                    continue
                if (
                    payload.get("taskId") == assignment.get("task_id")
                    and payload.get("dispatchId") == dispatch_id
                    and payload.get("outcome") in {"succeeded", "failed"}
                    and worker_done_sender(message) == assignment.get("terminal_handle")
                ):
                    assignment["completion_observed"] = True
                    assignment["outcome"] = payload["outcome"]
        if isinstance(observed_delivery_id, str) and observed_delivery_id:
            save_state(
                path,
                state,
                expected_generation=generation,
                reservation_held=True,
            )
        return result
    if name == "role_read":
        if assignment.get("completion_observed") is not True:
            raise ToolInputError(
                "role_read requires an observed worker_done for this Dispatch"
            )
        lines = bounded_integer(
            arguments,
            "lines",
            default=400,
            minimum=1,
            maximum=MAX_READ_LINES,
        )
        return run_orca(
            state,
            [
                "orchestration",
                "worker-read",
                "--dispatch",
                dispatch_id,
                "--limit",
                str(lines),
                "--json",
            ],
        )
    if name == "role_release":
        if assignment.get("completion_observed") is not True:
            raise ToolInputError(
                "role_release requires an observed worker_done for this Dispatch"
            )
        if assignment.get("launcher_owned_terminal") is not True:
            raise ToolInputError(
                "role release refuses a terminal whose ownership is unknown"
            )
        transport = role_transport(state, role)
        execution = role_execution(state, role)
        released = run_orca(
            state,
            [
                "orchestration",
                "worker-release",
                "--dispatch",
                dispatch_id,
                "--json",
            ],
        )
        release_state = released.get("state")
        if release_state not in {"retained", "released", "already_released"}:
            raise RuntimeError(f"worker was not released: {release_state or 'unknown'}")
        terminal_handle = require_nested_string(
            assignment, ("terminal_handle",), "role assignment"
        )
        if (
            execution == "background"
            or transport == "acp"
            or release_state == "retained"
        ):
            run_orca(
                state,
                [
                    "terminal",
                    "close",
                    "--terminal",
                    terminal_handle,
                    "--tab",
                    "--json",
                ],
            )
        if execution == "background":
            cleanup_background_resources(
                assignment,
                Path(require_state_string(state, "state_path")),
                role=role,
            )
        elif transport == "acp":
            raw_prompt = assignment.get("prompt_path")
            nonce = assignment.get("launch_nonce")
            if not isinstance(raw_prompt, str) or not isinstance(nonce, str):
                raise ToolInputError(
                    "ACP assignment is missing prompt cleanup identity"
                )
            remove_prompt_file(
                Path(raw_prompt),
                Path(require_state_string(state, "state_path")).parent,
                role=role,
                launch_nonce=nonce,
            )
        roles = state.get("roles")
        if not isinstance(roles, dict):
            raise ToolInputError("agent-team state has invalid roles")
        del roles[role]
        save_state(
            path,
            state,
            expected_generation=generation,
            reservation_held=True,
        )
        return released
    raise ToolInputError(f"unknown tool: {name}")


def tool_result(text: str, *, is_error: bool) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, arguments: object) -> dict[str, object]:
    if not isinstance(arguments, dict):
        return tool_result("arguments must be an object", is_error=True)
    try:
        result = execute_tool(name, arguments)
    except (
        ToolInputError,
        RuntimeError,
        TypeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        return tool_result(str(exc)[:4_000], is_error=True)
    return tool_result(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        is_error=False,
    )


def success(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle(request: object) -> dict[str, object] | None:
    if not isinstance(request, dict):
        return error(None, -32600, "request must be an object")
    request_id = request.get("id")
    method = request.get("method")
    if not isinstance(method, str):
        return error(request_id, -32600, "method must be a string")
    if request_id is None:
        return None
    if method == "initialize":
        params = request.get("params")
        protocol_version = "2025-06-18"
        if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str):
            protocol_version = params["protocolVersion"]
        return success(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-team", "version": "2.0.0"},
            },
        )
    if method == "tools/list":
        return success(request_id, {"tools": tools()})
    if method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return error(request_id, -32602, "tools/call requires a tool name")
        return success(
            request_id, call_tool(params["name"], params.get("arguments", {}))
        )
    return error(request_id, -32601, f"unknown method: {method}")


def emit(response: dict[str, object]) -> None:
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle(request)
        except json.JSONDecodeError:
            response = error(None, -32700, "invalid JSON")
        if response is not None:
            emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
