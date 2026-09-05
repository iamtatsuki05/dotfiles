from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from inspect import signature
from typing import cast

import agent_team.review_policy as review_policy_module
from agent_team.review_policy import (
    AssignmentCommand,
    CompletionId,
    DecisionRef,
    DependencyState,
    PolicyAuthorityProjection,
    PolicyFingerprint,
    PolicyProjectionKind,
    ReviewDecision,
    ReviewDecisionKind,
    ReviewerAssignment,
    ReviewPair,
    ReviewPolicyError,
    ReviewPolicyHandoffPort,
    ReviewPolicyState,
    ReviewPolicyUpdate,
    ReviewRequest,
    RunId,
    SerialReviewPolicy,
    TerminalId,
    WorkerAssignment,
    WorkerCompletion,
    WorkerCompletionKind,
    initial_review_policy_state,
    policy_authority_projection,
    reduce_policy,
    resolve_worker_reviewer_pair,
    validate_policy_authority_projection,
    validate_policy_update,
    validate_reviewer_assignment,
)
from agent_team.task_policy import (
    STATE_POLICY_VERSION,
    AttemptId,
    ClaimRef,
    DispatchId,
    GitObjectId,
    ReceiptRef,
    StateConflictError,
    TaskId,
    TaskKind,
    TaskLane,
    TaskPhase,
    TaskPolicyStateV4,
    TaskSpec,
    TreeDigest,
    VerificationProfileRef,
    WorkspaceIdentity,
    apply_expected_sequence_update,
)
from agent_team.topology import (
    AgentNode,
    Edge,
    EdgeKind,
    NodeId,
    ProfileRef,
    TeamDefinition,
    TeamId,
)

HEAD = GitObjectId("a" * 40)
TREE = TreeDigest("b" * 64)


def definition(*, self_review: bool = False, ambiguous: bool = False) -> TeamDefinition:
    reviewer_target = NodeId("worker") if self_review else NodeId("reviewer")
    reviewed_edges = [Edge(NodeId("worker"), reviewer_target, EdgeKind.REVIEWED_BY)]
    if ambiguous:
        reviewed_edges.append(
            Edge(NodeId("worker"), NodeId("reviewer-2"), EdgeKind.REVIEWED_BY)
        )
    nodes = [
        AgentNode(
            NodeId("main"),
            "Main",
            ProfileRef("main", "direct", "orchestrator"),
            is_main=True,
        ),
        AgentNode(
            NodeId("worker"),
            "Worker",
            ProfileRef("worker", "direct", "workspace-write"),
        ),
        AgentNode(
            NodeId("reviewer"),
            "Reviewer",
            ProfileRef("reviewer", "direct", "read-only"),
        ),
    ]
    if ambiguous:
        nodes.append(
            AgentNode(
                NodeId("reviewer-2"),
                "Reviewer 2",
                ProfileRef("reviewer-2", "direct", "read-only"),
            )
        )
    return TeamDefinition(
        TeamId("team"),
        tuple(nodes),
        (
            Edge(NodeId("main"), NodeId("worker"), EdgeKind.DELEGATES_TO),
            *reviewed_edges,
        ),
    )


def foreign_definition() -> TeamDefinition:
    return TeamDefinition(
        TeamId("team"),
        (
            AgentNode(
                NodeId("main"),
                "Main",
                ProfileRef("main", "direct", "orchestrator"),
                is_main=True,
            ),
            AgentNode(
                NodeId("foreign-worker"),
                "Foreign Worker",
                ProfileRef("worker", "direct", "workspace-write"),
            ),
            AgentNode(
                NodeId("foreign-reviewer"),
                "Foreign Reviewer",
                ProfileRef("reviewer", "direct", "read-only"),
            ),
        ),
        (
            Edge(NodeId("main"), NodeId("foreign-worker"), EdgeKind.DELEGATES_TO),
            Edge(
                NodeId("foreign-worker"),
                NodeId("foreign-reviewer"),
                EdgeKind.REVIEWED_BY,
            ),
        ),
    )


def task(
    *, lane: TaskLane = TaskLane.NORMAL, dependencies: tuple[str, ...] = ()
) -> TaskSpec:
    return TaskSpec(
        task_id=TaskId("task"),
        title="Implement task",
        context="A bounded context.",
        goal="Implement the requested change.",
        acceptance=("The change is verified.",),
        allowed_paths=("scripts/agent-team",),
        do_not_modify=(),
        dependencies=tuple(TaskId(value) for value in dependencies),
        verification=VerificationProfileRef("tests"),
        escalation_node=NodeId("main"),
        kind=TaskKind.IMPLEMENTATION,
        lane=lane,
        resource_claims=(),
    )


def policy(
    *,
    max_review_rounds: int = 2,
    task_value: TaskSpec | None = None,
    dependency_states: tuple[DependencyState, ...] = (),
    active_assignments: tuple[WorkerAssignment, ...] = (),
) -> SerialReviewPolicy:
    return SerialReviewPolicy(
        task=task_value or task(),
        team_definition=definition(),
        worker_node=NodeId("worker"),
        max_review_rounds=max_review_rounds,
        dependency_states=dependency_states,
        active_assignments=active_assignments,
    )


def state(
    *, phase: TaskPhase = TaskPhase.PENDING, sequence: int = 0
) -> TaskPolicyStateV4:
    return TaskPolicyStateV4(
        version=STATE_POLICY_VERSION,
        team_id=TeamId("team"),
        workspace=WorkspaceIdentity("/workspace/team"),
        sequence=sequence,
        task_id=TaskId("task"),
        attempt_id=None,
        dispatch_id=None,
        worker_node=None,
        reviewer_node=None,
        review_round=0,
        target_head=None,
        target_tree_digest=None,
        claim_ref=None,
        receipt_ref=None,
        phase=phase,
    )


def assignment(
    *,
    attempt: str = "attempt-1",
    dispatch: str = "dispatch-1",
    round: int = 1,
    run: str = "run-1",
    target_head: GitObjectId | None = None,
    target_tree_digest: TreeDigest | None = None,
    worker_terminal_id: TerminalId | None = None,
    reviewer_terminal_id: TerminalId | None = None,
    worker_node: NodeId | None = None,
    reviewer_node: NodeId | None = None,
) -> WorkerAssignment:
    return WorkerAssignment(
        run_id=RunId(run),
        task_id=TaskId("task"),
        dispatch_id=DispatchId(dispatch),
        attempt_id=AttemptId(attempt),
        worker_node=NodeId("worker") if worker_node is None else worker_node,
        reviewer_node=NodeId("reviewer") if reviewer_node is None else reviewer_node,
        worker_terminal_id=(
            TerminalId("worker-terminal")
            if worker_terminal_id is None
            else worker_terminal_id
        ),
        reviewer_terminal_id=(
            TerminalId("reviewer-terminal")
            if reviewer_terminal_id is None
            else reviewer_terminal_id
        ),
        review_round=round,
        target_head=target_head,
        target_tree_digest=target_tree_digest,
    )


def completion(
    assigned: WorkerAssignment,
    *,
    expected_sequence: int = 1,
    kind: WorkerCompletionKind = WorkerCompletionKind.SUCCEEDED,
    completion_id: str = "completion-1",
    target_head: GitObjectId | None = HEAD,
    target_tree_digest: TreeDigest | None = TREE,
) -> WorkerCompletion:
    return WorkerCompletion(
        expected_sequence=expected_sequence,
        run_id=assigned.run_id,
        task_id=assigned.task_id,
        dispatch_id=assigned.dispatch_id,
        attempt_id=assigned.attempt_id,
        worker_node=assigned.worker_node,
        reviewer_node=assigned.reviewer_node,
        worker_terminal_id=assigned.worker_terminal_id,
        review_round=assigned.review_round,
        completion_id=CompletionId(completion_id),
        target_head=target_head,
        target_tree_digest=target_tree_digest,
        kind=kind,
        explanation="Worker says APPROVED",  # body is deliberately non-authoritative
    )


def review_request(
    completed: WorkerCompletion, *, expected_sequence: int = 2
) -> ReviewRequest:
    return ReviewRequest(expected_sequence=expected_sequence, completion=completed)


def decision(
    assigned: WorkerAssignment,
    *,
    expected_sequence: int = 3,
    kind: ReviewDecisionKind = ReviewDecisionKind.APPROVED,
    reviewer_node: NodeId | None = None,
    decision_ref: str = "decision-1",
    completion_id: str = "completion-1",
    completion_expected_sequence: int | None = None,
    review_round: int | None = None,
    target_head: GitObjectId = HEAD,
    target_tree_digest: TreeDigest = TREE,
) -> ReviewDecision:
    return ReviewDecision(
        expected_sequence=expected_sequence,
        run_id=assigned.run_id,
        task_id=assigned.task_id,
        dispatch_id=assigned.dispatch_id,
        attempt_id=assigned.attempt_id,
        worker_node=assigned.worker_node,
        reviewer_node=NodeId("reviewer") if reviewer_node is None else reviewer_node,
        worker_terminal_id=assigned.worker_terminal_id,
        reviewer_terminal_id=assigned.reviewer_terminal_id,
        review_round=assigned.review_round if review_round is None else review_round,
        completion_id=CompletionId(completion_id),
        completion_expected_sequence=(
            1 if completion_expected_sequence is None else completion_expected_sequence
        ),
        target_head=target_head,
        target_tree_digest=target_tree_digest,
        decision_ref=DecisionRef(decision_ref),
        kind=kind,
        explanation="looks good",
    )


def projection_fields(value: PolicyAuthorityProjection) -> dict[str, object]:
    return {item.name: getattr(value, item.name) for item in fields(value)}


def forged_projection(
    value: PolicyAuthorityProjection, **changes: object
) -> PolicyAuthorityProjection:
    forged = object.__new__(PolicyAuthorityProjection)
    for item in fields(value):
        object.__setattr__(
            forged,
            item.name,
            changes.get(item.name, getattr(value, item.name)),
        )
    return forged


class ReviewPairTest(unittest.TestCase):
    def test_resolves_one_fixed_pair_independent_of_input_order(self) -> None:
        expected = ReviewPair(NodeId("worker"), NodeId("reviewer"))
        self.assertEqual(
            resolve_worker_reviewer_pair(definition(), NodeId("worker")), expected
        )

        reordered = TeamDefinition(
            TeamId("team"),
            tuple(reversed(definition().nodes)),
            tuple(reversed(definition().edges)),
        )
        self.assertEqual(
            resolve_worker_reviewer_pair(reordered, NodeId("worker")), expected
        )

    def test_rejects_unknown_self_and_ambiguous_pairs(self) -> None:
        cases = (
            (definition(), NodeId("missing"), "unknown-node"),
            (definition(self_review=True), NodeId("worker"), "self-review"),
            (definition(ambiguous=True), NodeId("worker"), "ambiguous-pair"),
        )
        for team_definition, worker_node, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(ReviewPolicyError, code),
            ):
                resolve_worker_reviewer_pair(team_definition, worker_node)


class ReviewPolicyContractTest(unittest.TestCase):
    def test_worker_and_reviewer_cannot_share_a_terminal(self) -> None:
        with self.assertRaisesRegex(ReviewPolicyError, "independent-terminal"):
            assignment(
                worker_terminal_id=TerminalId("same-terminal"),
                reviewer_terminal_id=TerminalId("same-terminal"),
            )

    def test_policy_accepts_normal_and_express_but_rejects_research(
        self,
    ) -> None:
        cases = (
            (0, TaskLane.NORMAL, "max-review-rounds"),
            (-1, TaskLane.NORMAL, "max-review-rounds"),
            (1, TaskLane.RESEARCH, "review-lane"),
        )
        for max_rounds, lane, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(ReviewPolicyError, code),
            ):
                policy(max_review_rounds=max_rounds, task_value=task(lane=lane))

    def test_admitted_express_uses_the_same_serial_gate_and_handoff(self) -> None:
        express_policy = policy(task_value=task(lane=TaskLane.EXPRESS))
        pending = initial_review_policy_state(RunId("run-1"), state())
        assigned = assignment()
        assigned_update = reduce_policy(
            pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            express_policy,
        )
        completed = completion(assigned)
        worker_done_update = reduce_policy(
            assigned_update.next_state,
            completed,
            express_policy,
        )
        review_update = reduce_policy(
            worker_done_update.next_state,
            review_request(completed),
            express_policy,
        )
        approved_update = reduce_policy(
            review_update.next_state,
            decision(assigned),
            express_policy,
        )

        self.assertEqual(
            express_policy.pair, ReviewPair(NodeId("worker"), NodeId("reviewer"))
        )
        self.assertEqual(assigned_update.policy_fingerprint, express_policy.fingerprint)
        self.assertEqual(review_update.policy_fingerprint, express_policy.fingerprint)
        validate_policy_update(approved_update, express_policy)
        effect = review_update.effects[0]
        validate_reviewer_assignment(
            effect, express_policy, worker_done_update.next_state
        )
        projection = policy_authority_projection(approved_update, express_policy)
        self.assertEqual(projection.policy_fingerprint, express_policy.fingerprint)
        validate_policy_authority_projection(
            projection,
            approved_update,
            express_policy,
        )

    def test_policy_rejects_unmet_or_missing_dependencies_and_multiple_assignments(
        self,
    ) -> None:
        dep_task = task(dependencies=("dependency",))
        cases = (
            ((), "dependency-unmet"),
            (
                (DependencyState(TaskId("dependency"), TaskPhase.ASSIGNED),),
                "dependency-unmet",
            ),
            (
                (DependencyState(TaskId("foreign"), TaskPhase.APPROVED),),
                "unknown-dependency",
            ),
        )
        for dependencies, code in cases:
            with self.subTest(code=code):
                review_policy = policy(
                    task_value=dep_task, dependency_states=dependencies
                )
                current = initial_review_policy_state(RunId("run-1"), state())
                with self.assertRaisesRegex(ReviewPolicyError, code):
                    reduce_policy(
                        current,
                        AssignmentCommand(expected_sequence=0, assignment=assignment()),
                        review_policy,
                    )

        with self.assertRaisesRegex(ReviewPolicyError, "active-assignment"):
            policy(active_assignments=(assignment(), assignment(attempt="attempt-2")))

    def test_state_and_typed_events_are_immutable(self) -> None:
        current = initial_review_policy_state(RunId("run-1"), state())
        with self.assertRaises(FrozenInstanceError):
            current.run_id = RunId("other")  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            completion(assignment()).kind = WorkerCompletionKind.FAILED  # type: ignore[misc]


class ReviewPolicyReducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.review_policy = policy()
        self.pending = initial_review_policy_state(RunId("run-1"), state())

    def test_valid_serial_path_ends_at_approved_and_never_issues_completed_or_verifying(
        self,
    ) -> None:
        assigned = assignment()
        assigned_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        self.assertEqual(
            assigned_update.next_state.task_state.phase, TaskPhase.ASSIGNED
        )
        self.assertEqual(assigned_update.next_state.task_state.sequence, 1)
        self.assertIs(assigned_update.next_state.last_event, assigned_update.event)
        completed = completion(assigned)
        done_update = reduce_policy(
            assigned_update.next_state, completed, self.review_policy
        )
        self.assertEqual(done_update.next_state.task_state.phase, TaskPhase.WORKER_DONE)
        self.assertIs(done_update.next_state.last_event, done_update.event)
        review_update = reduce_policy(
            done_update.next_state,
            review_request(completed),
            self.review_policy,
        )
        self.assertEqual(
            review_update.next_state.task_state.phase, TaskPhase.REVIEW_PENDING
        )
        self.assertIs(review_update.next_state.last_event, review_update.event)
        self.assertEqual(len(review_update.effects), 1)
        self.assertIsInstance(review_update.effects[0], ReviewerAssignment)
        approved_update = reduce_policy(
            review_update.next_state,
            decision(assigned),
            self.review_policy,
        )
        self.assertEqual(
            approved_update.next_state.task_state.phase, TaskPhase.APPROVED
        )
        self.assertIs(approved_update.next_state.last_event, approved_update.event)
        self.assertNotIn(
            approved_update.next_state.task_state.phase,
            (TaskPhase.COMPLETED, TaskPhase.VERIFYING),
        )

    def test_review_request_ignores_non_authoritative_completion_explanation(
        self,
    ) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_state, completed, self.review_policy
        ).next_state
        handoff_completion = replace(completed, explanation=None)

        review_pending = reduce_policy(
            worker_done,
            review_request(handoff_completion),
            self.review_policy,
        )
        self.assertEqual(
            review_pending.next_state.task_state.phase,
            TaskPhase.REVIEW_PENDING,
        )

    def test_handmade_states_cannot_bypass_phase_causality_or_review_gate(self) -> None:
        assigned = assignment()
        assigned_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        assigned_state = assigned_update.next_state
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_state, completed, self.review_policy
        ).next_state
        review_pending = reduce_policy(
            worker_done,
            review_request(completed),
            self.review_policy,
        ).next_state
        approved = reduce_policy(
            review_pending,
            decision(assigned),
            self.review_policy,
        ).next_state

        invalid_state_builders: tuple[Callable[[], ReviewPolicyState], ...] = (
            # assigned requires an assignment and no completion/decision.
            lambda: ReviewPolicyState(
                RunId("run-1"), assigned_state.task_state, completion=completed
            ),
            # worker_done/review_pending require a successful completion.
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(worker_done.task_state, phase=TaskPhase.WORKER_DONE),
                assignment=assigned,
                completion=completion(assigned, kind=WorkerCompletionKind.FAILED),
            ),
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(worker_done.task_state, phase=TaskPhase.REVIEW_PENDING),
                assignment=assigned,
                completion=completion(assigned, kind=WorkerCompletionKind.FAILED),
            ),
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(
                    worker_done.task_state,
                    target_head=None,
                    target_tree_digest=None,
                ),
                assignment=assigned,
                completion=completion(
                    assigned,
                    target_head=None,
                    target_tree_digest=None,
                ),
            ),
            # approved requires a matching APPROVED decision.
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(review_pending.task_state, phase=TaskPhase.APPROVED),
                assignment=assigned,
                completion=completed,
            ),
            # changes_requested requires a matching CHANGES_REQUESTED decision.
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(review_pending.task_state, phase=TaskPhase.CHANGES_REQUESTED),
                assignment=assigned,
                completion=completed,
                decision=decision(assigned),
            ),
            # ask_user/failed require their typed origin, not a successful worker event.
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(approved.task_state, phase=TaskPhase.ASK_USER),
                assignment=assigned,
                completion=completed,
                decision=decision(assigned),
            ),
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(approved.task_state, phase=TaskPhase.FAILED),
                assignment=assigned,
                completion=completed,
                decision=decision(assigned),
            ),
            # this policy never accepts verification/completion observations.
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(approved.task_state, phase=TaskPhase.VERIFYING),
                assignment=assigned,
                completion=completed,
                decision=decision(assigned),
            ),
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(approved.task_state, phase=TaskPhase.COMPLETED),
                assignment=assigned,
                completion=completed,
                decision=decision(assigned),
            ),
            lambda: ReviewPolicyState(
                RunId("run-1"),
                replace(approved.task_state, phase=TaskPhase.VERIFICATION_FAILED),
                assignment=assigned,
                completion=completed,
                decision=decision(assigned),
            ),
        )
        for build_invalid in invalid_state_builders:
            with self.assertRaises(ReviewPolicyError):
                build_invalid()

        tampered = approved
        object.__setattr__(
            tampered.task_state,
            "phase",
            TaskPhase.CHANGES_REQUESTED,
        )
        with self.assertRaises(ReviewPolicyError):
            reduce_policy(
                tampered,
                AssignmentCommand(
                    expected_sequence=4,
                    assignment=assignment(
                        attempt="attempt-2",
                        dispatch="dispatch-2",
                        round=2,
                    ),
                ),
                self.review_policy,
            )

    def test_reviewer_assignment_requires_matching_successful_completion(self) -> None:
        assigned = assignment(target_head=HEAD, target_tree_digest=TREE)
        successful = completion(assigned)
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        worker_done = reduce_policy(
            assigned_state,
            successful,
            self.review_policy,
        ).next_state
        valid = ReviewerAssignment(
            assigned,
            successful,
            policy_fingerprint=self.review_policy.fingerprint,
        )
        self.assertEqual(valid.task_id, assigned.task_id)
        self.assertEqual(valid.target_head, HEAD)
        self.assertEqual(valid.target_tree_digest, TREE)
        self.assertIs(
            validate_reviewer_assignment(valid, self.review_policy, worker_done),
            valid,
        )

        invalid = (
            completion(assigned, kind=WorkerCompletionKind.FAILED),
            completion(
                assignment(run="foreign-run", target_head=HEAD, target_tree_digest=TREE)
            ),
            completion(
                assigned,
                target_head=GitObjectId("c" * 40),
                target_tree_digest=TREE,
            ),
        )
        for event in invalid:
            with (
                self.subTest(kind=event.kind, run_id=event.run_id),
                self.assertRaises(ReviewPolicyError),
            ):
                ReviewerAssignment(
                    assigned,
                    event,
                    policy_fingerprint=self.review_policy.fingerprint,
                )

        with self.assertRaises(TypeError):
            ReviewerAssignment(assigned, successful)  # type: ignore[call-arg]

    def test_updates_and_effects_are_bound_to_the_actual_policy(self) -> None:
        assigned = assignment()
        update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        self.assertEqual(update.policy_fingerprint, self.review_policy.fingerprint)
        self.assertIs(validate_policy_update(update, self.review_policy), update)

        one_round_policy = policy(max_review_rounds=1)
        with self.assertRaises(ReviewPolicyError):
            validate_policy_update(update, one_round_policy)

        occupied_policy = policy(active_assignments=(assigned,))
        with self.assertRaisesRegex(ReviewPolicyError, "active-assignment"):
            validate_policy_update(update, occupied_policy)

        changed_refs = replace(
            update.next_state,
            task_state=replace(
                update.next_state.task_state,
                claim_ref=ClaimRef("claim-from-other-policy"),
                receipt_ref=ReceiptRef("receipt-from-other-policy"),
            ),
        )
        with self.assertRaises(ReviewPolicyError):
            validate_policy_update(
                replace(update, next_state=changed_refs),
                self.review_policy,
            )

        foreign_policy = SerialReviewPolicy(
            task=task(),
            team_definition=foreign_definition(),
            worker_node=NodeId("foreign-worker"),
            max_review_rounds=2,
        )
        foreign_assignment = assignment(
            worker_node=NodeId("foreign-worker"),
            reviewer_node=NodeId("foreign-reviewer"),
        )
        foreign_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=foreign_assignment),
            foreign_policy,
        )
        with self.assertRaises(ReviewPolicyError):
            validate_policy_update(foreign_update, self.review_policy)

        forged = replace(
            update,
            policy_fingerprint=PolicyFingerprint("c" * 64),
        )
        with self.assertRaises(ReviewPolicyError):
            validate_policy_update(forged, self.review_policy)

        completed = completion(assigned)
        worker_done = reduce_policy(
            update.next_state,
            completed,
            self.review_policy,
        ).next_state
        review_update = reduce_policy(
            worker_done,
            review_request(completed),
            self.review_policy,
        )
        effect = review_update.effects[0]
        self.assertIs(
            validate_reviewer_assignment(effect, self.review_policy, worker_done),
            effect,
        )
        forged_effect = replace(
            effect,
            policy_fingerprint=PolicyFingerprint("d" * 64),
        )
        with self.assertRaises(ReviewPolicyError):
            validate_reviewer_assignment(forged_effect, self.review_policy, worker_done)

        foreign_assignment = assignment(
            run="run-2",
            target_head=HEAD,
            target_tree_digest=TREE,
        )
        foreign_effect = ReviewerAssignment(
            foreign_assignment,
            completion(foreign_assignment),
            policy_fingerprint=self.review_policy.fingerprint,
        )
        with self.assertRaises(ReviewPolicyError):
            validate_reviewer_assignment(
                foreign_effect,
                self.review_policy,
                worker_done,
            )

    def test_review_request_rejects_nested_completion_sequence_replacement(
        self,
    ) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_state,
            completed,
            self.review_policy,
        ).next_state
        with self.assertRaises(ReviewPolicyError):
            reduce_policy(
                worker_done,
                review_request(replace(completed, expected_sequence=0)),
                self.review_policy,
            )

    def test_policy_update_reuses_reducer_rules_for_assignment_and_limit_edges(
        self,
    ) -> None:
        assigned = assignment()
        assigned_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        round_two = assignment(attempt="attempt-2", dispatch="dispatch-2", round=2)
        round_two_state = ReviewPolicyState(
            RunId("run-1"),
            replace(
                self.pending.task_state,
                sequence=1,
                attempt_id=round_two.attempt_id,
                dispatch_id=round_two.dispatch_id,
                worker_node=round_two.worker_node,
                reviewer_node=round_two.reviewer_node,
                review_round=2,
                phase=TaskPhase.ASSIGNED,
            ),
            assignment=round_two,
            last_event=AssignmentCommand(expected_sequence=0, assignment=round_two),
        )
        with self.assertRaises(ReviewPolicyError):
            validate_policy_update(
                ReviewPolicyUpdate(
                    0,
                    self.pending,
                    round_two_state,
                    AssignmentCommand(expected_sequence=0, assignment=round_two),
                    policy_fingerprint=self.review_policy.fingerprint,
                ),
                self.review_policy,
            )

        completed = completion(assigned)
        review_pending = reduce_policy(
            reduce_policy(
                assigned_update.next_state, completed, self.review_policy
            ).next_state,
            review_request(completed),
            self.review_policy,
        ).next_state
        changes = reduce_policy(
            review_pending,
            decision(assigned, kind=ReviewDecisionKind.CHANGES_REQUESTED),
            self.review_policy,
        ).next_state
        reused_attempt = ReviewPolicyState(
            RunId("run-1"),
            replace(
                changes.task_state,
                sequence=5,
                phase=TaskPhase.ASSIGNED,
                target_head=None,
                target_tree_digest=None,
            ),
            assignment=assigned,
            last_event=AssignmentCommand(expected_sequence=4, assignment=assigned),
        )
        with self.assertRaises(ReviewPolicyError):
            validate_policy_update(
                ReviewPolicyUpdate(
                    4,
                    changes,
                    reused_attempt,
                    AssignmentCommand(expected_sequence=4, assignment=assigned),
                    policy_fingerprint=self.review_policy.fingerprint,
                ),
                self.review_policy,
            )

        early_decision = decision(
            assigned,
            expected_sequence=3,
            kind=ReviewDecisionKind.CHANGES_REQUESTED,
        )
        early_limit_state = ReviewPolicyState(
            RunId("run-1"),
            replace(review_pending.task_state, sequence=4, phase=TaskPhase.ASK_USER),
            assignment=assigned,
            completion=completed,
            decision=early_decision,
            last_event=early_decision,
            reason_code="review-limit",
        )
        with self.assertRaises(ReviewPolicyError):
            validate_policy_update(
                ReviewPolicyUpdate(
                    3,
                    review_pending,
                    early_limit_state,
                    early_decision,
                    policy_fingerprint=self.review_policy.fingerprint,
                    reason_code="review-limit",
                ),
                self.review_policy,
            )

    def test_update_origin_comparator_ignores_explanation_only_reconstruction(
        self,
    ) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_state,
            completed,
            self.review_policy,
        ).next_state
        review_update = reduce_policy(
            worker_done,
            review_request(completed),
            self.review_policy,
        )
        reconstructed_event = review_request(replace(completed, explanation=None))
        reconstructed = replace(review_update, event=reconstructed_event)
        self.assertIs(
            validate_policy_update(reconstructed, self.review_policy),
            reconstructed,
        )

    def test_durable_handoff_projection_contains_authority_only(self) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_state,
            completed,
            self.review_policy,
        ).next_state
        review_update = reduce_policy(
            worker_done,
            review_request(completed),
            self.review_policy,
        )
        projection = policy_authority_projection(review_update, self.review_policy)
        self.assertEqual(projection.event_kind, PolicyProjectionKind.REVIEW_REQUEST)
        self.assertEqual(
            projection.worker_completion_kind, WorkerCompletionKind.SUCCEEDED
        )
        self.assertEqual(projection.completion_id, completed.completion_id)
        self.assertEqual(
            projection.sequence, review_update.next_state.task_state.sequence
        )
        self.assertEqual(projection.target_head, completed.target_head)
        self.assertEqual(projection.target_tree_digest, completed.target_tree_digest)
        self.assertFalse(hasattr(projection, "explanation"))
        self.assertNotIn("Worker says APPROVED", repr(projection))

    def test_authority_projection_is_issued_only_from_a_policy_bound_update(
        self,
    ) -> None:
        assigned = assignment()
        assigned_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_update.next_state,
            completed,
            self.review_policy,
        ).next_state
        review_update = reduce_policy(
            worker_done,
            review_request(completed),
            self.review_policy,
        )
        approved_update = reduce_policy(
            review_update.next_state,
            decision(assigned),
            self.review_policy,
        )
        canonical = policy_authority_projection(approved_update, self.review_policy)

        self.assertNotIn("_PROJECTION_ISSUER", vars(review_policy_module))
        constructor = cast(
            Callable[..., PolicyAuthorityProjection], PolicyAuthorityProjection
        )
        with self.assertRaises(TypeError):
            constructor(**projection_fields(canonical))

        forged_values = (
            forged_projection(canonical, workspace=WorkspaceIdentity("/attacker")),
            forged_projection(canonical, reviewer_node=NodeId("foreign-reviewer")),
            forged_projection(canonical, sequence=999),
            forged_projection(
                canonical,
                target_head=None,
                target_tree_digest=None,
            ),
        )
        for forged in forged_values:
            with self.subTest(forged=forged):
                with self.assertRaises(ReviewPolicyError):
                    validate_policy_authority_projection(
                        forged,
                        approved_update,
                        self.review_policy,
                    )
                factory = cast(
                    Callable[..., PolicyAuthorityProjection],
                    policy_authority_projection,
                )
                with self.assertRaises(ReviewPolicyError):
                    factory(forged, self.review_policy)

        parameter_names = tuple(
            signature(ReviewPolicyHandoffPort.save_authority).parameters
        )
        self.assertEqual(parameter_names, ("self", "update", "policy"))

    def test_public_update_requires_full_causal_event_identity_phase_and_effect(
        self,
    ) -> None:
        assigned = assignment()
        assigned_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        completed = completion(assigned)
        done_update = reduce_policy(
            assigned_update.next_state,
            completed,
            self.review_policy,
        )
        review_event = review_request(completed)
        review_update = reduce_policy(
            done_update.next_state,
            review_event,
            self.review_policy,
        )
        valid_effect = review_update.effects[0]
        foreign_assignment = assignment(
            run="run-2",
            target_head=HEAD,
            target_tree_digest=TREE,
        )
        foreign_effect = ReviewerAssignment(
            foreign_assignment,
            completion(foreign_assignment),
            policy_fingerprint=self.review_policy.fingerprint,
        )
        target_assignment = assignment(
            target_head=GitObjectId("c" * 40),
            target_tree_digest=TREE,
        )
        target_effect = ReviewerAssignment(
            target_assignment,
            completion(
                target_assignment,
                target_head=GitObjectId("c" * 40),
                target_tree_digest=TREE,
            ),
            policy_fingerprint=self.review_policy.fingerprint,
        )

        invalid_updates: tuple[Callable[[], ReviewPolicyUpdate], ...] = (
            # An update cannot omit its typed event or use a different sequence.
            lambda: ReviewPolicyUpdate(
                0,
                self.pending,
                assigned_update.next_state,
                None,  # type: ignore[arg-type]
                policy_fingerprint=self.review_policy.fingerprint,
            ),
            lambda: ReviewPolicyUpdate(
                0,
                self.pending,
                assigned_update.next_state,
                AssignmentCommand(expected_sequence=0, assignment=assigned),
            ),
            lambda: ReviewPolicyUpdate(
                0,
                self.pending,
                assigned_update.next_state,
                AssignmentCommand(expected_sequence=1, assignment=assigned),
                policy_fingerprint=self.review_policy.fingerprint,
            ),
            # Previous/next Run identities must remain the same.
            lambda: ReviewPolicyUpdate(
                0,
                self.pending,
                reduce_policy(
                    initial_review_policy_state(RunId("run-2"), state()),
                    AssignmentCommand(
                        expected_sequence=0,
                        assignment=assignment(run="run-2"),
                    ),
                    self.review_policy,
                ).next_state,
                AssignmentCommand(
                    expected_sequence=0,
                    assignment=assignment(run="run-2"),
                ),
                policy_fingerprint=self.review_policy.fingerprint,
            ),
            # A typed event must describe the next observation's phase and values.
            lambda: ReviewPolicyUpdate(
                1,
                assigned_update.next_state,
                review_update.next_state,
                review_event,
                policy_fingerprint=self.review_policy.fingerprint,
            ),
            lambda: ReviewPolicyUpdate(
                2,
                done_update.next_state,
                review_update.next_state,
                review_event,
                effects=(foreign_effect,),
                policy_fingerprint=self.review_policy.fingerprint,
            ),
            lambda: ReviewPolicyUpdate(
                2,
                done_update.next_state,
                review_update.next_state,
                review_event,
                effects=(target_effect,),
                policy_fingerprint=self.review_policy.fingerprint,
            ),
            # Only ReviewRequest can carry the Reviewer effect, exactly once.
            lambda: ReviewPolicyUpdate(
                0,
                self.pending,
                assigned_update.next_state,
                AssignmentCommand(expected_sequence=0, assignment=assigned),
                effects=(valid_effect,),
                policy_fingerprint=self.review_policy.fingerprint,
            ),
            lambda: ReviewPolicyUpdate(
                2,
                done_update.next_state,
                review_update.next_state,
                review_event,
                effects=(),
                policy_fingerprint=self.review_policy.fingerprint,
            ),
        )
        for build_invalid in invalid_updates:
            with self.assertRaises(ReviewPolicyError):
                build_invalid()

    def test_last_event_proves_immediate_sequence_causality(self) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_state,
            completed,
            self.review_policy,
        ).next_state
        review_pending = reduce_policy(
            worker_done,
            review_request(completed),
            self.review_policy,
        ).next_state

        invalid_state_builders: tuple[Callable[[], ReviewPolicyState], ...] = (
            lambda: replace(worker_done, last_event=None),
            lambda: replace(
                worker_done,
                last_event=replace(completed, expected_sequence=0),
            ),
            lambda: replace(
                review_pending,
                last_event=ReviewRequest(
                    expected_sequence=1,
                    completion=completed,
                ),
            ),
            lambda: replace(
                worker_done,
                task_state=replace(worker_done.task_state, sequence=5),
                last_event=replace(completed, expected_sequence=4),
            ),
        )
        for build_invalid in invalid_state_builders:
            with self.assertRaises(ReviewPolicyError):
                build_invalid()

    def test_over_limit_current_round_is_rejected_before_every_event(self) -> None:
        two_round_policy = policy(max_review_rounds=2)
        one_round_policy = policy(max_review_rounds=1)
        first = assignment()
        first_assigned = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=first),
            two_round_policy,
        ).next_state
        first_completion = completion(first)
        first_review_pending = reduce_policy(
            reduce_policy(
                first_assigned, first_completion, two_round_policy
            ).next_state,
            review_request(first_completion),
            two_round_policy,
        ).next_state
        changes = reduce_policy(
            first_review_pending,
            decision(first, kind=ReviewDecisionKind.CHANGES_REQUESTED),
            two_round_policy,
        ).next_state
        second = assignment(attempt="attempt-2", dispatch="dispatch-2", round=2)
        second_assigned = reduce_policy(
            changes,
            AssignmentCommand(expected_sequence=4, assignment=second),
            two_round_policy,
        ).next_state
        second_completion = completion(
            second,
            expected_sequence=second_assigned.task_state.sequence,
        )
        second_done = reduce_policy(
            second_assigned,
            second_completion,
            two_round_policy,
        ).next_state
        over_limit_effect = ReviewerAssignment(
            replace(
                second,
                target_head=HEAD,
                target_tree_digest=TREE,
            ),
            second_completion,
            policy_fingerprint=one_round_policy.fingerprint,
        )
        with self.assertRaisesRegex(ReviewPolicyError, "review-limit"):
            validate_reviewer_assignment(
                over_limit_effect,
                one_round_policy,
                second_done,
            )
        second_review_pending = reduce_policy(
            second_done,
            review_request(
                second_completion,
                expected_sequence=second_done.task_state.sequence,
            ),
            two_round_policy,
        ).next_state

        over_limit_events = (
            (
                second_assigned,
                second_completion,
            ),
            (
                second_done,
                review_request(
                    second_completion,
                    expected_sequence=second_done.task_state.sequence,
                ),
            ),
            (
                second_review_pending,
                decision(
                    second,
                    expected_sequence=second_review_pending.task_state.sequence,
                ),
            ),
        )
        for current, event in over_limit_events:
            with (
                self.subTest(phase=current.task_state.phase),
                self.assertRaisesRegex(ReviewPolicyError, "review-limit"),
            ):
                reduce_policy(current, event, one_round_policy)

        # The boundary itself is valid: approval at round == max is allowed.
        approved = reduce_policy(
            second_review_pending,
            decision(
                second,
                expected_sequence=second_review_pending.task_state.sequence,
                completion_expected_sequence=second_completion.expected_sequence,
            ),
            two_round_policy,
        )
        self.assertEqual(approved.next_state.task_state.phase, TaskPhase.APPROVED)

    def test_worker_completion_is_the_only_route_to_worker_done(self) -> None:
        assigned = assignment()
        assigned_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        for event in (
            completion(assigned, kind=WorkerCompletionKind.SUCCEEDED),
            completion(assigned, kind=WorkerCompletionKind.FAILED),
            completion(assigned, kind=WorkerCompletionKind.TIMEOUT),
        ):
            with self.subTest(kind=event.kind):
                update = reduce_policy(
                    assigned_update.next_state, event, self.review_policy
                )
                expected = (
                    TaskPhase.WORKER_DONE
                    if event.kind is WorkerCompletionKind.SUCCEEDED
                    else TaskPhase.FAILED
                )
                self.assertEqual(update.next_state.task_state.phase, expected)

    def test_wrong_identity_foreign_duplicate_late_and_stale_events_do_not_update_state(
        self,
    ) -> None:
        assigned = assignment()
        assigned_update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        worker_done = reduce_policy(
            assigned_update.next_state,
            completion(assigned),
            self.review_policy,
        )
        bad_events = (
            completion(assignment(dispatch="foreign-dispatch")),
            completion(assignment(run="foreign-run")),
            completion(assignment(attempt="old-attempt")),
        )
        for event in bad_events:
            with self.subTest(event=event):
                with self.assertRaisesRegex(ReviewPolicyError, "identity"):
                    reduce_policy(assigned_update.next_state, event, self.review_policy)
                self.assertEqual(assigned_update.next_state, assigned_update.next_state)

        with self.assertRaisesRegex(ReviewPolicyError, "stale-sequence"):
            reduce_policy(
                worker_done.next_state,
                completion(assigned, completion_id="completion-2"),
                self.review_policy,
            )
        with self.assertRaisesRegex(ReviewPolicyError, "stale-sequence"):
            reduce_policy(
                assigned_update.next_state,
                completion(assigned, expected_sequence=0),
                self.review_policy,
            )

    def test_target_revision_and_tree_identity_must_match_completion_and_decision(
        self,
    ) -> None:
        assigned = assignment(target_head=HEAD, target_tree_digest=TREE)
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        for head, tree in ((None, TREE), (HEAD, None), (GitObjectId("c" * 40), TREE)):
            with (
                self.subTest(head=head, tree=tree),
                self.assertRaises(ReviewPolicyError),
            ):
                event = completion(assigned, target_head=head, target_tree_digest=tree)
                reduce_policy(assigned_state, event, self.review_policy)

    def test_reviewer_decision_requires_fixed_reviewer_and_typed_approval(self) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        review_pending = reduce_policy(
            reduce_policy(assigned_state, completed, self.review_policy).next_state,
            review_request(completed),
            self.review_policy,
        ).next_state
        for reviewer in (NodeId("worker"), NodeId("foreign-reviewer")):
            with (
                self.subTest(reviewer=reviewer),
                self.assertRaisesRegex(ReviewPolicyError, "reviewer"),
            ):
                reduce_policy(
                    review_pending,
                    decision(assigned, reviewer_node=reviewer),
                    self.review_policy,
                )
        with self.assertRaisesRegex(ReviewPolicyError, "identity"):
            reduce_policy(
                review_pending,
                decision(
                    assigned,
                    completion_id="foreign-completion",
                ),
                self.review_policy,
            )

    def test_decision_keeps_completion_origin_sequence_in_its_causal_identity(
        self,
    ) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        worker_done = reduce_policy(
            assigned_state,
            completed,
            self.review_policy,
        ).next_state
        review_pending = reduce_policy(
            worker_done,
            review_request(completed),
            self.review_policy,
        ).next_state
        with self.assertRaises(ReviewPolicyError):
            reduce_policy(
                review_pending,
                decision(
                    assigned,
                    completion_expected_sequence=0,
                ),
                self.review_policy,
            )

    def test_changes_requested_invalidates_old_attempt_until_new_attempt_is_explicit(
        self,
    ) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        completed = completion(assigned)
        review_pending = reduce_policy(
            reduce_policy(assigned_state, completed, self.review_policy).next_state,
            review_request(completed),
            self.review_policy,
        ).next_state
        changes = reduce_policy(
            review_pending,
            decision(assigned, kind=ReviewDecisionKind.CHANGES_REQUESTED),
            self.review_policy,
        )
        self.assertEqual(
            changes.next_state.task_state.phase, TaskPhase.CHANGES_REQUESTED
        )
        with self.assertRaisesRegex(ReviewPolicyError, "phase"):
            reduce_policy(
                changes.next_state,
                completion(assigned, expected_sequence=4),
                self.review_policy,
            )
        with self.assertRaisesRegex(ReviewPolicyError, "phase"):
            reduce_policy(
                changes.next_state,
                decision(assigned, expected_sequence=4),
                self.review_policy,
            )

        retry = assignment(attempt="attempt-2", dispatch="dispatch-2", round=2)
        retried = reduce_policy(
            changes.next_state,
            AssignmentCommand(expected_sequence=4, assignment=retry),
            self.review_policy,
        )
        self.assertEqual(retried.next_state.task_state.phase, TaskPhase.ASSIGNED)
        self.assertEqual(retried.next_state.task_state.review_round, 2)
        with self.assertRaisesRegex(ReviewPolicyError, "identity"):
            reduce_policy(
                retried.next_state,
                completion(assigned, expected_sequence=5),
                self.review_policy,
            )

    def test_review_limit_routes_to_ask_user_without_completed(self) -> None:
        one_round_policy = policy(max_review_rounds=1)
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            one_round_policy,
        ).next_state
        completed = completion(assigned)
        review_pending = reduce_policy(
            reduce_policy(assigned_state, completed, one_round_policy).next_state,
            review_request(completed),
            one_round_policy,
        ).next_state
        update = reduce_policy(
            review_pending,
            decision(assigned, kind=ReviewDecisionKind.CHANGES_REQUESTED),
            one_round_policy,
        )
        self.assertEqual(update.next_state.task_state.phase, TaskPhase.ASK_USER)
        self.assertEqual(update.reason_code, "review-limit")
        self.assertNotEqual(update.next_state.task_state.phase, TaskPhase.COMPLETED)

    def test_question_escalation_and_failure_are_not_success(self) -> None:
        assigned = assignment()
        assigned_state = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        ).next_state
        for kind, expected in (
            (WorkerCompletionKind.QUESTION, TaskPhase.ASK_USER),
            (WorkerCompletionKind.ESCALATION, TaskPhase.ASK_USER),
            (WorkerCompletionKind.TIMEOUT, TaskPhase.FAILED),
        ):
            with self.subTest(kind=kind):
                update = reduce_policy(
                    assigned_state,
                    completion(assigned, kind=kind),
                    self.review_policy,
                )
                self.assertEqual(update.next_state.task_state.phase, expected)
                self.assertNotIn(expected, (TaskPhase.COMPLETED, TaskPhase.VERIFYING))

    def test_dependency_must_be_explicitly_approved_or_completed(self) -> None:
        dep_task = task(dependencies=("dependency",))
        review_policy = policy(
            task_value=dep_task,
            dependency_states=(
                DependencyState(TaskId("dependency"), TaskPhase.APPROVED),
            ),
        )
        current = initial_review_policy_state(RunId("run-1"), state())
        update = reduce_policy(
            current,
            AssignmentCommand(expected_sequence=0, assignment=assignment()),
            review_policy,
        )
        self.assertEqual(update.next_state.task_state.phase, TaskPhase.ASSIGNED)

    def test_typed_store_update_can_be_cas_applied_without_effects(self) -> None:
        assigned = assignment()
        update = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        self.assertEqual(update.expected_sequence, 0)
        self.assertEqual(update.policy_fingerprint, self.review_policy.fingerprint)
        task_update = update.task_update(self.review_policy)
        self.assertEqual(task_update.expected_sequence, 0)
        self.assertEqual(task_update.state, update.next_state.task_state)
        self.assertEqual(update.effects, ())

    def test_two_updates_from_one_sequence_allow_only_one_store_commit(self) -> None:
        assigned = assignment()
        first = reduce_policy(
            self.pending,
            AssignmentCommand(expected_sequence=0, assignment=assigned),
            self.review_policy,
        )
        second = reduce_policy(
            self.pending,
            AssignmentCommand(
                expected_sequence=0, assignment=assignment(attempt="attempt-2")
            ),
            self.review_policy,
        )

        class FakeStore:
            def __init__(self, value: TaskPolicyStateV4) -> None:
                self.value = value
                self.calls = 0

            def update(
                self,
                value: ReviewPolicyUpdate,
                policy_value: SerialReviewPolicy,
            ) -> ReviewPolicyState:
                self.value = apply_expected_sequence_update(
                    self.value, value.task_update(policy_value)
                )
                self.calls += 1
                return value.next_state

        store = FakeStore(self.pending.task_state)
        store.update(first, self.review_policy)
        before_conflict = store.value
        with self.assertRaises(StateConflictError):
            store.update(second, self.review_policy)
        self.assertEqual(store.calls, 1)
        self.assertEqual(store.value, before_conflict)


if __name__ == "__main__":
    unittest.main()
