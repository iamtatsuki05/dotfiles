from __future__ import annotations

import copy
import inspect
import pickle
import unittest
from collections.abc import Callable
from dataclasses import fields, replace
from functools import partial
from typing import Any, TypedDict, cast, get_type_hints
from unittest.mock import patch

import agent_team.path_resource_policy as path_resource_policy_module
import agent_team.policy_verification_handoff as handoff_module
import agent_team.review_policy as review_policy_module
from agent_team.path_resource_policy import (
    DispatchMode,
    LaneProfileBinding,
    LaneRoutingDecision,
    PathAccess,
    PathClaim,
    PathClaimPolicy,
    PathEntryKind,
    PathKind,
    PathMutation,
    PathObservation,
    PathOperation,
    ReservationDigest,
    ReservationStatus,
    ResourceClaimPolicy,
    ResourceKey,
    ResourceMode,
    ResourceReservationAuthority,
    ResourceReservationPort,
    ResourceReservationRequest,
    ResourceReservationResult,
    WorkspaceObservation,
)
from agent_team.review_policy import (
    AssignmentCommand,
    CompletionId,
    DecisionRef,
    ReviewDecision,
    ReviewDecisionKind,
    ReviewPolicyHandoffPort,
    ReviewRequest,
    SerialReviewPolicy,
    WorkerAssignment,
    WorkerCompletion,
    WorkerCompletionKind,
    initial_review_policy_state,
    reduce_policy,
)
from agent_team.task_policy import (
    STATE_POLICY_VERSION,
    AttemptId,
    ClaimRef,
    DispatchId,
    GitObjectId,
    ResourceClaim,
    TaskId,
    TaskKind,
    TaskLane,
    TaskPhase,
    TaskPolicyStateV4,
    TaskSpec,
    TreeDigest,
    VerificationProfileRef,
    WorkspaceIdentity,
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
WORKSPACE = WorkspaceObservation(
    WorkspaceIdentity("/repo"), "/repo", device=1, inode=10, case_sensitive=True
)
AUTHORITY = ResourceReservationAuthority("owner-1", lease_epoch=3, fencing_token=7)
CANARY = "handoff-authority-canary"


def _team_definition(*, team_id: str = "team") -> TeamDefinition:
    return TeamDefinition(
        TeamId(team_id),
        (
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
        ),
        (
            Edge(NodeId("main"), NodeId("worker"), EdgeKind.DELEGATES_TO),
            Edge(NodeId("worker"), NodeId("reviewer"), EdgeKind.REVIEWED_BY),
        ),
    )


def _review_task(
    *,
    lane: TaskLane = TaskLane.NORMAL,
    kind: TaskKind = TaskKind.IMPLEMENTATION,
    title: str = CANARY,
) -> TaskSpec:
    return TaskSpec(
        task_id=TaskId("task"),
        title=title,
        context="A bounded context.",
        goal="Implement the requested change.",
        acceptance=("The change is verified.",),
        allowed_paths=("scripts/agent-team",),
        do_not_modify=(),
        dependencies=(),
        verification=VerificationProfileRef("tests"),
        escalation_node=NodeId("main"),
        kind=kind,
        lane=lane,
        resource_claims=(),
    )


def _review_policy(
    value: TaskSpec, *, team_definition: TeamDefinition | None = None
) -> SerialReviewPolicy:
    return SerialReviewPolicy(
        task=value,
        team_definition=team_definition or _team_definition(),
        worker_node=NodeId("worker"),
        max_review_rounds=2,
    )


def _review_state(
    *,
    phase: TaskPhase = TaskPhase.PENDING,
    team_id: str = "team",
    workspace: str = "/repo",
    task_id: str = "task",
) -> TaskPolicyStateV4:
    return TaskPolicyStateV4(
        version=STATE_POLICY_VERSION,
        team_id=TeamId(team_id),
        workspace=WorkspaceIdentity(workspace),
        sequence=0,
        task_id=TaskId(task_id),
        attempt_id=None,
        dispatch_id=None,
        worker_node=None,
        reviewer_node=None,
        review_round=0,
        target_head=None,
        target_tree_digest=None,
        claim_ref=ClaimRef("claim-1"),
        receipt_ref=None,
        phase=phase,
    )


def _assignment(
    *, attempt: str = "attempt-1", task_id: str = "task"
) -> WorkerAssignment:
    return WorkerAssignment(
        run_id=review_policy_module.RunId("run-1"),
        task_id=TaskId(task_id),
        dispatch_id=DispatchId("dispatch-1"),
        attempt_id=AttemptId(attempt),
        worker_node=NodeId("worker"),
        reviewer_node=NodeId("reviewer"),
        worker_terminal_id=review_policy_module.TerminalId("worker-terminal"),
        reviewer_terminal_id=review_policy_module.TerminalId("reviewer-terminal"),
        review_round=1,
        target_head=None,
        target_tree_digest=None,
    )


def _completion(assigned: WorkerAssignment) -> WorkerCompletion:
    return WorkerCompletion(
        expected_sequence=1,
        run_id=assigned.run_id,
        task_id=assigned.task_id,
        dispatch_id=assigned.dispatch_id,
        attempt_id=assigned.attempt_id,
        worker_node=assigned.worker_node,
        reviewer_node=assigned.reviewer_node,
        worker_terminal_id=assigned.worker_terminal_id,
        review_round=assigned.review_round,
        completion_id=CompletionId("completion-1"),
        target_head=HEAD,
        target_tree_digest=TREE,
        kind=WorkerCompletionKind.SUCCEEDED,
        explanation=CANARY,
    )


def _review_decision(assigned: WorkerAssignment) -> ReviewDecision:
    return ReviewDecision(
        expected_sequence=3,
        run_id=assigned.run_id,
        task_id=assigned.task_id,
        dispatch_id=assigned.dispatch_id,
        attempt_id=assigned.attempt_id,
        worker_node=assigned.worker_node,
        reviewer_node=assigned.reviewer_node,
        worker_terminal_id=assigned.worker_terminal_id,
        reviewer_terminal_id=assigned.reviewer_terminal_id,
        review_round=assigned.review_round,
        completion_id=CompletionId("completion-1"),
        completion_expected_sequence=1,
        target_head=HEAD,
        target_tree_digest=TREE,
        decision_ref=DecisionRef("decision-1"),
        kind=ReviewDecisionKind.APPROVED,
        explanation=CANARY,
    )


def _review_path(
    *,
    approved: bool = True,
    task: TaskSpec | None = None,
    attempt: str = "attempt-1",
    team_definition: TeamDefinition | None = None,
    workspace: str = "/repo",
) -> tuple[Any, SerialReviewPolicy]:
    value = _review_task() if task is None else task
    definition = team_definition or _team_definition()
    policy = _review_policy(value, team_definition=definition)
    pending = initial_review_policy_state(
        review_policy_module.RunId("run-1"),
        _review_state(
            team_id=str(definition.team_id),
            workspace=workspace,
            task_id=str(value.task_id),
        ),
    )
    assigned = _assignment(attempt=attempt, task_id=str(value.task_id))
    assigned_update = reduce_policy(
        pending, AssignmentCommand(expected_sequence=0, assignment=assigned), policy
    )
    completed = _completion(assigned)
    worker_done = reduce_policy(assigned_update.next_state, completed, policy)
    review_update = reduce_policy(
        worker_done.next_state,
        ReviewRequest(expected_sequence=2, completion=completed),
        policy,
    )
    if not approved:
        return review_update, policy
    approved_update = reduce_policy(
        review_update.next_state, _review_decision(assigned), policy
    )
    return approved_update, policy


def _path_task(
    *,
    lane: TaskLane = TaskLane.NORMAL,
    kind: TaskKind = TaskKind.IMPLEMENTATION,
    resources: tuple[str, ...] = ("cache",),
) -> TaskSpec:
    return TaskSpec(
        task_id=TaskId("task"),
        title=CANARY,
        context="A bounded context.",
        goal="Produce the requested result.",
        acceptance=("The result is checked.",),
        allowed_paths=("src/file.txt",),
        do_not_modify=(),
        dependencies=(),
        verification=VerificationProfileRef("tests"),
        escalation_node=NodeId("main"),
        kind=kind,
        lane=lane,
        resource_claims=tuple(ResourceClaim(item) for item in resources),
    )


def _path_policy(value: TaskSpec) -> PathClaimPolicy:
    return PathClaimPolicy.from_task_spec(
        value,
        workspace=WORKSPACE,
        allowed=(PathClaim("src/file.txt", PathKind.EXACT, PathAccess.WRITE),),
        denied=(),
        reserved_roots=(),
    )


def _path_observations() -> tuple[PathObservation, ...]:
    return (
        PathObservation(
            relative_path=".",
            canonical_path="/repo",
            entry_kind=PathEntryKind.DIRECTORY,
            device=1,
            inode=10,
            nlink=2,
            parent_device=None,
            parent_inode=None,
            ancestor_symlink=False,
        ),
        PathObservation(
            relative_path="src",
            canonical_path="/repo/src",
            entry_kind=PathEntryKind.DIRECTORY,
            device=1,
            inode=20,
            nlink=2,
            parent_device=1,
            parent_inode=10,
            ancestor_symlink=False,
        ),
        PathObservation(
            relative_path="src/file.txt",
            canonical_path="/repo/src/file.txt",
            entry_kind=PathEntryKind.REGULAR,
            device=1,
            inode=30,
            nlink=1,
            parent_device=1,
            parent_inode=20,
            ancestor_symlink=False,
        ),
    )


class RecordingReservationPort(ResourceReservationPort):
    def __init__(
        self,
        *,
        status: ReservationStatus = ReservationStatus.RESERVED,
        mutate: Callable[[ResourceReservationResult], ResourceReservationResult]
        | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.mutate = mutate
        self.error = error
        self.calls: list[ResourceReservationRequest] = []

    def reserve(self, request: ResourceReservationRequest) -> ResourceReservationResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        result = ResourceReservationResult(
            self.status,
            request.reservation_id,
            tuple(item.key for item in request.claims),
            request.authority,
            request.task_id,
            request.request_digest,
        )
        return self.mutate(result) if self.mutate is not None else result


class _RouteInputs(TypedDict):
    task: TaskSpec
    path_policy: PathClaimPolicy
    path_mutation: PathMutation
    path_observations: tuple[PathObservation, ...]
    resource_claims: tuple[ResourceClaimPolicy, ...]
    known_keys: frozenset[ResourceKey]
    profile: LaneProfileBinding
    reservation_port: ResourceReservationPort
    reservation_id: str
    reservation_authority: ResourceReservationAuthority | None


def _route_inputs(
    value: TaskSpec,
    *,
    port: RecordingReservationPort | None = None,
    profile: LaneProfileBinding | None = None,
    policy: PathClaimPolicy | None = None,
    operation: PathOperation = PathOperation.MODIFY,
    observations: tuple[PathObservation, ...] | None = None,
    authority: ResourceReservationAuthority | None = AUTHORITY,
) -> _RouteInputs:
    claims = tuple(
        ResourceClaimPolicy(
            item,
            ResourceKey(item.name),
            ResourceMode.SHARED,
        )
        for item in value.resource_claims
    )
    serial_policy = (
        SerialReviewPolicy(
            task=value,
            team_definition=_team_definition(),
            worker_node=NodeId("worker"),
            max_review_rounds=2,
        )
        if value.lane in {TaskLane.NORMAL, TaskLane.EXPRESS}
        else None
    )
    selected_profile = profile or LaneProfileBinding(
        team_definition=_team_definition(),
        worker_node=NodeId("worker"),
        reviewer_pair=None if serial_policy is None else serial_policy.pair,
        serial_review_policy=serial_policy,
    )
    selected_port = port or RecordingReservationPort()
    return {
        "task": value,
        "path_policy": policy or _path_policy(value),
        "path_mutation": PathMutation(operation, "src/file.txt", None),
        "path_observations": observations or _path_observations(),
        "resource_claims": claims,
        "known_keys": frozenset(
            ResourceKey(item.name) for item in value.resource_claims
        ),
        "profile": selected_profile,
        "reservation_port": selected_port,
        "reservation_id": "reservation-1",
        "reservation_authority": authority if claims else None,
    }


class RecordingPolicyVerificationStore:
    """Structural fake for the injected #74 registry/store boundary."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.writes = 0
        self.reads = 0
        self.review_records: dict[str, Any] = {}
        self.completion_records: dict[str, Any] = {}
        self.approval_records: dict[str, Any] = {}

    @staticmethod
    def _record_key(value: object) -> str:
        reference = getattr(value, "reference", None)
        if type(reference) is not str:
            reference = getattr(value, "approval_ref", None)
        if type(reference) is not str:
            raise AssertionError("stored record has no reference")
        return reference

    def _save(self, name: str, records: dict[str, Any], value: Any) -> Any:
        self.calls.append((name, (value,), {}))
        self.writes += 1
        key = self._record_key(value)
        existing = records.get(key)
        if existing is None:
            records[key] = value
            return value
        return existing

    def _read(self, name: str, records: dict[str, Any], reference: str) -> Any:
        self.calls.append((name, (reference,), {}))
        self.reads += 1
        value = records.get(reference)
        if value is None:
            raise LookupError(reference)
        return value

    def save_review_authority(self, record: Any) -> Any:
        return self._save("save_review_authority", self.review_records, record)

    def read_review_authority(self, reference: str) -> Any:
        return self._read("read_review_authority", self.review_records, reference)

    def save_completion_admission(self, record: Any) -> Any:
        return self._save("save_completion_admission", self.completion_records, record)

    def read_completion_admission(self, reference: str) -> Any:
        return self._read(
            "read_completion_admission", self.completion_records, reference
        )

    def save_approval(self, record: Any) -> Any:
        return self._save("save_approval", self.approval_records, record)

    def read_approval(self, reference: str) -> Any:
        return self._read("read_approval", self.approval_records, reference)

    def state_port(self) -> Any:
        return None


def _new_handoff(
    _test: unittest.TestCase,
) -> tuple[Any, RecordingPolicyVerificationStore]:
    store = RecordingPolicyVerificationStore()
    return handoff_module.PolicyVerificationHandoff(store), store


def _assert_owner_types(
    _test: unittest.TestCase,
) -> tuple[type[Any], type[Any], Any]:
    return (
        review_policy_module.ReviewAuthorityRef,
        path_resource_policy_module.CompletionAdmissionRef,
        handoff_module,
    )


def _forged_record(value: Any, **changes: object) -> Any:
    forged = object.__new__(type(value))
    for field in fields(value):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, object.__getattribute__(value, field.name)),
        )
    return forged


def _forge_ref(value: Any, *, preserve_issuer: bool = False, **changes: object) -> Any:
    forged = _forged_record(value, **changes)
    object.__setattr__(
        forged,
        "_issuer",
        object.__getattribute__(value, "_issuer") if preserve_issuer else object(),
    )
    return forged


class PolicyVerificationHandoffAuthorityTest(unittest.TestCase):
    def _review_ref(
        self, *, approved: bool = True
    ) -> tuple[Any, Any, Any, RecordingPolicyVerificationStore]:
        _assert_owner_types(self)
        handoff, store = _new_handoff(self)
        update, policy = _review_path(approved=approved)
        ref = handoff.save_authority(update, policy)
        return handoff, ref, policy, store

    def _owner_refs(
        self,
    ) -> tuple[Any, Any, Any, RecordingPolicyVerificationStore]:
        _assert_owner_types(self)
        handoff, store = _new_handoff(self)
        task = _path_task()
        update, policy = _review_path(task=task)
        review_ref = handoff.save_authority(update, policy)
        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(task, port=RecordingReservationPort())
        )
        return handoff, review_ref, completion_ref, store

    def _assert_no_ref(self, operation: Callable[[], object]) -> None:
        try:
            value = operation()
        except ValueError as error:
            self.assertTrue(
                hasattr(error, "code") or hasattr(error, "reason_code"),
                f"rejection must expose a bounded error code: {error!r}",
            )
            self.assertNotIn(CANARY, str(error))
            return
        self.assertIsNone(value, f"rejected handoff returned an authority: {value!r}")

    def test_expected_types_and_public_ports_are_present(self) -> None:
        review_ref_type, completion_ref_type, module = _assert_owner_types(self)
        self.assertTrue(inspect.isclass(review_ref_type))
        self.assertTrue(inspect.isclass(completion_ref_type))
        handoff_type = getattr(module, "PolicyVerificationHandoff", None)
        self.assertTrue(inspect.isclass(handoff_type))
        self.assertEqual(
            tuple(inspect.signature(ReviewPolicyHandoffPort.save_authority).parameters),
            ("self", "update", "policy"),
        )
        self.assertIs(
            get_type_hints(ReviewPolicyHandoffPort.save_authority)["return"],
            review_ref_type,
        )
        for name in ("save_authority", "issue_completion_admission", "compose"):
            self.assertTrue(callable(getattr(handoff_type, name, None)), name)

    def test_public_issuance_signatures_never_accept_raw_owner_values(self) -> None:
        _, _, module = _assert_owner_types(self)
        handoff_type = module.PolicyVerificationHandoff
        forbidden = {
            "projection",
            "path_admission",
            "decision",
            "reservation_result",
            "routing_digest",
            "reservation_digest",
            "parallel_candidate",
            "raw_result",
            "result",
        }
        for method_name in ("save_authority", "issue_completion_admission", "compose"):
            parameters = inspect.signature(
                getattr(handoff_type, method_name)
            ).parameters
            with self.subTest(method=method_name):
                self.assertFalse(forbidden.intersection(parameters))
        self.assertEqual(
            tuple(inspect.signature(handoff_type.compose).parameters),
            ("self", "review_ref", "completion_ref"),
        )
        self.assertFalse(
            any(
                name in dir(handoff_type)
                for name in ("projection_to_ref", "decision_to_ref", "result_to_ref")
            )
        )

        handoff, _ = _new_handoff(self)
        update, policy = _review_path()
        with self.assertRaises(TypeError):
            handoff.save_authority(update, policy, object())
        value = _path_task()
        with self.assertRaises(TypeError):
            handoff.issue_completion_admission(
                **_route_inputs(value), decision=object()
            )

    def test_actual_approved_update_issues_body_free_review_ref(self) -> None:
        _handoff, ref, _, store = self._review_ref()
        ref_type = review_policy_module.ReviewAuthorityRef
        self.assertIs(type(ref), ref_type)
        self.assertIsNot(type(ref), str)
        self.assertIs(type(ref.reference), str)
        self.assertIs(type(ref.digest), str)
        self.assertEqual(len(ref.digest), 64)
        self.assertNotIn(CANARY, repr(ref))
        self.assertNotIn(CANARY, ref.reference)
        self.assertNotIn(CANARY, ref.digest)
        self.assertEqual(store.writes, 1)
        self.assertEqual(store.reads, 1)
        self.assertEqual(
            [name for name, _, _ in store.calls],
            ["save_review_authority", "read_review_authority"],
        )
        stored = store.review_records[ref.reference]
        self.assertEqual(stored.digest, ref.digest)
        self.assertNotIn(CANARY, repr(stored))
        self.assertTrue(
            all(
                CANARY not in str(object.__getattribute__(stored, item.name))
                for item in fields(stored)
                if item.name != "_issuer"
            )
        )
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.assertRaises((TypeError, pickle.PicklingError)):
                operation(stored)

    def test_actual_normal_and_express_routes_issue_completion_refs(self) -> None:
        for lane, kind in (
            (TaskLane.NORMAL, TaskKind.IMPLEMENTATION),
            (TaskLane.EXPRESS, TaskKind.SMALL_CHANGE),
        ):
            with self.subTest(lane=lane):
                handoff, store = _new_handoff(self)
                value = _path_task(lane=lane, kind=kind)
                probe_port = RecordingReservationPort()
                probe_route = _route_inputs(value, port=probe_port)
                decision = path_resource_policy_module.route_task(**probe_route)
                self.assertTrue(decision.candidate)
                self.assertIs(decision.dispatch_mode, DispatchMode.SERIAL)
                self.assertTrue(decision.serial_review_required)
                self.assertTrue(decision.completion_gate_required)
                self.assertTrue(decision.permits_workspace_write)
                self.assertFalse(decision.parallel_candidate)
                self.assertIsNone(decision.reason_code)
                port = RecordingReservationPort()
                route = _route_inputs(value, port=port)
                ref = handoff.issue_completion_admission(**route)
                self.assertIs(
                    type(ref), path_resource_policy_module.CompletionAdmissionRef
                )
                self.assertEqual(len(port.calls), 1)
                self.assertNotIn(CANARY, repr(ref))
                self.assertEqual(store.writes, 1)
                self.assertEqual(store.reads, 1)
                self.assertEqual(
                    [name for name, _, _ in store.calls],
                    ["save_completion_admission", "read_completion_admission"],
                )
                stored = store.completion_records[ref.reference]
                self.assertEqual(stored.digest, ref.digest)
                self.assertNotIn(CANARY, repr(stored))
                self.assertTrue(
                    all(
                        CANARY not in str(object.__getattribute__(stored, item.name))
                        for item in fields(stored)
                        if item.name != "_issuer"
                    )
                )
                for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                    with self.assertRaises((TypeError, pickle.PicklingError)):
                        operation(stored)

    def test_completion_digest_is_canonical_and_field_sensitive(self) -> None:
        task = _path_task()

        def issue(
            *,
            observations: tuple[PathObservation, ...],
            authority: ResourceReservationAuthority = AUTHORITY,
        ) -> Any:
            handoff, _ = _new_handoff(self)
            return handoff.issue_completion_admission(
                **_route_inputs(
                    task,
                    port=RecordingReservationPort(),
                    observations=observations,
                    authority=authority,
                )
            )

        observations = _path_observations()
        baseline = issue(observations=observations)
        reordered = issue(observations=tuple(reversed(observations)))
        changed_path = issue(
            observations=(*observations[:2], replace(observations[2], inode=31))
        )
        changed_fence = issue(
            observations=observations,
            authority=ResourceReservationAuthority(
                AUTHORITY.owner_id,
                AUTHORITY.lease_epoch,
                AUTHORITY.fencing_token + 1,
            ),
        )
        self.assertEqual(baseline.reference, reordered.reference)
        self.assertEqual(baseline.digest, reordered.digest)
        self.assertNotEqual(baseline.digest, changed_path.digest)
        self.assertNotEqual(baseline.digest, changed_fence.digest)

    def test_claim_free_completion_issues_without_reservation_call(self) -> None:
        handoff, store = _new_handoff(self)
        task = _path_task(resources=())
        port = RecordingReservationPort()
        ref = handoff.issue_completion_admission(**_route_inputs(task, port=port))
        self.assertIs(type(ref), path_resource_policy_module.CompletionAdmissionRef)
        self.assertEqual(port.calls, [])
        self.assertEqual(store.writes, 1)
        stored = store.completion_records[ref.reference]
        self.assertIsNone(stored.reservation_digest)

    def test_claim_free_still_rejects_invalid_reservation_port_via_route(self) -> None:
        handoff, store = _new_handoff(self)
        task = _path_task(resources=())
        route = _route_inputs(task)
        route["reservation_port"] = cast(ResourceReservationPort, None)
        with patch.object(
            handoff_module,
            "route_task",
            wraps=path_resource_policy_module.route_task,
        ) as routed:
            self._assert_no_ref(partial(handoff.issue_completion_admission, **route))
            routed.assert_called_once()
        self.assertEqual(store.writes, 0)

    def test_completion_preserves_owner_grammar_for_slash_identifiers(self) -> None:
        handoff, store = _new_handoff(self)
        task = _path_task()
        port = RecordingReservationPort()
        route = _route_inputs(task, port=port)
        route["reservation_id"] = "lease/123"
        route["reservation_authority"] = ResourceReservationAuthority(
            "owner/path", lease_epoch=3, fencing_token=7
        )
        route["resource_claims"] = (
            ResourceClaimPolicy(
                task.resource_claims[0],
                ResourceKey("cache/path"),
                ResourceMode.SHARED,
            ),
        )
        route["known_keys"] = frozenset((ResourceKey("cache/path"),))
        ref = handoff.issue_completion_admission(**route)
        stored = store.completion_records[ref.reference]
        self.assertEqual(stored.reservation_id, "lease/123")
        self.assertEqual(stored.reservation_owner, "owner/path")
        self.assertEqual(stored.reservation_claim_keys, ("cache/path",))

    def test_research_read_only_candidate_never_becomes_completion_authority(
        self,
    ) -> None:
        value = _path_task(lane=TaskLane.RESEARCH, kind=TaskKind.RESEARCH, resources=())
        readonly_team = _team_definition()
        readonly_worker = replace(
            readonly_team.nodes[1],
            profile=replace(readonly_team.nodes[1].profile, permission="read-only"),
        )
        profile = LaneProfileBinding(
            team_definition=replace(
                readonly_team,
                nodes=(readonly_team.nodes[0], readonly_worker, readonly_team.nodes[2]),
            ),
            worker_node=NodeId("worker"),
            reviewer_pair=None,
            serial_review_policy=None,
        )
        port = RecordingReservationPort()
        route = _route_inputs(
            value,
            port=port,
            profile=profile,
            operation=PathOperation.READ,
            policy=PathClaimPolicy.from_task_spec(
                value,
                workspace=WORKSPACE,
                allowed=(PathClaim("src/file.txt", PathKind.EXACT, PathAccess.READ),),
                denied=(),
                reserved_roots=(),
            ),
        )
        decision = path_resource_policy_module.route_task(**route)
        self.assertTrue(decision.candidate)
        self.assertIs(decision.dispatch_mode, DispatchMode.READ_ONLY)
        self.assertEqual(port.calls, [])
        handoff, store = _new_handoff(self)
        before = store.writes
        with patch.object(
            handoff_module,
            "route_task",
            wraps=path_resource_policy_module.route_task,
        ) as routed:
            self._assert_no_ref(partial(handoff.issue_completion_admission, **route))
            routed.assert_called_once()
        self.assertEqual(port.calls, [])
        self.assertEqual(store.writes, before)

    def test_conflict_stale_unknown_reservations_never_fallback_or_issue(self) -> None:
        for status in (
            ReservationStatus.CONFLICT,
            ReservationStatus.STALE,
            ReservationStatus.UNKNOWN,
        ):
            with self.subTest(status=status):
                handoff, store = _new_handoff(self)
                value = _path_task()
                port = RecordingReservationPort(status=status)
                route = _route_inputs(value, port=port)
                before = store.writes
                self._assert_no_ref(
                    partial(handoff.issue_completion_admission, **route)
                )
                self.assertEqual(len(port.calls), 1)
                self.assertEqual(store.writes, before)

    def test_reservation_identity_mismatch_never_issues(self) -> None:
        value = _path_task()
        mutations: tuple[
            tuple[
                str,
                Callable[[ResourceReservationResult], ResourceReservationResult],
            ],
            ...,
        ] = (
            ("task", lambda result: replace(result, task_id=TaskId("foreign"))),
            ("reservation", lambda result: replace(result, reservation_id="foreign")),
            ("claims", lambda result: replace(result, claim_keys=())),
            ("authority", lambda result: replace(result, authority=None)),
            (
                "digest",
                lambda result: replace(
                    result, request_digest=ReservationDigest("0" * 64)
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(identity=label):
                handoff, store = _new_handoff(self)
                port = RecordingReservationPort(mutate=mutate)
                route = _route_inputs(value, port=port)
                before = store.writes
                self._assert_no_ref(
                    partial(handoff.issue_completion_admission, **route)
                )
                self.assertEqual(len(port.calls), 1)
                self.assertEqual(store.writes, before)

    def test_reservation_port_failure_never_falls_back(self) -> None:
        class ProviderFailure(Exception):
            pass

        handoff, store = _new_handoff(self)
        port = RecordingReservationPort(
            error=ProviderFailure("provider detail " + CANARY)
        )
        route = _route_inputs(_path_task(), port=port)
        self._assert_no_ref(lambda: handoff.issue_completion_admission(**route))
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(store.writes, 0)

    def test_forged_route_postcondition_is_rejected_without_reservation(self) -> None:
        handoff, store = _new_handoff(self)
        value = _path_task()
        forged = LaneRoutingDecision(
            lane=TaskLane.NORMAL,
            candidate=True,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            parallel_candidate=False,
            reservation=None,
            reason_code=None,
        )
        route_function = getattr(handoff_module, "route_task", None)
        self.assertTrue(callable(route_function))
        port = RecordingReservationPort()
        route = _route_inputs(value, port=port)
        with patch.object(handoff_module, "route_task", return_value=forged) as mocked:
            before = store.writes
            self._assert_no_ref(lambda: handoff.issue_completion_admission(**route))
            mocked.assert_called_once()
            self.assertEqual(port.calls, [])
            self.assertEqual(store.writes, before)

    def test_forged_route_lane_and_reservation_identity_are_rejected(self) -> None:
        actual_route = path_resource_policy_module.route_task
        for label in ("lane", "task", "reservation", "claims", "authority", "digest"):
            with self.subTest(field=label):
                handoff, store = _new_handoff(self)
                task = _path_task()
                port = RecordingReservationPort()
                route = _route_inputs(task, port=port)

                def forged_route(
                    routed_task: TaskSpec,
                    selected_label: str = label,
                    **kwargs: Any,
                ) -> LaneRoutingDecision:
                    decision = actual_route(routed_task, **kwargs)
                    if selected_label == "lane":
                        return replace(decision, lane=TaskLane.EXPRESS)
                    reservation = cast(ResourceReservationResult, decision.reservation)
                    if selected_label == "task":
                        reservation = replace(
                            reservation, task_id=TaskId("foreign-task")
                        )
                    elif selected_label == "reservation":
                        reservation = replace(
                            reservation, reservation_id="foreign-reservation"
                        )
                    elif selected_label == "claims":
                        reservation = replace(reservation, claim_keys=())
                    elif selected_label == "authority":
                        reservation = replace(reservation, authority=None)
                    else:
                        reservation = replace(
                            reservation,
                            request_digest=ReservationDigest("0" * 64),
                        )
                    return replace(decision, reservation=reservation)

                with patch.object(
                    handoff_module, "route_task", side_effect=forged_route
                ):
                    self._assert_no_ref(
                        partial(handoff.issue_completion_admission, **route)
                    )
                self.assertEqual(len(port.calls), 1)
                self.assertEqual(store.writes, 0)

    def test_route_cannot_skip_or_repeat_the_reservation_effect(self) -> None:
        actual_route = path_resource_policy_module.route_task
        for mode in ("skip", "repeat"):
            with self.subTest(mode=mode):
                handoff, store = _new_handoff(self)
                task = _path_task()
                port = RecordingReservationPort()
                route = _route_inputs(task, port=port)

                def forged_route(
                    routed_task: TaskSpec,
                    selected_mode: str = mode,
                    observed_port: RecordingReservationPort = port,
                    **kwargs: Any,
                ) -> LaneRoutingDecision:
                    if selected_mode == "skip":
                        return LaneRoutingDecision(
                            lane=routed_task.lane,
                            candidate=True,
                            dispatch_mode=DispatchMode.SERIAL,
                            serial_review_required=True,
                            completion_gate_required=True,
                            permits_workspace_write=True,
                            parallel_candidate=False,
                            reservation=ResourceReservationResult(
                                ReservationStatus.RESERVED,
                                "reservation-1",
                                (ResourceKey("cache"),),
                                AUTHORITY,
                                routed_task.task_id,
                                ReservationDigest("0" * 64),
                            ),
                            reason_code=None,
                        )
                    decision = actual_route(routed_task, **kwargs)
                    captured_port = cast(
                        ResourceReservationPort, kwargs["reservation_port"]
                    )
                    try:
                        captured_port.reserve(observed_port.calls[0])
                    except RuntimeError:
                        pass
                    return decision

                with patch.object(
                    handoff_module, "route_task", side_effect=forged_route
                ):
                    self._assert_no_ref(
                        partial(handoff.issue_completion_admission, **route)
                    )
                self.assertEqual(len(port.calls), 0 if mode == "skip" else 1)
                self.assertEqual(store.writes, 0)

    def test_route_input_mutation_does_not_issue_from_a_different_snapshot(
        self,
    ) -> None:
        def mutating_port(
            target: str,
            task: TaskSpec,
            observations: tuple[PathObservation, ...],
            profile: LaneProfileBinding,
        ) -> RecordingReservationPort:
            class MutatingPort(RecordingReservationPort):
                def reserve(
                    self, request: ResourceReservationRequest
                ) -> ResourceReservationResult:
                    result = super().reserve(request)
                    if target == "task-id":
                        object.__setattr__(task, "task_id", TaskId("foreign-task"))
                    elif target == "task-title":
                        object.__setattr__(task, "title", "changed title")
                    elif target == "observation":
                        object.__setattr__(observations[2], "inode", 99)
                    elif target == "policy-rounds":
                        policy = cast(SerialReviewPolicy, profile.serial_review_policy)
                        object.__setattr__(policy, "max_review_rounds", 3)
                    elif target == "profile-permission":
                        worker = profile.team_definition.nodes[1]
                        object.__setattr__(worker.profile, "permission", "read-only")
                    elif target == "profile-label":
                        worker = profile.team_definition.nodes[1]
                        object.__setattr__(worker, "label", "Changed worker")
                    elif target == "profile-edge":
                        edge = profile.team_definition.edges[0]
                        object.__setattr__(edge, "kind", EdgeKind.REVIEWED_BY)
                    else:
                        pair = profile.reviewer_pair
                        if pair is None:
                            raise AssertionError("serial profile has no reviewer pair")
                        object.__setattr__(pair, "worker_node", NodeId("main"))
                    return result

            return MutatingPort()

        for target in (
            "task-id",
            "task-title",
            "observation",
            "profile-permission",
            "profile-label",
            "profile-edge",
            "profile-pair",
            "policy-rounds",
        ):
            with self.subTest(target=target):
                handoff, store = _new_handoff(self)
                task = _path_task()
                observations = _path_observations()

                route = _route_inputs(task, observations=observations)
                profile = route["profile"]
                port = mutating_port(target, task, observations, profile)
                route["reservation_port"] = port
                with (
                    patch.object(
                        handoff_module,
                        "route_task",
                        wraps=path_resource_policy_module.route_task,
                    ) as routed,
                    self.assertRaises(
                        handoff_module.PolicyVerificationHandoffError
                    ) as raised,
                ):
                    handoff.issue_completion_admission(**route)
                self.assertEqual(raised.exception.code, "completion-input-drift")
                routed.assert_called_once()
                self.assertEqual(len(port.calls), 1)
                self.assertEqual(store.writes, 0)
                self.assertEqual(store.reads, 0)
                self.assertEqual(store.completion_records, {})

    def test_unicode_workspace_composes_exact_owner_records(self) -> None:
        workspace_path = "/repo/日本語"
        workspace = WorkspaceObservation(
            WorkspaceIdentity(workspace_path),
            workspace_path,
            device=1,
            inode=10,
            case_sensitive=True,
        )
        observations = tuple(
            replace(
                observation,
                canonical_path=(
                    workspace_path
                    if observation.relative_path == "."
                    else f"{workspace_path}/{observation.relative_path}"
                ),
            )
            for observation in _path_observations()
        )
        task = _path_task()
        policy = PathClaimPolicy.from_task_spec(
            task,
            workspace=workspace,
            allowed=(PathClaim("src/file.txt", PathKind.EXACT, PathAccess.WRITE),),
            denied=(),
            reserved_roots=(),
        )
        handoff, store = _new_handoff(self)
        update, review_policy = _review_path(task=task, workspace=workspace_path)
        review_ref = handoff.save_authority(update, review_policy)
        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(
                task,
                port=RecordingReservationPort(),
                policy=policy,
                observations=observations,
            )
        )
        approval_ref = handoff.compose(review_ref, completion_ref)
        self.assertTrue(approval_ref)
        self.assertEqual(
            store.review_records[review_ref.reference].workspace, workspace_path
        )
        self.assertEqual(
            store.completion_records[completion_ref.reference].workspace,
            workspace_path,
        )

    def test_unicode_owner_identifiers_preserve_upstream_grammar(self) -> None:
        definition = _team_definition(team_id="チーム")
        task = replace(_path_task(), task_id=TaskId("課題"))
        policy = _review_policy(task, team_definition=definition)
        profile = LaneProfileBinding(
            team_definition=definition,
            worker_node=NodeId("worker"),
            reviewer_pair=policy.pair,
            serial_review_policy=policy,
        )
        handoff, store = _new_handoff(self)
        update, review_policy = _review_path(
            task=task,
            team_definition=definition,
        )
        review_ref = handoff.save_authority(update, review_policy)
        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(
                task,
                port=RecordingReservationPort(),
                profile=profile,
            )
        )
        approval_ref = handoff.compose(review_ref, completion_ref)
        self.assertTrue(approval_ref)
        self.assertEqual(store.review_records[review_ref.reference].team_id, "チーム")
        self.assertEqual(store.review_records[review_ref.reference].task_id, "課題")
        self.assertEqual(
            store.completion_records[completion_ref.reference].team_id,
            "チーム",
        )
        self.assertEqual(
            store.completion_records[completion_ref.reference].task_id,
            "課題",
        )

    def test_unicode_comparison_is_exact_while_refs_remain_ascii(self) -> None:
        self.assertTrue(handoff_module._same_text("日本語", "日本語"))
        self.assertFalse(handoff_module._same_text("日本語", "日本語2"))
        self.assertFalse(handoff_module._same_text("é", "e\N{COMBINING ACUTE ACCENT}"))
        self.assertFalse(handoff_module._same_text("\ud800", "\ud800"))
        with self.assertRaises(handoff_module.PolicyVerificationHandoffError) as raised:
            handoff_module._reference("レビュー", "review reference")
        self.assertEqual(raised.exception.code, "invalid-reference")

    def test_wrong_policy_and_forged_review_update_never_issue(self) -> None:
        handoff, store = _new_handoff(self)
        update, policy = _review_path()
        foreign_policy = replace(policy, task=replace(policy.task, title="foreign"))
        before = store.writes
        self._assert_no_ref(lambda: handoff.save_authority(update, foreign_policy))
        self.assertEqual(store.writes, before)

        forged = object.__new__(type(update))
        for item in fields(update):
            object.__setattr__(forged, item.name, getattr(update, item.name))
        object.__setattr__(forged, "policy_fingerprint", "0" * 64)
        self._assert_no_ref(lambda: handoff.save_authority(forged, policy))
        self.assertEqual(store.writes, before)

    def test_non_approved_review_ref_is_not_accepted_by_composer(self) -> None:
        handoff, _ = _new_handoff(self)
        pending_update, policy = _review_path(approved=False)
        review_ref = handoff.save_authority(pending_update, policy)
        value = _path_task()
        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(value, port=RecordingReservationPort())
        )
        self._assert_no_ref(lambda: handoff.compose(review_ref, completion_ref))

    def test_refs_are_return_only_and_copy_pickle_closed(self) -> None:
        _, review_ref, completion_ref, _ = self._owner_refs()
        for value in (review_ref, completion_ref):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    type(value)()
                with self.assertRaises(TypeError):
                    type(value)("bare", "0" * 64)
                for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                    with self.assertRaises((TypeError, pickle.PicklingError)):
                        operation(value)

    def test_foreign_wrong_issuer_and_bare_refs_never_compose(self) -> None:
        handoff, review_ref, completion_ref, _ = self._owner_refs()
        for label, left, right in (
            ("bare review", "bare", completion_ref),
            ("foreign review", _forge_ref(review_ref), completion_ref),
            (
                "changed review digest",
                _forge_ref(review_ref, preserve_issuer=True, digest="0" * 64),
                completion_ref,
            ),
            ("bare completion", review_ref, "bare"),
            ("foreign completion", review_ref, _forge_ref(completion_ref)),
            (
                "changed completion digest",
                review_ref,
                _forge_ref(completion_ref, preserve_issuer=True, digest="0" * 64),
            ),
        ):
            with self.subTest(ref=label):
                self._assert_no_ref(partial(handoff.compose, left, right))

    def test_ref_pair_cannot_be_substituted_with_another_valid_record(self) -> None:
        handoff, store = _new_handoff(self)
        task = _path_task()
        first_update, policy = _review_path(task=task, attempt="attempt-1")
        second_update, _ = _review_path(task=task, attempt="attempt-2")
        first_review = handoff.save_authority(first_update, policy)
        second_review = handoff.save_authority(second_update, policy)
        first_completion = handoff.issue_completion_admission(
            **_route_inputs(task, port=RecordingReservationPort())
        )
        second_completion = handoff.issue_completion_admission(
            **_route_inputs(
                task,
                port=RecordingReservationPort(),
                authority=ResourceReservationAuthority(
                    AUTHORITY.owner_id,
                    AUTHORITY.lease_epoch,
                    AUTHORITY.fencing_token + 1,
                ),
            )
        )
        substituted_review = first_review
        object.__setattr__(substituted_review, "reference", second_review.reference)
        object.__setattr__(substituted_review, "digest", second_review.digest)
        substituted_completion = first_completion
        object.__setattr__(
            substituted_completion, "reference", second_completion.reference
        )
        object.__setattr__(substituted_completion, "digest", second_completion.digest)
        before = store.writes
        self._assert_no_ref(
            partial(handoff.compose, substituted_review, first_completion)
        )
        self._assert_no_ref(
            partial(handoff.compose, second_review, substituted_completion)
        )
        self.assertEqual(store.writes, before)

    def test_same_exact_replay_is_idempotent_and_changed_record_conflicts(self) -> None:
        handoff, store = _new_handoff(self)
        task = _path_task()
        update, policy = _review_path(task=task)
        first = handoff.save_authority(update, policy)
        writes_after_first = store.writes
        replay = handoff.save_authority(update, policy)
        self.assertEqual(first.reference, replay.reference)
        self.assertEqual(first.digest, replay.digest)
        self.assertLessEqual(store.writes, writes_after_first + 1)

        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(task, port=RecordingReservationPort())
        )
        changed = _forge_ref(first, preserve_issuer=True, digest="0" * 64)
        self._assert_no_ref(lambda: handoff.compose(changed, completion_ref))
        approval = handoff.compose(first, completion_ref)
        self.assertIs(type(approval), str)
        self.assertTrue(approval)

    def test_store_response_loss_succeeds_only_after_exact_readback(self) -> None:
        _, _, module = _assert_owner_types(self)

        class StoreFailure(Exception):
            pass

        class CommittedThenLostStore(RecordingPolicyVerificationStore):
            def save_review_authority(self, record: object) -> object:
                super().save_review_authority(record)
                raise StoreFailure("review response lost " + CANARY)

            def save_completion_admission(self, record: object) -> object:
                super().save_completion_admission(record)
                raise StoreFailure("completion response lost " + CANARY)

        store = CommittedThenLostStore()
        handoff = module.PolicyVerificationHandoff(store)
        task = _path_task()
        update, policy = _review_path(task=task)
        review_ref = handoff.save_authority(update, policy)
        port = RecordingReservationPort()
        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(task, port=port)
        )
        self.assertTrue(review_ref.reference)
        self.assertTrue(completion_ref.reference)
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(store.reads, 2)

    def test_unrecorded_store_response_loss_requires_recovery_without_retry(
        self,
    ) -> None:
        _, _, module = _assert_owner_types(self)

        class StoreFailure(Exception):
            pass

        class LostBeforeCommitStore(RecordingPolicyVerificationStore):
            def save_review_authority(self, record: object) -> object:
                del record
                raise StoreFailure("review response lost " + CANARY)

            def save_completion_admission(self, record: object) -> object:
                del record
                raise StoreFailure("completion response lost " + CANARY)

        store = LostBeforeCommitStore()
        handoff = module.PolicyVerificationHandoff(store)
        task = _path_task()
        update, policy = _review_path(task=task)
        with self.assertRaises(module.PolicyVerificationRecoveryRequired):
            handoff.save_authority(update, policy)
        port = RecordingReservationPort()
        with self.assertRaises(module.PolicyVerificationRecoveryRequired):
            handoff.issue_completion_admission(**_route_inputs(task, port=port))
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(store.writes, 0)

    def test_successful_save_with_unreadable_readback_requires_recovery(self) -> None:
        _, _, module = _assert_owner_types(self)

        class StoreFailure(Exception):
            pass

        class UnreadableStore(RecordingPolicyVerificationStore):
            def read_review_authority(self, reference: str) -> object:
                del reference
                raise StoreFailure("readback secret " + CANARY)

        store = UnreadableStore()
        handoff = module.PolicyVerificationHandoff(store)
        update, policy = _review_path()
        with self.assertRaises(module.PolicyVerificationRecoveryRequired) as raised:
            handoff.save_authority(update, policy)
        self.assertNotIn(CANARY, str(raised.exception))
        self.assertEqual(store.writes, 1)

    def test_approval_response_loss_and_replay_require_exact_readback(self) -> None:
        _, _, module = _assert_owner_types(self)

        class StoreFailure(Exception):
            pass

        class CommittedThenLostApprovalStore(RecordingPolicyVerificationStore):
            def save_approval(self, record: Any) -> Any:
                super().save_approval(record)
                raise StoreFailure("approval response lost " + CANARY)

        store = CommittedThenLostApprovalStore()
        handoff = module.PolicyVerificationHandoff(store)
        task = _path_task()
        update, policy = _review_path(task=task)
        review_ref = handoff.save_authority(update, policy)
        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(task, port=RecordingReservationPort())
        )
        first = handoff.compose(review_ref, completion_ref)
        replay = handoff.compose(review_ref, completion_ref)
        self.assertEqual(first, replay)
        self.assertEqual(len(store.approval_records), 1)

        class LostBeforeApprovalCommitStore(RecordingPolicyVerificationStore):
            def save_approval(self, record: Any) -> Any:
                del record
                raise StoreFailure("approval response lost " + CANARY)

        lost_store = LostBeforeApprovalCommitStore()
        lost_handoff = module.PolicyVerificationHandoff(lost_store)
        review_ref = lost_handoff.save_authority(update, policy)
        completion_ref = lost_handoff.issue_completion_admission(
            **_route_inputs(task, port=RecordingReservationPort())
        )
        with self.assertRaises(module.PolicyVerificationRecoveryRequired) as raised:
            lost_handoff.compose(review_ref, completion_ref)
        self.assertNotIn(CANARY, str(raised.exception))
        self.assertEqual(lost_store.approval_records, {})

    def test_malformed_store_readback_is_a_bounded_conflict(self) -> None:
        _, _, module = _assert_owner_types(self)

        class MalformedStore(RecordingPolicyVerificationStore):
            def read_review_authority(self, reference: str) -> object:
                record = super().read_review_authority(reference)
                return _forged_record(record, phase=[])

        store = MalformedStore()
        handoff = module.PolicyVerificationHandoff(store)
        update, policy = _review_path()
        with self.assertRaises(module.PolicyVerificationHandoffError) as raised:
            handoff.save_authority(update, policy)
        self.assertEqual(raised.exception.code, "review-authority-conflict")
        self.assertNotIsInstance(raised.exception.__cause__, TypeError)

    def test_owner_ref_mutation_does_not_overwrite_exact_stored_record(self) -> None:
        handoff, store = _new_handoff(self)
        update, policy = _review_path()
        review_ref = handoff.save_authority(update, policy)
        value = _path_task()
        port = RecordingReservationPort()
        completion_ref = handoff.issue_completion_admission(
            **_route_inputs(value, port=port)
        )
        before = store.writes
        changed_review = _forge_ref(
            review_ref,
            preserve_issuer=True,
            reference="foreign",
            digest="0" * 64,
        )
        self._assert_no_ref(lambda: handoff.compose(changed_review, completion_ref))
        self.assertEqual(store.writes, before)
        self._assert_no_ref(
            lambda: handoff.compose(
                review_ref,
                _forge_ref(completion_ref, preserve_issuer=True, digest="0" * 64),
            )
        )
        self.assertEqual(store.writes, before)


if __name__ == "__main__":
    unittest.main()
