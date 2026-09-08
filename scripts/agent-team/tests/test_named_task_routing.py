from __future__ import annotations

import hashlib
import unittest
from copy import deepcopy
from typing import cast

from agent_team.contracts import (
    ErrorCode,
    NodeRef,
    Role,
    RuntimeFailure,
    TaskDispatch,
)
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.task_execution import (
    acknowledge_task,
    prepare_dispatch,
    validate_saved_tasks,
    validate_task_assignment,
)
from agent_team.task_spec import TaskSpec, VerificationSpec


def _task(task_id: str = "task-a") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective="Complete the requested change.",
        acceptance_criteria=("The acceptance criteria are met.",),
        allowed_paths=("agent_team/",),
        forbidden_paths=("upstreams.json",),
        dependencies=(),
        verification=(VerificationSpec("unit", ("python3", "-m", "unittest"), 60),),
        evidence_requirements=("Report the command and result.",),
        consultation_conditions=("Ask before exceeding the declared scope.",),
    )


def _node(node_id: str, kind: Role) -> NodeRef:
    return NodeRef(node_id=node_id, kind=kind)


def _graph(*, plan_only: bool = False, implementation_only: bool = False) -> GraphSpec:
    nodes = (
        _node("main", Role.MAIN),
        _node("plan-a", Role.PLANNER),
        _node("plan-b", Role.PLANNER),
        _node("work-a", Role.WORKER),
        _node("work-b", Role.WORKER),
        _node("review-plan-a", Role.REVIEWER),
        _node("review-plan-b", Role.REVIEWER),
        _node("review-a", Role.REVIEWER),
        _node("review-b", Role.REVIEWER),
    )
    edges = (
        GraphEdge("main", "plan-a", "delegates-to"),
        GraphEdge("main", "plan-b", "delegates-to"),
        GraphEdge("plan-a", "work-a", "delegates-to"),
        GraphEdge("plan-b", "work-b", "delegates-to"),
        GraphEdge("plan-a", "review-plan-a", "reviewed-by"),
        GraphEdge("plan-b", "review-plan-b", "reviewed-by"),
        GraphEdge("work-a", "review-a", "reviewed-by"),
        GraphEdge("work-b", "review-b", "reviewed-by"),
    )
    return GraphSpec(
        nodes=nodes,
        edges=edges,
        coordination=Coordination("agent", ("main",), "serial", 1),
        routes=(
            TaskRoute(
                "task-a",
                None if implementation_only else "plan-a",
                None if implementation_only else "review-plan-a",
                None if plan_only else "work-a",
                None if plan_only else "review-a",
            ),
        ),
    )


def _state(
    *,
    plan_only: bool = False,
    implementation_only: bool = False,
    max_review_rounds: int = 2,
) -> dict[str, object]:
    graph = _graph(plan_only=plan_only, implementation_only=implementation_only)
    return {
        "version": 4,
        "graph": graph.as_dict(),
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "max_review_rounds": max_review_rounds,
        "tasks": {},
        "task_specs": [_task().as_dict()],
    }


def _result(
    *,
    dispatch_id: str,
    target: NodeRef,
    body: str = "provider result",
    outcome: str = "succeeded",
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "role": target.node_id,
        "role_kind": target.kind.value,
        "outcome": outcome,
        "body": body,
    }
    if evidence is not None:
        value["task_evidence"] = evidence
    return value


def _record(state: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], cast(dict[str, object], state["tasks"])["task-a"])


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


class NamedTaskRoutingTest(unittest.TestCase):
    def test_plan_pair_forbids_initial_worker_dispatch(self) -> None:
        saved = _state()
        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(
                saved,
                TaskDispatch(_node("work-a", Role.WORKER), _task(), "skip plan"),
            )
        self.assertEqual(saved, before)

    def test_each_named_writer_and_reviewer_route_is_exact(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        plan_reviewer = _node("review-plan-a", Role.REVIEWER)
        worker = _node("work-a", Role.WORKER)
        implementation_reviewer = _node("review-a", Role.REVIEWER)

        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        review, _prompt = prepare_dispatch(
            saved, TaskDispatch(plan_reviewer, current, "review plan")
        )
        review["dispatch_id"] = "plan-review-dispatch"
        plan_verdict = _review(
            current,
            stage="plan",
            revision=hashlib.sha256(b"plan body").hexdigest(),
        )
        acknowledge_task(
            saved,
            _result(
                dispatch_id="plan-review-dispatch",
                target=plan_reviewer,
                evidence=plan_verdict,
            ),
        )

        implementation, _prompt = prepare_dispatch(
            saved, TaskDispatch(worker, current, "implement")
        )
        implementation["dispatch_id"] = "implementation-dispatch"
        acknowledge_task(
            saved,
            _result(
                dispatch_id="implementation-dispatch",
                target=worker,
                body="implementation",
            ),
        )
        review, _prompt = prepare_dispatch(
            saved,
            TaskDispatch(implementation_reviewer, current, "review implementation"),
            revision="tree-revision",
        )
        review["dispatch_id"] = "implementation-review-dispatch"

        self.assertEqual(_record(saved)["status"], "reviewing_implementation")
        self.assertEqual(_record(saved)["role"], implementation_reviewer.node_id)
        self.assertEqual(_record(saved)["role_kind"], Role.REVIEWER.value)
        self.assertEqual(_record(saved)["writer_role"], worker.node_id)
        self.assertEqual(_record(saved)["writer_kind"], Role.WORKER.value)

    def test_same_kind_different_node_cannot_replace_writer_or_reviewer(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure) as raised:
            prepare_dispatch(
                saved,
                TaskDispatch(_node("plan-b", Role.PLANNER), current, "wrong plan"),
            )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(saved, before)

        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure) as raised:
            prepare_dispatch(
                saved,
                TaskDispatch(
                    _node("review-plan-b", Role.REVIEWER), current, "wrong reviewer"
                ),
            )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(saved, before)

    def test_cross_stage_reviewer_substitution_is_rejected(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure) as raised:
            prepare_dispatch(
                saved,
                TaskDispatch(_node("review-a", Role.REVIEWER), current, "wrong stage"),
            )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(saved, before)

    def test_result_requires_matching_node_id_and_kind(self) -> None:
        saved = _state(implementation_only=True)
        current = _task()
        worker = _node("work-a", Role.WORKER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(worker, current, "implement")
        )
        prepared["dispatch_id"] = "worker-dispatch"
        before = deepcopy(saved)
        for forged in (
            {"role": "work-b", "role_kind": Role.WORKER.value},
            {"role": "work-a", "role_kind": Role.REVIEWER.value},
            {"role": "work-a", "role_kind": None},
        ):
            value = _result(dispatch_id="worker-dispatch", target=worker)
            value.update(forged)
            with self.subTest(forged=forged), self.assertRaises(RuntimeFailure):
                acknowledge_task(saved, value)
            self.assertEqual(saved, before)

    def test_saved_record_tampering_with_named_identity_is_rejected(self) -> None:
        saved = _state(implementation_only=True)
        current = _task()
        worker = _node("work-a", Role.WORKER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(worker, current, "implement")
        )
        prepared["dispatch_id"] = "worker-dispatch"
        _record(saved)["role"] = "work-b"
        with self.assertRaises(RuntimeFailure) as raised:
            validate_saved_tasks(saved)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_saved_role_spec_kind_must_match_the_graph(self) -> None:
        saved = _state()
        role_specs = cast(dict[str, object], saved["role_specs"])
        cast(dict[str, object], role_specs["work-a"])["kind"] = Role.REVIEWER.value
        with self.assertRaises(RuntimeFailure) as raised:
            validate_saved_tasks(saved)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_saved_plan_revision_must_match_the_planner_result_digest(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        _record(saved)["revision"] = "forged-plan-revision"
        with self.assertRaises(RuntimeFailure) as raised:
            validate_saved_tasks(saved)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_saved_plan_review_round_cannot_be_reset_after_request_changes(
        self,
    ) -> None:
        saved = _state(max_review_rounds=1)
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        reviewer = _node("review-plan-a", Role.REVIEWER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(reviewer, current, "review plan")
        )
        prepared["dispatch_id"] = "review-dispatch"
        acknowledge_task(
            saved,
            _result(
                dispatch_id="review-dispatch",
                target=reviewer,
                evidence=_review(
                    current,
                    stage="plan",
                    revision=hashlib.sha256(b"plan body").hexdigest(),
                    decision="request_changes",
                    findings=["revise"],
                ),
            ),
        )
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "revise plan")
        )
        prepared["dispatch_id"] = "retry-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="retry-dispatch", target=planner, body="retry plan"),
        )
        cast(dict[str, int], _record(saved)["review_rounds"])["plan"] = 0
        with self.assertRaises(RuntimeFailure) as raised:
            validate_saved_tasks(saved)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_saved_approval_requires_matching_review_evidence(self) -> None:
        cases = (
            (False, "plan_approved", "review-plan-a", Role.PLANNER, "plan"),
            (
                True,
                "implementation_approved",
                "review-a",
                Role.WORKER,
                "implementation",
            ),
        )
        for implementation_only, status, reviewer_id, writer_kind, stage in cases:
            with self.subTest(status=status):
                saved = _state(implementation_only=implementation_only)
                current = _task()
                writer_id = "work-a" if implementation_only else "plan-a"
                writer = _node(writer_id, writer_kind)
                prepared, _prompt = prepare_dispatch(
                    saved, TaskDispatch(writer, current, "write")
                )
                prepared["dispatch_id"] = "writer-dispatch"
                body = "implementation" if implementation_only else "plan body"
                acknowledge_task(
                    saved,
                    _result(
                        dispatch_id="writer-dispatch",
                        target=writer,
                        body=body,
                    ),
                )
                record = _record(saved)
                reviewer = _node(reviewer_id, Role.REVIEWER)
                record.update(
                    {
                        "status": status,
                        "role": reviewer.node_id,
                        "role_kind": reviewer.kind.value,
                        "stage": stage,
                        "revision": (
                            "tree-revision"
                            if implementation_only
                            else hashlib.sha256(body.encode()).hexdigest()
                        ),
                    }
                )
                record.pop("task_evidence", None)
                record.pop("review_result", None)
                with self.assertRaises(RuntimeFailure) as raised:
                    validate_saved_tasks(saved)
                self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_saved_writer_result_body_must_match_record_result(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        cast(dict[str, object], _record(saved)["result"])["body"] = "tampered body"
        with self.assertRaises(RuntimeFailure) as raised:
            validate_saved_tasks(saved)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_saved_review_result_must_be_successful_and_match_record_result(
        self,
    ) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        reviewer = _node("review-plan-a", Role.REVIEWER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(reviewer, current, "review plan")
        )
        prepared["dispatch_id"] = "review-dispatch"
        acknowledge_task(
            saved,
            _result(
                dispatch_id="review-dispatch",
                target=reviewer,
                evidence=_review(
                    current,
                    stage="plan",
                    revision=hashlib.sha256(b"plan body").hexdigest(),
                ),
            ),
        )
        cast(dict[str, object], _record(saved)["review_result"])["outcome"] = "failed"
        with self.assertRaises(RuntimeFailure) as raised:
            validate_saved_tasks(saved)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_saved_writer_result_identity_must_match_the_named_route(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        writer_result = cast(dict[str, object], _record(saved)["writer_result"])
        writer_result["role"] = "plan-b"
        with self.assertRaises(RuntimeFailure) as raised:
            validate_saved_tasks(saved)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_named_assignment_identity_is_bound_to_the_saved_node(self) -> None:
        saved = _state(implementation_only=True)
        current = _task()
        worker = _node("work-a", Role.WORKER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(worker, current, "implement")
        )
        prepared["dispatch_id"] = "worker-dispatch"
        assignment = {
            "task_spec": current.as_dict(),
            "dispatch_id": "worker-dispatch",
            "role": "work-a",
            "role_kind": Role.WORKER.value,
        }
        self.assertEqual(validate_task_assignment(saved, assignment), current)
        forged = dict(assignment)
        forged["role"] = "work-b"
        with self.assertRaises(RuntimeFailure) as raised:
            validate_task_assignment(saved, forged)
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_request_changes_retry_keeps_exact_writer_reviewer_and_round(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        reviewer = _node("review-plan-a", Role.REVIEWER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(reviewer, current, "review plan")
        )
        prepared["dispatch_id"] = "review-dispatch"
        verdict = _review(
            current,
            stage="plan",
            revision=hashlib.sha256(b"plan body").hexdigest(),
            decision="request_changes",
            findings=["revise"],
        )
        acknowledge_task(
            saved,
            _result(dispatch_id="review-dispatch", target=reviewer, evidence=verdict),
        )
        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(
                saved,
                TaskDispatch(_node("plan-b", Role.PLANNER), current, "replace writer"),
            )
        self.assertEqual(saved, before)
        retry, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "revise the same plan")
        )
        self.assertEqual(retry["writer_role"], planner.node_id)
        self.assertEqual(retry["writer_kind"], Role.PLANNER.value)
        self.assertEqual(cast(dict[str, int], retry["review_rounds"])["plan"], 1)

    def test_implementation_review_rejects_a_stale_verdict_revision(self) -> None:
        saved = _state(implementation_only=True)
        current = _task()
        worker = _node("work-a", Role.WORKER)
        reviewer = _node("review-a", Role.REVIEWER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(worker, current, "implement")
        )
        prepared["dispatch_id"] = "worker-dispatch"
        acknowledge_task(
            saved,
            _result(
                dispatch_id="worker-dispatch",
                target=worker,
                body="implementation",
            ),
        )
        prepared, _prompt = prepare_dispatch(
            saved,
            TaskDispatch(reviewer, current, "review implementation"),
            revision="tree-revision-new",
        )
        prepared["dispatch_id"] = "review-dispatch"
        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure) as raised:
            acknowledge_task(
                saved,
                _result(
                    dispatch_id="review-dispatch",
                    target=reviewer,
                    evidence=_review(
                        current,
                        stage="implementation",
                        revision="tree-revision-old",
                    ),
                ),
            )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(saved, before)

    def test_consult_keeps_named_reviewer_and_does_not_advance_task(self) -> None:
        saved = _state()
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        reviewer = _node("review-plan-a", Role.REVIEWER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "make a plan")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(reviewer, current, "review plan")
        )
        prepared["dispatch_id"] = "review-dispatch"
        verdict = _review(
            current,
            stage="plan",
            revision=hashlib.sha256(b"plan body").hexdigest(),
            decision="consult",
            findings=["needs user input"],
        )
        acknowledge_task(
            saved,
            _result(dispatch_id="review-dispatch", target=reviewer, evidence=verdict),
        )
        record = _record(saved)
        self.assertEqual(record["status"], "consultation_required")
        self.assertEqual(record["role"], reviewer.node_id)
        self.assertEqual(record["role_kind"], Role.REVIEWER.value)

    def test_plan_only_route_never_silently_becomes_completed(self) -> None:
        saved = _state(plan_only=True)
        current = _task()
        planner = _node("plan-a", Role.PLANNER)
        reviewer = _node("review-plan-a", Role.REVIEWER)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(planner, current, "investigate")
        )
        prepared["dispatch_id"] = "plan-dispatch"
        acknowledge_task(
            saved,
            _result(dispatch_id="plan-dispatch", target=planner, body="plan body"),
        )
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(reviewer, current, "review investigation")
        )
        prepared["dispatch_id"] = "review-dispatch"
        verdict = _review(
            current,
            stage="plan",
            revision=hashlib.sha256(b"plan body").hexdigest(),
        )
        acknowledge_task(
            saved,
            _result(dispatch_id="review-dispatch", target=reviewer, evidence=verdict),
        )
        self.assertEqual(_record(saved)["status"], "plan_approved")
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(
                saved,
                TaskDispatch(
                    _node("work-a", Role.WORKER), current, "implicit implementation"
                ),
            )
        self.assertNotEqual(_record(saved)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
