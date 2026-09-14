from __future__ import annotations

import copy
import hashlib
import json
import unittest

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphSpec
from agent_team.parallel_admission import admission_blocker
from agent_team.task_spec import TaskSpec, VerificationSpec


def _task(
    task_id: str,
    allowed_paths: tuple[str, ...],
    *,
    forbidden_paths: tuple[str, ...] = (),
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Complete {task_id}.",
        acceptance_criteria=("The declared criteria are met.",),
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
        dependencies=(),
        verification=(VerificationSpec("unit", ("python3", "-m", "unittest"), 30),),
        evidence_requirements=("Record the result.",),
        consultation_conditions=(),
    )


def _graph(max_active: int = 2) -> GraphSpec:
    nodes = (
        NodeRef("worker-a", Role.WORKER),
        NodeRef("worker-b", Role.WORKER),
        NodeRef("worker-c", Role.WORKER),
        NodeRef("planner-a", Role.PLANNER),
        NodeRef("reviewer-a", Role.REVIEWER),
    )
    return GraphSpec(
        nodes=nodes,
        edges=(),
        coordination=Coordination(
            mode="program",
            entry_nodes=("worker-a", "worker-b", "worker-c", "planner-a"),
            dispatch_mode="parallel",
            max_active=max_active,
        ),
        routes=(),
    )


def _assignment(
    node_id: str,
    role: Role,
    task: TaskSpec,
    *,
    released: bool = False,
) -> dict[str, object]:
    assignment: dict[str, object] = {
        "role": node_id,
        "role_kind": role.value,
        # This intentionally differs from the TaskSpec task ID. It is the native
        # provider/runner identity and must never select a TaskSpec.
        "task_id": f"provider-{task.task_id}",
        "dispatch_id": f"dispatch-{task.task_id}",
        "task_spec": task.as_dict(),
    }
    if released:
        assignment["pending_delivery"] = {
            "delivery_id": f"delivery-{task.task_id}",
            "stage": "released",
        }
    return assignment


def _digest(task: TaskSpec) -> str:
    encoded = json.dumps(task.as_dict(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ParallelAdmissionTest(unittest.TestCase):
    def test_same_node_busy_reason_is_stable_and_includes_logical_task(self) -> None:
        active_task = _task("task-a", ("src/a/",))
        target_task = _task("task-b", ("src/b/",))
        reason = admission_blocker(
            _graph(2),
            (active_task, target_task),
            {
                "worker-a": _assignment("worker-a", Role.WORKER, active_task),
            },
            NodeRef("worker-a", Role.WORKER),
            target_task,
        )

        self.assertEqual(
            reason,
            "same_node_busy:node_id=worker-a:logical_task_id=task-a",
        )

    def test_provider_task_id_is_not_used_as_catalog_key(self) -> None:
        active_task = _task("task-a", ("src/a/",))
        target_task = _task("task-b", ("src/b/",))
        assignments = {
            "worker-a": {
                **_assignment("worker-a", Role.WORKER, active_task),
                "task_id": "task-b",
            }
        }

        self.assertIsNone(
            admission_blocker(
                _graph(2),
                (active_task, target_task),
                assignments,
                NodeRef("worker-b", Role.WORKER),
                target_task,
            )
        )

    def test_missing_or_altered_task_spec_is_rejected_instead_of_counted_as_free(
        self,
    ) -> None:
        active_task = _task("task-a", ("src/a/",))
        target_task = _task("task-b", ("src/b/",))
        missing = _assignment("worker-a", Role.WORKER, active_task)
        del missing["task_spec"]
        altered = _assignment("worker-a", Role.WORKER, active_task)
        altered_spec = active_task.as_dict()
        altered_spec["allowed_paths"] = ["src/other/"]
        altered["task_spec"] = altered_spec

        for invalid in (missing, altered):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "task_spec"),
            ):
                admission_blocker(
                    _graph(2),
                    (active_task, target_task),
                    {"worker-a": invalid},
                    NodeRef("worker-b", Role.WORKER),
                    target_task,
                )

    def test_max_active_one_is_valid_and_blocks_second_assignment(self) -> None:
        active_task = _task("task-a", ("src/a/",))
        target_task = _task("task-b", ("src/b/",))

        reason = admission_blocker(
            _graph(1),
            (active_task, target_task),
            {"worker-a": _assignment("worker-a", Role.WORKER, active_task)},
            NodeRef("worker-b", Role.WORKER),
            target_task,
        )

        self.assertEqual(
            reason,
            "max_active_reached:node_id=worker-b:logical_task_id=task-b",
        )

    def test_max_active_two_allows_one_disjoint_writer_but_blocks_third(self) -> None:
        task_a = _task("task-a", ("src/a/",))
        task_b = _task("task-b", ("src/b/",))
        task_c = _task("task-c", ("src/c/",))
        assignments = {
            "worker-a": _assignment("worker-a", Role.WORKER, task_a),
        }

        self.assertIsNone(
            admission_blocker(
                _graph(2),
                (task_a, task_b, task_c),
                assignments,
                NodeRef("worker-b", Role.WORKER),
                task_b,
            )
        )
        assignments["worker-b"] = _assignment("worker-b", Role.WORKER, task_b)
        self.assertEqual(
            admission_blocker(
                _graph(2),
                (task_a, task_b, task_c),
                assignments,
                NodeRef("worker-c", Role.WORKER),
                task_c,
            ),
            "max_active_reached:node_id=worker-c:logical_task_id=task-c",
        )

    def test_released_assignment_still_occupies_active_capacity(self) -> None:
        task_a = _task("task-a", ("src/a/",))
        task_b = _task("task-b", ("src/b/",))
        reason = admission_blocker(
            _graph(1),
            (task_a, task_b),
            {"worker-a": _assignment("worker-a", Role.WORKER, task_a, released=True)},
            NodeRef("worker-b", Role.WORKER),
            task_b,
        )

        self.assertEqual(
            reason,
            "max_active_reached:node_id=worker-b:logical_task_id=task-b",
        )

    def test_disjoint_and_same_prefix_sibling_scopes_are_admissible(self) -> None:
        active_task = _task("task-a", ("src/a",))
        disjoint_task = _task("task-b", ("src/b",))
        sibling_task = _task("task-c", ("src/ab",))
        assignments = {
            "worker-a": _assignment("worker-a", Role.WORKER, active_task),
        }

        self.assertIsNone(
            admission_blocker(
                _graph(2),
                (active_task, disjoint_task, sibling_task),
                assignments,
                NodeRef("worker-b", Role.WORKER),
                disjoint_task,
            )
        )
        self.assertIsNone(
            admission_blocker(
                _graph(2),
                (active_task, disjoint_task, sibling_task),
                assignments,
                NodeRef("worker-b", Role.WORKER),
                sibling_task,
            )
        )

    def test_parent_child_scopes_block(self) -> None:
        active_task = _task("task-a", ("src/",))
        target_task = _task("task-b", ("src/a",))

        reason = admission_blocker(
            _graph(2),
            (active_task, target_task),
            {"worker-a": _assignment("worker-a", Role.WORKER, active_task)},
            NodeRef("worker-b", Role.WORKER),
            target_task,
        )

        self.assertEqual(
            reason,
            "write_scope_conflict:node_id=worker-a:logical_task_id=task-a:"
            "target_node_id=worker-b:target_logical_task_id=task-b",
        )

    def test_case_and_nfc_nfd_aliases_block_even_when_targets_are_missing(self) -> None:
        active_task = _task("task-a", ("Reports/Caf\u00e9/",))
        target_task = _task("task-b", ("reports/cafe\u0301/new-file.txt",))

        reason = admission_blocker(
            _graph(2),
            (active_task, target_task),
            {"worker-a": _assignment("worker-a", Role.WORKER, active_task)},
            NodeRef("worker-b", Role.WORKER),
            target_task,
        )

        self.assertEqual(
            reason,
            "write_scope_conflict:node_id=worker-a:logical_task_id=task-a:"
            "target_node_id=worker-b:target_logical_task_id=task-b",
        )

    def test_forbidden_paths_do_not_exempt_a_potential_write_scope(self) -> None:
        active_task = _task("task-a", ("src/blocked/other",))
        target_task = _task(
            "task-b",
            ("src/blocked/",),
            forbidden_paths=("src/blocked/",),
        )

        reason = admission_blocker(
            _graph(2),
            (active_task, target_task),
            {"worker-a": _assignment("worker-a", Role.WORKER, active_task)},
            NodeRef("worker-b", Role.WORKER),
            target_task,
        )

        self.assertEqual(
            reason,
            "write_scope_conflict:node_id=worker-a:logical_task_id=task-a:"
            "target_node_id=worker-b:target_logical_task_id=task-b",
        )

    def test_planner_and_reviewer_do_not_claim_worker_write_scope(self) -> None:
        active_task = _task("task-a", ("src/",))
        plan_task = _task("plan-task", ("src/",))
        before = _digest(plan_task)

        reason = admission_blocker(
            _graph(2),
            (active_task, plan_task),
            {"worker-a": _assignment("worker-a", Role.WORKER, active_task)},
            NodeRef("planner-a", Role.PLANNER),
            plan_task,
        )

        self.assertIsNone(reason)
        self.assertEqual(_digest(plan_task), before)

        self.assertIsNone(
            admission_blocker(
                _graph(2),
                (active_task, plan_task),
                {"worker-a": _assignment("worker-a", Role.WORKER, active_task)},
                NodeRef("reviewer-a", Role.REVIEWER),
                plan_task,
            )
        )

    def test_task_digest_and_input_mappings_are_not_mutated(self) -> None:
        active_task = _task("task-a", ("src/a/",))
        target_task = _task("task-b", ("src/b/",))
        assignments = {"worker-a": _assignment("worker-a", Role.WORKER, active_task)}
        before_assignments = copy.deepcopy(assignments)
        before_digest = _digest(target_task)

        self.assertIsNone(
            admission_blocker(
                _graph(2),
                (active_task, target_task),
                assignments,
                NodeRef("worker-b", Role.WORKER),
                target_task,
            )
        )
        self.assertEqual(assignments, before_assignments)
        self.assertEqual(_digest(target_task), before_digest)
