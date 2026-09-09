from __future__ import annotations

import contextlib
import io
import json
import threading
import unittest
from unittest import mock

import test_native_acp_runner as fixture_support

from agent_team import cli, native_backend, orca_acp
from agent_team.adapters import ProcessResult
from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec
from agent_team.native_acp_dependencies import NativeAcpExecutables
from agent_team.runtime import read_state, write_state


class NamedOrcaAcpRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_support.NativeAcpRunnerTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        state, self.executables, self.prompt = self.fixture._state()
        state.pop("native")
        state.update(
            version=4,
            runtime="orca",
            worktree_id="repo::workspace",
            orca_socket=str(self.fixture.root / "orca.sock"),
            graph=GraphSpec(
                (NodeRef("main", Role.MAIN), NodeRef("planner", Role.PLANNER)),
                (GraphEdge("main", "planner", "delegates-to"),),
                Coordination("agent", ("main",), "serial", 1),
                (),
            ).as_dict(),
            max_review_rounds=2,
            task_specs=[],
            tasks={},
        )
        for role, spec in state["role_specs"].items():
            spec["kind"] = role
        assignment = state["roles"]["planner"]
        assignment.pop("launcher_owned_runner")
        assignment.update(
            role="planner", role_kind="planner", launcher_owned_terminal=True
        )
        self.state = state
        self.path = self.fixture.state_dir / "state.json"
        write_state(self.path, state, require_existing=True)

    def arguments(self):
        return {
            "state": self.state,
            "role": "planner",
            "state_path": self.path,
            "task_id": "task-1",
            "dispatch_id": "dispatch-1",
            "terminal_handle": "terminal-1",
            "prompt_path": self.prompt,
            "launch_nonce": "planner1234",
        }

    def test_named_orca_assignment_uses_saved_scoped_bindings(self):
        with mock.patch.object(
            cli,
            "_saved_acp_executables",
            side_effect=AssertionError("legacy acpx binding"),
        ):
            _assignment, spec, executables = cli._acp_assignment(**self.arguments())
        self.assertIsInstance(executables, NativeAcpExecutables)
        self.assertEqual(executables.as_dict(), self.executables.as_dict())
        self.assertEqual(spec["permission"], "read-only")

    def test_named_orca_turn_publishes_trusted_readonly_result_without_native_backend(
        self,
    ):
        receipt = {
            "output": "investigation complete",
            "session_id": "session-1",
            "model": "fable",
            "effort": "high",
            "cleanup_confirmed": True,
        }
        self.fixture.write_client_result(receipt)
        with (
            mock.patch.object(
                cli.ProcessRunner,
                "run",
                return_value=ProcessResult(0, json.dumps(receipt), ""),
            ) as run,
            mock.patch.object(
                cli, "run_acpx", side_effect=AssertionError("acpx fallback")
            ),
            mock.patch.object(
                native_backend,
                "publish_completion",
                side_effect=AssertionError("native publication"),
            ),
            mock.patch.object(orca_acp, "_send_worker_done") as send,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = cli._acp_run_turn(
                **self.arguments(), cancellation=threading.Event()
            )
        self.assertEqual(result, 0)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertIn("--question-socket", argv)
        self.assertIn("--result-file", argv)
        self.assertNotIn("acpx", argv)
        published = read_state(self.path)["orca_result"]
        self.assertEqual(published["outcome"], "succeeded")
        self.assertEqual(published["role_kind"], "planner")
        self.assertTrue(published["cleanup_confirmed"])
        self.assertNotIn("logical_task_id", published)
        self.assertIn("investigation complete", published["body"])
        send.assert_called_once()

    def test_cancel_before_client_launch_records_known_no_process_failure(self):
        cancelled = threading.Event()
        cancelled.set()
        with (
            mock.patch.object(cli.ProcessRunner, "run") as run,
            mock.patch.object(orca_acp, "_send_worker_done") as send,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = cli._acp_run_turn(**self.arguments(), cancellation=cancelled)
        self.assertEqual(result, 1)
        run.assert_not_called()
        published = read_state(self.path)["orca_result"]
        self.assertEqual(published["outcome"], "failed")
        self.assertTrue(published["cleanup_confirmed"])
        send.assert_called_once()

    def test_named_orca_runner_installs_cancellation_controller(self):
        arguments = self.arguments()
        arguments.pop("state")
        with (
            mock.patch.object(cli, "_acp_run_turn", return_value=0) as turn,
            mock.patch.object(cli.signal, "signal") as install,
        ):
            self.assertEqual(cli.acp_run(**arguments), 0)
        self.assertIsInstance(turn.call_args.kwargs["cancellation"], threading.Event)
        self.assertEqual(install.call_count, 6)

    def test_saved_stop_request_cancels_before_provider_start(self):
        self.state["orca_stop_requested"] = True
        write_state(self.path, self.state, require_existing=True)
        with (
            mock.patch.object(
                cli.subprocess,
                "Popen",
                side_effect=AssertionError("provider started after stop request"),
            ) as popen,
            mock.patch.object(orca_acp, "_send_worker_done") as send,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = cli._acp_run_turn(
                **self.arguments(), cancellation=threading.Event()
            )
        self.assertEqual(result, 1)
        popen.assert_not_called()
        send.assert_not_called()
        published = read_state(self.path)["orca_result"]
        self.assertEqual(published["outcome"], "failed")
        self.assertTrue(published["cleanup_confirmed"])
