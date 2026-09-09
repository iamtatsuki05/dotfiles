from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from typing import cast

from agent_team import task_execution as tasks
from agent_team.contracts import ErrorCode, NodeRef, Role, RuntimeFailure, TaskDispatch
from agent_team.named_graph import (
    Coordination,
    GraphEdge,
    GraphSpec,
    TaskRoute,
    validate_graph,
)
from agent_team.task_spec import TaskSpec, VerificationSpec

REVISION = "a" * 64
NEXT_REVISION = "b" * 64


def _task(
    task_id: str,
    path: str,
    *,
    dependencies: tuple[str, ...] = (),
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Complete {task_id}.",
        acceptance_criteria=(f"The acceptance criteria for {task_id} are met.",),
        allowed_paths=(path,),
        forbidden_paths=("upstreams.json",),
        dependencies=dependencies,
        verification=(VerificationSpec("unit", (sys.executable, "-c", "pass"), 2),),
        evidence_requirements=("Report the command and result.",),
        consultation_conditions=("Ask before exceeding the declared scope.",),
    )


def _catalog() -> tuple[TaskSpec, ...]:
    return (
        _task("task-a", "src/a/"),
        _task("task-b", "src/b/"),
        _task("task-dependency", "src/dependency/"),
        _task("task-c", "src/c/", dependencies=("task-dependency",)),
        _task("task-plan-only", "docs/plan/"),
        _task("task-plan-impl", "src/plan-impl/"),
    )


def _node(node_id: str, kind: Role) -> NodeRef:
    return NodeRef(node_id, kind)


def _graph(catalog: tuple[TaskSpec, ...]) -> GraphSpec:
    nodes: list[NodeRef] = [_node("main", Role.MAIN)]
    edges: list[GraphEdge] = []
    routes: list[TaskRoute] = []
    for task in catalog:
        if task.task_id == "task-plan-only":
            plan_writer = "plan-po"
            plan_reviewer = "review-po"
            nodes.extend(
                (
                    _node(plan_writer, Role.PLANNER),
                    _node(plan_reviewer, Role.REVIEWER),
                )
            )
            edges.extend(
                (
                    GraphEdge("main", plan_writer, "delegates-to"),
                    GraphEdge(plan_writer, plan_reviewer, "reviewed-by"),
                )
            )
            routes.append(
                TaskRoute(task.task_id, plan_writer, plan_reviewer, None, None)
            )
            continue
        if task.task_id == "task-plan-impl":
            plan_writer = "plan-pi"
            plan_reviewer = "review-plan-pi"
            implementation_writer = "work-pi"
            implementation_reviewer = "review-pi"
            nodes.extend(
                (
                    _node(plan_writer, Role.PLANNER),
                    _node(plan_reviewer, Role.REVIEWER),
                    _node(implementation_writer, Role.WORKER),
                    _node(implementation_reviewer, Role.REVIEWER),
                )
            )
            edges.extend(
                (
                    GraphEdge("main", plan_writer, "delegates-to"),
                    GraphEdge(plan_writer, implementation_writer, "delegates-to"),
                    GraphEdge(plan_writer, plan_reviewer, "reviewed-by"),
                    GraphEdge(
                        implementation_writer,
                        implementation_reviewer,
                        "reviewed-by",
                    ),
                )
            )
            routes.append(
                TaskRoute(
                    task.task_id,
                    plan_writer,
                    plan_reviewer,
                    implementation_writer,
                    implementation_reviewer,
                )
            )
            continue

        suffix = task.task_id.removeprefix("task-")
        writer = f"work-{suffix}"
        reviewer = f"review-{suffix}"
        nodes.extend((_node(writer, Role.WORKER), _node(reviewer, Role.REVIEWER)))
        edges.extend(
            (
                GraphEdge("main", writer, "delegates-to"),
                GraphEdge(writer, reviewer, "reviewed-by"),
            )
        )
        routes.append(TaskRoute(task.task_id, None, None, writer, reviewer))

    graph = GraphSpec(
        tuple(nodes),
        tuple(edges),
        Coordination("agent", ("main",), "parallel", 3),
        tuple(routes),
    )
    validate_graph(graph, catalog)
    return graph


def _state(
    *,
    max_review_rounds: int = 3,
    catalog: tuple[TaskSpec, ...] | None = None,
    batch: tuple[str, ...] | None = None,
    batch_phase: str = "writers",
    batch_revision: str | None = None,
) -> dict[str, object]:
    selected = _catalog() if catalog is None else catalog
    graph = _graph(selected)
    value: dict[str, object] = {
        "version": 5,
        "run_id": "run-1",
        "graph": graph.as_dict(),
        "role_specs": {node.node_id: {"kind": node.kind.value} for node in graph.nodes},
        "task_specs": [task.as_dict() for task in selected],
        "max_review_rounds": max_review_rounds,
        "tasks": {},
        "roles": {},
        "native": {"phase": "running"},
    }
    if batch is not None:
        value["agent_batch"] = {
            "task_ids": list(batch),
            "phase": batch_phase,
            "revision": batch_revision,
        }
    return value


def _task_by_id(state: dict[str, object], task_id: str) -> TaskSpec:
    for raw in cast(list[dict[str, object]], state["task_specs"]):
        task = TaskSpec.from_dict(raw)
        if task.task_id == task_id:
            return task
    raise AssertionError(f"unknown fixture task: {task_id}")


def _result(
    *,
    dispatch_id: str,
    target: NodeRef,
    body: str = "provider result",
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "role": target.node_id,
        "role_kind": target.kind.value,
        "outcome": "succeeded",
        "body": body,
    }
    if evidence is not None:
        value["task_evidence"] = evidence
    return value


def _review(
    task: TaskSpec,
    *,
    stage: str,
    revision: str,
    decision: str = "approve",
) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "stage": stage,
        "revision": revision,
        "decision": decision,
        "findings": [] if decision == "approve" else ["Review requires a correction."],
    }


def _review_record(
    state: dict[str, object],
    task: TaskSpec,
    *,
    status: str = "completed",
    review_revision: str = REVISION,
    workspace_revision: str | None = None,
    passed: bool = True,
    cleanup_confirmed: bool = True,
) -> dict[str, object]:
    route = GraphSpec.from_dict(state["graph"]).route(task.task_id)
    plan_only = route.implementation_writer is None
    stage = "plan" if plan_only else "implementation"
    writer_id = route.plan_writer if plan_only else route.implementation_writer
    reviewer_id = route.plan_reviewer if plan_only else route.implementation_reviewer
    if writer_id is None or reviewer_id is None:
        raise AssertionError(f"task {task.task_id} has no final review route")
    writer_kind = Role.PLANNER if plan_only else Role.WORKER
    writer_dispatch = f"writer-{task.task_id}"
    review_dispatch = f"review-{task.task_id}"
    writer_body = f"{task.task_id} result"
    writer = _result(
        dispatch_id=writer_dispatch,
        target=_node(writer_id, writer_kind),
        body=writer_body,
    )
    verdict_revision = (
        hashlib.sha256(writer_body.encode("utf-8")).hexdigest()
        if plan_only
        else review_revision
    )
    verdict = _review(
        task,
        stage=stage,
        revision=verdict_revision,
    )
    review_result = _result(
        dispatch_id=review_dispatch,
        target=_node(reviewer_id, Role.REVIEWER),
        body="approved",
        evidence=verdict,
    )
    command = task.verification[0]
    verification_revision = workspace_revision if plan_only else review_revision
    if verification_revision is None:
        verification_revision = review_revision
    verification = {
        "revision": verification_revision,
        "passed": passed,
        "commands": [
            {
                "name": command.name,
                "argv": list(command.argv),
                "timeout_seconds": command.timeout_seconds,
                "returncode": 0 if passed else 1,
                "stdout_sha256": "c" * 64,
                "stderr_sha256": "d" * 64,
                "error": None if passed else "verification command failed",
            }
        ],
        "error": None if passed else "verification command failed",
        "cleanup_confirmed": cleanup_confirmed,
    }
    record: dict[str, object] = {
        "spec": task.as_dict(),
        "digest": tasks.task_digest(task),
        "dispatch_id": review_dispatch,
        "status": status,
        "stage": stage,
        "revision": verdict_revision,
        "review_rounds": {
            "plan": 1 if plan_only else 0,
            "implementation": 0 if plan_only else 1,
        },
        "role": reviewer_id,
        "role_kind": Role.REVIEWER.value,
        "writer_role": writer_id,
        "writer_kind": writer_kind.value,
        "result": review_result,
        "writer_result": writer,
        "review_result": review_result,
        "review_source_dispatch_id": writer_dispatch,
        "task_evidence": verdict,
        "verification": verification,
    }
    if plan_only:
        record["workspace_revision"] = workspace_revision
    return record


def _put_completed(
    state: dict[str, object], task_id: str, *, revision: str = REVISION
) -> dict[str, object]:
    task = _task_by_id(state, task_id)
    record = _review_record(state, task, review_revision=revision)
    cast(dict[str, object], state["tasks"])[task_id] = record
    return record


def _prepare_writer(
    state: dict[str, object], task_id: str, *, dispatch_id: str, body: str
) -> dict[str, object]:
    task = _task_by_id(state, task_id)
    route = GraphSpec.from_dict(state["graph"]).route(task_id)
    current = cast(
        dict[str, object], cast(dict[str, object], state["tasks"]).get(task_id, {})
    )
    implementation_retry = (
        current.get("status")
        in {
            "plan_approved",
            "verification_failed",
        }
        and route.implementation_writer is not None
    )
    writer_id = (
        route.implementation_writer
        if implementation_retry
        else route.plan_writer or route.implementation_writer
    )
    writer_kind = (
        Role.WORKER
        if implementation_retry or route.plan_writer is None
        else Role.PLANNER
    )
    if writer_id is None:
        raise AssertionError(f"task {task_id} has no writer")
    record, _prompt = tasks.prepare_dispatch(
        state,
        TaskDispatch(
            _node(writer_id, writer_kind),
            task,
            f"write {task_id}",
        ),
    )
    record["dispatch_id"] = dispatch_id
    tasks.acknowledge_task(
        state,
        _result(
            dispatch_id=dispatch_id,
            target=_node(writer_id, writer_kind),
            body=body,
        ),
    )
    return cast(dict[str, object], cast(dict[str, object], state["tasks"])[task_id])


def _prepare_review(
    state: dict[str, object],
    task_id: str,
    *,
    dispatch_id: str,
    revision: str,
    decision: str = "approve",
    workspace_revision: str | None = None,
) -> dict[str, object]:
    task = _task_by_id(state, task_id)
    route = GraphSpec.from_dict(state["graph"]).route(task_id)
    stage = cast(str, cast(dict[str, object], state["tasks"])[task_id]["stage"])
    reviewer_id = (
        route.plan_reviewer if stage == "plan" else route.implementation_reviewer
    )
    if reviewer_id is None:
        raise AssertionError(f"task {task_id} has no reviewer")
    kwargs: dict[str, str] = {"revision": revision}
    if workspace_revision is not None:
        kwargs["workspace_revision"] = workspace_revision
    record, _prompt = tasks.prepare_dispatch(
        state,
        TaskDispatch(_node(reviewer_id, Role.REVIEWER), task, f"review {task_id}"),
        **kwargs,
    )
    record["dispatch_id"] = dispatch_id
    verdict_revision = cast(str, record["revision"])
    tasks.acknowledge_task(
        state,
        _result(
            dispatch_id=dispatch_id,
            target=_node(reviewer_id, Role.REVIEWER),
            body="review result",
            evidence=_review(
                task,
                stage=stage,
                revision=verdict_revision,
                decision=decision,
            ),
        ),
    )
    return cast(dict[str, object], cast(dict[str, object], state["tasks"])[task_id])


def _set_verification(
    task: TaskSpec,
    record: dict[str, object],
    *,
    passed: bool,
    revision: str,
    cleanup_confirmed: bool = True,
) -> None:
    command = task.verification[0]
    record["verification"] = {
        "revision": revision,
        "passed": passed,
        "commands": [
            {
                "name": command.name,
                "argv": list(command.argv),
                "timeout_seconds": command.timeout_seconds,
                "returncode": 0 if passed else 1,
                "stdout_sha256": "e" * 64,
                "stderr_sha256": "f" * 64,
                "error": None if passed else "verification command failed",
            }
        ],
        "error": None if passed else "verification command failed",
        "cleanup_confirmed": cleanup_confirmed,
    }
    record["status"] = "completed" if passed else "verification_failed"


class AgentBatchFixtureTest(unittest.TestCase):
    def test_fixture_is_a_valid_agent_parallel_graph_with_main_delegation_edges(
        self,
    ) -> None:
        state = _state()
        graph = GraphSpec.from_dict(state["graph"])
        self.assertEqual(graph.coordination.mode, "agent")
        self.assertEqual(graph.coordination.dispatch_mode, "parallel")
        self.assertEqual(graph.main_node, _node("main", Role.MAIN))
        validate_graph(
            graph, tuple(TaskSpec.from_dict(value) for value in state["task_specs"])
        )
        self.assertNotIn("program_wave", state)


class AgentBatchOpeningTest(unittest.TestCase):
    def test_open_normalizes_exact_catalog_order_and_initializes_writers_phase(
        self,
    ) -> None:
        state = _state()

        batch = tasks.open_agent_batch(state, ("task-b", "task-a"))

        self.assertEqual(
            batch,
            {"task_ids": ["task-a", "task-b"], "phase": "writers", "revision": None},
        )
        self.assertEqual(state["agent_batch"], batch)

    def test_malformed_batch_ids_fail_without_mutation(self) -> None:
        for malformed in ((), ("task-a", "task-a"), ("task-a", 1)):
            with self.subTest(malformed=malformed):
                state = _state()
                before = copy.deepcopy(state)
                with self.assertRaises(RuntimeFailure) as raised:
                    tasks.open_agent_batch(state, cast(tuple[str, ...], malformed))
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)
                self.assertEqual(state, before)

    def test_semantic_batch_ids_fail_without_mutation(self) -> None:
        cases = (("unknown-task",), ("task-dependency", "task-c"))
        for task_ids in cases:
            with self.subTest(task_ids=task_ids):
                state = _state()
                before = copy.deepcopy(state)
                with self.assertRaises(RuntimeFailure) as raised:
                    tasks.open_agent_batch(state, task_ids)
                self.assertEqual(
                    raised.exception.code,
                    ErrorCode.INVALID_REQUEST
                    if task_ids == ("unknown-task",)
                    else ErrorCode.ORDER_VIOLATION,
                )
                self.assertEqual(state, before)

    def test_dependency_must_be_completed_outside_batch_and_is_preserved(self) -> None:
        state = _state(
            batch=("task-dependency",),
            batch_phase="verification",
            batch_revision=REVISION,
        )
        dependency = _put_completed(state, "task-dependency")
        dependency_before = copy.deepcopy(dependency)

        tasks.open_agent_batch(state, ("task-c",))

        self.assertEqual(state["agent_batch"]["task_ids"], ["task-c"])
        self.assertEqual(state["tasks"]["task-dependency"], dependency_before)

    def test_unresolved_external_dependency_fails_without_mutation(self) -> None:
        state = _state()
        before = copy.deepcopy(state)

        with self.assertRaises(RuntimeFailure) as raised:
            tasks.open_agent_batch(state, ("task-c",))

        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

    def test_replacing_batch_requires_completed_members_but_keeps_exact_ids(
        self,
    ) -> None:
        state = _state(batch=("task-a", "task-b"))
        before = copy.deepcopy(state)

        with self.assertRaises(RuntimeFailure) as raised:
            tasks.open_agent_batch(state, ("task-c",))

        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

        state["agent_batch"] = {
            "task_ids": ["task-a", "task-b"],
            "phase": "verification",
            "revision": REVISION,
        }
        _put_completed(state, "task-a")
        _put_completed(state, "task-b")
        tasks.open_agent_batch(state, ("task-plan-only",))
        self.assertEqual(state["agent_batch"]["task_ids"], ["task-plan-only"])

    def test_replacement_requires_roles_and_delivery_cleanup_without_mutation(
        self,
    ) -> None:
        state = _state(
            batch=("task-a", "task-b"),
            batch_phase="verification",
            batch_revision=REVISION,
        )
        _put_completed(state, "task-a")
        _put_completed(state, "task-b")
        for roles, pending_delivery in (
            ({"work-a": {"task_spec": _task_by_id(state, "task-a").as_dict()}}, None),
            ({}, "delivery-pending"),
        ):
            state["roles"] = roles
            state["pending_delivery_id"] = pending_delivery
            before = copy.deepcopy(state)
            with (
                self.subTest(roles=bool(roles), pending=pending_delivery),
                self.assertRaises(RuntimeFailure) as raised,
            ):
                tasks.open_agent_batch(state, ("task-plan-only",))
            self.assertEqual(raised.exception.code, ErrorCode.BUSY)
            self.assertEqual(state, before)
        state["roles"] = {}
        state["pending_delivery_id"] = None
        state["tasks"]["task-a"]["status"] = "verifying"
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.open_agent_batch(state, ("task-plan-only",))
        self.assertEqual(raised.exception.code, ErrorCode.BUSY)
        self.assertEqual(state, before)

        state["tasks"]["task-a"]["status"] = "completed"
        tasks.open_agent_batch(state, ("task-plan-only",))

        self.assertEqual(state["agent_batch"]["task_ids"], ["task-plan-only"])
        self.assertEqual(state["roles"], {})
        self.assertIsNone(state["pending_delivery_id"])


class AgentBatchDispatchTest(unittest.TestCase):
    def test_dispatch_requires_explicit_batch_and_rejects_nonmember_without_mutation(
        self,
    ) -> None:
        state = _state()
        task = _task_by_id(state, "task-a")
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(_node("work-a", Role.WORKER), task, "write"),
            )
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

        tasks.open_agent_batch(state, ("task-a", "task-b"))
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(
                    _node("work-dependency", Role.WORKER),
                    _task_by_id(state, "task-dependency"),
                    "write outside batch",
                ),
            )
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

    def test_final_review_waits_for_all_writers_and_binds_one_batch_revision(
        self,
    ) -> None:
        state = _state()
        tasks.open_agent_batch(state, ("task-a", "task-b"))
        _prepare_writer(state, "task-a", dispatch_id="write-a", body="a result")
        task_b = _task_by_id(state, "task-b")
        record_b, _prompt = tasks.prepare_dispatch(
            state,
            TaskDispatch(_node("work-b", Role.WORKER), task_b, "write task-b"),
        )
        record_b["dispatch_id"] = "write-b"
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(
                    _node("review-a", Role.REVIEWER),
                    _task_by_id(state, "task-a"),
                    "review",
                ),
                revision=REVISION,
            )
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

        tasks.acknowledge_task(
            state,
            _result(
                dispatch_id="write-b",
                target=_node("work-b", Role.WORKER),
                body="b result",
            ),
        )
        _prepare_review(state, "task-a", dispatch_id="review-a", revision=REVISION)
        self.assertEqual(
            state["agent_batch"],
            {
                "task_ids": ["task-a", "task-b"],
                "phase": "reviewers",
                "revision": REVISION,
            },
        )

        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(
                    _node("review-b", Role.REVIEWER),
                    _task_by_id(state, "task-b"),
                    "review",
                ),
                revision=NEXT_REVISION,
            )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(state, before)
        _prepare_review(state, "task-b", dispatch_id="review-b", revision=REVISION)

    def test_intermediate_plan_review_stays_in_writers_phase_in_mixed_batch(
        self,
    ) -> None:
        state = _state()
        tasks.open_agent_batch(state, ("task-plan-impl", "task-plan-only", "task-a"))
        _prepare_writer(
            state, "task-plan-impl", dispatch_id="write-pi", body="plan body"
        )
        _prepare_writer(
            state, "task-plan-only", dispatch_id="write-po", body="plan only body"
        )
        _prepare_writer(state, "task-a", dispatch_id="write-a", body="implementation")

        plan_revision = hashlib.sha256(b"plan body").hexdigest()
        _prepare_review(
            state,
            "task-plan-impl",
            dispatch_id="review-plan-pi",
            revision=plan_revision,
        )
        self.assertEqual(state["agent_batch"]["phase"], "writers")
        self.assertEqual(state["tasks"]["task-plan-impl"]["status"], "plan_approved")

        _prepare_writer(
            state, "task-plan-impl", dispatch_id="write-pi-2", body="implementation"
        )
        _prepare_review(
            state,
            "task-plan-only",
            dispatch_id="review-po",
            revision=hashlib.sha256(b"plan only body").hexdigest(),
            workspace_revision=REVISION,
        )
        self.assertEqual(state["agent_batch"]["phase"], "reviewers")
        self.assertEqual(state["agent_batch"]["revision"], REVISION)
        self.assertEqual(
            state["tasks"]["task-plan-only"]["workspace_revision"], REVISION
        )
        _prepare_review(state, "task-a", dispatch_id="review-a", revision=REVISION)
        _prepare_review(
            state, "task-plan-impl", dispatch_id="review-pi", revision=REVISION
        )
        tasks.prepare_agent_batch_verification(state, "task-a", REVISION)
        self.assertEqual(state["agent_batch"]["phase"], "verification")

    def test_plan_only_approved_peer_reopens_with_implementation_retry(self) -> None:
        state = _state()
        tasks.open_agent_batch(state, ("task-plan-only", "task-a"))
        _prepare_writer(
            state, "task-plan-only", dispatch_id="write-po", body="plan only body"
        )
        _prepare_writer(state, "task-a", dispatch_id="write-a", body="implementation")
        plan_revision = hashlib.sha256(b"plan only body").hexdigest()
        _prepare_review(
            state,
            "task-plan-only",
            dispatch_id="review-po",
            revision=plan_revision,
            workspace_revision=REVISION,
        )
        _prepare_review(
            state,
            "task-a",
            dispatch_id="review-a",
            revision=REVISION,
            decision="request_changes",
        )
        plan_peer = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])["task-plan-only"]
        )
        peer_review = copy.deepcopy(plan_peer["review_result"])
        peer_rounds = copy.deepcopy(plan_peer["review_rounds"])

        _prepare_writer(
            state, "task-a", dispatch_id="retry-a", body="repaired implementation"
        )

        self.assertEqual(state["agent_batch"]["phase"], "writers")
        self.assertEqual(
            state["tasks"]["task-plan-only"]["status"], "awaiting_plan_review"
        )
        self.assertEqual(state["tasks"]["task-plan-only"]["review_result"], peer_review)
        self.assertEqual(state["tasks"]["task-plan-only"]["review_rounds"], peer_rounds)
        self.assertEqual(
            state["tasks"]["task-a"]["status"], "awaiting_implementation_review"
        )

        self.assertEqual(state["tasks"]["task-plan-only"]["revision"], plan_revision)
        self.assertIsNone(state["tasks"]["task-plan-only"]["workspace_revision"])
        _prepare_review(
            state,
            "task-plan-only",
            dispatch_id="review-po-2",
            revision=plan_revision,
            workspace_revision=NEXT_REVISION,
        )
        _prepare_review(
            state, "task-a", dispatch_id="review-a-2", revision=NEXT_REVISION
        )
        tasks.prepare_agent_batch_verification(state, "task-plan-only", NEXT_REVISION)
        self.assertEqual(state["agent_batch"]["revision"], NEXT_REVISION)
        self.assertEqual(state["tasks"]["task-plan-only"]["revision"], plan_revision)

    def test_wrong_route_and_changed_task_spec_fail_without_sealing_or_mutation(
        self,
    ) -> None:
        state = _state()
        tasks.open_agent_batch(state, ("task-a", "task-b"))
        _prepare_writer(state, "task-a", dispatch_id="write-a", body="a")
        _prepare_writer(state, "task-b", dispatch_id="write-b", body="b")
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(
                    _node("review-b", Role.REVIEWER),
                    _task_by_id(state, "task-a"),
                    "wrong route",
                ),
                revision=REVISION,
            )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(state, before)

        altered = _task_by_id(state, "task-a").as_dict()
        altered["allowed_paths"] = ["src/other/"]
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(
                    _node("review-a", Role.REVIEWER),
                    TaskSpec.from_dict(altered),
                    "changed spec",
                ),
                revision=REVISION,
            )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(state, before)


class AgentBatchVerificationTest(unittest.TestCase):
    def _approved_state(self) -> dict[str, object]:
        state = _state()
        tasks.open_agent_batch(state, ("task-a", "task-b"))
        _prepare_writer(state, "task-a", dispatch_id="write-a", body="a")
        _prepare_writer(state, "task-b", dispatch_id="write-b", body="b")
        _prepare_review(state, "task-a", dispatch_id="review-a", revision=REVISION)
        _prepare_review(state, "task-b", dispatch_id="review-b", revision=REVISION)
        return state

    def test_verification_requires_final_approval_membership_revision_and_drain(
        self,
    ) -> None:
        state = self._approved_state()
        before = copy.deepcopy(state)
        cast(dict[str, object], state["roles"])["review-a"] = {
            "task_spec": _task_by_id(state, "task-a").as_dict(),
        }
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_agent_batch_verification(state, "task-a", REVISION)
        self.assertEqual(raised.exception.code, ErrorCode.BUSY)
        del cast(dict[str, object], state["roles"])["review-a"]
        self.assertEqual(state, before)

        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_agent_batch_verification(state, "task-c", REVISION)
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

        tasks.prepare_agent_batch_verification(state, "task-a", REVISION)
        self.assertEqual(state["agent_batch"]["phase"], "verification")

    def test_repeated_verification_allows_completed_or_failed_peers(self) -> None:
        state = self._approved_state()
        tasks.prepare_agent_batch_verification(state, "task-a", REVISION)
        record_b = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])["task-b"]
        )
        _set_verification(
            _task_by_id(state, "task-b"), record_b, passed=False, revision=REVISION
        )
        tasks.prepare_agent_batch_verification(state, "task-a", REVISION)
        self.assertEqual(state["agent_batch"]["phase"], "verification")

    def test_unanswered_consultation_blocks_retry_and_answered_consultation_reopens_atomically(
        self,
    ) -> None:
        state = self._approved_state()
        # Rebuild the sealed reviewer results so one peer needs consultation.
        record_a = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])["task-a"]
        )
        record_a["status"] = "consultation_required"
        verdict = _review(
            _task_by_id(state, "task-a"),
            stage="implementation",
            revision=REVISION,
            decision="consult",
        )
        record_a["task_evidence"] = verdict
        review_result = cast(dict[str, object], record_a["review_result"])
        review_result["task_evidence"] = verdict
        pending = tasks.task_consultation(state, record_a)
        self.assertIsNotNone(pending)
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(
                    _node("work-a", Role.WORKER), _task_by_id(state, "task-a"), "retry"
                ),
            )
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

        assert pending is not None
        tasks.answer_task_consultation(
            state, cast(str, pending["consultation_id"]), "Proceed within scope."
        )
        _prepare_writer(state, "task-a", dispatch_id="retry-a", body="retry result")
        self.assertEqual(state["agent_batch"]["phase"], "writers")
        self.assertEqual(
            state["tasks"]["task-a"]["status"], "awaiting_implementation_review"
        )

    def test_max_review_rounds_blocks_valid_retry_without_mutation(self) -> None:
        state = _state(max_review_rounds=1)
        tasks.open_agent_batch(state, ("task-a", "task-b"))
        _prepare_writer(state, "task-a", dispatch_id="write-a", body="a")
        _prepare_writer(state, "task-b", dispatch_id="write-b", body="b")
        _prepare_review(state, "task-a", dispatch_id="review-a", revision=REVISION)
        _prepare_review(
            state,
            "task-b",
            dispatch_id="review-b",
            revision=REVISION,
            decision="request_changes",
        )
        before = copy.deepcopy(state)
        with self.assertRaises(RuntimeFailure) as raised:
            tasks.prepare_dispatch(
                state,
                TaskDispatch(
                    _node("work-b", Role.WORKER), _task_by_id(state, "task-b"), "retry"
                ),
            )
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(state, before)

    def test_retry_after_partial_verification_reopens_peers_and_preserves_dependency(
        self,
    ) -> None:
        state = _state(
            batch=("task-dependency",),
            batch_phase="verification",
            batch_revision=REVISION,
        )
        dependency = _put_completed(state, "task-dependency")
        dependency_before = copy.deepcopy(dependency)
        tasks.open_agent_batch(state, ("task-a", "task-b"))
        _prepare_writer(state, "task-a", dispatch_id="write-a", body="a")
        _prepare_writer(state, "task-b", dispatch_id="write-b", body="b")
        _prepare_review(state, "task-a", dispatch_id="review-a", revision=REVISION)
        _prepare_review(state, "task-b", dispatch_id="review-b", revision=REVISION)
        tasks.prepare_agent_batch_verification(state, "task-a", REVISION)
        record_a = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])["task-a"]
        )
        record_b = cast(
            dict[str, object], cast(dict[str, object], state["tasks"])["task-b"]
        )
        rounds_a = copy.deepcopy(record_a["review_rounds"])
        review_a = copy.deepcopy(record_a["review_result"])
        _set_verification(
            _task_by_id(state, "task-a"), record_a, passed=True, revision=REVISION
        )
        _set_verification(
            _task_by_id(state, "task-b"), record_b, passed=False, revision=REVISION
        )

        _prepare_writer(state, "task-b", dispatch_id="retry-b", body="b repaired")

        self.assertEqual(
            state["agent_batch"],
            {"task_ids": ["task-a", "task-b"], "phase": "writers", "revision": None},
        )
        self.assertEqual(
            state["tasks"]["task-a"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(state["tasks"]["task-a"]["review_rounds"], rounds_a)
        self.assertEqual(state["tasks"]["task-a"]["review_result"], review_a)
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(state["tasks"]["task-dependency"], dependency_before)


if __name__ == "__main__":
    unittest.main()
