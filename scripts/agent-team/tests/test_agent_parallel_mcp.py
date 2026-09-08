from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_team import mcp_protocol, native_mcp
from agent_team.contracts import TaskBatchOpen, TaskBatchReceipt
from agent_team.mcp_protocol import ToolInputError
from agent_team.runtime import MAX_PROMPT_CHARS


class _RecordingBackend:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def request(self, request: object) -> TaskBatchReceipt:
        self.requests.append(request)
        assert isinstance(request, TaskBatchOpen)
        return TaskBatchReceipt(request.task_ids, "writers", None)


def _saved_state(
    *, mode: str, dispatch_mode: str, version: int = 5
) -> dict[str, object]:
    return {
        "version": version,
        "runtime": "tmux",
        "run_id": "run-1",
        "graph": {
            "coordination": {
                "mode": mode,
                "dispatch_mode": dispatch_mode,
            }
        },
    }


class AgentParallelMcpTest(unittest.TestCase):
    def make_session(
        self, backend: _RecordingBackend, state: dict[str, object]
    ) -> native_mcp.NativeMcpSession:
        session = native_mcp.NativeMcpSession.__new__(native_mcp.NativeMcpSession)
        session.path = Path("/tmp/agent-team-mcp-state.json")
        session.run_id = state["run_id"]
        session.runtime = state["runtime"]
        session.backend = backend
        return session

    def test_batch_tool_is_advertised_only_for_agent_parallel(self) -> None:
        default_names = [tool["name"] for tool in mcp_protocol.tools()]
        serial_names = [tool["name"] for tool in mcp_protocol.tools(("worker",))]
        parallel_tools = mcp_protocol.tools(("worker",), agent_parallel=True)
        parallel_names = [tool["name"] for tool in parallel_tools]

        self.assertNotIn("task_batch_open", default_names)
        self.assertNotIn("task_batch_open", serial_names)
        self.assertEqual(
            parallel_names,
            [*serial_names, "task_batch_open"],
        )
        batch_tool = parallel_tools[-1]
        self.assertEqual(batch_tool["name"], "task_batch_open")
        self.assertEqual(
            batch_tool["inputSchema"],
            {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "uniqueItems": True,
                    }
                },
                "required": ["task_ids"],
                "additionalProperties": False,
            },
        )

    def test_agent_parallel_batch_routes_typed_request_and_wires_receipt(self) -> None:
        state = _saved_state(mode="agent", dispatch_mode="parallel")
        backend = _RecordingBackend()
        session = self.make_session(backend, state)

        with mock.patch.object(native_mcp, "read_state", return_value=state):
            result = session.execute(
                "task_batch_open", {"task_ids": ["task-a", "task-b"]}
            )

        self.assertEqual(
            result,
            {
                "task_ids": ["task-a", "task-b"],
                "phase": "writers",
                "revision": None,
            },
        )
        self.assertEqual(
            backend.requests,
            [TaskBatchOpen(("task-a", "task-b"))],
        )

    def test_batch_input_shape_is_rejected_before_backend_request(self) -> None:
        state = _saved_state(mode="agent", dispatch_mode="parallel")
        for arguments in (
            {},
            {"task_ids": ()},
            {"task_ids": []},
            {"task_ids": ["task-a", "task-a"]},
            {"task_ids": [""]},
            {"task_ids": [" "]},
            {"task_ids": [1]},
            {"task_ids": ["x" * (MAX_PROMPT_CHARS + 1)]},
            {"task_ids": ["task-a"], "extra": True},
        ):
            with self.subTest(arguments=arguments):
                backend = _RecordingBackend()
                session = self.make_session(backend, state)
                with (
                    mock.patch.object(native_mcp, "read_state", return_value=state),
                    self.assertRaises(ToolInputError),
                ):
                    session.execute("task_batch_open", arguments)
                self.assertEqual(backend.requests, [])

    def test_batch_tool_is_rejected_before_backend_request_outside_agent_parallel(
        self,
    ) -> None:
        for version, mode, dispatch_mode in (
            (5, "agent", "serial"),
            (5, "program", "parallel"),
            (4, "agent", "parallel"),
        ):
            with self.subTest(version=version, mode=mode, dispatch_mode=dispatch_mode):
                state = _saved_state(
                    version=version, mode=mode, dispatch_mode=dispatch_mode
                )
                backend = _RecordingBackend()
                session = self.make_session(backend, state)
                with (
                    mock.patch.object(native_mcp, "read_state", return_value=state),
                    self.assertRaisesRegex(ToolInputError, "agent/parallel"),
                ):
                    session.execute("task_batch_open", {"task_ids": ["task-a"]})
                self.assertEqual(backend.requests, [])


if __name__ == "__main__":
    unittest.main()
