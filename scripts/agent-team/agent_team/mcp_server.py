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

from .acp_dependencies import AcpDependencyError, AcpExecutables
from .adapters import (
    AdapterContext,
    AdapterSnapshot,
    ReadSnapshot,
    background_adapter,
    create_read_snapshot,
    remove_owned_tree,
)
from .contracts import ErrorCode, RuntimeFailure
from .harness_launch import build_snapshot_role_command
from .locking import _LifecycleReservation
from .orca import (
    OrcaClient,
    OrcaCommandError,
    OrcaProtocolError,
    OrcaTransportError,
    orca_executable,
)
from .runtime import (
    MAX_PROMPT_CHARS,
    RuntimeValidationError,
    build_acp_agent_command,
    build_acp_runner_command,
    build_acp_session_name,
    build_background_runner_command,
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
DELIVERY_MESSAGE_TYPES: Final = frozenset({"worker_done", "question", "escalation"})
PENDING_DELIVERY_KIND: Final = "pending_delivery_kind"
PENDING_DELIVERY_STAGE: Final = "pending_delivery_stage"
PENDING_QUESTION_IDS: Final = "pending_question_ids"
REPLIED_QUESTION_IDS: Final = "replied_question_ids"


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


def _message_payload(message: dict[str, object]) -> dict[str, object] | None:
    payload = message.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _question_message_id(message: dict[str, object]) -> str | None:
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        return message_id
    return None


def _message_matches_assignment(
    message: dict[str, object],
    payload: dict[str, object] | None,
    assignment: dict[str, object],
) -> bool:
    terminal_handle = assignment.get("terminal_handle")
    if not isinstance(terminal_handle, str):
        return False
    if worker_done_sender(message) != terminal_handle:
        return False
    if payload is None:
        return True
    for key, assignment_key in (
        ("taskId", "task_id"),
        ("dispatchId", "dispatch_id"),
    ):
        observed = payload.get(key)
        expected = assignment.get(assignment_key)
        if observed is not None and observed != expected:
            return False
    return True


def _question_matches_dispatch(
    message: dict[str, object],
    payload: dict[str, object] | None,
    assignment: dict[str, object],
    dispatch_id: str,
) -> bool:
    if message.get("from_handle") != f"dispatch:{dispatch_id}":
        return False
    if payload is None:
        return False
    return (
        payload.get("taskId") == assignment.get("task_id")
        and payload.get("dispatchId") == dispatch_id
    )


def _escalation_matches_dispatch(
    message: dict[str, object],
    payload: dict[str, object] | None,
    assignment: dict[str, object],
    dispatch_id: str,
) -> bool:
    terminal_handle = assignment.get("terminal_handle")
    if not isinstance(terminal_handle, str):
        return False
    if message.get("from_handle") not in {
        terminal_handle,
        f"dispatch:{dispatch_id}",
    }:
        return False
    if payload is None:
        return False
    return (
        payload.get("taskId") == assignment.get("task_id")
        and payload.get("dispatchId") == dispatch_id
    )


def _validate_question_reply(
    result: dict[str, object],
    *,
    message_id: str,
    body: str,
    run_id: str,
    dispatch_id: str,
) -> None:
    if not isinstance(result.get("duplicate"), bool):
        raise TypeError("Orca question reply was missing an answer receipt")
    message = result.get("message")
    question = result.get("question")
    if not isinstance(message, dict) or not isinstance(question, dict):
        raise TypeError("Orca question reply response was invalid")
    answer_message_id = require_nested_string(message, ("id",), "question reply")
    if require_nested_string(message, ("thread_id",), "question reply") != message_id:
        raise RuntimeError("Orca question reply thread does not match the question")
    if require_nested_string(message, ("run_id",), "question reply") != run_id:
        raise RuntimeError("Orca question reply Run does not match the team")
    if message.get("body") != body:
        raise RuntimeError("Orca question reply body does not match the request")
    if require_nested_string(question, ("message_id",), "question reply") != message_id:
        raise RuntimeError("Orca answered question does not match the request")
    if require_nested_string(question, ("run_id",), "question reply") != run_id:
        raise RuntimeError("Orca answered question Run does not match the team")
    if (
        require_nested_string(question, ("dispatch_id",), "question reply")
        != dispatch_id
    ):
        raise RuntimeError("Orca answered question Dispatch does not match the role")
    if question.get("status") != "answered":
        raise RuntimeError("Orca question reply did not answer the question")
    if (
        require_nested_string(question, ("answer_message_id",), "question reply")
        != answer_message_id
    ):
        raise RuntimeError("Orca question answer message does not match the receipt")
    if question.get("answer_body") != body:
        raise RuntimeError("Orca question answer body does not match the request")


def _validate_worker_read_response(
    result: dict[str, object], *, dispatch_id: str, terminal_handle: str
) -> None:
    if result.get("dispatchId") != dispatch_id:
        raise RuntimeError("worker-read response Dispatch does not match the role")
    source_value = result.get("source")
    if source_value not in {"terminal", "transcript"}:
        raise TypeError("worker-read response has an invalid output source")
    source_identity = result.get("sourceIdentity")
    if not isinstance(source_identity, str) or not source_identity:
        raise TypeError("worker-read response is missing output source identity")
    output = result.get(source_value)
    if not isinstance(output, dict):
        raise TypeError("worker-read response is missing structured output")
    if source_value == "terminal":
        if output.get("handle") != terminal_handle:
            raise RuntimeError("worker-read terminal handle does not match the role")
        tail = output.get("tail")
        if not isinstance(tail, list) or any(
            not isinstance(line, str) for line in tail
        ):
            raise TypeError("worker-read terminal output is invalid")
    else:
        messages = output.get("messages")
        if not isinstance(messages, list):
            raise TypeError("worker-read transcript output is invalid")


def _validate_terminal_close_response(
    result: dict[str, object], *, terminal_handle: str
) -> None:
    close = result.get("close")
    if not isinstance(close, dict):
        raise TypeError("terminal close response is missing a close receipt")
    if close.get("handle") != terminal_handle:
        raise RuntimeError("terminal close handle does not match the role")
    tab_id = close.get("tabId")
    if not isinstance(tab_id, str) or not tab_id:
        raise TypeError("terminal close response is missing the closed tab")
    if close.get("closeMode") != "tab":
        raise RuntimeError("terminal close response did not close the requested tab")
    if not isinstance(close.get("ptyKilled"), bool):
        raise TypeError("terminal close response has an invalid process receipt")


def _validate_worker_stop_response(
    result: dict[str, object], *, dispatch_id: str, terminal_handle: str | None
) -> bool:
    if not isinstance(terminal_handle, str) or not terminal_handle:
        raise RuntimeError(
            "worker-stop response cannot be verified without terminal identity"
        )
    if result.get("dispatchId") != dispatch_id:
        raise RuntimeError("worker-stop response Dispatch does not match the role")
    state = result.get("state")
    process_action = result.get("processAction")
    already_settled = result.get("alreadySettled")
    if not isinstance(state, str) or not state:
        raise TypeError("worker-stop response is missing its state")
    if not isinstance(process_action, str) or not process_action:
        raise TypeError("worker-stop response is missing its process receipt")
    if state == "stop_unknown":
        if (
            process_action not in {"none", "unknown", "closed_agent_terminal"}
            or already_settled is not False
            or "close" in result
        ):
            raise RuntimeError("worker-stop response is invalid")
        raise RuntimeError("worker-stop did not confirm the stop outcome")
    if process_action == "closed_agent_terminal":
        if state != "stopped" or not isinstance(already_settled, bool):
            raise RuntimeError("worker-stop response is invalid")
        close = result.get("close")
        if (
            not isinstance(close, dict)
            or close.get("handle") != terminal_handle
            or close.get("ptyKilled") is not True
        ):
            raise RuntimeError("worker-stop response has no process stop receipt")
        return True
    if process_action != "none" or "close" in result:
        raise RuntimeError("worker-stop response is invalid")
    if not isinstance(already_settled, bool):
        raise TypeError("worker-stop response is missing its settlement receipt")
    if state == "stopped" and not already_settled:
        return False
    if (
        state
        in {
            "succeeded",
            "failed",
            "stopped",
            "abandoned",
            "completed",
            "circuit_broken",
        }
        and already_settled
    ):
        raise RuntimeError("settled Dispatch has no confirmed process stop receipt")
    raise RuntimeError("worker-stop response is invalid")


def _rollback_owned_dispatch(
    state: dict[str, object], *, dispatch_id: str, terminal_handle: str
) -> None:
    stopped = run_orca(
        state,
        ["orchestration", "worker-stop", "--dispatch", dispatch_id, "--json"],
    )
    process_stopped = _validate_worker_stop_response(
        stopped, dispatch_id=dispatch_id, terminal_handle=terminal_handle
    )
    if process_stopped:
        return
    closed = run_orca(
        state,
        ["terminal", "close", "--terminal", terminal_handle, "--json"],
    )
    verdict = OrcaClient._decode_terminal_close(closed, terminal_id=terminal_handle)
    if verdict.close_mode is not None or not verdict.pty_killed:
        raise RuntimeError("terminal rollback did not confirm process stop")


def _delivery_kind(result: dict[str, object]) -> str:
    messages = result.get("messages")
    if not isinstance(messages, list):
        return "unknown"
    kinds = {
        message.get("type")
        for message in messages
        if isinstance(message, dict)
        and isinstance(message.get("type"), str)
        and message.get("type") in DELIVERY_MESSAGE_TYPES
    }
    if len(kinds) == 1:
        return cast(str, next(iter(kinds)))
    return "unknown"


def _stage_invalid_delivery(
    state: dict[str, object], delivery_id: str, kind: str
) -> None:
    state["pending_delivery_id"] = delivery_id
    state[PENDING_DELIVERY_KIND] = kind
    state[PENDING_DELIVERY_STAGE] = "invalid"
    state[PENDING_QUESTION_IDS] = []
    state[REPLIED_QUESTION_IDS] = []


def _clear_pending_delivery(state: dict[str, object]) -> None:
    state.pop("pending_delivery_id", None)
    state.pop(PENDING_DELIVERY_KIND, None)
    state.pop(PENDING_DELIVERY_STAGE, None)
    state.pop(PENDING_QUESTION_IDS, None)
    state.pop(REPLIED_QUESTION_IDS, None)


def role_command(state: dict[str, object], role: str) -> str:
    try:
        return build_snapshot_role_command(state, role)
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


def acp_agent_command(
    team_id: str,
    role: str,
    launch_nonce: str,
    *,
    executables: AcpExecutables,
) -> str:
    try:
        return build_acp_agent_command(
            team_id, role, launch_nonce, executables=executables
        )
    except RuntimeValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def _saved_acp_executables(spec: dict[str, object]) -> AcpExecutables:
    try:
        executables = AcpExecutables.from_dict(spec.get("acp_executables"))
        executables.verify()
    except AcpDependencyError as exc:
        raise ToolInputError(str(exc)) from exc
    return executables


def _acp_adapter_snapshot(executables: AcpExecutables) -> dict[str, object]:
    identity = executables.client.stat()
    return {
        "adapter_id": "claude-acp-0.70.0",
        "revision": "acpx@0.13.2",
        "executable": str(executables.client),
        "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
        "identity": {
            "device": identity.st_dev,
            "inode": identity.st_ino,
            "size": identity.st_size,
            "mtime_ns": identity.st_mtime_ns,
            "sha256": executables.client_sha256,
        },
    }


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


def _retain_failed_role_start(
    path: Path,
    state: dict[str, object],
    role: str,
    known_resources: dict[str, object],
    *,
    expected_generation: StateGeneration | None,
    reservation_held: bool,
) -> None:
    roles = state.get("roles")
    if not isinstance(roles, dict):
        raise ToolInputError("agent-team state has invalid roles")
    if role not in roles:
        state["pending_role_start"] = {
            "role": role,
            "reason": "role startup rollback could not confirm cleanup",
            **{
                key: str(value) if isinstance(value, Path) else value
                for key, value in known_resources.items()
                if value is not None
            },
        }
    save_state(
        path,
        state,
        expected_generation=expected_generation,
        reservation_held=reservation_held,
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
    terminal_creation_attempted = False
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
        terminal_creation_attempted = True
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
        dispatch_id = validate_acp_dispatch_response(
            dispatch,
            task_id=task_id,
            terminal_handle=terminal_handle,
            run_id=run_id,
            context="dispatch",
        )
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
        local_cleanup_allowed = not terminal_creation_attempted
        if dispatch_id is not None and terminal_handle is not None:
            try:
                _rollback_owned_dispatch(
                    state, dispatch_id=dispatch_id, terminal_handle=terminal_handle
                )
                local_cleanup_allowed = True
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"Dispatch rollback failed: {cleanup_error}")
        if local_cleanup_allowed:
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
        if local_cleanup_allowed and not cleanup_errors:
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
        if not local_cleanup_allowed or cleanup_errors:
            try:
                _retain_failed_role_start(
                    path,
                    state,
                    role,
                    {
                        "task_id": task_id,
                        "dispatch_id": dispatch_id,
                        "terminal_handle": terminal_handle,
                        "launch_nonce": launch_nonce,
                        "prompt_path": prompt_path,
                        "provider_private_root": private_root,
                        "terminal_creation_attempted": terminal_creation_attempted,
                        "snapshot_root": snapshot.root
                        if snapshot is not None
                        else None,
                    },
                    expected_generation=expected_generation,
                    reservation_held=reservation_held,
                )
            except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
                cleanup_errors.append(
                    f"ownership evidence save failed: {cleanup_error}"
                )
            if not local_cleanup_allowed:
                cleanup_errors.append("role resources retained; cleanup is unconfirmed")
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
    spec = role_spec(state, role)
    if (
        spec.get("transport") != "acp"
        or spec.get("provider") != "claude"
        or spec.get("execution") != "background"
        or spec.get("adapter_id") != "claude-acp-0.70.0"
        or spec.get("permission") != "read-only"
    ):
        raise ToolInputError(
            "ACP role does not satisfy the Claude read-only capability"
        )
    executables = _saved_acp_executables(spec)
    launch_nonce = secrets.token_hex(16)
    prompt_path: Path | None = None
    task_id: str | None = None
    provider_private_root: Path | None = None
    snapshot_root: Path | None = None
    terminal_handle: str | None = None
    dispatch_id: str | None = None
    persisted = False
    terminal_creation_attempted = False
    cleanup_errors: list[str] = []
    try:
        agent_command = acp_agent_command(
            team_id,
            role,
            launch_nonce,
            executables=executables,
        )
        task_id = _create_task(state, role, text)
        provider_private_root = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
        snapshot_root = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
        prompt_path = create_prompt_file(Path(path).parent, role, launch_nonce, text)
        terminal_creation_attempted = True
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
        dispatch_id = validate_acp_dispatch_response(
            dispatch,
            task_id=task_id,
            terminal_handle=terminal_handle,
            run_id=run_id,
            context="dispatch",
        )
        if task_id is None or provider_private_root is None or snapshot_root is None:
            raise RuntimeError("ACP role startup did not allocate its private roots")
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
            "adapter_id": spec.get("adapter_id"),
            "provider_private_root": str(provider_private_root),
            "snapshot_root": str(snapshot_root),
            "adapter_snapshot": _acp_adapter_snapshot(executables),
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
        local_cleanup_allowed = not terminal_creation_attempted
        if dispatch_id is not None and terminal_handle is not None:
            try:
                _rollback_owned_dispatch(
                    state, dispatch_id=dispatch_id, terminal_handle=terminal_handle
                )
                local_cleanup_allowed = True
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"Dispatch rollback failed: {cleanup_error}")
        if local_cleanup_allowed:
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
                if root is None:
                    continue
                try:
                    remove_owned_tree(root)
                except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
                    cleanup_errors.append(
                        f"background resource cleanup failed: {cleanup_error}"
                    )
        if task_id is not None:
            try:
                _mark_task_failed(state, task_id, "agent-team ACP role startup failed")
            except (
                RuntimeError,
                TypeError,
                OSError,
                subprocess.TimeoutExpired,
            ) as cleanup_error:
                cleanup_errors.append(f"task cleanup failed: {cleanup_error}")
        if local_cleanup_allowed and not cleanup_errors:
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
        if not local_cleanup_allowed or cleanup_errors:
            try:
                _retain_failed_role_start(
                    path,
                    state,
                    role,
                    {
                        "task_id": task_id,
                        "dispatch_id": dispatch_id,
                        "terminal_handle": terminal_handle,
                        "launch_nonce": launch_nonce,
                        "prompt_path": prompt_path,
                        "provider_private_root": provider_private_root,
                        "terminal_creation_attempted": terminal_creation_attempted,
                        "snapshot_root": snapshot_root,
                    },
                    expected_generation=expected_generation,
                    reservation_held=reservation_held,
                )
            except (RuntimeError, OSError, TypeError, ValueError) as cleanup_error:
                cleanup_errors.append(
                    f"ownership evidence save failed: {cleanup_error}"
                )
            if not local_cleanup_allowed:
                cleanup_errors.append("role resources retained; cleanup is unconfirmed")
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
    if "pending_role_start" in state:
        raise ToolInputError(
            "role startup cleanup is pending; inspect the retained ownership evidence"
        )
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


def _observe_delivery(
    result: dict[str, object],
    assignment: dict[str, object],
    dispatch_id: str,
) -> tuple[str, list[str]] | None:
    messages = result.get("messages")
    if not isinstance(messages, list):
        return None
    if any(
        not isinstance(message, dict)
        or not isinstance(message.get("type"), str)
        or message.get("type") not in DELIVERY_MESSAGE_TYPES
        for message in messages
    ):
        raise ToolInputError("an Orca Delivery contains an unknown message")
    lifecycle_messages = [
        message
        for message in messages
        if isinstance(message, dict)
        and isinstance(message.get("type"), str)
        and message.get("type") in DELIVERY_MESSAGE_TYPES
    ]
    if not lifecycle_messages:
        return None
    kinds = {message.get("type") for message in lifecycle_messages}
    if len(kinds) != 1:
        raise ToolInputError(
            "an Orca Delivery cannot mix worker_done, question, and escalation"
        )
    kind = next(iter(kinds))
    if kind == "worker_done":
        if len(lifecycle_messages) != 1:
            raise ToolInputError("an Orca Delivery contains duplicate completions")
        matching: list[tuple[dict[str, object], dict[str, object]]] = []
        for message in lifecycle_messages:
            payload = _message_payload(message)
            if (
                payload is not None
                and payload.get("taskId") == assignment.get("task_id")
                and payload.get("dispatchId") == dispatch_id
                and isinstance(payload.get("outcome"), str)
                and payload.get("outcome") in {"succeeded", "failed"}
                and _message_matches_assignment(message, payload, assignment)
            ):
                matching.append((message, payload))
        if len(matching) != 1:
            return None
        assignment["completion_observed"] = True
        assignment["outcome"] = matching[0][1]["outcome"]
        return "worker_done", []
    if kind == "question":
        question_ids: list[str] = []
        for message in lifecycle_messages:
            payload = _message_payload(message)
            if not _question_matches_dispatch(
                message, payload, assignment, dispatch_id
            ):
                return None
            message_id = _question_message_id(message)
            if message_id is None:
                raise ToolInputError("question message is missing an id")
            if message_id in question_ids:
                raise ToolInputError("an Orca Delivery contains duplicate questions")
            question_ids.append(message_id)
        return "question", question_ids
    if len(lifecycle_messages) != 1:
        raise ToolInputError("an Orca Delivery contains duplicate escalations")
    message = lifecycle_messages[0]
    if not _escalation_matches_dispatch(
        message, _message_payload(message), assignment, dispatch_id
    ):
        return None
    return "escalation", []


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
        delivery_kind = state.get(PENDING_DELIVERY_KIND)
        delivery_stage = state.get(PENDING_DELIVERY_STAGE)
        if delivery_kind == "worker_done":
            if delivery_stage != "released":
                raise ToolInputError(
                    "read and release the completed role before acknowledging its Delivery"
                )
        elif delivery_kind == "question":
            question_ids = state.get(PENDING_QUESTION_IDS)
            replied_ids = state.get(REPLIED_QUESTION_IDS)
            if delivery_stage != "observed" or (
                not isinstance(question_ids, list)
                or not isinstance(replied_ids, list)
                or any(not isinstance(item, str) for item in question_ids)
                or any(not isinstance(item, str) for item in replied_ids)
                or not set(question_ids).issubset(replied_ids)
            ):
                raise ToolInputError(
                    "reply to every question before acknowledging its Delivery"
                )
        elif delivery_kind == "escalation":
            raise ToolInputError("escalation must remain pending for user review")
        else:
            raise ToolInputError("Delivery has no observed lifecycle event")
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
        if result.get("acknowledged") != delivery_id:
            raise RuntimeError("Orca did not acknowledge the requested Delivery")
        _clear_pending_delivery(state)
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
        pending_delivery_id = state.get("pending_delivery_id")
        if (
            not isinstance(pending_delivery_id, str)
            or not pending_delivery_id
            or state.get(PENDING_DELIVERY_KIND) != "question"
            or state.get(PENDING_DELIVERY_STAGE) != "observed"
        ):
            raise ToolInputError("message does not match a pending question")
        question_ids = state.get(PENDING_QUESTION_IDS)
        replied_ids = state.get(REPLIED_QUESTION_IDS)
        if (
            not isinstance(question_ids, list)
            or not isinstance(replied_ids, list)
            or any(not isinstance(item, str) for item in question_ids)
            or any(not isinstance(item, str) for item in replied_ids)
        ):
            raise ToolInputError("pending question state is invalid")
        if message_id not in question_ids:
            raise ToolInputError("message does not match a pending question")
        if message_id in replied_ids:
            raise ToolInputError("question message has already been replied to")
        roles = state.get("roles")
        if not isinstance(roles, dict) or len(roles) != 1:
            raise ToolInputError("question reply requires one active role assignment")
        assignment = next(iter(roles.values()))
        if not isinstance(assignment, dict):
            raise ToolInputError("question reply requires an active role assignment")
        dispatch_id = require_nested_string(
            assignment, ("dispatch_id",), "role assignment"
        )
        run_id = require_state_string(state, "run_id")
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
        _validate_question_reply(
            result,
            message_id=message_id,
            body=body,
            run_id=run_id,
            dispatch_id=dispatch_id,
        )
        _assert_state_generation(path, generation)
        state[REPLIED_QUESTION_IDS] = [*replied_ids, message_id]
        save_state(
            path,
            state,
            expected_generation=generation,
            reservation_held=True,
        )
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
        raw_delivery_id = result.get("deliveryId")
        if raw_delivery_id is None:
            messages = result.get("messages")
            if isinstance(messages, list) and messages:
                raise ToolInputError("Orca Delivery is missing a valid deliveryId")
            return result
        if not isinstance(raw_delivery_id, str) or not raw_delivery_id:
            raise ToolInputError("Orca Delivery has an invalid deliveryId")
        delivery_kind = _delivery_kind(result)
        try:
            observed = _observe_delivery(result, assignment, dispatch_id)
        except ToolInputError:
            _stage_invalid_delivery(state, raw_delivery_id, delivery_kind)
            save_state(
                path,
                state,
                expected_generation=generation,
                reservation_held=True,
            )
            raise
        if observed is None:
            _stage_invalid_delivery(state, raw_delivery_id, delivery_kind)
            save_state(
                path,
                state,
                expected_generation=generation,
                reservation_held=True,
            )
            raise ToolInputError(
                "Orca Delivery could not be validated for the assigned Dispatch"
            )
        observed_kind, question_ids = observed
        state["pending_delivery_id"] = raw_delivery_id
        state[PENDING_DELIVERY_KIND] = observed_kind
        state[PENDING_DELIVERY_STAGE] = "observed"
        state[PENDING_QUESTION_IDS] = question_ids
        state[REPLIED_QUESTION_IDS] = []
        save_state(
            path,
            state,
            expected_generation=generation,
            reservation_held=True,
        )
        return result
    if name == "role_read":
        if (
            assignment.get("completion_observed") is not True
            or not isinstance(state.get("pending_delivery_id"), str)
            or state.get(PENDING_DELIVERY_KIND) != "worker_done"
            or state.get(PENDING_DELIVERY_STAGE) != "observed"
        ):
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
        result = run_orca(
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
        _validate_worker_read_response(
            result,
            dispatch_id=dispatch_id,
            terminal_handle=require_nested_string(
                assignment, ("terminal_handle",), "role assignment"
            ),
        )
        state[PENDING_DELIVERY_STAGE] = "read"
        save_state(
            path,
            state,
            expected_generation=generation,
            reservation_held=True,
        )
        return result
    if name == "role_release":
        if (
            assignment.get("completion_observed") is not True
            or not isinstance(state.get("pending_delivery_id"), str)
            or state.get(PENDING_DELIVERY_KIND) != "worker_done"
            or state.get(PENDING_DELIVERY_STAGE) != "read"
        ):
            raise ToolInputError(
                "role_release requires a successful role_read after worker_done"
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
        if released.get("dispatchId") != dispatch_id:
            raise RuntimeError("worker release response Dispatch does not match")
        release_state = released.get("state")
        if release_state not in {"retained", "released", "already_released"}:
            raise RuntimeError(f"worker was not released: {release_state or 'unknown'}")
        process_action = released.get("processAction")
        if release_state == "released" and process_action not in {
            "none",
            "closed_exited_terminal",
            "closed_agent_terminal",
        }:
            raise RuntimeError("worker release response has no process receipt")
        if release_state == "already_released" and process_action != "none":
            raise RuntimeError("already released worker has an invalid process receipt")
        if release_state == "retained":
            if released.get("processAction") != "none":
                raise RuntimeError(
                    "retained worker release has an invalid process action"
                )
            reason = released.get("reason")
            if reason in {
                "federation_unsupported",
                "external_terminal",
                "identity_unproven",
                "no_owned_resource",
                "ownership_transferred",
                "user_requested",
                "user_takeover",
            }:
                raise RuntimeError(f"worker terminal release retained: {reason}")
            raise RuntimeError(
                "worker terminal release retained; inspect the assigned terminal"
            )
        terminal_handle = require_nested_string(
            assignment, ("terminal_handle",), "role assignment"
        )
        if execution == "background" or transport == "acp":
            closed = run_orca(
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
            _validate_terminal_close_response(closed, terminal_handle=terminal_handle)
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
        state[PENDING_DELIVERY_STAGE] = "released"
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
