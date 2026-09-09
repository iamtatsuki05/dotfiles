"""Map Main's MCP tools to the selected typed runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import cast

from .contracts import (
    DeliveryAck,
    DeliveryRef,
    ErrorCode,
    MessageRef,
    MessageReply,
    Role,
    RoleGet,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    RuntimeRequest,
    TaskBatchOpen,
    TaskDispatch,
    TaskGet,
    TaskVerify,
    _OpaqueRef,
    role_kind,
)
from .mcp_protocol import (
    MAX_READ_LINES,
    MAX_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    ToolInputError,
    bounded_integer,
    bounded_text,
    require_role,
)
from .native_terminal import is_native_runtime
from .runtime import (
    MAX_PROMPT_CHARS,
    NAMED_STATE_VERSION,
    PARALLEL_STATE_VERSION,
    read_state,
    resolve_state_role,
)
from .task_spec import TaskSpec


def _agent_parallel_state(state: Mapping[str, object]) -> bool:
    graph = state.get("graph")
    if not isinstance(graph, Mapping):
        return False
    coordination = graph.get("coordination")
    return (
        state.get("version") == PARALLEL_STATE_VERSION
        and isinstance(coordination, Mapping)
        and coordination.get("mode") == "agent"
        and coordination.get("dispatch_mode") == "parallel"
    )


def _task_batch_ids(arguments: dict[str, object]) -> tuple[str, ...]:
    if set(arguments) != {"task_ids"}:
        raise ToolInputError("task_batch_open requires exactly task_ids")
    raw_ids = arguments["task_ids"]
    if not isinstance(raw_ids, list):
        raise ToolInputError("task_ids must be a list")
    if not raw_ids:
        raise ToolInputError("task_ids must not be empty")
    task_ids: list[str] = []
    for index, task_id in enumerate(raw_ids):
        if not isinstance(task_id, str) or not task_id.strip():
            raise ToolInputError(f"task_ids[{index}] must be a non-empty string")
        if len(task_id) > MAX_PROMPT_CHARS:
            raise ToolInputError(
                f"task_ids[{index}] must be at most {MAX_PROMPT_CHARS} characters"
            )
        task_ids.append(task_id)
    if len(set(task_ids)) != len(task_ids):
        raise ToolInputError("task_ids must not contain duplicates")
    return tuple(task_ids)


class RuntimeMcpSession:
    def __init__(self, path: Path, state: dict[str, object]) -> None:
        from .cli import _management_plan_from_state, _runtime_engine, _start_spec

        runtime = state.get("runtime")
        if not is_native_runtime(runtime) and not (
            runtime == "orca"
            and state.get("version") in {NAMED_STATE_VERSION, PARALLEL_STATE_VERSION}
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "MCP requires a supported typed runtime"
            )
        self.path = path.resolve()
        self.run_id = state["run_id"]
        self.runtime = runtime
        plan = _management_plan_from_state(state)
        _engine, self.backend = _runtime_engine(plan, resume_existing=True)
        self.backend.start(_start_spec(plan, attach=False))

    def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        current = read_state(self.path)
        if current["runtime"] != self.runtime or current["run_id"] != self.run_id:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "MCP run identity changed"
            )
        request: RuntimeRequest
        if name == "task_batch_open":
            if not _agent_parallel_state(current):
                raise ToolInputError(
                    "task_batch_open is supported only in agent/parallel mode"
                )
            request = TaskBatchOpen(_task_batch_ids(arguments))
        elif name in {"task_get", "task_verify"}:
            if set(arguments) != {"task_id"}:
                raise ToolInputError(f"{name} requires exactly task_id")
            task_id = bounded_text(arguments, "task_id", maximum=MAX_PROMPT_CHARS)
            request = TaskGet(task_id) if name == "task_get" else TaskVerify(task_id)
        elif name == "delivery_ack":
            request = DeliveryAck(
                DeliveryRef(bounded_text(arguments, "delivery_id", maximum=256))
            )
        elif name == "message_reply":
            if set(arguments) != {"message_id", "body"}:
                raise ToolInputError(
                    "message_reply requires exactly message_id and body"
                )
            if self.runtime == "orca":
                from .native_question_channel import MAX_FRAME_BYTES

                maximum_body = MAX_FRAME_BYTES
            else:
                maximum_body = 20_000
            request = MessageReply(
                MessageRef(bounded_text(arguments, "message_id", maximum=256)),
                bounded_text(arguments, "body", maximum=maximum_body),
            )
        else:
            if current["version"] in {NAMED_STATE_VERSION, PARALLEL_STATE_VERSION}:
                role = resolve_state_role(
                    current, bounded_text(arguments, "role", maximum=64)
                )
                if role_kind(role) is Role.MAIN:
                    raise ToolInputError("Main is not a dispatchable node")
            else:
                role = Role(require_role(arguments))
            if name == "role_get":
                request = RoleGet(role)
            elif name == "task_dispatch":
                if set(arguments) != {"role", "task", "message"}:
                    raise ToolInputError(
                        "task_dispatch requires exactly role, task, and message"
                    )
                request = TaskDispatch(
                    role,
                    TaskSpec.from_dict(arguments.get("task")),
                    bounded_text(arguments, "message", maximum=MAX_PROMPT_CHARS),
                )
            elif name == "role_prompt":
                request = RolePrompt(
                    role, bounded_text(arguments, "text", maximum=MAX_PROMPT_CHARS)
                )
            elif name == "role_wait":
                request = RoleWait(
                    role,
                    bounded_integer(
                        arguments,
                        "timeout_ms",
                        default=300_000,
                        minimum=MIN_TIMEOUT_MS,
                        maximum=MAX_TIMEOUT_MS,
                    ),
                )
            elif name == "role_read":
                request = RoleRead(
                    role,
                    bounded_integer(
                        arguments,
                        "lines",
                        default=400,
                        minimum=1,
                        maximum=MAX_READ_LINES,
                    ),
                )
            elif name == "role_release":
                request = RoleRelease(role)
            else:
                raise ToolInputError(f"unknown tool: {name}")
        result = _wire(self.backend.request(request))
        if not isinstance(result, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "runtime tool receipt is not an object",
            )
        return cast(dict[str, object], result)


def _wire(value: object) -> object:
    if isinstance(value, _OpaqueRef):
        return value._value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _wire(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise RuntimeFailure(
        ErrorCode.BACKEND_PROTOCOL_FAILURE, "unsupported runtime tool receipt"
    )


_session: RuntimeMcpSession | None = None


def execute_tool(
    name: str, arguments: dict[str, object], path: Path
) -> dict[str, object]:
    global _session
    path = path.resolve()
    state = read_state(path)
    if _session is None:
        _session = RuntimeMcpSession(path, state)
    elif _session.path != path or _session.run_id != state["run_id"]:
        raise RuntimeFailure(ErrorCode.IDENTITY_MISMATCH, "MCP state identity changed")
    return _session.execute(name, arguments)
