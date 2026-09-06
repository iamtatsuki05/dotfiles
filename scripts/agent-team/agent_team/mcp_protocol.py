"""MCP schemas and stdio framing without a backend dependency."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import Final

from .contracts import RuntimeFailure
from .task_spec import task_schema

ExecuteTool = Callable[[str, dict[str, object]], dict[str, object]]
ROLES: Final = ("planner", "worker", "reviewer")
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
    task_only = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "minLength": 1}},
        "required": ["task_id"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "task_get",
            "description": "タスクの工程、レビュー判定、検証証拠を取得します。",
            "inputSchema": task_only,
        },
        {
            "name": "task_verify",
            "description": "実装レビュー承認後、同じコードの版に宣言済みの固定argvで検証を実行します。全件成功した場合だけタスクを完了にします。",
            "inputSchema": task_only,
        },
        {
            "name": "task_dispatch",
            "description": "構造化TaskSpecを保存し、指定roleへ割り当てます。依存未完了や工程順序違反は起動前に拒否します。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role": role_schema(),
                    "task": task_schema(),
                    "message": {"type": "string", "minLength": 1},
                },
                "required": ["role", "task", "message"],
                "additionalProperties": False,
            },
        },
        {
            "name": "role_get",
            "description": "1 roleのTaskとDispatch状態を取得します。",
            "inputSchema": role_only,
        },
        {
            "name": "role_prompt",
            "description": "1 role用のTaskを作り、構成された実行方式でroleを起動します。",
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
            "description": "指定roleの完了、質問、escalation通知を待ちます。",
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
            "description": "指定roleの出力を読みます。",
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
            "description": "結果を読み終えたroleの所有資源を解放します。",
            "inputSchema": role_only,
        },
        {
            "name": "delivery_ack",
            "description": "処理済みのDelivery全体をacknowledgeします。",
            "inputSchema": {
                "type": "object",
                "properties": {"delivery_id": {"type": "string", "minLength": 1}},
                "required": ["delivery_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "message_reply",
            "description": "roleから届いたquestion messageへ回答します。",
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


def tool_result(text: str, *, is_error: bool) -> dict[str, object]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(
    name: str, arguments: object, execute_tool: ExecuteTool
) -> dict[str, object]:
    if not isinstance(arguments, dict):
        return tool_result("arguments must be an object", is_error=True)
    try:
        result = execute_tool(name, arguments)
    except (
        ValueError,
        RuntimeFailure,
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


def handle(request: object, execute_tool: ExecuteTool) -> dict[str, object] | None:
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
            request_id,
            call_tool(params["name"], params.get("arguments", {}), execute_tool),
        )
    return error(request_id, -32601, f"unknown method: {method}")


def emit(response: dict[str, object]) -> None:
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)


def serve(execute_tool: ExecuteTool) -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ToolInputError):
            emit(error(None, -32700, "invalid JSON"))
            continue
        response = handle(request, execute_tool)
        if response is not None:
            emit(response)
    return 0


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ToolInputError("duplicate JSON object key")
        result[key] = value
    return result
