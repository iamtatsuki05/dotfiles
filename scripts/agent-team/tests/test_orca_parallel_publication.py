from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_orca_parallel_state import _state

from agent_team import cli, orca_acp, runtime_mcp
from agent_team.contracts import RuntimeFailure, TaskGet, TaskStatusReceipt
from agent_team.runtime import read_state, write_state


class OrcaParallelPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = _state(self.root)
        self.path = Path(self.state["state_path"])
        write_state(self.path, self.state)

    def publish(self, role: str, **overrides: object) -> str:
        assignment = self.state["roles"][role]
        arguments = {
            field: assignment[field]
            for field in (
                "role",
                "role_kind",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
            )
        }
        return orca_acp.publish_completion(
            self.path,
            run_id=self.state["run_id"],
            outcome="succeeded",
            body=role,
            cleanup_confirmed=True,
            **{**arguments, **overrides},
        )

    def test_both_results_persist_in_exact_assignments_and_duplicate_is_rejected(self):
        with mock.patch.object(orca_acp, "_send_worker_done") as send:
            for role in ("worker-a", "worker-b"):
                self.assertEqual(self.publish(role), "succeeded")
            with self.assertRaisesRegex(RuntimeFailure, "already published"):
                self.publish("worker-a")
        self.assertEqual(send.call_count, 2)
        state = read_state(self.path)
        self.assertNotIn("orca_result", state)
        for role in ("worker-a", "worker-b"):
            self.assertEqual(state["roles"][role]["orca_result"]["body"], role)
            self.assertTrue(
                state["roles"][role]["orca_result"]["notification_expected"]
            )
        self.assertTrue(cli._uses_scoped_acp(state))

    def test_lost_send_keeps_result_and_does_not_block_peer_or_retry_notification(self):
        with (
            mock.patch.object(
                orca_acp, "_send_worker_done", side_effect=RuntimeError("lost send")
            ),
            self.assertRaisesRegex(RuntimeError, "lost send"),
        ):
            self.publish("worker-a")
        saved_a = copy.deepcopy(read_state(self.path)["roles"]["worker-a"])
        with mock.patch.object(orca_acp, "_send_worker_done") as send:
            self.assertEqual(self.publish("worker-b"), "succeeded")
            with self.assertRaisesRegex(RuntimeFailure, "already published"):
                self.publish("worker-a")
        send.assert_called_once()
        self.assertEqual(read_state(self.path)["roles"]["worker-a"], saved_a)

    def test_orca_parallel_tools_use_typed_session_and_exact_task_request(self):
        backend = mock.Mock()
        backend.request.return_value = TaskStatusReceipt("task-a", "running", {})
        with mock.patch.object(
            cli, "_runtime_engine", return_value=(None, backend)
        ) as engine:
            session = runtime_mcp.RuntimeMcpSession(self.path, self.state)
        self.assertEqual(engine.call_args.args[0]["runtime"], "orca")
        self.assertTrue(engine.call_args.kwargs["resume_existing"])
        self.assertEqual(
            session.execute("task_get", {"task_id": "task-a"})["status"], "running"
        )
        backend.request.assert_called_once_with(TaskGet("task-a"))
        with (
            mock.patch.dict(cli.os.environ, {"AGENT_TEAM_STATE_PATH": str(self.path)}),
            mock.patch.object(
                runtime_mcp, "execute_tool", return_value={"typed": True}
            ) as execute,
            mock.patch(
                "agent_team.mcp_server.execute_tool",
                side_effect=AssertionError("legacy Orca route"),
            ),
        ):
            self.assertEqual(
                cli._execute_mcp_tool("task_get", {"task_id": "task-a"}),
                {"typed": True},
            )
            catalog = {tool["name"]: tool for tool in cli._mcp_tools()}
            self.assertIn("RunのDelivery全体", catalog["role_wait"]["description"])
            self.assertIn(
                "全完了のread/releaseと全質問へのreply",
                catalog["delivery_ack"]["description"],
            )
        execute.assert_called_once_with("task_get", {"task_id": "task-a"}, self.path)
