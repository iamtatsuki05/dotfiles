from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import test_orca_parallel_stop_integration as stop_fixture
import test_orca_program_state as state_fixture

from agent_team import orca_program
from agent_team.adapters import (
    ExecutionError,
    ProcessResult,
    ProcessRunner,
    _process_group_exited,
)
from agent_team.contracts import Role, RuntimeFailure
from agent_team.named_graph import GraphSpec
from agent_team.process_identity import read_process_argv
from agent_team.runtime import read_state, write_state


class OrcaProgramCleanupTest(unittest.TestCase):
    def test_parallel_stop_cleans_workers_but_retains_unclean_coordinator(self):
        case = stop_fixture.OrcaParallelStopIntegrationTest(methodName="runTest")
        case.setUp()
        try:
            state = read_state(case.f.path)
            graph = GraphSpec.from_dict(state["graph"])
            graph = replace(
                graph,
                nodes=tuple(node for node in graph.nodes if node.kind is not Role.MAIN),
                edges=tuple(edge for edge in graph.edges if edge.source != "main"),
                coordination=replace(
                    graph.coordination,
                    mode="program",
                    entry_nodes=("worker-a", "worker-b"),
                ),
            )
            state["graph"] = graph.as_dict()
            state["role_specs"].pop("main")
            state["program_wave"] = state.pop("agent_batch")
            state["coordinator_terminal"] = state.pop("main_terminal")
            argv = orca_program.launch_argv(case.f.path, state["run_id"], "nonce1234")
            state["coordinator_argv"] = argv
            state["coordinator_process"] = {
                "pid": 999999999,
                "process_group_id": 999999999,
                "argv": argv,
                "launch_nonce": "nonce1234",
                "phase": "exited",
                "exit_code": 1,
                "cli_cleanup_confirmed": False,
            }
            write_state(case.f.path, state, require_existing=True)
            case.f.state = state
            case.f.backend._state = state
            for role in ("worker-a", "worker-b"):
                case.suppress(role)
            with self.assertRaisesRegex(
                RuntimeFailure, "CLI process cleanup is unconfirmed"
            ):
                case.stop()
            case.final.assert_not_called()
            self.assertEqual(len(case.f.closed), 2)
            self.assertNotIn(state["coordinator_terminal"], case.f.closed)
            self.assertTrue(case.f.path.exists())
        finally:
            case.doCleanups()

    def test_exited_receipt_waits_for_pid_reaping_and_group_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            state = state_fixture._state(Path(directory))
            state["coordinator_process"] = {
                **state_fixture._running_process(state),
                "phase": "exited",
                "exit_code": 0,
                "cli_cleanup_confirmed": True,
            }
            with (
                mock.patch.object(orca_program.os, "getpgid", return_value=43),
                mock.patch.object(orca_program, "read_process_argv", return_value=None),
            ):
                self.assertFalse(orca_program.exit_cleanup_confirmed(state))
            with (
                mock.patch.object(
                    orca_program.os, "getpgid", side_effect=ProcessLookupError
                ),
                mock.patch.object(
                    orca_program, "_process_group_exited", return_value=False
                ),
            ):
                self.assertFalse(orca_program.exit_cleanup_confirmed(state))
            with (
                mock.patch.object(
                    orca_program.os, "getpgid", side_effect=ProcessLookupError
                ),
                mock.patch.object(
                    orca_program, "_process_group_exited", return_value=True
                ),
            ):
                self.assertTrue(orca_program.exit_cleanup_confirmed(state))

    def test_real_cli_child_is_reaped_before_clean_result(self):
        runner = orca_program.CoordinatorCliRunner()
        with tempfile.TemporaryDirectory() as directory:
            result = runner.run(
                [
                    sys.executable,
                    "-c",
                    "import os,json; print(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp()}))",
                ],
                cwd=Path(directory),
                env=orca_program.launch_environment(),
                timeout_seconds=5,
            )
        identity = json.loads(result.stdout)
        self.assertNotEqual(identity["pgid"], os.getpgrp())
        self.assertIsNone(read_process_argv(identity["pid"]))
        self.assertTrue(_process_group_exited(identity["pgid"]))
        self.assertTrue(runner.cli_cleanup_confirmed)

    def test_timed_out_cli_child_has_confirmed_cleanup(self):
        runner = orca_program.CoordinatorCliRunner()
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "child.json"
            command = (
                "import os,json,time; from pathlib import Path; Path("
                + repr(str(receipt))
                + ").write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp()})); time.sleep(30)"
            )
            with self.assertRaises(ExecutionError) as raised:
                runner.run(
                    [sys.executable, "-c", command],
                    cwd=Path(directory),
                    env=orca_program.launch_environment(),
                    timeout_seconds=1,
                )
            identity = json.loads(receipt.read_text())
            self.assertTrue(raised.exception.cleanup_confirmed)
            self.assertIsNone(read_process_argv(identity["pid"]))
            self.assertTrue(_process_group_exited(identity["pgid"]))
            self.assertTrue(runner.cli_cleanup_confirmed)

    def test_unconfirmed_cli_cleanup_stays_in_exit_receipt_after_later_success(self):
        runner = orca_program.CoordinatorCliRunner()
        with (
            mock.patch.object(
                ProcessRunner,
                "run",
                side_effect=ExecutionError(
                    "group exit unknown", cleanup_confirmed=False
                ),
            ),
            self.assertRaises(ExecutionError),
        ):
            runner.run(["unused"], cwd=Path.cwd(), env={})
        with mock.patch.object(
            ProcessRunner, "run", return_value=ProcessResult(0, "", "")
        ):
            runner.run(["unused"], cwd=Path.cwd(), env={})
        self.assertFalse(runner.cli_cleanup_confirmed)
        with tempfile.TemporaryDirectory() as directory:
            state = state_fixture._state(Path(directory))
            path = Path(str(state["state_path"]))
            state["coordinator_process"] = {
                **state_fixture._running_process(state),
                "pid": os.getpid(),
                "process_group_id": os.getpgrp(),
            }
            state.pop("pending_coordinator_start")
            state["orca_stop_requested"] = True
            write_state(path, state)
            orca_program._record_exit(
                path,
                "run-1",
                "nonce1234",
                1,
                cli_cleanup_confirmed=runner.cli_cleanup_confirmed,
            )
            saved = read_state(path)
            self.assertEqual(saved["coordinator_process"]["phase"], "exited")
            self.assertIs(saved["coordinator_process"]["cli_cleanup_confirmed"], False)
            with self.assertRaisesRegex(
                RuntimeFailure, "CLI process cleanup is unconfirmed"
            ):
                orca_program.wait_for_exit(path, saved)
            self.assertEqual(read_state(path), saved)


if __name__ == "__main__":
    unittest.main()
