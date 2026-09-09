from __future__ import annotations

import copy
import unittest

import test_program_policy as support

from agent_team.contracts import NodeRef, Role, RuntimeFailure, TaskDispatch
from agent_team.task_execution import prepare_dispatch, validate_saved_tasks


class ParallelTaskStateTest(unittest.TestCase):
    def state(self):
        a, b = support._task("task-a"), support._task("task-b")
        state = support._state((a, b), wave=support._wave("task-a", "task-b"))
        state["version"] = 5
        state["graph"]["coordination"].update(dispatch_mode="parallel", max_active=2)
        return state, a, b

    def test_explicit_parallel_state_keeps_named_writer_routes_and_task_records(self):
        state, a, b = self.state()
        validate_saved_tasks(state)
        for task, node in ((a, "worker-a"), (b, "worker-b")):
            record, _ = prepare_dispatch(
                state, TaskDispatch(NodeRef(node, Role.WORKER), task, "作成")
            )
            self.assertEqual(record["role"], node)
            self.assertEqual(record["role_kind"], "worker")
            record["dispatch_id"] = "dispatch-" + task.task_id
            state["tasks"][task.task_id] = record
        self.assertEqual(set(state["tasks"]), {"task-a", "task-b"})
        validate_saved_tasks(state)

    def test_serial_and_parallel_state_versions_cannot_reinterpret_each_other(self):
        for version, mode, maximum in ((4, "parallel", 2), (5, "serial", 1)):
            state, _a, _b = self.state()
            state["version"] = version
            state["graph"]["coordination"].update(
                dispatch_mode=mode, max_active=maximum
            )
            before = copy.deepcopy(state)
            with self.subTest(version=version), self.assertRaises(RuntimeFailure):
                validate_saved_tasks(state)
            self.assertEqual(state, before)

    def test_wrong_worker_remains_rejected_before_parallel_task_mutation(self):
        state, a, _b = self.state()
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(
                state, TaskDispatch(NodeRef("worker-b", Role.WORKER), a, "作成")
            )
        self.assertEqual(state, before)
