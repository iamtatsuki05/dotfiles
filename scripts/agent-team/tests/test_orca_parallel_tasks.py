from __future__ import annotations

import copy
import unittest
from unittest import mock

import test_orca_parallel_fifo as fifo_fixture

from agent_team.contracts import NodeRef, Role, RolePrompt, RuntimeFailure, TaskDispatch
from agent_team.runtime import read_state
from agent_team.task_spec import TaskSpec


class OrcaParallelTasksTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fifo_fixture.OrcaParallelFifoTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_wait_returns_every_owner_in_one_fifo_batch(self):
        f = self.fixture
        f.messages = [f.completion(role) for role in ("worker-a", "worker-b")]
        receipt = f.wait("worker-b")
        self.assertEqual(
            {event.identity.dispatch_id._value for event in receipt.events},
            {"dispatch-a", "dispatch-b"},
        )
        self.assertEqual(len(f.remote_calls), 1)
        state = read_state(f.path)
        self.assertEqual(
            {
                assignment["pending_delivery_id"]
                for assignment in state["roles"].values()
            },
            {f.delivery_id},
        )

    def test_ack_removes_batch_members_and_preserves_an_unrelated_active_peer(self):
        f = self.fixture
        f.messages = [f.completion("worker-a")]
        before_b = copy.deepcopy(read_state(f.path)["roles"]["worker-b"])
        f.wait()
        f.consume("worker-a")
        f.ack()
        state = read_state(f.path)
        self.assertNotIn("worker-a", state["roles"])
        self.assertEqual(state["roles"]["worker-b"], before_b)
        self.assertNotIn("orca_delivery_batch", state)
        self.assertEqual(state["tasks"]["task-b"]["status"], "running")

    def test_parallel_admission_rejects_before_start_assignment_effect(self):
        f = self.fixture
        request = TaskDispatch(
            NodeRef("worker-a", Role.WORKER),
            TaskSpec.from_dict(f.state["roles"]["worker-a"]["task_spec"]),
            "Declared work",
        )
        before = f.path.read_bytes()
        with (
            mock.patch("agent_team.orca_dispatch.start_assignment") as start,
            self.assertRaisesRegex(RuntimeFailure, "same_node_busy"),
        ):
            f.tasks.prompt(request)
        start.assert_not_called()
        self.assertEqual(f.path.read_bytes(), before)

    def test_parallel_rejects_taskless_role_prompt(self):
        f = self.fixture
        before = f.path.read_bytes()
        with self.assertRaisesRegex(RuntimeFailure, "TaskDispatch"):
            f.tasks.prompt(RolePrompt(NodeRef("worker-a", Role.WORKER), "work"))
        self.assertEqual(f.path.read_bytes(), before)
