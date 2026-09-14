from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from dataclasses import replace
from unittest import mock

import test_orca_task_pipeline as pipeline_fixture

from agent_team import orca_program, orca_tasks, program_driver, workspace_revision
from agent_team.contracts import Role
from agent_team.named_graph import GraphSpec
from agent_team.runtime import read_state, write_state
from agent_team.task_execution import new_program_wave


class OrcaProgramPipelineTest(unittest.TestCase):
    def test_serial_program_drives_review_changes_and_real_fixed_argv(self):
        for changes in (False, True):
            with self.subTest(request_changes=changes):
                self._run_serial(changes)

    def _run_serial(self, changes):
        fixture = pipeline_fixture.OrcaTaskPipelineIntegrationTest(methodName="runTest")
        fixture.setUp()
        try:
            state = read_state(fixture.path)
            graph = GraphSpec.from_dict(state["graph"])
            graph = replace(
                graph,
                nodes=tuple(node for node in graph.nodes if node.kind is not Role.MAIN),
                edges=tuple(edge for edge in graph.edges if edge.source != "lead"),
                coordination=replace(
                    graph.coordination,
                    mode="program",
                    entry_nodes=("worker-a",),
                ),
            )
            state["graph"] = graph.as_dict()
            state["role_specs"].pop("lead")
            state["program_wave"] = new_program_wave(state)
            state["coordinator_terminal"] = state.pop("main_terminal")
            argv = orca_program.launch_argv(fixture.path, state["run_id"], "nonce1234")
            state["coordinator_argv"] = argv
            state["coordinator_process"] = {
                "pid": os.getpid(),
                "process_group_id": os.getpgrp(),
                "argv": argv,
                "launch_nonce": "nonce1234",
                "phase": "running",
                "exit_code": None,
                "cli_cleanup_confirmed": None,
            }
            write_state(fixture.path, state, require_existing=True)
            backend = fixture.fixture.backend
            backend._state = state
            reviews = []

            def remote(current, args, **kwargs):
                if tuple(args[:2]) == ("orchestration", "check") and "--wait" in args:
                    saved = read_state(fixture.path)
                    role = next(iter(saved["roles"]))
                    fixture._set_terminal_identity(role)
                    evidence = None
                    if saved["roles"][role]["role_kind"] == "reviewer":
                        decision = (
                            "request_changes" if changes and not reviews else "approve"
                        )
                        reviews.append(decision)
                        evidence = fixture._review_evidence(
                            decision=decision,
                            revision=fixture.revision,
                            findings=["correct the implementation"]
                            if decision == "request_changes"
                            else [],
                        )
                    fixture._publish(
                        role,
                        body=json.dumps(evidence)
                        if evidence
                        else "completed implementation",
                        evidence=evidence,
                    )
                    current = read_state(fixture.path)
                return fixture._run_orca(current, args, **kwargs)

            snapshots = 0
            original_snapshot = backend.program_snapshot

            def snapshot():
                nonlocal snapshots
                snapshots += 1
                self.assertLess(snapshots, 60, "program did not make bounded progress")
                return original_snapshot()

            output = io.StringIO()
            with (
                mock.patch.object(
                    orca_program, "read_process_argv", return_value=tuple(argv)
                ),
                mock.patch.object(orca_tasks.remote, "run_orca", side_effect=remote),
                mock.patch.object(backend, "program_snapshot", side_effect=snapshot),
                mock.patch.object(
                    workspace_revision,
                    "snapshot_revision",
                    return_value=fixture.revision,
                ),
                contextlib.redirect_stdout(output),
            ):
                code = program_driver.drive(backend)
            self.assertEqual(code, 0, output.getvalue())
            saved = read_state(fixture.path)
            record = saved["tasks"][fixture.task.task_id]
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["verification"]["revision"], fixture.revision)
            self.assertEqual(
                record["verification"]["commands"][0]["argv"], ["/usr/bin/true"]
            )
            self.assertEqual(record["verification"]["commands"][0]["returncode"], 0)
            self.assertEqual(
                reviews,
                ["request_changes", "approve"] if changes else ["approve"],
            )
            self.assertEqual(saved["roles"], {})
            self.assertNotIn("pending_delivery_id", saved)
            acknowledgements = [args for args in fixture.orca_calls if "--ack" in args]
            self.assertEqual(len(acknowledgements), 4 if changes else 2)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
