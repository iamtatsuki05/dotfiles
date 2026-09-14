from __future__ import annotations

import copy
import io
import unittest
from contextlib import redirect_stdout
from typing import Literal, cast

from agent_team import native_program, program_driver, program_policy
from agent_team.contracts import (
    AckReceipt,
    BackendRequest,
    BackendResult,
    DeliveryAck,
    DeliveryRef,
    NodeRef,
    ReadReceipt,
    ReleaseReceipt,
    Role,
    RoleRead,
    RoleRelease,
)
from agent_team.mcp_protocol import MAX_READ_LINES
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.task_execution import task_digest
from agent_team.task_spec import TaskSpec, VerificationSpec

REVISION = "a" * 64


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Complete {task_id}.",
        acceptance_criteria=(f"The {task_id} acceptance criteria are met.",),
        allowed_paths=(f"{task_id}.txt",),
        forbidden_paths=(),
        dependencies=(),
        verification=(VerificationSpec("unit", ("python3", "-m", "unittest"), 60),),
        evidence_requirements=("Report the command and result.",),
        consultation_conditions=("Ask before exceeding the declared scope.",),
    )


def _graph(
    tasks: tuple[TaskSpec, ...], *, dispatch_mode: Literal["serial", "parallel"]
) -> GraphSpec:
    nodes = tuple(
        node
        for task in tasks
        for node in (
            NodeRef(f"worker-{task.task_id[-1]}", Role.WORKER),
            NodeRef(f"reviewer-{task.task_id[-1]}", Role.REVIEWER),
        )
    )
    return GraphSpec(
        nodes=nodes,
        edges=tuple(
            GraphEdge(
                f"worker-{task.task_id[-1]}",
                f"reviewer-{task.task_id[-1]}",
                "reviewed-by",
            )
            for task in tasks
        ),
        coordination=Coordination(
            "program",
            tuple(f"worker-{task.task_id[-1]}" for task in tasks),
            dispatch_mode,
            1 if dispatch_mode == "serial" else len(tasks),
        ),
        routes=tuple(
            TaskRoute(
                task.task_id,
                None,
                None,
                f"worker-{task.task_id[-1]}",
                f"reviewer-{task.task_id[-1]}",
            )
            for task in tasks
        ),
    )


def _record(
    task: TaskSpec, role: NodeRef, *, status: str = "running"
) -> dict[str, object]:
    return {
        "spec": task.as_dict(),
        "digest": task_digest(task),
        "dispatch_id": f"dispatch-{task.task_id}",
        "status": status,
        "stage": "implementation",
        "revision": REVISION,
        "review_rounds": {"plan": 0, "implementation": 0},
        "role": role.node_id,
        "role_kind": role.kind.value,
        "writer_role": role.node_id,
        "writer_kind": role.kind.value,
    }


def _assignment(task: TaskSpec, role: NodeRef) -> dict[str, object]:
    return {
        "role": role.node_id,
        "role_kind": role.kind.value,
        "task_id": f"provider-{task.task_id}",
        "task_spec": task.as_dict(),
        "dispatch_id": f"dispatch-{task.task_id}",
        "terminal_handle": f"terminal-{role.node_id}",
        "launch_nonce": f"nonce-{role.node_id}",
        "completion_observed": False,
    }


def _completion_assignment(task: TaskSpec, role: NodeRef) -> dict[str, object]:
    assignment = _assignment(task, role)
    assignment.update(
        {
            "pending_delivery_id": "run-delivery",
            "pending_delivery_kind": "worker_done",
            "pending_delivery_stage": "observed",
            "orca_result": {
                "delivery_id": "run-delivery",
                "role": role.node_id,
                "outcome": "succeeded",
                "body": f"completed {task.task_id}",
            },
        }
    )
    return assignment


def _question_assignment(task: TaskSpec, role: NodeRef) -> dict[str, object]:
    assignment = _assignment(task, role)
    assignment.update(
        {
            "pending_delivery_id": "run-delivery",
            "pending_delivery_kind": "question",
            "pending_delivery_stage": "observed",
            "pending_question_ids": ["message-worker-b"],
            "replied_question_ids": [],
            "orca_question": {
                "phase": "observed",
                "message_id": "message-worker-b",
                "request": {
                    "kind": "question",
                    "session_id": "session-worker-b",
                    "tool_call_id": "tool-worker-b",
                    "questions": [{"field": "question_0_custom", "body": "Continue?"}],
                },
                "answers": None,
            },
        }
    )
    return assignment


def _parallel_state() -> dict[str, object]:
    first, second = _task("task-a"), _task("task-b")
    graph = _graph((first, second), dispatch_mode="parallel")
    role_a = NodeRef("worker-a", Role.WORKER)
    role_b = NodeRef("worker-b", Role.WORKER)
    return {
        "version": 5,
        "runtime": "orca",
        "run_id": "run-1",
        "graph": graph.as_dict(),
        "task_specs": [first.as_dict(), second.as_dict()],
        "max_review_rounds": 2,
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "tasks": {
            "task-a": _record(first, role_a),
            "task-b": _record(second, role_b),
        },
        "roles": {
            "worker-a": _completion_assignment(first, role_a),
            "worker-b": _question_assignment(second, role_b),
        },
        "program_wave": {
            "task_ids": ["task-a", "task-b"],
            "phase": "writers",
            "revision": None,
        },
        "orca_delivery_batch": {
            "delivery_id": "run-delivery",
            "phase": "observed",
            "members": [
                {
                    "role": "worker-a",
                    "role_kind": "worker",
                    "task_id": "provider-task-a",
                    "dispatch_id": "dispatch-task-a",
                    "terminal_handle": "terminal-worker-a",
                    "launch_nonce": "nonce-worker-a",
                    "message_id": "message-worker-a",
                    "kind": "worker_done",
                },
                {
                    "role": "worker-b",
                    "role_kind": "worker",
                    "task_id": "provider-task-b",
                    "dispatch_id": "dispatch-task-b",
                    "terminal_handle": "terminal-worker-b",
                    "launch_nonce": "nonce-worker-b",
                    "message_id": "message-worker-b",
                    "kind": "question",
                },
            ],
        },
    }


def _serial_question_state() -> dict[str, object]:
    task = _task("task-a")
    graph = _graph((task,), dispatch_mode="serial")
    role = NodeRef("worker-a", Role.WORKER)
    assignment = _assignment(task, role)
    assignment["orca_question"] = {
        "session_id": "session-a",
        "tool_call_id": "tool-a",
        "message_id": "message-a",
        "phase": "observed",
        "request": {
            "kind": "question",
            "session_id": "session-a",
            "tool_call_id": "tool-a",
            "questions": [{"field": "question_0_custom", "body": "Continue?"}],
        },
        "answers": None,
        "thread_id": "message-a",
        "answer_message_id": None,
        "answer_sha256": None,
        "error": None,
    }
    return {
        "version": 4,
        "runtime": "orca",
        "run_id": "run-1",
        "graph": graph.as_dict(),
        "task_specs": [task.as_dict()],
        "max_review_rounds": 2,
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "tasks": {"task-a": _record(task, role)},
        "roles": {"worker-a": assignment},
        "program_wave": {
            "task_ids": ["task-a"],
            "phase": "writers",
            "revision": None,
        },
        "pending_delivery_id": "delivery-a",
        "pending_delivery_kind": "question",
        "pending_delivery_stage": "observed",
        "pending_question_ids": ["message-a"],
        "replied_question_ids": [],
    }


class _BatchBackend:
    def __init__(self) -> None:
        self.state = _parallel_state()
        self.requests: list[BackendRequest] = []

    def program_snapshot(self) -> dict[str, object]:
        return copy.deepcopy(self.state)

    def program_transition(self, transition: str) -> None:
        raise AssertionError(f"unexpected transition: {transition}")

    def request(self, request: BackendRequest) -> BackendResult:
        self.requests.append(request)
        if isinstance(request, RoleRead):
            if not isinstance(request.role, NodeRef):
                raise TypeError("expected a named RoleRead target")
            roles = cast(dict[str, dict[str, object]], self.state["roles"])
            assignment = roles[request.role.node_id]
            assignment["pending_delivery_stage"] = "read"
            return ReadReceipt("completion")
        if isinstance(request, RoleRelease):
            if not isinstance(request.role, NodeRef):
                raise TypeError("expected a named RoleRelease target")
            roles = cast(dict[str, dict[str, object]], self.state["roles"])
            roles[request.role.node_id]["pending_delivery_stage"] = "released"
            question = cast(dict[str, object], roles["worker-b"]["orca_question"])
            question["phase"] = "replied"
            question["answers"] = {"question_0_custom": "yes"}
            return ReleaseReceipt("released")
        if isinstance(request, DeliveryAck):
            self.assert_delivery(request)
            self.state.pop("orca_delivery_batch")
            cast(dict[str, dict[str, object]], self.state["roles"]).clear()
            for record in cast(
                dict[str, dict[str, object]], self.state["tasks"]
            ).values():
                record["status"] = "completed"
            cast(dict[str, object], self.state["program_wave"]).update(
                {"phase": "verification", "revision": REVISION}
            )
            return AckReceipt(True)
        raise AssertionError(f"unexpected request: {request!r}")

    def assert_delivery(self, request: DeliveryAck) -> None:
        self_delivery = request.delivery_id
        if self_delivery != DeliveryRef("run-delivery"):
            raise AssertionError(f"unexpected Delivery: {self_delivery!r}")


class ProgramDriverTest(unittest.TestCase):
    def test_fifo_completion_is_drained_before_question_and_whole_batch_ack(
        self,
    ) -> None:
        state = _parallel_state()

        self.assertEqual(
            program_policy.select_action(state),
            program_policy.ProgramAction(
                "wait",
                task_id="task-a",
                role=NodeRef("worker-a", Role.WORKER),
                stage="implementation",
                message="completion_drain_required",
            ),
        )
        roles = cast(dict[str, dict[str, object]], state["roles"])
        roles["worker-a"]["pending_delivery_stage"] = "read"
        self.assertEqual(
            program_policy.select_action(state),
            program_policy.ProgramAction(
                "wait",
                task_id="task-a",
                role=NodeRef("worker-a", Role.WORKER),
                stage="implementation",
                message="completion_drain_required",
            ),
        )
        roles["worker-a"]["pending_delivery_stage"] = "released"
        question = cast(
            dict[str, object],
            roles["worker-b"]["orca_question"],
        )
        question["phase"] = "replied"
        question["answers"] = {"question_0_custom": "yes"}
        self.assertEqual(
            program_policy.select_action(state),
            program_policy.ProgramAction("acknowledge"),
        )

    def test_observed_batch_does_not_dispatch_while_question_is_unanswered(
        self,
    ) -> None:
        state = _parallel_state()
        roles = cast(dict[str, dict[str, object]], state["roles"])
        roles["worker-a"]["pending_delivery_stage"] = "released"

        action = program_policy.select_action(state)

        self.assertEqual(action.kind, "wait_user")
        self.assertEqual(action.role, NodeRef("worker-b", Role.WORKER))
        self.assertEqual(action.message, "question_answer_required")

    def test_missing_completion_owner_pauses_instead_of_assuming_released(self) -> None:
        state = _parallel_state()
        cast(dict[str, dict[str, object]], state["roles"]).pop("worker-a")

        action = program_policy.select_action(state)

        self.assertEqual(action.kind, "pause")
        self.assertEqual(action.message, "orca_delivery_batch_assignment_unknown")

    def test_batch_subset_can_progress_with_an_active_peer_outside_the_batch(
        self,
    ) -> None:
        state = _parallel_state()
        graph = cast(dict[str, object], state["graph"])
        coordination = cast(dict[str, object], graph["coordination"])
        coordination["max_active"] = 3
        roles = cast(dict[str, dict[str, object]], state["roles"])
        peer = _assignment(_task("task-a"), NodeRef("reviewer-a", Role.REVIEWER))
        roles["reviewer-a"] = peer

        action = program_policy.select_action(state)

        self.assertEqual(action.kind, "wait")
        self.assertEqual(action.role, NodeRef("worker-a", Role.WORKER))

    def test_publish_before_wait_does_not_pause_without_a_pending_delivery(
        self,
    ) -> None:
        state = _parallel_state()
        state.pop("orca_delivery_batch")
        roles = cast(dict[str, dict[str, object]], state["roles"])
        for assignment in roles.values():
            for key in (
                "pending_delivery_id",
                "pending_delivery_kind",
                "pending_delivery_stage",
                "pending_question_ids",
                "replied_question_ids",
            ):
                assignment.pop(key, None)
        question = cast(dict[str, object], roles["worker-b"]["orca_question"])
        question["phase"] = "asking"

        action = program_policy.select_action(state)

        self.assertEqual(action.kind, "wait")
        self.assertEqual(action.role, NodeRef("worker-a", Role.WORKER))

    def test_lost_batch_ack_effect_pauses_and_retains_state(self) -> None:
        state = _parallel_state()
        batch = cast(dict[str, object], state["orca_delivery_batch"])
        batch["phase"] = "acknowledging"
        before = copy.deepcopy(state)

        action = program_policy.select_action(state)

        self.assertEqual(
            action,
            program_policy.ProgramAction(
                "pause", message="orca_delivery_batch_effect_unconfirmed"
            ),
        )
        self.assertEqual(state, before)

    def test_unknown_assignment_effect_pauses_without_a_batch(self) -> None:
        state = _parallel_state()
        state.pop("orca_delivery_batch")
        roles = cast(dict[str, dict[str, object]], state["roles"])
        roles["worker-a"]["pending_orca_effect"] = {"operation": "unknown"}

        action = program_policy.select_action(state)

        self.assertEqual(action.kind, "pause")
        self.assertEqual(action.message, "orca_delivery_batch_effect_unconfirmed")

    def test_orca_serial_notice_exposes_exact_json_answer_fields(self) -> None:
        state = _serial_question_state()
        action = program_policy.select_action(state)
        self.assertEqual(action.kind, "wait_user")
        self.assertEqual(action.role, NodeRef("worker-a", Role.WORKER))

        notice = program_driver._notice(
            state,
            reason="question_answer_required",
            task_id="task-a",
            role=NodeRef("worker-a", Role.WORKER),
        )

        self.assertEqual(notice["answer_fields"], ["question_0_custom"])
        self.assertEqual(notice["answer_template"], {"question_0_custom": "<answer>"})
        self.assertIn("--message-id", str(notice["answer_command"]))

    def test_native_entrypoint_uses_common_driver(self) -> None:
        from pathlib import Path
        from unittest import mock

        from agent_team import cli
        from agent_team.native_backend import NativeBackend

        backend = mock.Mock(spec=NativeBackend)
        with (
            mock.patch.object(native_program, "_ready_state", return_value={}),
            mock.patch.object(cli, "_management_plan_from_state", return_value={}),
            mock.patch.object(cli, "_runtime_engine", return_value=(None, backend)),
            mock.patch.object(cli, "_start_spec", return_value=mock.sentinel.spec),
            mock.patch.object(program_driver, "drive", return_value=0) as driver,
        ):
            self.assertEqual(native_program.run(Path("/state.json"), "run-1"), 0)
        backend.start.assert_called_once_with(mock.sentinel.spec)
        driver.assert_called_once_with(backend)

    def test_driver_applies_fifo_drain_and_one_whole_batch_ack(self) -> None:
        backend = _BatchBackend()

        with redirect_stdout(io.StringIO()):
            result = program_driver.drive(backend)

        self.assertEqual(result, 0)
        self.assertEqual(
            [type(request).__name__ for request in backend.requests],
            ["RoleRead", "RoleRelease", "DeliveryAck"],
        )
        self.assertEqual(
            cast(DeliveryAck, backend.requests[-1]).delivery_id,
            DeliveryRef("run-delivery"),
        )

    def test_orca_completion_read_uses_orca_read_line_limit(self) -> None:
        backend = _BatchBackend()
        program_driver._wait_or_read(
            backend, backend.state, NodeRef("worker-a", Role.WORKER)
        )

        request = cast(RoleRead, backend.requests[-1])
        self.assertEqual(request.lines, MAX_READ_LINES)


if __name__ == "__main__":
    unittest.main()
