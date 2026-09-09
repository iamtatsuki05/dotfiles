from __future__ import annotations

import hashlib
import unittest
from typing import cast

from agent_team import program_driver
from agent_team.contracts import (
    AckReceipt,
    BackendRequest,
    BackendResult,
    DeliveryAck,
    DeliveryRef,
    NodeRef,
    Role,
    RoleWait,
)
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.program_policy import ProgramAction, select_action
from agent_team.task_execution import task_consultation, task_digest
from agent_team.task_spec import TaskSpec, VerificationSpec

REVISION = "a" * 64


def _task(
    task_id: str,
    path: str,
    *,
    dependencies: tuple[str, ...] = (),
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Complete {task_id}.",
        acceptance_criteria=(f"The {task_id} acceptance criteria are met.",),
        allowed_paths=(path,),
        forbidden_paths=(),
        dependencies=dependencies,
        verification=(VerificationSpec("unit", ("python3", "-m", "unittest"), 60),),
        evidence_requirements=("Report the command and result.",),
        consultation_conditions=("Ask before exceeding the declared scope.",),
    )


def _graph(tasks: tuple[TaskSpec, ...], *, max_active: int = 2) -> GraphSpec:
    nodes = tuple(
        node
        for task in tasks
        for node in (
            NodeRef(f"worker-{task.task_id[-1]}", Role.WORKER),
            NodeRef(f"reviewer-{task.task_id[-1]}", Role.REVIEWER),
        )
    )
    edges = tuple(
        GraphEdge(
            f"worker-{task.task_id[-1]}",
            f"reviewer-{task.task_id[-1]}",
            "reviewed-by",
        )
        for task in tasks
    )
    routes = tuple(
        TaskRoute(
            task.task_id,
            None,
            None,
            f"worker-{task.task_id[-1]}",
            f"reviewer-{task.task_id[-1]}",
        )
        for task in tasks
    )
    return GraphSpec(
        nodes=nodes,
        edges=edges,
        coordination=Coordination(
            "program",
            tuple(f"worker-{task.task_id[-1]}" for task in tasks),
            "parallel",
            max_active,
        ),
        routes=routes,
    )


def _record(
    task: TaskSpec,
    *,
    status: str,
    role: NodeRef,
    stage: str = "implementation",
    revision: str | None = None,
    rounds: int = 0,
) -> dict[str, object]:
    return {
        "spec": task.as_dict(),
        "digest": task_digest(task),
        "dispatch_id": f"dispatch-{task.task_id}",
        "status": status,
        "stage": stage,
        "revision": revision,
        "review_rounds": {"plan": 0, "implementation": rounds},
        "role": role.node_id,
        "role_kind": role.kind.value,
        "writer_role": role.node_id,
        "writer_kind": role.kind.value,
    }


def _assignment(
    task: TaskSpec,
    role: NodeRef,
    *,
    pending: dict[str, object] | None = None,
    question: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
) -> dict[str, object]:
    assignment: dict[str, object] = {
        "role": role.node_id,
        "role_kind": role.kind.value,
        "task_id": f"provider-{task.task_id}",
        "task_spec": task.as_dict(),
        "dispatch_id": f"dispatch-{task.task_id}",
        "terminal_handle": f"terminal-{role.node_id}",
        "launch_nonce": f"nonce-{role.node_id}",
        "completion_observed": False,
    }
    if pending is not None:
        assignment.update(pending)
    if question is not None:
        assignment["native_question"] = question
    if result is not None:
        assignment["native_result"] = result
    return assignment


def _question(
    task: TaskSpec,
    role: NodeRef,
    *,
    answer: str | None = None,
    phase: str = "observed",
) -> dict[str, object]:
    message_id = f"message-{role.node_id}"
    answers = {} if answer is None else {message_id: answer}
    return {
        "role": role.node_id,
        "role_kind": role.kind.value,
        "run_id": "run-1",
        "task_id": f"provider-{task.task_id}",
        "dispatch_id": f"dispatch-{task.task_id}",
        "terminal_handle": f"terminal-{role.node_id}",
        "launch_nonce": f"nonce-{role.node_id}",
        "phase": phase,
        "request": {
            "kind": "question",
            "session_id": f"session-{role.node_id}",
            "tool_call_id": f"tool-{role.node_id}",
            "questions": [{"field": "question_0_custom", "body": "Continue?"}],
        },
        "delivery_id": f"delivery-{role.node_id}",
        "message_ids": [message_id],
        "answers": answers,
        "error": None,
    }


def _state(
    tasks: tuple[TaskSpec, ...],
    *,
    records: dict[str, object] | None = None,
    roles: dict[str, object] | None = None,
    max_active: int = 2,
    phase: str = "writers",
    revision: str | None = None,
) -> dict[str, object]:
    graph = _graph(tasks, max_active=max_active)
    return {
        "version": 5,
        "run_id": "run-1",
        "graph": graph.as_dict(),
        "task_specs": [task.as_dict() for task in tasks],
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "max_review_rounds": 2,
        "tasks": {} if records is None else records,
        "roles": {} if roles is None else roles,
        "native": {"phase": "running"},
        "program_wave": {
            "task_ids": [task.task_id for task in tasks],
            "phase": phase,
            "revision": revision,
        },
    }


def _answered_consultation_record(
    state: dict[str, object], task: TaskSpec, role: NodeRef
) -> dict[str, object]:
    record = _record(
        task,
        status="consultation_required",
        role=role,
        revision=REVISION,
        rounds=1,
    )
    record["review_result"] = {
        "dispatch_id": f"review-{task.task_id}",
        "task_evidence": {
            "stage": "implementation",
            "decision": "consult",
            "findings": ["確認してください"],
        },
    }
    cast(dict[str, object], state["tasks"])[task.task_id] = record
    pending = task_consultation(state, record)
    if pending is None:
        raise AssertionError("consultation fixture was not pending")
    body = "現在の範囲で進めてください"
    record["consultation_answer"] = {
        "consultation_id": pending["consultation_id"],
        "body": body,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    return record


class ParallelProgramPolicyTest(unittest.TestCase):
    def test_selector_admits_second_independent_writer_while_first_runs(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("worker-a", Role.WORKER)
        state = _state(
            (first, second),
            records={
                "task-a": _record(first, status="running", role=first_role),
            },
            roles={"worker-a": _assignment(first, first_role)},
        )

        action = select_action(state)

        self.assertEqual(
            action,
            ProgramAction(
                "dispatch",
                task_id="task-b",
                role=NodeRef("worker-b", Role.WORKER),
                stage="implementation",
                message="宣言されたTaskSpecに沿って実装してください。",
            ),
        )

    def test_selector_prioritizes_completion_and_keeps_peer_question_bound(
        self,
    ) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("worker-a", Role.WORKER)
        second_role = NodeRef("worker-b", Role.WORKER)
        first_assignment = _assignment(
            first,
            first_role,
            pending={
                "pending_delivery_id": "result-a",
                "pending_delivery_kind": "worker_done",
                "pending_delivery_stage": "observed",
            },
            result={"delivery_id": "result-a", "outcome": "succeeded", "body": "a"},
        )
        second_question = _question(second, second_role)
        second_assignment = _assignment(
            second,
            second_role,
            pending={
                "pending_delivery_id": "delivery-worker-b",
                "pending_delivery_kind": "question",
                "pending_delivery_stage": "observed",
                "pending_question_ids": ["message-worker-b"],
                "replied_question_ids": [],
            },
            question=second_question,
        )
        state = _state(
            (first, second),
            records={
                "task-a": _record(first, status="running", role=first_role),
                "task-b": _record(second, status="running", role=second_role),
            },
            roles={"worker-a": first_assignment, "worker-b": second_assignment},
        )

        observed = select_action(state)
        self.assertEqual(observed.kind, "wait")
        self.assertEqual(observed.role, first_role)
        self.assertEqual(observed.task_id, "task-a")

        first_assignment["pending_delivery_stage"] = "released"
        acknowledged = select_action(state)
        self.assertEqual(acknowledged, ProgramAction("acknowledge", role=first_role))

        cast(dict[str, object], state["roles"]).pop("worker-a")
        waiting = select_action(state)
        self.assertEqual(waiting.kind, "wait_user")
        self.assertEqual(waiting.role, second_role)
        self.assertEqual(waiting.task_id, "task-b")

    def test_unanswered_question_does_not_block_independent_ready_peer(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("worker-a", Role.WORKER)
        question = _question(first, first_role)
        state = _state(
            (first, second),
            records={
                "task-a": _record(first, status="running", role=first_role),
            },
            roles={
                "worker-a": _assignment(
                    first,
                    first_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-a",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-a"],
                        "replied_question_ids": [],
                    },
                    question=question,
                )
            },
        )

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-b")
        self.assertEqual(action.role, NodeRef("worker-b", Role.WORKER))

    def test_unobserved_completion_precedes_an_unanswered_peer_question(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("worker-a", Role.WORKER)
        second_role = NodeRef("worker-b", Role.WORKER)
        state = _state(
            (first, second),
            records={
                "task-a": _record(first, status="running", role=first_role),
                "task-b": _record(second, status="running", role=second_role),
            },
            roles={
                "worker-a": _assignment(
                    first,
                    first_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-a",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-a"],
                        "replied_question_ids": [],
                    },
                    question=_question(first, first_role),
                ),
                "worker-b": _assignment(
                    second,
                    second_role,
                    result={
                        "role": "worker-b",
                        "delivery_id": "result-worker-b",
                        "outcome": "succeeded",
                        "body": "completed",
                    },
                ),
            },
        )

        action = select_action(state)

        self.assertEqual(action.kind, "wait")
        self.assertEqual(action.role, second_role)
        self.assertEqual(action.task_id, "task-b")

    def test_published_question_precedes_an_unanswered_peer_question(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("worker-a", Role.WORKER)
        second_role = NodeRef("worker-b", Role.WORKER)
        state = _state(
            (first, second),
            records={
                "task-a": _record(first, status="running", role=first_role),
                "task-b": _record(second, status="running", role=second_role),
            },
            roles={
                "worker-a": _assignment(
                    first,
                    first_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-a",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-a"],
                        "replied_question_ids": [],
                    },
                    question=_question(first, first_role),
                ),
                "worker-b": _assignment(
                    second,
                    second_role,
                    question=_question(second, second_role, phase="published"),
                ),
            },
        )

        action = select_action(state)

        self.assertEqual(action.kind, "wait")
        self.assertEqual(action.role, second_role)
        self.assertEqual(action.task_id, "task-b")

    def test_scope_conflict_is_skipped_for_a_later_independent_candidate(self) -> None:
        first = _task("task-a", "src/")
        conflicting = _task("task-b", "src/a/")
        independent = _task("task-c", "docs/")
        role = NodeRef("worker-a", Role.WORKER)
        state = _state(
            (first, conflicting, independent),
            max_active=3,
            records={"task-a": _record(first, status="running", role=role)},
            roles={"worker-a": _assignment(first, role)},
        )

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-c")
        self.assertEqual(action.role, NodeRef("worker-c", Role.WORKER))

    def test_cap_one_waits_for_the_existing_assignment(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        role = NodeRef("worker-a", Role.WORKER)
        state = _state(
            (first, second),
            max_active=1,
            records={"task-a": _record(first, status="running", role=role)},
            roles={"worker-a": _assignment(first, role)},
        )

        action = select_action(state)

        self.assertEqual(action.kind, "wait")
        self.assertEqual(action.role, role)
        self.assertEqual(action.task_id, "task-a")

    def test_dependency_blocks_candidate_until_predecessor_is_completed(self) -> None:
        first = _task("task-a", "src/a/")
        dependent = _task("task-b", "src/b/", dependencies=("task-a",))
        state = _state((first, dependent))
        cast(dict[str, object], state["program_wave"])["task_ids"] = ["task-a"]

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-a")

    def test_writer_barrier_seals_after_all_parallel_writers_reach_review(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
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
        )

        self.assertEqual(select_action(state), ProgramAction("seal_wave"))

    def test_verification_failure_requeues_the_declared_implementation_writer(
        self,
    ) -> None:
        task = _task("task-a", "src/a/")
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
        )

        action = select_action(state)

        self.assertEqual(action.kind, "dispatch")
        self.assertEqual(action.task_id, "task-a")
        self.assertEqual(action.role, NodeRef("worker-a", Role.WORKER))
        self.assertEqual(action.stage, "implementation")

    def test_review_rejection_waits_for_all_active_reviewers_before_reopen(
        self,
    ) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("reviewer-a", Role.REVIEWER)
        second_role = NodeRef("reviewer-b", Role.REVIEWER)
        state = _state(
            (first, second),
            records={
                "task-a": _record(
                    first,
                    status="implementation_changes_requested",
                    role=first_role,
                    revision=REVISION,
                    rounds=1,
                ),
                "task-b": _record(
                    second,
                    status="implementation_approved",
                    role=second_role,
                    revision=REVISION,
                    rounds=1,
                ),
            },
            roles={
                "reviewer-a": _assignment(first, first_role),
                "reviewer-b": _assignment(second, second_role),
            },
            phase="reviewers",
            revision=REVISION,
        )

        waiting_a = select_action(state)
        self.assertEqual(waiting_a.kind, "wait")
        self.assertEqual(waiting_a.role, first_role)
        cast(dict[str, object], state["roles"]).pop("reviewer-a")
        waiting_b = select_action(state)
        self.assertEqual(waiting_b.kind, "wait")
        self.assertEqual(waiting_b.role, second_role)
        cast(dict[str, object], state["roles"]).pop("reviewer-b")

        reopened = select_action(state)
        self.assertEqual(reopened, ProgramAction("reopen_wave", task_id="task-a"))

    def test_answered_review_consultation_also_waits_for_active_review_peers(
        self,
    ) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("reviewer-a", Role.REVIEWER)
        second_role = NodeRef("reviewer-b", Role.REVIEWER)
        state = _state(
            (first, second),
            phase="reviewers",
            revision=REVISION,
            records={
                "task-b": _record(
                    second,
                    status="implementation_approved",
                    role=second_role,
                    revision=REVISION,
                    rounds=1,
                )
            },
            roles={
                "reviewer-a": _assignment(first, first_role),
                "reviewer-b": _assignment(second, second_role),
            },
        )
        _answered_consultation_record(state, first, first_role)

        waiting = select_action(state)
        self.assertEqual(waiting.kind, "wait")
        self.assertEqual(waiting.role, first_role)
        cast(dict[str, object], state["roles"]).pop("reviewer-a")
        waiting = select_action(state)
        self.assertEqual(waiting.kind, "wait")
        self.assertEqual(waiting.role, second_role)
        cast(dict[str, object], state["roles"]).pop("reviewer-b")

        self.assertEqual(
            select_action(state), ProgramAction("reopen_wave", task_id="task-a")
        )

    def test_review_rework_waits_for_an_unanswered_consultation_in_the_same_wave(self):
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        state = _state(
            (first, second),
            phase="reviewers",
            revision=REVISION,
            records={
                "task-a": _record(
                    first,
                    status="implementation_changes_requested",
                    role=NodeRef("reviewer-a", Role.REVIEWER),
                    revision=REVISION,
                    rounds=1,
                )
            },
        )
        consultation = _answered_consultation_record(
            state, second, NodeRef("reviewer-b", Role.REVIEWER)
        )
        consultation.pop("consultation_answer")
        action = select_action(state)
        self.assertEqual(action.kind, "wait_user")
        self.assertEqual(action.task_id, "task-b")
        self.assertEqual(action.message, "reviewer_consultation_required")

    def test_terminal_failure_is_not_masked_by_a_peer_question_wait(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        second_role = NodeRef("worker-b", Role.WORKER)
        state = _state(
            (first, second),
            records={
                "task-a": _record(
                    first, status="failed", role=NodeRef("worker-a", Role.WORKER)
                )
            },
            roles={
                "worker-b": _assignment(
                    second,
                    second_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-b",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-b"],
                        "replied_question_ids": [],
                    },
                    question=_question(second, second_role),
                )
            },
        )

        action = select_action(state)

        self.assertEqual(action.kind, "pause")
        self.assertEqual(action.task_id, "task-a")
        self.assertEqual(action.message, "terminal_task_failure")

    def test_rejection_is_not_masked_by_a_peer_question_wait(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        second_role = NodeRef("worker-b", Role.WORKER)
        state = _state(
            (first, second),
            roles={
                "worker-b": _assignment(
                    second,
                    second_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-b",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-b"],
                        "replied_question_ids": [],
                    },
                    question=_question(second, second_role),
                )
            },
        )
        state["max_review_rounds"] = 0

        action = select_action(state)

        self.assertEqual(action.kind, "reject")
        self.assertEqual(action.message, "max_review_rounds_invalid")

    def test_failed_question_delivery_is_not_converted_to_a_user_wait(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        first_role = NodeRef("worker-a", Role.WORKER)
        second_role = NodeRef("worker-b", Role.WORKER)
        failed_question = _question(second, second_role, phase="failed")
        failed_question["error"] = "question channel failed"
        state = _state(
            (first, second),
            records={
                "task-a": _record(first, status="running", role=first_role),
                "task-b": _record(second, status="running", role=second_role),
            },
            roles={
                "worker-a": _assignment(
                    first,
                    first_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-a",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-a"],
                        "replied_question_ids": [],
                    },
                    question=_question(first, first_role),
                ),
                "worker-b": _assignment(
                    second,
                    second_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-b",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-b"],
                        "replied_question_ids": [],
                    },
                    question=failed_question,
                ),
            },
        )

        action = select_action(state)

        self.assertEqual(action.kind, "reject")
        self.assertEqual(action.task_id, "task-b")
        self.assertEqual(action.message, "question_delivery_failed")

    def test_parallel_acknowledge_requires_an_exact_role(self) -> None:
        with self.assertRaises(ValueError):
            ProgramAction("acknowledge", task_id="task-a")


class ParallelProgramDriverTest(unittest.TestCase):
    def test_wait_uses_a_positive_timeout_and_the_selected_node(self) -> None:
        class Backend:
            def __init__(self) -> None:
                self.requests: list[object] = []

            def program_snapshot(self) -> dict[str, object]:
                raise AssertionError("program_snapshot is not used")

            def program_transition(self, transition: str) -> None:
                raise AssertionError(f"program_transition is not used: {transition}")

            def request(self, request: BackendRequest) -> BackendResult:
                self.requests.append(request)
                return AckReceipt(True)

        backend = Backend()
        role = NodeRef("worker-a", Role.WORKER)
        program_driver._wait_or_read(
            backend,
            {"version": 5, "roles": {"worker-a": {"role": "worker-a"}}},
            role,
        )

        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        if not isinstance(request, RoleWait):
            raise TypeError("expected a RoleWait request")
        self.assertEqual(request.role, role)
        self.assertGreater(request.timeout_ms, 0)

    def test_acknowledge_reads_delivery_from_the_selected_assignment(self) -> None:
        role_b = NodeRef("worker-b", Role.WORKER)
        state = {
            "version": 5,
            "roles": {
                "worker-a": {"role": "worker-a", "pending_delivery_id": "delivery-a"},
                "worker-b": {"role": "worker-b", "pending_delivery_id": "delivery-b"},
            },
        }

        class Backend:
            def __init__(self) -> None:
                self.requests: list[object] = []

            def program_snapshot(self) -> dict[str, object]:
                raise AssertionError("program_snapshot is not used")

            def program_transition(self, transition: str) -> None:
                raise AssertionError(f"program_transition is not used: {transition}")

            def request(self, request: BackendRequest) -> BackendResult:
                self.requests.append(request)
                return AckReceipt(True)

        backend = Backend()
        program_driver._acknowledge(backend, state, role_b)

        self.assertEqual(backend.requests, [DeliveryAck(DeliveryRef("delivery-b"))])

    def test_notice_keeps_node_identity_and_all_unanswered_question_messages(
        self,
    ) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        role_a = NodeRef("worker-a", Role.WORKER)
        role_b = NodeRef("worker-b", Role.WORKER)
        state = _state(
            (first, second),
            records={
                "task-a": _record(first, status="running", role=role_a),
                "task-b": _record(second, status="running", role=role_b),
            },
            roles={
                "worker-a": _assignment(
                    first,
                    role_a,
                    pending={
                        "pending_delivery_id": "delivery-worker-a",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-a"],
                        "replied_question_ids": [],
                    },
                    question=_question(first, role_a),
                ),
                "worker-b": _assignment(
                    second,
                    role_b,
                    pending={
                        "pending_delivery_id": "delivery-worker-b",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-b"],
                        "replied_question_ids": [],
                    },
                    question=_question(second, role_b),
                ),
            },
        )

        notice = program_driver._notice(
            state,
            reason="question_answer_required",
            task_id="task-b",
            role=role_b,
        )

        self.assertEqual(notice["node_id"], "worker-b")
        messages = cast(list[dict[str, object]], notice["messages"])
        self.assertEqual(messages[0]["message_id"], "message-worker-b")
        questions = cast(list[dict[str, object]], notice["questions"])
        self.assertEqual(
            {item["node_id"] for item in questions}, {"worker-a", "worker-b"}
        )
        self.assertIn("--message-id", str(notice["answer_command"]))

    def test_review_round_limit_without_a_question_has_no_answer_command(self) -> None:
        task = _task("task-a", "src/a/")
        state = _state(
            (task,),
            records={
                "task-a": _record(
                    task,
                    status="awaiting_implementation_review",
                    role=NodeRef("worker-a", Role.WORKER),
                    rounds=2,
                )
            },
            phase="reviewers",
            revision=REVISION,
        )
        action = select_action(state)
        self.assertEqual(action.kind, "wait_user")
        self.assertEqual(action.message, "review_round_limit")

        notice = program_driver._notice(
            state, reason=action.message, task_id=action.task_id, role=action.role
        )

        self.assertEqual(notice["reason"], "review_round_limit")
        self.assertEqual(notice["questions"], [])
        self.assertNotIn("messages", notice)
        self.assertNotIn("answer_command", notice)

    def test_notice_does_not_select_another_node_for_a_nonquestion_wait(self) -> None:
        first, second = _task("task-a", "src/a/"), _task("task-b", "src/b/")
        second_role = NodeRef("worker-b", Role.WORKER)
        state = _state(
            (first, second),
            roles={
                "worker-b": _assignment(
                    second,
                    second_role,
                    pending={
                        "pending_delivery_id": "delivery-worker-b",
                        "pending_delivery_kind": "question",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": ["message-worker-b"],
                        "replied_question_ids": [],
                    },
                    question=_question(second, second_role),
                )
            },
        )

        notice = program_driver._notice(
            state,
            reason="reviewer_consultation_required",
            task_id="task-a",
        )

        self.assertNotIn("node_id", notice)
        self.assertNotIn("messages", notice)
        questions = cast(list[dict[str, object]], notice["questions"])
        self.assertEqual(questions[0]["node_id"], "worker-b")
        self.assertEqual(notice["task_id"], "task-a")
        self.assertIn("--message-id", str(notice["answer_command"]))


if __name__ == "__main__":
    unittest.main()
