from __future__ import annotations

import copy
import unittest
from typing import cast

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.program_policy import ProgramAction, select_action
from agent_team.task_execution import task_digest
from agent_team.task_spec import TaskSpec, VerificationSpec

REVISION = "a" * 64


def _task(task_id: str, *, dependencies: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Complete {task_id}.",
        acceptance_criteria=(f"The {task_id} acceptance criteria are met.",),
        allowed_paths=(f"{task_id}.txt",),
        forbidden_paths=("upstreams.json",),
        dependencies=dependencies,
        verification=(VerificationSpec("unit", ("python3", "-m", "unittest"), 60),),
        evidence_requirements=("Report the command and result.",),
        consultation_conditions=("Ask before exceeding the declared scope.",),
    )


def _program_graph(tasks: tuple[TaskSpec, ...]) -> GraphSpec:
    nodes = (
        NodeRef("worker-a", Role.WORKER),
        NodeRef("worker-b", Role.WORKER),
        NodeRef("reviewer-a", Role.REVIEWER),
        NodeRef("reviewer-b", Role.REVIEWER),
    )
    edges = (
        GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
        GraphEdge("worker-b", "reviewer-b", "reviewed-by"),
    )
    routes = tuple(
        TaskRoute(
            task.task_id,
            None,
            None,
            "worker-a" if task.task_id == "task-a" else "worker-b",
            "reviewer-a" if task.task_id == "task-a" else "reviewer-b",
        )
        for task in tasks
    )
    return GraphSpec(
        nodes=nodes,
        edges=edges,
        coordination=Coordination("program", ("worker-a", "worker-b"), "serial", 1),
        routes=routes,
    )


def _state(
    tasks: tuple[TaskSpec, ...],
    *,
    records: dict[str, object] | None = None,
    wave: dict[str, object] | None = None,
) -> dict[str, object]:
    graph = _program_graph(tasks)
    state: dict[str, object] = {
        "version": 4,
        "graph": graph.as_dict(),
        "task_specs": [task.as_dict() for task in tasks],
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "max_review_rounds": 2,
        "tasks": {} if records is None else records,
        "roles": {},
        "native": {"phase": "running"},
    }
    if wave is not None:
        state["program_wave"] = wave
    return state


def _wave(
    *task_ids: str,
    phase: str = "writers",
    revision: str | None = None,
) -> dict[str, object]:
    return {"task_ids": list(task_ids), "phase": phase, "revision": revision}


def _record(
    task: TaskSpec,
    *,
    status: str,
    role: NodeRef,
    dispatch_id: str = "dispatch",
    stage: str = "implementation",
    revision: str | None = None,
    rounds: int = 0,
) -> dict[str, object]:
    return {
        "spec": task.as_dict(),
        "digest": task_digest(task),
        "dispatch_id": dispatch_id,
        "status": status,
        "stage": stage,
        "revision": revision,
        "review_rounds": {"plan": 0, "implementation": rounds},
        "role": role.node_id,
        "role_kind": role.kind.value,
        "writer_role": "worker-a" if task.task_id == "task-a" else "worker-b",
        "writer_kind": Role.WORKER.value,
    }


def _mixed_state(
    tasks: tuple[TaskSpec, ...],
    *,
    records: dict[str, object],
    phase: str = "writers",
    revision: str | None = None,
    first_plan: bool = True,
) -> dict[str, object]:
    nodes = (
        NodeRef("planner-a", Role.PLANNER),
        NodeRef("planner-b", Role.PLANNER),
        NodeRef("worker-a", Role.WORKER),
        NodeRef("worker-b", Role.WORKER),
        NodeRef("plan-reviewer-a", Role.REVIEWER),
        NodeRef("plan-reviewer-b", Role.REVIEWER),
        NodeRef("reviewer-a", Role.REVIEWER),
        NodeRef("reviewer-b", Role.REVIEWER),
    )
    graph = GraphSpec(
        nodes=nodes,
        edges=(
            GraphEdge("planner-a", "worker-a", "delegates-to"),
            GraphEdge("planner-b", "worker-b", "delegates-to"),
            GraphEdge("planner-a", "plan-reviewer-a", "reviewed-by"),
            GraphEdge("planner-b", "plan-reviewer-b", "reviewed-by"),
            GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
            GraphEdge("worker-b", "reviewer-b", "reviewed-by"),
        ),
        coordination=Coordination("program", ("planner-a", "planner-b"), "serial", 1),
        routes=(
            TaskRoute(
                "task-a",
                "planner-a" if first_plan else None,
                "plan-reviewer-a" if first_plan else None,
                "worker-a",
                "reviewer-a",
            ),
            TaskRoute(
                "task-b", "planner-b", "plan-reviewer-b", "worker-b", "reviewer-b"
            ),
        ),
    )
    return {
        "version": 4,
        "graph": graph.as_dict(),
        "task_specs": [task.as_dict() for task in tasks],
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "max_review_rounds": 2,
        "tasks": records,
        "roles": {},
        "native": {"phase": "running"},
        "program_wave": {
            "task_ids": [task.task_id for task in tasks],
            "phase": phase,
            "revision": revision,
        },
    }


def _active_assignment(
    state: dict[str, object], role: NodeRef, *, task_id: str = "task-a"
) -> None:
    cast(dict[str, object], state["roles"])[role.node_id] = {
        "role": role.node_id,
        "role_kind": role.kind.value,
        "task_id": task_id,
        "dispatch_id": "dispatch",
        "completion_observed": False,
    }


class ProgramPolicyTest(unittest.TestCase):
    def test_ready_tasks_follow_declared_order_after_dependency_readiness(self) -> None:
        dependent = _task("task-b", dependencies=("task-a",))
        first = _task("task-a")
        state = _state(
            (dependent, first),
            wave=_wave("task-a"),
        )

        action = select_action(state)

        self.assertEqual(
            action,
            ProgramAction(
                "dispatch",
                task_id="task-a",
                role=NodeRef("worker-a", Role.WORKER),
                stage="implementation",
                message="宣言されたTaskSpecに沿って実装してください。",
            ),
        )

    def test_missing_wave_marker_is_rejected_before_effect(self) -> None:
        state = _state((_task("task-a"), _task("task-b")))
        before = copy.deepcopy(state)

        action = select_action(state)

        self.assertEqual(action.kind, "reject")
        self.assertEqual(action.message, "program_wave_required")
        self.assertEqual(state, before)

    def test_serial_wave_dispatches_all_writers_before_any_reviewer(self) -> None:
        first = _task("task-a")
        second = _task("task-b")
        state = _state(
            (first, second),
            records={
                "task-a": _record(
                    first,
                    status="awaiting_implementation_review",
                    role=NodeRef("worker-a", Role.WORKER),
                )
            },
            wave=_wave("task-a", "task-b"),
        )

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-b")
        self.assertEqual(action.role, NodeRef("worker-b", Role.WORKER))
        self.assertEqual(action.stage, "implementation")

    def test_reviewer_is_selected_only_after_all_wave_writers_finish(self) -> None:
        first = _task("task-a")
        second = _task("task-b")
        state = _state(
            (first, second),
            records={
                "task-a": _record(
                    first,
                    status="awaiting_implementation_review",
                    role=NodeRef("worker-a", Role.WORKER),
                ),
                "task-b": _record(
                    second,
                    status="awaiting_implementation_review",
                    role=NodeRef("worker-b", Role.WORKER),
                ),
            },
            wave=_wave("task-a", "task-b", phase="reviewers", revision=REVISION),
        )

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-a")
        self.assertEqual(action.role, NodeRef("reviewer-a", Role.REVIEWER))
        self.assertEqual(action.stage, "review")

    def test_plan_review_precedes_implementation_review_in_a_mixed_wave(self) -> None:
        first = _task("task-a")
        second = _task("task-b")
        first_record = _record(
            first,
            status="awaiting_implementation_review",
            role=NodeRef("worker-a", Role.WORKER),
        )
        second_record = _record(
            second,
            status="awaiting_plan_review",
            role=NodeRef("planner-b", Role.PLANNER),
            stage="plan",
        )
        state = _mixed_state(
            (first, second),
            records={"task-a": first_record, "task-b": second_record},
        )

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-b")
        self.assertEqual(action.role, NodeRef("plan-reviewer-b", Role.REVIEWER))
        self.assertEqual(action.stage, "review")

    def test_earlier_implementation_writer_precedes_later_plan_review(self) -> None:
        first = _task("task-a")
        second = _task("task-b")
        second_record = _record(
            second,
            status="awaiting_plan_review",
            role=NodeRef("planner-b", Role.PLANNER),
            stage="plan",
        )
        state = _mixed_state(
            (first, second), records={"task-b": second_record}, first_plan=False
        )

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-a")
        self.assertEqual(action.role, NodeRef("worker-a", Role.WORKER))
        self.assertEqual(action.stage, "implementation")

    def test_plan_only_approval_cannot_precede_the_mixed_wave_barrier(
        self,
    ) -> None:
        first, second = _task("task-a"), _task("task-b")
        state = _mixed_state(
            (first, second),
            records={
                "task-a": _record(
                    first,
                    status="plan_approved",
                    role=NodeRef("plan-reviewer-a", Role.REVIEWER),
                    stage="plan",
                ),
                "task-b": _record(
                    second,
                    status="awaiting_implementation_review",
                    role=NodeRef("worker-b", Role.WORKER),
                ),
            },
        )
        graph = cast(dict[str, object], state["graph"])
        routes = cast(list[dict[str, object]], graph["routes"])
        routes[0].update(implementation_writer=None, implementation_reviewer=None)
        before = copy.deepcopy(state)
        action = select_action(state)
        self.assertEqual(action.kind, "reject")
        self.assertEqual(action.message, "invalid_program_state")
        self.assertEqual(state, before)

    def test_writers_seal_before_implementation_review(self) -> None:
        first = _task("task-a")
        second = _task("task-b")
        state = _state(
            (first, second),
            records={
                "task-a": _record(
                    first,
                    status="awaiting_implementation_review",
                    role=NodeRef("worker-a", Role.WORKER),
                ),
                "task-b": _record(
                    second,
                    status="awaiting_implementation_review",
                    role=NodeRef("worker-b", Role.WORKER),
                ),
            },
            wave=_wave("task-a", "task-b", revision=None),
        )

        self.assertEqual(select_action(state), ProgramAction("seal_wave"))

    def test_review_change_reopens_wave_before_retry(self) -> None:
        task = _task("task-a")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="implementation_changes_requested",
                    role=NodeRef("reviewer-a", Role.REVIEWER),
                    revision=REVISION,
                    rounds=1,
                )
            },
            wave=_wave("task-a", phase="reviewers", revision=REVISION),
        )

        self.assertEqual(
            select_action(state),
            ProgramAction("reopen_wave", task_id="task-a"),
        )

    def test_approved_reviewers_enter_verification_wave(self) -> None:
        first = _task("task-a")
        second = _task("task-b")
        state = _state(
            (first, second),
            records={
                "task-a": _record(
                    first,
                    status="implementation_approved",
                    role=NodeRef("reviewer-a", Role.REVIEWER),
                    revision=REVISION,
                    rounds=1,
                ),
                "task-b": _record(
                    second,
                    status="implementation_approved",
                    role=NodeRef("reviewer-b", Role.REVIEWER),
                    revision=REVISION,
                    rounds=1,
                ),
            },
            wave=_wave("task-a", "task-b", phase="reviewers", revision=REVISION),
        )

        self.assertEqual(select_action(state), ProgramAction("verify_wave"))

    def test_verification_failure_reopens_wave(self) -> None:
        task = _task("task-a")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="verification_failed",
                    role=NodeRef("reviewer-a", Role.REVIEWER),
                    revision=REVISION,
                    rounds=1,
                )
            },
            wave=_wave("task-a", phase="verification", revision=REVISION),
        )

        self.assertEqual(
            select_action(state),
            ProgramAction("reopen_wave", task_id="task-a"),
        )

    def test_active_assignment_is_waited_and_state_is_not_mutated(self) -> None:
        task = _task("task-a")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="running",
                    role=NodeRef("worker-a", Role.WORKER),
                )
            },
            wave=_wave("task-a"),
        )
        _active_assignment(state, NodeRef("worker-a", Role.WORKER))
        before = copy.deepcopy(state)

        action = select_action(state)

        self.assertEqual(action.kind, "wait")
        self.assertEqual(action.role, NodeRef("worker-a", Role.WORKER))
        self.assertEqual(state, before)

    def test_unanswered_question_waits_for_user_and_all_answers_acknowledge(
        self,
    ) -> None:
        task = _task("task-a")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="running",
                    role=NodeRef("worker-a", Role.WORKER),
                )
            },
            wave=_wave("task-a"),
        )
        _active_assignment(state, NodeRef("worker-a", Role.WORKER))
        state.update(
            {
                "pending_delivery_id": "delivery-1",
                "pending_delivery_kind": "question",
                "pending_delivery_stage": "observed",
                "pending_question_ids": ["question-1", "question-2"],
                "replied_question_ids": [],
                "native_question": {
                    "role": "worker-a",
                    "role_kind": "worker",
                    "task_id": "task-a",
                    "delivery_id": "delivery-1",
                    "message_ids": ["question-1", "question-2"],
                    "answers": {},
                    "phase": "observed",
                },
            }
        )

        waiting = select_action(state)

        self.assertEqual(waiting.kind, "wait_user")
        self.assertEqual(waiting.task_id, "task-a")
        self.assertEqual(waiting.role, NodeRef("worker-a", Role.WORKER))

        question = cast(dict[str, object], state["native_question"])
        question["answers"] = {"question-1": "yes", "question-2": "yes"}
        acknowledged = select_action(state)
        self.assertEqual(acknowledged, ProgramAction("acknowledge"))

    def test_approved_implementation_selects_verify_at_wave_revision(self) -> None:
        task = _task("task-a")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="implementation_approved",
                    role=NodeRef("reviewer-a", Role.REVIEWER),
                    revision=REVISION,
                    rounds=1,
                )
            },
            wave=_wave("task-a", phase="verification", revision=REVISION),
        )

        action = select_action(state)

        self.assertEqual(
            action,
            ProgramAction("verify", task_id="task-a", stage="verification"),
        )

    def test_terminal_failure_does_not_redispatch(self) -> None:
        task = _task("task-a")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="failed",
                    role=NodeRef("worker-a", Role.WORKER),
                )
            },
            wave=_wave("task-a"),
        )

        action = select_action(state)

        self.assertEqual(action.kind, "pause")
        self.assertEqual(action.task_id, "task-a")
        self.assertEqual(action.message, "terminal_task_failure")

    def test_completed_catalog_returns_complete(self) -> None:
        task = _task("task-a")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="completed",
                    role=NodeRef("reviewer-a", Role.REVIEWER),
                    revision=REVISION,
                    rounds=1,
                )
            },
            wave=_wave("task-a", phase="verification", revision=REVISION),
        )

        self.assertEqual(select_action(state), ProgramAction("complete"))


if __name__ == "__main__":
    unittest.main()
