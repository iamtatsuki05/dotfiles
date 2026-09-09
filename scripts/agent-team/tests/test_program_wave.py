from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from dataclasses import replace

import test_named_task_routing as routing

from agent_team import task_execution as tasks
from agent_team.contracts import NodeRef, Role, RuntimeFailure, TaskDispatch
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.task_spec import VerificationSpec


def state_fixture():
    catalog = tuple(
        replace(
            routing._task(f"task-{letter}"),
            allowed_paths=(f"{letter}.txt",),
            dependencies=("task-a",) if letter == "c" else (),
            verification=(VerificationSpec("unit", (sys.executable, "-c", "pass"), 2),),
        )
        for letter in "abc"
    )
    graph = GraphSpec(
        tuple(
            NodeRef(f"{kind}-{letter}", role)
            for letter in "abc"
            for kind, role in (("work", Role.WORKER), ("review", Role.REVIEWER))
        ),
        tuple(
            GraphEdge(f"work-{letter}", f"review-{letter}", "reviewed-by")
            for letter in "abc"
        ),
        Coordination("program", ("work-a", "work-b", "work-c"), "serial", 1),
        tuple(
            TaskRoute(task.task_id, None, None, f"work-{letter}", f"review-{letter}")
            for letter, task in zip("abc", catalog, strict=True)
        ),
    )
    state = {
        "version": 4,
        "graph": graph.as_dict(),
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "task_specs": [task.as_dict() for task in catalog],
        "max_review_rounds": 3,
        "tasks": {},
        "roles": {},
        "program_wave": {
            "task_ids": ["task-a", "task-b"],
            "phase": "writers",
            "revision": None,
        },
    }
    return state, catalog


def write_result(state, task, dispatch):
    writer = NodeRef(f"work-{task.task_id[-1]}", Role.WORKER)
    record, _ = tasks.prepare_dispatch(
        state, TaskDispatch(writer, task, "実装してください")
    )
    record["dispatch_id"] = dispatch
    tasks.acknowledge_task(state, routing._result(dispatch_id=dispatch, target=writer))
    tasks.validate_saved_tasks(state)


def review_result(state, task, revision, dispatch, decision="approve"):
    reviewer = NodeRef(f"review-{task.task_id[-1]}", Role.REVIEWER)
    record, _ = tasks.prepare_dispatch(
        state, TaskDispatch(reviewer, task, "レビューしてください"), revision=revision
    )
    record["dispatch_id"] = dispatch
    evidence = routing._review(
        task,
        stage="implementation",
        revision=revision,
        decision=decision,
        findings=[] if decision == "approve" else ["修正が必要"],
    )
    tasks.acknowledge_task(
        state, routing._result(dispatch_id=dispatch, target=reviewer, evidence=evidence)
    )
    tasks.validate_saved_tasks(state)


def verification_result(state, task):
    record = state["tasks"][task.task_id]
    command = task.verification[0]
    record["verification"] = {
        "revision": record["revision"],
        "passed": True,
        "cleanup_confirmed": True,
        "error": None,
        "commands": [
            {
                "name": command.name,
                "argv": list(command.argv),
                "timeout_seconds": command.timeout_seconds,
                "returncode": 0,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "error": None,
            }
        ],
    }
    record["status"] = "completed"
    tasks.validate_saved_tasks(state)


class ProgramWaveTest(unittest.TestCase):
    def test_sequential_writers_all_finish_before_integrated_review(self):
        state, catalog = state_fixture()
        write_result(state, catalog[0], "write-a-1")
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            review_result(state, catalog[0], "a" * 64, "review-a-1")
        self.assertEqual(state, before)
        write_result(state, catalog[1], "write-b-1")
        tasks.transition_program_wave(state, "seal_wave", revision="a" * 64)
        review_result(state, catalog[0], "a" * 64, "review-a-1")
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            tasks.transition_program_wave(state, "verify_wave")
        self.assertEqual(state, before)
        review_result(state, catalog[1], "a" * 64, "review-b-1")
        tasks.transition_program_wave(state, "verify_wave")
        self.assertEqual(state["program_wave"]["phase"], "verification")

    def test_reopen_invalidates_approvals_without_resetting_rounds(self):
        state, catalog = state_fixture()
        for task in catalog[:2]:
            write_result(state, task, f"write-{task.task_id}-1")
        tasks.transition_program_wave(state, "seal_wave", revision="a" * 64)
        review_result(state, catalog[0], "a" * 64, "review-a-1")
        review_result(state, catalog[1], "a" * 64, "review-b-1", "request_changes")
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            write_result(state, catalog[1], "premature-retry")
        self.assertEqual(state, before)
        tasks.transition_program_wave(state, "reopen_wave")
        approved = state["tasks"]["task-a"]
        self.assertEqual(approved["status"], "awaiting_implementation_review")
        self.assertIsNone(approved["revision"])
        self.assertEqual(approved["review_rounds"]["implementation"], 1)
        self.assertEqual(
            approved["review_result"], before["tasks"]["task-a"]["review_result"]
        )
        tasks.validate_saved_tasks(state)
        write_result(state, catalog[1], "write-b-2")
        tasks.transition_program_wave(state, "seal_wave", revision="b" * 64)
        for task in catalog[:2]:
            review_result(state, task, "b" * 64, f"review-{task.task_id}-2")
        tasks.transition_program_wave(state, "verify_wave")
        self.assertEqual(
            {record["revision"] for record in state["tasks"].values()}, {"b" * 64}
        )

    def test_successor_waits_until_whole_current_wave_is_completed(self):
        state, catalog = state_fixture()
        for task in catalog[:2]:
            write_result(state, task, f"write-{task.task_id}")
        tasks.transition_program_wave(state, "seal_wave", revision="a" * 64)
        for task in catalog[:2]:
            review_result(state, task, "a" * 64, f"review-{task.task_id}")
        tasks.transition_program_wave(state, "verify_wave")
        verification_result(state, catalog[0])
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            write_result(state, catalog[2], "premature-successor")
        with self.assertRaises(RuntimeFailure):
            tasks.transition_program_wave(state, "next_wave")
        self.assertEqual(state, before)
        verification_result(state, catalog[1])
        tasks.transition_program_wave(state, "next_wave")
        self.assertEqual(
            state["program_wave"],
            {"task_ids": ["task-c"], "phase": "writers", "revision": None},
        )
        write_result(state, catalog[2], "write-c")

    def test_missing_wave_and_mismatched_revision_fail_before_dispatch(self):
        state, catalog = state_fixture()
        for change in (
            lambda value: value.pop("program_wave"),
            lambda value: value["program_wave"].update(revision="a" * 64),
        ):
            bad = copy.deepcopy(state)
            change(bad)
            before = copy.deepcopy(bad)
            with self.assertRaises(RuntimeFailure):
                write_result(bad, catalog[0], "invalid-write")
            self.assertEqual(bad, before)

    def test_exhausted_approved_peer_blocks_reopen_without_reset_or_mutation(self):
        state, catalog = state_fixture()
        state["max_review_rounds"] = 1
        for task in catalog[:2]:
            write_result(state, task, f"write-{task.task_id}")
        tasks.transition_program_wave(state, "seal_wave", revision="a" * 64)
        review_result(state, catalog[0], "a" * 64, "review-a")
        review_result(state, catalog[1], "a" * 64, "review-b", "request_changes")
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            tasks.transition_program_wave(state, "reopen_wave")
        self.assertEqual(state, before)
