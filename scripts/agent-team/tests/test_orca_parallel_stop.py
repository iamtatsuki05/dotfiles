from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import test_orca_parallel_fifo as fifo_fixture

from agent_team import orca_parallel_stop
from agent_team.contracts import RuntimeFailure
from agent_team.runtime import read_state


class OrcaParallelStopTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fifo_fixture.OrcaParallelFifoTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_release_removes_owned_paths_but_keeps_assignment_until_whole_ack(self):
        f = self.fixture
        f.messages = [f.completion("worker-a")]
        f.wait()
        f.consume("worker-a")
        assignment = read_state(f.path)["roles"]["worker-a"]
        self.assertEqual(assignment["pending_delivery_stage"], "released")
        self.assertEqual(assignment["orca_release"]["phase"], "released")
        self.assertEqual(f.ack_calls(), [])
        for field in ("provider_private_root", "snapshot_root", "prompt_path"):
            self.assertFalse(Path(assignment[field]).exists())

    def test_stop_acks_safe_batch_and_retains_unconfirmed_active_peer(self):
        f = self.fixture
        f.messages = [f.completion("worker-a")]
        f.wait()
        with (
            mock.patch.object(orca_parallel_stop, "STOP_WAIT_SECONDS", 0),
            self.assertRaises(RuntimeFailure),
        ):
            f.tasks.stop()
        state = read_state(f.path)
        self.assertTrue(state["orca_stop_requested"])
        self.assertNotIn("worker-a", state["roles"])
        self.assertIn("worker-b", state["roles"])
        self.assertEqual(len(f.ack_calls()), 1)
