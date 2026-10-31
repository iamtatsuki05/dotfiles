from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from typing import cast

from agent_team.contracts import NodeRef, Role, RuntimeFailure, TaskDispatch
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.program_policy import ProgramAction, select_action
from agent_team.task_execution import (
    acknowledge_task,
    prepare_dispatch,
    transition_program_wave,
    validate_saved_tasks,
)
from agent_team.task_spec import TaskSpec, VerificationSpec

PLAN_BODY = "plan body"
PLAN_REVISED_BODY = "revised plan body"
CODE_REVISION = "a" * 64
NEXT_CODE_REVISION = "b" * 64
STALE_CODE_REVISION = "c" * 64


def _task(task_id: str, *, dependencies: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Complete {task_id}.",
        acceptance_criteria=(f"The {task_id} acceptance criteria are met.",),
        allowed_paths=(f"{task_id}.txt",),
        forbidden_paths=("upstreams.json",),
        dependencies=dependencies,
        verification=(VerificationSpec("unit", (sys.executable, "-c", "pass"), 5),),
        evidence_requirements=("Report the command and result.",),
        consultation_conditions=("Ask before exceeding the declared scope.",),
    )


def _result(
    target: NodeRef,
    dispatch_id: str,
    *,
    body: str = "provider result",
    evidence: dict[str, object] | None = None,
    outcome: str = "succeeded",
) -> dict[str, object]:
    result: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "role": target.node_id,
        "role_kind": target.kind.value,
        "outcome": outcome,
        "body": body,
    }
    if evidence is not None:
        result["task_evidence"] = evidence
    return result


def _review(
    task: TaskSpec,
    *,
    stage: str,
    revision: str,
    decision: str = "approve",
    findings: list[str] | None = None,
) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "stage": stage,
        "revision": revision,
        "decision": decision,
        "findings": [] if findings is None else findings,
    }


def _verification(task: TaskSpec, revision: str) -> dict[str, object]:
    empty = hashlib.sha256(b"").hexdigest()
    return {
        "revision": revision,
        "passed": True,
        "commands": [
            {
                "name": command.name,
                "argv": list(command.argv),
                "timeout_seconds": command.timeout_seconds,
                "returncode": 0,
                "stdout_sha256": empty,
                "stderr_sha256": empty,
                "error": None,
            }
            for command in task.verification
        ],
        "error": None,
        "cleanup_confirmed": True,
    }


def _pure_plan_state(*, max_review_rounds: int = 2) -> dict[str, object]:
    from test_named_task_routing import _graph

    graph = _graph(plan_only=True)
    return {
        "version": 4,
        "graph": graph.as_dict(),
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "task_specs": [_task("task-a").as_dict()],
        "max_review_rounds": max_review_rounds,
        "tasks": {},
        "roles": {},
    }


def _mixed_graph() -> GraphSpec:
    nodes = (
        NodeRef("planner-a", Role.PLANNER),
        NodeRef("plan-reviewer-a", Role.REVIEWER),
        NodeRef("worker-b", Role.WORKER),
        NodeRef("reviewer-b", Role.REVIEWER),
    )
    return GraphSpec(
        nodes=nodes,
        edges=(
            GraphEdge("planner-a", "plan-reviewer-a", "reviewed-by"),
            GraphEdge("worker-b", "reviewer-b", "reviewed-by"),
        ),
        coordination=Coordination("program", ("planner-a", "worker-b"), "serial", 1),
        routes=(
            TaskRoute("task-a", "planner-a", "plan-reviewer-a", None, None),
            TaskRoute("task-b", None, None, "worker-b", "reviewer-b"),
        ),
    )


def _mixed_state(*, max_review_rounds: int = 2) -> dict[str, object]:
    graph = _mixed_graph()
    tasks = (_task("task-a"), _task("task-b"))
    return {
        "version": 4,
        "graph": graph.as_dict(),
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "task_specs": [task.as_dict() for task in tasks],
        "max_review_rounds": max_review_rounds,
        "tasks": {},
        "roles": {},
        "program_wave": {
            "task_ids": [task.task_id for task in tasks],
            "phase": "writers",
            "revision": None,
        },
    }


def _dispatch_and_ack(
    state: dict[str, object],
    task: TaskSpec,
    target: NodeRef,
    dispatch_id: str,
    *,
    body: str,
    workspace_revision: str | None = None,
) -> None:
    prepared, _prompt = prepare_dispatch(
        state,
        TaskDispatch(target, task, "作成してください"),
        revision=None,
        workspace_revision=workspace_revision,
    )
    prepared["dispatch_id"] = dispatch_id
    acknowledge_task(state, _result(target, dispatch_id, body=body))
    validate_saved_tasks(state)


def _review_and_ack(
    state: dict[str, object],
    task: TaskSpec,
    target: NodeRef,
    dispatch_id: str,
    *,
    stage: str,
    revision: str,
    decision: str = "approve",
    workspace_revision: str | None = None,
) -> None:
    prepared, _prompt = prepare_dispatch(
        state,
        TaskDispatch(target, task, "レビューしてください"),
        revision=None if stage == "plan" else revision,
        workspace_revision=workspace_revision,
    )
    prepared["dispatch_id"] = dispatch_id
    acknowledge_task(
        state,
        _result(
            target,
            dispatch_id,
            body="review result",
            evidence=_review(
                task,
                stage=stage,
                revision=revision,
                decision=decision,
                findings=[] if decision == "approve" else ["修正が必要"],
            ),
        ),
    )
    validate_saved_tasks(state)


def _mark_completed(state: dict[str, object], task_id: str, revision: str) -> None:
    records = cast(dict[str, object], state["tasks"])
    record = cast(dict[str, object], records[task_id])
    task = TaskSpec.from_dict(record["spec"])
    record["status"] = "completed"
    record["verification"] = _verification(task, revision)
    validate_saved_tasks(state)


class PlanOnlyCompletionTest(unittest.TestCase):
    def test_plan_only_approval_requires_workspace_revision_and_validates_completion(
        self,
    ) -> None:
        state = _pure_plan_state()
        task = _task("task-a")
        planner = NodeRef("plan-a", Role.PLANNER)
        reviewer = NodeRef("review-plan-a", Role.REVIEWER)

        _dispatch_and_ack(state, task, planner, "plan-dispatch-1", body=PLAN_BODY)
        plan_revision = hashlib.sha256(PLAN_BODY.encode()).hexdigest()
        _review_and_ack(
            state,
            task,
            reviewer,
            "plan-review-dispatch-1",
            stage="plan",
            revision=plan_revision,
            workspace_revision=CODE_REVISION,
        )

        record = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])[task.task_id]
        )
        self.assertEqual(record["status"], "plan_approved")
        self.assertEqual(record["revision"], plan_revision)
        self.assertEqual(record["workspace_revision"], CODE_REVISION)

        _mark_completed(state, task.task_id, CODE_REVISION)
        self.assertEqual(record["status"], "completed")

    def test_plan_hash_cannot_be_used_as_workspace_revision_or_verification_revision(
        self,
    ) -> None:
        state = _pure_plan_state()
        task = _task("task-a")
        _dispatch_and_ack(
            state,
            task,
            NodeRef("plan-a", Role.PLANNER),
            "plan-dispatch-1",
            body=PLAN_BODY,
        )
        plan_revision = hashlib.sha256(PLAN_BODY.encode()).hexdigest()
        _review_and_ack(
            state,
            task,
            NodeRef("review-plan-a", Role.REVIEWER),
            "plan-review-dispatch-1",
            stage="plan",
            revision=plan_revision,
            workspace_revision=CODE_REVISION,
        )
        _mark_completed(state, task.task_id, CODE_REVISION)
        record = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])[task.task_id]
        )

        record["verification"] = copy.deepcopy(record["verification"])
        cast(dict[str, object], record["verification"])["revision"] = plan_revision
        with self.assertRaises(RuntimeFailure):
            validate_saved_tasks(state)

        cast(dict[str, object], record["verification"])["revision"] = CODE_REVISION
        record.pop("workspace_revision")
        with self.assertRaises(RuntimeFailure):
            validate_saved_tasks(state)

    def test_plan_review_limit_is_preserved_after_retry(self) -> None:
        state = _pure_plan_state(max_review_rounds=1)
        task = _task("task-a")
        planner = NodeRef("plan-a", Role.PLANNER)
        reviewer = NodeRef("review-plan-a", Role.REVIEWER)

        _dispatch_and_ack(state, task, planner, "plan-dispatch-1", body=PLAN_BODY)
        plan_revision = hashlib.sha256(PLAN_BODY.encode()).hexdigest()
        _review_and_ack(
            state,
            task,
            reviewer,
            "plan-review-dispatch-1",
            stage="plan",
            revision=plan_revision,
            decision="request_changes",
            workspace_revision=CODE_REVISION,
        )
        _dispatch_and_ack(
            state, task, planner, "plan-dispatch-2", body=PLAN_REVISED_BODY
        )
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(
                state,
                TaskDispatch(reviewer, task, "再レビューしてください"),
                revision=None,
                workspace_revision=NEXT_CODE_REVISION,
            )
        self.assertEqual(state, before)
        record = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])[task.task_id]
        )
        self.assertEqual(record["review_rounds"], {"plan": 1, "implementation": 0})

    def test_mixed_wave_seals_after_all_writers_then_reviews_plan_only_at_same_code_revision(
        self,
    ) -> None:
        state = _mixed_state()
        plan_task, implementation_task = _task("task-a"), _task("task-b")
        planner = NodeRef("planner-a", Role.PLANNER)
        plan_reviewer = NodeRef("plan-reviewer-a", Role.REVIEWER)
        worker = NodeRef("worker-b", Role.WORKER)
        implementation_reviewer = NodeRef("reviewer-b", Role.REVIEWER)

        _dispatch_and_ack(state, plan_task, planner, "plan-dispatch-1", body=PLAN_BODY)
        plan_revision = hashlib.sha256(PLAN_BODY.encode()).hexdigest()
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(
                state,
                TaskDispatch(plan_reviewer, plan_task, "最終レビューしてください"),
                revision=None,
                workspace_revision=CODE_REVISION,
            )
        self.assertEqual(state, before)
        self.assertEqual(
            select_action(state),
            ProgramAction(
                "dispatch",
                task_id="task-b",
                role=worker,
                stage="implementation",
                message="宣言されたTaskSpecに沿って実装してください。",
            ),
        )

        _dispatch_and_ack(
            state,
            implementation_task,
            worker,
            "implementation-dispatch-1",
            body="implementation",
        )
        self.assertEqual(select_action(state), ProgramAction("seal_wave"))
        transition_program_wave(state, "seal_wave", revision=CODE_REVISION)
        self.assertEqual(
            cast(dict[str, object], state["program_wave"]),
            {
                "task_ids": ["task-a", "task-b"],
                "phase": "reviewers",
                "revision": CODE_REVISION,
            },
        )

        for bad_revision in (None, plan_revision, STALE_CODE_REVISION):
            before = copy.deepcopy(state)
            with self.assertRaises(RuntimeFailure):
                prepare_dispatch(
                    state,
                    TaskDispatch(plan_reviewer, plan_task, "最終レビューしてください"),
                    revision=None,
                    workspace_revision=bad_revision,
                )
            self.assertEqual(state, before)

        _review_and_ack(
            state,
            plan_task,
            plan_reviewer,
            "plan-review-dispatch-1",
            stage="plan",
            revision=plan_revision,
            workspace_revision=CODE_REVISION,
        )
        _review_and_ack(
            state,
            implementation_task,
            implementation_reviewer,
            "implementation-review-dispatch-1",
            stage="implementation",
            revision=CODE_REVISION,
        )
        self.assertEqual(select_action(state), ProgramAction("verify_wave"))
        transition_program_wave(state, "verify_wave")
        self.assertEqual(
            select_action(state),
            ProgramAction("verify", task_id="task-a", stage="verification"),
        )

        _mark_completed(state, "task-a", CODE_REVISION)
        self.assertEqual(
            select_action(state),
            ProgramAction("verify", task_id="task-b", stage="verification"),
        )
        _mark_completed(state, "task-b", CODE_REVISION)
        self.assertEqual(select_action(state), ProgramAction("complete"))

    def test_mixed_wave_reopens_plan_changes_and_reapproves_at_new_revision(
        self,
    ) -> None:
        state = _mixed_state()
        plan_task, implementation_task = _task("task-a"), _task("task-b")
        planner = NodeRef("planner-a", Role.PLANNER)
        plan_reviewer = NodeRef("plan-reviewer-a", Role.REVIEWER)
        worker = NodeRef("worker-b", Role.WORKER)
        implementation_reviewer = NodeRef("reviewer-b", Role.REVIEWER)

        _dispatch_and_ack(state, plan_task, planner, "plan-dispatch-1", body=PLAN_BODY)
        _dispatch_and_ack(
            state,
            implementation_task,
            worker,
            "implementation-dispatch-1",
            body="implementation",
        )
        transition_program_wave(state, "seal_wave", revision=CODE_REVISION)
        plan_revision = hashlib.sha256(PLAN_BODY.encode()).hexdigest()
        _review_and_ack(
            state,
            plan_task,
            plan_reviewer,
            "plan-review-dispatch-1",
            stage="plan",
            revision=plan_revision,
            decision="request_changes",
            workspace_revision=CODE_REVISION,
        )
        _review_and_ack(
            state,
            implementation_task,
            implementation_reviewer,
            "implementation-review-dispatch-1",
            stage="implementation",
            revision=CODE_REVISION,
        )
        self.assertEqual(
            select_action(state), ProgramAction("reopen_wave", task_id="task-a")
        )
        transition_program_wave(state, "reopen_wave")

        records = cast(dict[str, object], state["tasks"])
        plan_record = cast(dict[str, object], records["task-a"])
        implementation_record = cast(dict[str, object], records["task-b"])
        self.assertEqual(plan_record["status"], "plan_changes_requested")
        self.assertEqual(plan_record["role"], plan_reviewer.node_id)
        self.assertEqual(plan_record["revision"], plan_revision)
        self.assertEqual(plan_record["workspace_revision"], CODE_REVISION)
        self.assertEqual(
            cast(dict[str, object], plan_record["writer_result"])["body"], PLAN_BODY
        )
        self.assertEqual(plan_record["review_rounds"], {"plan": 1, "implementation": 0})
        self.assertEqual(
            implementation_record["status"], "awaiting_implementation_review"
        )
        self.assertEqual(implementation_record["role"], worker.node_id)
        self.assertEqual(
            cast(dict[str, object], implementation_record["writer_result"])["body"],
            "implementation",
        )
        self.assertEqual(
            implementation_record["review_rounds"], {"plan": 0, "implementation": 1}
        )
        validate_saved_tasks(state)

        self.assertEqual(
            select_action(state),
            ProgramAction(
                "dispatch",
                task_id="task-a",
                role=planner,
                stage="plan",
                message="宣言されたTaskSpecに沿って計画を作成してください。",
            ),
        )
        _dispatch_and_ack(
            state, plan_task, planner, "plan-dispatch-2", body=PLAN_REVISED_BODY
        )
        self.assertEqual(select_action(state), ProgramAction("seal_wave"))
        transition_program_wave(state, "seal_wave", revision=NEXT_CODE_REVISION)
        revised_plan_revision = hashlib.sha256(PLAN_REVISED_BODY.encode()).hexdigest()
        _review_and_ack(
            state,
            plan_task,
            plan_reviewer,
            "plan-review-dispatch-2",
            stage="plan",
            revision=revised_plan_revision,
            workspace_revision=NEXT_CODE_REVISION,
        )
        _review_and_ack(
            state,
            implementation_task,
            implementation_reviewer,
            "implementation-review-dispatch-2",
            stage="implementation",
            revision=NEXT_CODE_REVISION,
        )
        self.assertEqual(select_action(state), ProgramAction("verify_wave"))
        transition_program_wave(state, "verify_wave")
        validate_saved_tasks(state)

    def test_reopen_invalidates_approved_plan_only_peer_before_implementation_retry(
        self,
    ) -> None:
        state = _mixed_state()
        plan_task, implementation_task = _task("task-a"), _task("task-b")
        planner = NodeRef("planner-a", Role.PLANNER)
        plan_reviewer = NodeRef("plan-reviewer-a", Role.REVIEWER)
        worker = NodeRef("worker-b", Role.WORKER)
        implementation_reviewer = NodeRef("reviewer-b", Role.REVIEWER)

        _dispatch_and_ack(state, plan_task, planner, "plan-dispatch-1", body=PLAN_BODY)
        _dispatch_and_ack(
            state,
            implementation_task,
            worker,
            "implementation-dispatch-1",
            body="implementation",
        )
        transition_program_wave(state, "seal_wave", revision=CODE_REVISION)
        plan_revision = hashlib.sha256(PLAN_BODY.encode()).hexdigest()
        _review_and_ack(
            state,
            plan_task,
            plan_reviewer,
            "plan-review-dispatch-1",
            stage="plan",
            revision=plan_revision,
            workspace_revision=CODE_REVISION,
        )
        _review_and_ack(
            state,
            implementation_task,
            implementation_reviewer,
            "implementation-review-dispatch-1",
            stage="implementation",
            revision=CODE_REVISION,
            decision="request_changes",
        )
        self.assertEqual(
            select_action(state), ProgramAction("reopen_wave", task_id="task-b")
        )
        transition_program_wave(state, "reopen_wave")

        records = cast(dict[str, object], state["tasks"])
        plan_record = cast(dict[str, object], records["task-a"])
        implementation_record = cast(dict[str, object], records["task-b"])
        self.assertEqual(plan_record["status"], "awaiting_plan_review")
        self.assertEqual(plan_record["role"], planner.node_id)
        self.assertEqual(plan_record["revision"], plan_revision)
        self.assertIsNone(plan_record["workspace_revision"])
        self.assertEqual(
            cast(dict[str, object], plan_record["writer_result"])["body"], PLAN_BODY
        )
        self.assertEqual(
            implementation_record["status"], "implementation_changes_requested"
        )
        self.assertEqual(implementation_record["role"], implementation_reviewer.node_id)
        self.assertEqual(implementation_record["revision"], CODE_REVISION)
        validate_saved_tasks(state)

        self.assertEqual(
            select_action(state),
            ProgramAction(
                "dispatch",
                task_id="task-b",
                role=worker,
                stage="implementation",
                message="宣言されたTaskSpecに沿って実装してください。",
            ),
        )
        _dispatch_and_ack(
            state,
            implementation_task,
            worker,
            "implementation-dispatch-2",
            body="implementation retry",
        )
        self.assertEqual(select_action(state), ProgramAction("seal_wave"))
        transition_program_wave(state, "seal_wave", revision=NEXT_CODE_REVISION)

        _review_and_ack(
            state,
            plan_task,
            plan_reviewer,
            "plan-review-dispatch-2",
            stage="plan",
            revision=plan_revision,
            workspace_revision=NEXT_CODE_REVISION,
        )
        _review_and_ack(
            state,
            implementation_task,
            implementation_reviewer,
            "implementation-review-dispatch-2",
            stage="implementation",
            revision=NEXT_CODE_REVISION,
        )
        self.assertEqual(select_action(state), ProgramAction("verify_wave"))
        transition_program_wave(state, "verify_wave")
        validate_saved_tasks(state)


if __name__ == "__main__":
    unittest.main()
