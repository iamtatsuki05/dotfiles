from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from typing import cast

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
    PathResourcePolicyError,
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
    adapt_resource_claims,
    path_claims_overlap,
    resource_claims_conflict,
    route_task,
)
from agent_team.review_policy import (
    ReviewPair,
    SerialReviewPolicy,
)
from agent_team.task_policy import (
    ResourceClaim,
    TaskId,
    TaskKind,
    TaskLane,
    TaskSpec,
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

WORKSPACE = WorkspaceObservation(
    WorkspaceIdentity("/repo"), "/repo", device=1, inode=10, case_sensitive=True
)
AUTHORITY = ResourceReservationAuthority("owner-1", lease_epoch=3, fencing_token=7)


class EvilString(str):
    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __str__(self) -> str:
        return "evil"


class EvilWorkerNodeString(str):
    __hash__ = str.__hash__

    def __ne__(self, other: object) -> bool:
        return False

    def __str__(self) -> str:
        return "worker"


class EvilAuthority(ResourceReservationAuthority):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __str__(self) -> str:
        return "evil-authority"


class EvilTeam(TeamDefinition):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __str__(self) -> str:
        return "evil-team"


class EvilAgentNode(AgentNode):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


class EvilReviewPair(ReviewPair):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


class EvilSerialReviewPolicy(SerialReviewPolicy):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


def inode_for(relative_path: str) -> int:
    return 20 + sum(
        (index + 1) * ord(character) for index, character in enumerate(relative_path)
    )


def task(
    *,
    lane: TaskLane = TaskLane.NORMAL,
    kind: TaskKind = TaskKind.IMPLEMENTATION,
    allowed: tuple[str, ...] = ("src/file.txt",),
    denied: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
) -> TaskSpec:
    return TaskSpec(
        task_id=TaskId("task"),
        title="Implement task",
        context="A bounded context.",
        goal="Produce the requested result.",
        acceptance=("The result is checked.",),
        allowed_paths=allowed,
        do_not_modify=denied,
        dependencies=tuple(TaskId(value) for value in dependencies),
        verification=VerificationProfileRef("tests"),
        escalation_node=NodeId("main"),
        kind=kind,
        lane=lane,
        resource_claims=tuple(ResourceClaim(value) for value in resources),
    )


def observation(
    relative_path: str,
    *,
    entry_kind: PathEntryKind = PathEntryKind.REGULAR,
    canonical_path: str | None = None,
    device: int | None = 1,
    inode: int | None = None,
    nlink: int | None = 1,
    parent_device: int | None = 1,
    parent_inode: int | None = None,
    ancestor_symlink: bool = False,
) -> PathObservation:
    canonical = canonical_path
    if canonical is None:
        canonical = "/repo" if relative_path in {"", "."} else f"/repo/{relative_path}"
    if (
        inode is None
        and entry_kind is not PathEntryKind.MISSING
        and relative_path not in {"", "."}
    ):
        inode = inode_for(relative_path)
    if parent_inode is None and relative_path not in {"", "."}:
        parent = relative_path.rsplit("/", 1)[0] if "/" in relative_path else "."
        parent_inode = 10 if parent == "." else inode_for(parent)
    return PathObservation(
        relative_path=relative_path,
        canonical_path=canonical,
        entry_kind=entry_kind,
        device=device,
        inode=inode,
        nlink=nlink,
        parent_device=parent_device,
        parent_inode=parent_inode,
        ancestor_symlink=ancestor_symlink,
    )


def root_observation() -> PathObservation:
    return observation(
        ".",
        entry_kind=PathEntryKind.DIRECTORY,
        device=1,
        inode=10,
        nlink=2,
        parent_device=None,
        parent_inode=None,
    )


def reservation_request(
    value: TaskSpec,
    claims: tuple[ResourceClaimPolicy, ...],
    authority: ResourceReservationAuthority | None,
) -> ResourceReservationRequest:
    return ResourceReservationRequest(value.task_id, claims, "reservation-1", authority)


def policy_for(
    value: TaskSpec,
    *,
    allowed: tuple[PathClaim, ...] | None = None,
    denied: tuple[PathClaim, ...] | None = None,
    reserved: tuple[str, ...] = (),
) -> PathClaimPolicy:
    allowed_claims = allowed or tuple(
        PathClaim(path, PathKind.EXACT, PathAccess.WRITE)
        for path in value.allowed_paths
    )
    denied_claims = denied or tuple(
        PathClaim(path, PathKind.EXACT, PathAccess.WRITE)
        for path in value.do_not_modify
    )
    return PathClaimPolicy.from_task_spec(
        value,
        workspace=WORKSPACE,
        allowed=allowed_claims,
        denied=denied_claims,
        reserved_roots=reserved,
    )


def team_definition() -> TeamDefinition:
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


def research_team_definition() -> TeamDefinition:
    value = team_definition()
    worker = value.nodes[1]
    readonly_worker = replace(
        worker,
        profile=replace(worker.profile, permission="read-only"),
    )
    return replace(value, nodes=(value.nodes[0], readonly_worker, value.nodes[2]))


def wrong_reviewer_permission_team_definition() -> TeamDefinition:
    value = team_definition()
    reviewer = value.nodes[2]
    write_reviewer = replace(
        reviewer,
        profile=replace(reviewer.profile, permission="workspace-write"),
    )
    return replace(value, nodes=(value.nodes[0], value.nodes[1], write_reviewer))


def missing_worker_team_definition() -> TeamDefinition:
    value = team_definition()
    return replace(value, nodes=(value.nodes[0], value.nodes[2]))


def serial_policy(value: TaskSpec) -> SerialReviewPolicy:
    return SerialReviewPolicy(
        task=value,
        team_definition=team_definition(),
        worker_node=NodeId("worker"),
        max_review_rounds=2,
    )


def profile(
    value: TaskSpec,
    *,
    team: TeamDefinition | None = None,
    worker_node: NodeId | None = None,
    reviewer_pair: ReviewPair | None = None,
    review: SerialReviewPolicy | None = None,
) -> LaneProfileBinding:
    selected_team = team or team_definition()
    selected_pair = reviewer_pair
    if selected_pair is None and review is not None:
        selected_pair = review.pair
    return LaneProfileBinding(
        team_definition=selected_team,
        worker_node=NodeId("worker") if worker_node is None else worker_node,
        reviewer_pair=selected_pair,
        serial_review_policy=review,
    )


class FakeReservationPort(ResourceReservationPort):
    def __init__(self, result: ResourceReservationResult) -> None:
        self.result = result
        self.requests: list[ResourceReservationRequest] = []

    def reserve(self, request: ResourceReservationRequest) -> ResourceReservationResult:
        self.requests.append(request)
        return self.result


class PathClaimPolicyTest(unittest.TestCase):
    def test_adapts_each_task_path_once_and_keeps_input_order_out_of_result(
        self,
    ) -> None:
        value = task(allowed=("src/b.txt", "src/a.txt"), denied=(".git",))
        policy = policy_for(
            value,
            allowed=(
                PathClaim("src/a.txt", PathKind.EXACT, PathAccess.WRITE),
                PathClaim("src/b.txt", PathKind.EXACT, PathAccess.WRITE),
            ),
            denied=(PathClaim(".git", PathKind.DIRECTORY, PathAccess.READ),),
        )

        self.assertEqual(
            tuple(claim.relative_path for claim in policy.allowed),
            ("src/a.txt", "src/b.txt"),
        )
        self.assertEqual(policy.denied[0].relative_path, ".git")

        with self.assertRaisesRegex(PathResourcePolicyError, "missing-claim"):
            policy_for(
                value,
                allowed=(PathClaim("src/a.txt", PathKind.EXACT, PathAccess.WRITE),),
            )

    def test_rejects_path_guessing_controls_and_overlapping_claims(self) -> None:
        invalid = ("/absolute", "../escape", "src/../file", "src/file/", "src/*.py")
        for value in invalid:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(PathResourcePolicyError, "path"),
            ):
                PathClaim(value, PathKind.EXACT, PathAccess.WRITE)

        with self.assertRaisesRegex(PathResourcePolicyError, "path-overlap"):
            PathClaimPolicy(
                workspace=WORKSPACE,
                allowed=(
                    PathClaim("src", PathKind.DIRECTORY, PathAccess.WRITE),
                    PathClaim("src/file.txt", PathKind.EXACT, PathAccess.WRITE),
                ),
                denied=(),
                reserved_roots=(),
            )

    def test_forged_path_claim_kind_is_rejected_at_policy_boundary(self) -> None:
        forged = object.__new__(PathClaim)
        object.__setattr__(forged, "relative_path", "src/file.txt")
        object.__setattr__(forged, "kind", "bogus")
        object.__setattr__(forged, "access", PathAccess.WRITE)
        with self.assertRaisesRegex(PathResourcePolicyError, "unknown-path-kind"):
            PathClaimPolicy(
                workspace=WORKSPACE,
                allowed=(forged,),
                denied=(),
                reserved_roots=(),
            )

        policy = object.__new__(PathClaimPolicy)
        object.__setattr__(policy, "workspace", WORKSPACE)
        object.__setattr__(policy, "allowed", (forged,))
        object.__setattr__(policy, "denied", ())
        object.__setattr__(policy, "reserved_roots", ())
        with self.assertRaisesRegex(PathResourcePolicyError, "unknown-path-kind"):
            policy.admit(
                PathMutation(PathOperation.MODIFY, "src/file.txt", None),
                (
                    root_observation(),
                    observation("src", entry_kind=PathEntryKind.DIRECTORY),
                    observation("src/file.txt"),
                ),
            )

    def test_exact_and_directory_use_component_boundaries(self) -> None:
        value = task(allowed=("src",))
        directory_policy = policy_for(
            value,
            allowed=(PathClaim("src", PathKind.DIRECTORY, PathAccess.WRITE),),
        )
        admitted = directory_policy.admit(
            PathMutation(PathOperation.MODIFY, "src/file.txt", None),
            (
                root_observation(),
                observation("src", entry_kind=PathEntryKind.DIRECTORY),
                observation("src/file.txt"),
            ),
        )
        outside = directory_policy.admit(
            PathMutation(PathOperation.MODIFY, "src-file.txt", None),
            (observation("src-file.txt"), root_observation()),
        )
        self.assertTrue(admitted.candidate)
        self.assertFalse(outside.candidate)
        self.assertEqual(outside.reason_code, "path-outside-allowed")

        exact_value = task(allowed=("src/file.txt",))
        exact_policy = policy_for(exact_value)
        self.assertFalse(
            exact_policy.admit(
                PathMutation(PathOperation.MODIFY, "src/file2.txt", None),
                (
                    root_observation(),
                    observation("src", entry_kind=PathEntryKind.DIRECTORY),
                    observation("src/file2.txt"),
                ),
            ).candidate
        )

    def test_deny_and_reserved_claims_are_evaluated_before_allow(self) -> None:
        value = task(allowed=("src",), denied=("src/secret.txt",))
        policy = policy_for(
            value,
            allowed=(PathClaim("src", PathKind.DIRECTORY, PathAccess.WRITE),),
            denied=(PathClaim("src/secret.txt", PathKind.EXACT, PathAccess.WRITE),),
        )
        denied = policy.admit(
            PathMutation(PathOperation.MODIFY, "src/secret.txt", None),
            (
                root_observation(),
                observation("src", entry_kind=PathEntryKind.DIRECTORY),
                observation("src/secret.txt"),
            ),
        )
        self.assertFalse(denied.candidate)
        self.assertEqual(denied.reason_code, "denied-path")

        reserved_value = task(allowed=(".git",))
        reserved_policy = policy_for(
            reserved_value,
            allowed=(PathClaim(".git", PathKind.DIRECTORY, PathAccess.WRITE),),
            reserved=(".git",),
        )
        reserved = reserved_policy.admit(
            PathMutation(PathOperation.MODIFY, ".git/config", None),
            (
                root_observation(),
                observation(".git", entry_kind=PathEntryKind.DIRECTORY),
                observation(".git/config"),
            ),
        )
        self.assertFalse(reserved.candidate)
        self.assertEqual(reserved.reason_code, "reserved-path")

    def test_delete_and_rename_require_all_touched_paths_and_parents(self) -> None:
        value = task(allowed=("src",))
        policy = policy_for(
            value,
            allowed=(PathClaim("src", PathKind.DIRECTORY, PathAccess.WRITE),),
        )
        deleted = policy.admit(
            PathMutation(PathOperation.DELETE, "src/file.txt", None),
            (
                root_observation(),
                observation("src", entry_kind=PathEntryKind.DIRECTORY),
                observation("src/file.txt"),
            ),
        )
        self.assertTrue(deleted.candidate)

        missing_parent = policy.admit(
            PathMutation(PathOperation.DELETE, "src/file.txt", None),
            (observation("src/file.txt"), root_observation()),
        )
        self.assertFalse(missing_parent.candidate)
        self.assertEqual(missing_parent.reason_code, "unknown-path-observation")

        renamed = policy.admit(
            PathMutation(PathOperation.RENAME, "src/old.txt", "src/new.txt"),
            (
                root_observation(),
                observation("src", entry_kind=PathEntryKind.DIRECTORY),
                observation("src/old.txt"),
                observation(
                    "src/new.txt",
                    entry_kind=PathEntryKind.MISSING,
                    device=None,
                    inode=None,
                    nlink=None,
                ),
            ),
        )
        self.assertTrue(renamed.candidate)

    def test_admission_rejects_uncertain_identity_without_filesystem_access(
        self,
    ) -> None:
        value = task(allowed=("src/file.txt",))
        policy = policy_for(value)
        cases = (
            (
                observation("src/file.txt", entry_kind=PathEntryKind.SYMLINK),
                "symlink-path",
            ),
            (observation("src/file.txt", ancestor_symlink=True), "symlink-path"),
            (observation("src/file.txt", nlink=2), "hardlink-path"),
            (observation("src/file.txt", device=2), "device-mismatch"),
            (
                observation("src/file.txt", canonical_path="/outside/file.txt"),
                "outside-workspace",
            ),
            (
                observation("src/file.txt", entry_kind=PathEntryKind.OTHER),
                "special-path",
            ),
            (
                observation("src/file.txt", parent_device=None),
                "unknown-path-observation",
            ),
        )
        for item, reason in cases:
            with self.subTest(reason=reason):
                decision = policy.admit(
                    PathMutation(PathOperation.MODIFY, "src/file.txt", None),
                    (
                        root_observation(),
                        observation("src", entry_kind=PathEntryKind.DIRECTORY),
                        item,
                    ),
                )
                self.assertFalse(decision.candidate)
                self.assertEqual(decision.reason_code, reason)

    def test_create_checks_missing_target_and_rename_checks_destination_parent(
        self,
    ) -> None:
        value = task(allowed=("src",))
        policy = policy_for(
            value,
            allowed=(PathClaim("src", PathKind.DIRECTORY, PathAccess.WRITE),),
        )
        created = policy.admit(
            PathMutation(PathOperation.CREATE, "src/new.txt", None),
            (
                root_observation(),
                observation(
                    "src/new.txt",
                    entry_kind=PathEntryKind.MISSING,
                    device=None,
                    inode=None,
                    nlink=None,
                ),
                observation("src", entry_kind=PathEntryKind.DIRECTORY),
            ),
        )
        self.assertTrue(created.candidate)

        renamed_outside = policy.admit(
            PathMutation(PathOperation.RENAME, "src/old.txt", "other/new.txt"),
            (
                observation("src/old.txt"),
                observation(
                    "other/new.txt",
                    entry_kind=PathEntryKind.MISSING,
                    device=None,
                    inode=None,
                    nlink=None,
                ),
            ),
        )
        self.assertFalse(renamed_outside.candidate)
        self.assertEqual(renamed_outside.reason_code, "path-outside-allowed")

        deleted_with_missing_parent = policy.admit(
            PathMutation(PathOperation.DELETE, "src/file.txt", None),
            (
                observation("src/file.txt"),
                observation(
                    "src",
                    entry_kind=PathEntryKind.MISSING,
                    device=None,
                    inode=None,
                    nlink=None,
                ),
            ),
        )
        self.assertFalse(deleted_with_missing_parent.candidate)
        self.assertEqual(
            deleted_with_missing_parent.reason_code, "unknown-path-observation"
        )

    def test_top_level_delete_and_rename_require_explicit_root_claim_and_observation(
        self,
    ) -> None:
        value = task(allowed=("file.txt", "new.txt", "."))
        policy = policy_for(
            value,
            allowed=(
                PathClaim("file.txt", PathKind.EXACT, PathAccess.WRITE),
                PathClaim("new.txt", PathKind.EXACT, PathAccess.WRITE),
                PathClaim(".", PathKind.EXACT, PathAccess.WRITE),
            ),
        )
        root = root_observation()
        deleted = policy.admit(
            PathMutation(PathOperation.DELETE, "file.txt", None),
            (observation("file.txt"), root),
        )
        self.assertTrue(deleted.candidate)
        renamed = policy.admit(
            PathMutation(PathOperation.RENAME, "file.txt", "new.txt"),
            (
                observation("file.txt"),
                observation(
                    "new.txt",
                    entry_kind=PathEntryKind.MISSING,
                    device=None,
                    inode=None,
                    nlink=None,
                ),
                root,
            ),
        )
        self.assertTrue(renamed.candidate)

        missing_root = policy.admit(
            PathMutation(PathOperation.DELETE, "file.txt", None),
            (observation("file.txt"),),
        )
        self.assertFalse(missing_root.candidate)
        self.assertEqual(missing_root.reason_code, "unknown-path-observation")

        with self.assertRaisesRegex(PathResourcePolicyError, "empty-value"):
            PathClaim("", PathKind.DIRECTORY, PathAccess.WRITE)
        with self.assertRaisesRegex(PathResourcePolicyError, "empty-value"):
            PathClaimPolicy.from_task_spec(
                task(allowed=("file.txt",)),
                workspace=WORKSPACE,
                allowed=(PathClaim("file.txt", PathKind.EXACT, PathAccess.WRITE),),
                denied=(),
                reserved_roots=("",),
            )

    def test_root_observation_requires_positive_nlink(self) -> None:
        value = task(allowed=("src/file.txt",))
        policy = policy_for(value)
        mutation = PathMutation(PathOperation.MODIFY, "src/file.txt", None)
        for nlink in (None, 0):
            with self.subTest(nlink=nlink):
                root = replace(root_observation(), nlink=nlink)
                decision = policy.admit(
                    mutation,
                    (
                        root,
                        observation("src", entry_kind=PathEntryKind.DIRECTORY),
                        observation("src/file.txt"),
                    ),
                )
                self.assertFalse(decision.candidate)
                self.assertEqual(decision.reason_code, "unknown-path-observation")

    def test_nested_operations_require_complete_ancestor_chain(self) -> None:
        value = task(allowed=("a",))
        policy = policy_for(
            value,
            allowed=(PathClaim("a", PathKind.DIRECTORY, PathAccess.WRITE),),
        )
        mutation = PathMutation(PathOperation.MODIFY, "a/b/c.txt", None)
        complete = (
            root_observation(),
            observation("a", entry_kind=PathEntryKind.DIRECTORY, inode=30),
            observation(
                "a/b",
                entry_kind=PathEntryKind.DIRECTORY,
                inode=40,
                parent_inode=30,
            ),
            observation("a/b/c.txt", inode=50, parent_inode=40),
        )
        self.assertTrue(policy.admit(mutation, complete).candidate)
        for omitted in ("a", "a/b", "."):
            incomplete = tuple(
                item for item in complete if item.relative_path != omitted
            )
            decision = policy.admit(mutation, incomplete)
            with self.subTest(omitted=omitted):
                self.assertFalse(decision.candidate)
                self.assertEqual(decision.reason_code, "unknown-path-observation")

        missing_ancestor = policy.admit(
            PathMutation(PathOperation.CREATE, "a/b/new.txt", None),
            (
                root_observation(),
                observation("a", entry_kind=PathEntryKind.DIRECTORY, inode=30),
                observation(
                    "a/b",
                    entry_kind=PathEntryKind.MISSING,
                    device=None,
                    inode=None,
                    nlink=None,
                ),
                observation(
                    "a/b/new.txt",
                    entry_kind=PathEntryKind.MISSING,
                    device=None,
                    inode=None,
                    nlink=None,
                    parent_inode=40,
                ),
            ),
        )
        self.assertFalse(missing_ancestor.candidate)
        self.assertEqual(missing_ancestor.reason_code, "unknown-path-observation")

    def test_different_observed_paths_with_same_physical_identity_are_rejected(
        self,
    ) -> None:
        value = task(allowed=("src",))
        policy = policy_for(
            value,
            allowed=(PathClaim("src", PathKind.DIRECTORY, PathAccess.WRITE),),
        )
        decision = policy.admit(
            PathMutation(PathOperation.RENAME, "src/old.txt", "src/new.txt"),
            (
                root_observation(),
                observation("src", entry_kind=PathEntryKind.DIRECTORY, inode=20),
                observation("src/old.txt", inode=99),
                observation(
                    "src/new.txt",
                    inode=99,
                    entry_kind=PathEntryKind.REGULAR,
                ),
            ),
        )
        self.assertFalse(decision.candidate)
        self.assertEqual(decision.reason_code, "hardlink-path")

    def test_case_insensitive_collision_is_not_casefolded_on_case_sensitive_workspace(
        self,
    ) -> None:
        insensitive = WorkspaceObservation(
            WorkspaceIdentity("/repo"), "/repo", 1, 10, case_sensitive=False
        )
        value = task(allowed=("src/Foo.txt", "src/foo.txt"))
        with self.assertRaisesRegex(PathResourcePolicyError, "case-collision"):
            PathClaimPolicy.from_task_spec(
                value,
                workspace=insensitive,
                allowed=(
                    PathClaim("src/Foo.txt", PathKind.EXACT, PathAccess.WRITE),
                    PathClaim("src/foo.txt", PathKind.EXACT, PathAccess.WRITE),
                ),
                denied=(),
                reserved_roots=(),
            )

        value = task(allowed=("src/Foo.txt", "src/foo.txt"))
        sensitive = policy_for(value)
        self.assertFalse(
            path_claims_overlap(sensitive, policy_for(task(allowed=("src/bar.txt",))))
        )

    def test_cross_policy_overlap_is_deterministic(self) -> None:
        left = policy_for(
            task(allowed=("src",)),
            allowed=(PathClaim("src", PathKind.DIRECTORY, PathAccess.WRITE),),
        )
        right = policy_for(task(allowed=("src/file.txt",)))
        self.assertTrue(path_claims_overlap(left, right))

    def test_case_insensitive_observation_collision_is_rejected(self) -> None:
        workspace = WorkspaceObservation(
            WorkspaceIdentity("/repo"), "/repo", 1, 10, case_sensitive=False
        )
        value = task(allowed=("src/file.txt",))
        policy = PathClaimPolicy.from_task_spec(
            value,
            workspace=workspace,
            allowed=(PathClaim("src/file.txt", PathKind.EXACT, PathAccess.WRITE),),
            denied=(),
            reserved_roots=(),
        )
        decision = policy.admit(
            PathMutation(PathOperation.MODIFY, "src/file.txt", None),
            (
                observation("src/File.txt", canonical_path="/repo/src/file.txt"),
                observation("src/file.txt"),
            ),
        )
        self.assertFalse(decision.candidate)
        self.assertEqual(decision.reason_code, "case-collision")

    def test_physical_workspace_overlap_is_conservative_when_case_semantics_differ(
        self,
    ) -> None:
        sensitive = policy_for(task(allowed=("src/a.txt",)))
        insensitive_workspace = WorkspaceObservation(
            WorkspaceIdentity("/repo"), "/elsewhere", 1, 10, case_sensitive=False
        )
        insensitive = PathClaimPolicy.from_task_spec(
            task(allowed=("src/b.txt",)),
            workspace=insensitive_workspace,
            allowed=(PathClaim("src/b.txt", PathKind.EXACT, PathAccess.WRITE),),
            denied=(),
            reserved_roots=(),
        )
        self.assertTrue(path_claims_overlap(sensitive, insensitive))

    def test_observation_order_does_not_change_rejection_reason(self) -> None:
        value = task(allowed=("src/a.txt", "src/b.txt"))
        policy = policy_for(value)
        mutation = PathMutation(PathOperation.MODIFY, "src/a.txt", None)
        first = policy.admit(
            mutation,
            (
                observation("src/b.txt", entry_kind=PathEntryKind.OTHER),
                observation("src/a.txt", entry_kind=PathEntryKind.SYMLINK),
            ),
        )
        second = policy.admit(
            mutation,
            (
                observation("src/a.txt", entry_kind=PathEntryKind.SYMLINK),
                observation("src/b.txt", entry_kind=PathEntryKind.OTHER),
            ),
        )
        self.assertEqual(first, second)

    def test_malformed_observation_permutation_has_one_stable_reason(self) -> None:
        value = task(allowed=("src/file.txt",))
        policy = policy_for(value)

        def forged(relative_path: str) -> PathObservation:
            result = object.__new__(PathObservation)
            object.__setattr__(result, "relative_path", relative_path)
            object.__setattr__(result, "canonical_path", "/repo/src/file.txt")
            object.__setattr__(result, "entry_kind", PathEntryKind.REGULAR)
            object.__setattr__(result, "device", 1)
            object.__setattr__(result, "inode", 20)
            object.__setattr__(result, "nlink", 1)
            object.__setattr__(result, "parent_device", 1)
            object.__setattr__(result, "parent_inode", 10)
            object.__setattr__(result, "ancestor_symlink", False)
            return result

        absolute = forged("/bad")
        traversal = forged("a/../b")
        mutation = PathMutation(PathOperation.MODIFY, "src/file.txt", None)
        first = policy.admit(mutation, (absolute, traversal))
        second = policy.admit(mutation, (traversal, absolute))
        self.assertEqual(first, second)

    def test_malformed_claim_permutation_has_one_stable_reason(self) -> None:
        workspace = WORKSPACE

        def forged(relative_path: str) -> PathClaim:
            result = object.__new__(PathClaim)
            object.__setattr__(result, "relative_path", relative_path)
            object.__setattr__(result, "kind", PathKind.EXACT)
            object.__setattr__(result, "access", PathAccess.WRITE)
            return result

        claims = (forged("/bad"), forged("a/../b"))
        reasons: list[str] = []
        for candidate in (claims, tuple(reversed(claims))):
            with self.assertRaises(PathResourcePolicyError) as raised:
                PathClaimPolicy(
                    workspace=workspace,
                    allowed=candidate,
                    denied=(),
                    reserved_roots=(),
                )
            reasons.append(raised.exception.code)
        self.assertEqual(reasons, [reasons[0], reasons[0]])

    def test_malformed_reserved_root_permutation_has_one_stable_reason(self) -> None:
        reasons: list[str] = []
        for roots in (("/bad", "a/../b"), ("a/../b", "/bad")):
            with self.assertRaises(PathResourcePolicyError) as raised:
                PathClaimPolicy(
                    workspace=WORKSPACE,
                    allowed=(PathClaim("safe", PathKind.EXACT, PathAccess.WRITE),),
                    denied=(),
                    reserved_roots=roots,
                )
            reasons.append(raised.exception.code)
        self.assertEqual(reasons, [reasons[0], reasons[0]])

        reasons.clear()
        for roots in (("/bad", "a/../b"), ("a/../b", "/bad")):
            forged = object.__new__(PathClaimPolicy)
            object.__setattr__(forged, "workspace", WORKSPACE)
            object.__setattr__(
                forged,
                "allowed",
                (PathClaim("safe", PathKind.EXACT, PathAccess.WRITE),),
            )
            object.__setattr__(forged, "denied", ())
            object.__setattr__(forged, "reserved_roots", roots)
            with self.assertRaises(PathResourcePolicyError) as raised:
                forged.admit(
                    PathMutation(PathOperation.MODIFY, "safe", None),
                    (root_observation(), observation("safe")),
                )
            reasons.append(raised.exception.code)
        self.assertEqual(reasons, [reasons[0], reasons[0]])

    def test_from_task_spec_forged_claim_permutation_has_one_stable_reason(
        self,
    ) -> None:
        def forged(relative_path: str) -> PathClaim:
            result = object.__new__(PathClaim)
            object.__setattr__(result, "relative_path", relative_path)
            object.__setattr__(result, "kind", PathKind.EXACT)
            object.__setattr__(result, "access", PathAccess.WRITE)
            return result

        value = task(allowed=("safe",))
        claims = (forged("/bad"), forged("a/../b"))
        reasons: list[str] = []
        for candidate in (claims, tuple(reversed(claims))):
            with self.assertRaises(PathResourcePolicyError) as raised:
                PathClaimPolicy.from_task_spec(
                    value,
                    workspace=WORKSPACE,
                    allowed=candidate,
                    denied=(),
                    reserved_roots=(),
                )
            reasons.append(raised.exception.code)
        self.assertEqual(reasons, [reasons[0], reasons[0]])

    def test_malformed_path_reason_is_stable_across_hash_seeds(self) -> None:
        script = """
from agent_team.path_resource_policy import PathAccess, PathClaim, PathClaimPolicy, PathKind, PathResourcePolicyError, WorkspaceObservation
from agent_team.task_policy import WorkspaceIdentity
workspace = WorkspaceObservation(WorkspaceIdentity('/repo'), '/repo', 1, 10, True)
def forged(path):
    value = object.__new__(PathClaim)
    object.__setattr__(value, 'relative_path', path)
    object.__setattr__(value, 'kind', PathKind.EXACT)
    object.__setattr__(value, 'access', PathAccess.WRITE)
    return value
try:
    PathClaimPolicy(workspace, (forged('/bad'), forged('a/../b')), (), ())
except PathResourcePolicyError as error:
    print(error.code)
"""
        outputs: list[str] = []
        for seed in ("11", "12", "13", "14"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = seed
            env["PYTHONPATH"] = "scripts/agent-team"
            result = subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            outputs.append(result.stdout.strip())
        self.assertEqual(outputs, [outputs[0]] * len(outputs))

    def test_task_path_permutation_has_one_stable_reason(self) -> None:
        values = (
            ("/bad", "a/../b"),
            ("a/../b", "/bad"),
        )
        reasons: list[str] = []
        for paths in values:
            value = task(allowed=paths)
            with self.assertRaises(PathResourcePolicyError) as raised:
                PathClaimPolicy.from_task_spec(
                    value,
                    workspace=WORKSPACE,
                    allowed=(PathClaim("safe", PathKind.EXACT, PathAccess.WRITE),),
                    denied=(),
                    reserved_roots=(),
                )
            reasons.append(raised.exception.code)
        self.assertEqual(reasons, [reasons[0], reasons[0]])


class ResourceClaimPolicyTest(unittest.TestCase):
    def test_reservation_authority_requires_explicit_owner_epoch_and_token(
        self,
    ) -> None:
        cases = (
            ("", 3, 7),
            ("owner-1", -1, 7),
            ("owner-1", 3, 0),
        )
        for owner_id, lease_epoch, fencing_token in cases:
            with (
                self.subTest(
                    owner_id=owner_id,
                    lease_epoch=lease_epoch,
                    fencing_token=fencing_token,
                ),
                self.assertRaises(PathResourcePolicyError),
            ):
                ResourceReservationAuthority(owner_id, lease_epoch, fencing_token)

    def test_malformed_binding_permutation_has_one_stable_reason(self) -> None:
        value = task(resources=("cache", "gpu"))
        malformed_mode = object.__new__(ResourceClaimPolicy)
        object.__setattr__(malformed_mode, "claim", ResourceClaim("cache"))
        object.__setattr__(malformed_mode, "key", ResourceKey("cache"))
        object.__setattr__(malformed_mode, "mode", "bogus")
        unknown_key = ResourceClaimPolicy(
            ResourceClaim("gpu"), ResourceKey("unknown"), ResourceMode.SHARED
        )
        bindings = (malformed_mode, unknown_key)
        reasons: list[str] = []
        for candidate in (bindings, tuple(reversed(bindings))):
            with self.assertRaises(PathResourcePolicyError) as raised:
                adapt_resource_claims(
                    value,
                    candidate,
                    known_keys=frozenset({ResourceKey("cache"), ResourceKey("gpu")}),
                )
            reasons.append(raised.exception.code)
        self.assertEqual(reasons, [reasons[0], reasons[0]])

    def test_malformed_known_key_reason_is_stable_across_hash_seeds(self) -> None:
        script = """
from agent_team.path_resource_policy import PathResourcePolicyError, ResourceClaimPolicy, ResourceKey, ResourceMode, adapt_resource_claims
from agent_team.task_policy import ResourceClaim, TaskId, TaskKind, TaskLane, TaskSpec, VerificationProfileRef
from agent_team.topology import NodeId
task = TaskSpec(TaskId('task'), 'title', 'context', 'goal', ('ok',), ('src/file.txt',), (), (), VerificationProfileRef('tests'), NodeId('main'), TaskKind.IMPLEMENTATION, TaskLane.NORMAL, (ResourceClaim('cache'),))
binding = ResourceClaimPolicy(ResourceClaim('cache'), ResourceKey('cache'), ResourceMode.SHARED)
try:
    adapt_resource_claims(task, (binding,), known_keys=frozenset({ResourceKey(''), ResourceKey('\\x00')}))
except PathResourcePolicyError as error:
    print(error.code)
"""
        outputs: list[str] = []
        for seed in ("1", "2", "3", "4"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = seed
            env["PYTHONPATH"] = "scripts/agent-team"
            result = subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            outputs.append(result.stdout.strip())
        self.assertEqual(outputs, [outputs[0]] * len(outputs))

    def test_adapts_explicit_key_and_mode_without_inference(self) -> None:
        value = task(resources=("gpu", "cache"))
        bindings = (
            ResourceClaimPolicy(
                ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
            ),
            ResourceClaimPolicy(
                ResourceClaim("gpu"), ResourceKey("gpu"), ResourceMode.EXCLUSIVE
            ),
        )
        adapted = adapt_resource_claims(
            value,
            bindings,
            known_keys=frozenset({ResourceKey("cache"), ResourceKey("gpu")}),
        )
        self.assertEqual(tuple(item.key for item in adapted), ("cache", "gpu"))
        self.assertEqual(adapted[1].mode, ResourceMode.EXCLUSIVE)

        with self.assertRaisesRegex(PathResourcePolicyError, "missing-resource-claim"):
            adapt_resource_claims(
                value,
                bindings[:1],
                known_keys=frozenset({ResourceKey("cache"), ResourceKey("gpu")}),
            )

        with self.assertRaisesRegex(PathResourcePolicyError, "unknown-resource-key"):
            adapt_resource_claims(
                task(resources=("gpu",)),
                (
                    ResourceClaimPolicy(
                        ResourceClaim("gpu"), ResourceKey("other"), ResourceMode.SHARED
                    ),
                ),
                known_keys=frozenset({ResourceKey("gpu")}),
            )

    def test_shared_only_claims_can_coexist_but_exclusive_claims_conflict(self) -> None:
        shared_a = (
            ResourceClaimPolicy(
                ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
            ),
        )
        shared_b = (
            ResourceClaimPolicy(
                ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
            ),
        )
        exclusive = (
            ResourceClaimPolicy(
                ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.EXCLUSIVE
            ),
        )
        other = (
            ResourceClaimPolicy(
                ResourceClaim("gpu"), ResourceKey("gpu"), ResourceMode.EXCLUSIVE
            ),
        )
        self.assertFalse(resource_claims_conflict(shared_a, shared_b))
        self.assertTrue(resource_claims_conflict(shared_a, exclusive))
        self.assertTrue(resource_claims_conflict(exclusive, exclusive))
        self.assertFalse(resource_claims_conflict(exclusive, other))

    def test_reservation_values_are_canonical_and_duplicate_keys_are_rejected(
        self,
    ) -> None:
        result = ResourceReservationResult(
            ReservationStatus.RESERVED,
            "reservation-1",
            (ResourceKey("gpu"), ResourceKey("cache")),
            AUTHORITY,
            TaskId("task"),
            ReservationDigest("a" * 64),
        )
        self.assertEqual(result.claim_keys, (ResourceKey("cache"), ResourceKey("gpu")))
        with self.assertRaisesRegex(PathResourcePolicyError, "duplicate-resource-key"):
            ResourceReservationResult(
                ReservationStatus.RESERVED,
                "reservation-1",
                (ResourceKey("cache"), ResourceKey("cache")),
                AUTHORITY,
                TaskId("task"),
                ReservationDigest("a" * 64),
            )

    def test_evil_authority_subclass_and_strings_are_rejected(self) -> None:
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-type"):
            ResourceReservationAuthority(EvilString("owner"), 1, 1)
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-type"):
            ResourceClaimPolicy(
                ResourceClaim("cache"),
                cast(ResourceKey, EvilString("cache")),
                ResourceMode.SHARED,
            )
        with self.assertRaisesRegex(PathResourcePolicyError, "reservation-identity"):
            ResourceReservationResult(
                ReservationStatus.RESERVED,
                "reservation-1",
                (ResourceKey("cache"),),
                AUTHORITY,
                TaskId("task"),
                cast(ReservationDigest, EvilString("a" * 64)),
            )

        authority = object.__new__(EvilAuthority)
        object.__setattr__(authority, "owner_id", "owner-1")
        object.__setattr__(authority, "lease_epoch", 3)
        object.__setattr__(authority, "fencing_token", 7)
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-authority"):
            self._route_with_result_for_forged_result(
                task(resources=("cache",)),
                ResourceClaimPolicy(
                    ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
                ),
                ResourceReservationResult(
                    ReservationStatus.RESERVED,
                    "reservation-1",
                    (ResourceKey("cache"),),
                    authority,
                    TaskId("task"),
                    ReservationDigest("a" * 64),
                ),
            )

    def test_forged_result_status_is_typed_non_candidate(self) -> None:
        value = task(resources=("cache",))
        claim = ResourceClaimPolicy(
            ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
        )
        request = reservation_request(value, (claim,), AUTHORITY)
        forged = object.__new__(ResourceReservationResult)
        object.__setattr__(forged, "status", "bogus")
        object.__setattr__(forged, "reservation_id", "reservation-1")
        object.__setattr__(forged, "claim_keys", (ResourceKey("cache"),))
        object.__setattr__(forged, "authority", AUTHORITY)
        object.__setattr__(forged, "task_id", value.task_id)
        object.__setattr__(forged, "request_digest", request.request_digest)
        decision, port = self._route_with_result_for_forged_result(value, claim, forged)
        self.assertFalse(decision.candidate)
        self.assertEqual(decision.reason_code, "unknown-reservation-status")
        self.assertEqual(len(port.requests), 1)

    def _route_with_result_for_forged_result(
        self,
        value: TaskSpec,
        claim: ResourceClaimPolicy,
        result: ResourceReservationResult,
    ) -> tuple[LaneRoutingDecision, FakeReservationPort]:
        port = FakeReservationPort(result)
        decision = route_task(
            value,
            path_policy=policy_for(value),
            path_mutation=PathMutation(PathOperation.MODIFY, "src/file.txt", None),
            path_observations=(
                root_observation(),
                observation("src", entry_kind=PathEntryKind.DIRECTORY),
                observation("src/file.txt"),
            ),
            resource_claims=(claim,),
            known_keys=frozenset({ResourceKey("cache")}),
            profile=profile(value, review=serial_policy(value)),
            reservation_port=port,
            reservation_id="reservation-1",
            reservation_authority=AUTHORITY,
        )
        return decision, port


class LaneRoutingTest(unittest.TestCase):
    def route(
        self,
        value: TaskSpec,
        *,
        profile_value: LaneProfileBinding | None = None,
        claims: tuple[ResourceClaimPolicy, ...] = (),
        known_keys: frozenset[ResourceKey] | None = None,
        result: ResourceReservationResult | None = None,
        mutation: PathMutation | None = None,
        observations: tuple[PathObservation, ...] | None = None,
        authority: ResourceReservationAuthority | None = AUTHORITY,
        include_authority_for_claim_free: bool = False,
    ) -> tuple[LaneRoutingDecision, FakeReservationPort]:
        review = (
            serial_policy(value)
            if value.lane in {TaskLane.NORMAL, TaskLane.EXPRESS}
            else None
        )
        selected_profile = profile_value or profile(value, review=review)
        selected_known_keys = (
            frozenset(item.key for item in claims) if known_keys is None else known_keys
        )
        selected_mutation = mutation or PathMutation(
            PathOperation.MODIFY, "src/file.txt", None
        )
        selected_observations = observations or (
            root_observation(),
            observation("src", entry_kind=PathEntryKind.DIRECTORY),
            observation("src/file.txt"),
        )
        selected_result = result or ResourceReservationResult(
            ReservationStatus.RESERVED,
            "reservation-1",
            tuple(item.key for item in claims),
            authority if claims else None,
            value.task_id if claims else None,
            (
                reservation_request(value, claims, authority).request_digest
                if claims and authority is not None
                else None
            ),
        )
        port = FakeReservationPort(selected_result)
        decision = route_task(
            value,
            path_policy=policy_for(value),
            path_mutation=selected_mutation,
            path_observations=selected_observations,
            resource_claims=claims,
            known_keys=selected_known_keys,
            profile=selected_profile,
            reservation_port=port,
            reservation_id="reservation-1",
            reservation_authority=(
                authority if claims or include_authority_for_claim_free else None
            ),
        )
        return decision, port

    def test_normal_requires_matching_serial_policy_and_reservation(self) -> None:
        value = task(resources=("cache",))
        claims = (
            ResourceClaimPolicy(
                ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
            ),
        )
        decision, port = self.route(value, claims=claims)
        self.assertTrue(decision.candidate)
        self.assertEqual(decision.dispatch_mode, DispatchMode.SERIAL)
        self.assertTrue(decision.serial_review_required)
        self.assertTrue(decision.completion_gate_required)
        self.assertFalse(decision.parallel_candidate)
        self.assertEqual(len(port.requests), 1)

        unmatched = profile(value, review=serial_policy(replace(value, title="other")))
        rejected, unmatched_port = self.route(
            value, profile_value=unmatched, claims=claims
        )
        self.assertFalse(rejected.candidate)
        self.assertEqual(rejected.reason_code, "review-policy-mismatch")
        self.assertEqual(unmatched_port.requests, [])

        self.assertEqual(port.requests[0].authority, AUTHORITY)

    def test_resource_tasks_without_authority_are_rejected_before_port(self) -> None:
        value = task(resources=("cache",))
        claim = ResourceClaimPolicy(
            ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
        )
        rejected, port = self.route(value, claims=(claim,), authority=None)
        self.assertFalse(rejected.candidate)
        self.assertEqual(rejected.reason_code, "missing-authority")
        self.assertEqual(port.requests, [])

    def test_route_reuses_known_key_adapter_and_rejects_unknown_or_duplicate_claims(
        self,
    ) -> None:
        value = task(resources=("cache",))
        claim = ResourceClaimPolicy(
            ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
        )
        rejected, port = self.route(
            value,
            claims=(claim,),
            known_keys=frozenset({ResourceKey("other")}),
        )
        self.assertFalse(rejected.candidate)
        self.assertEqual(rejected.reason_code, "unknown-resource-key")
        self.assertEqual(port.requests, [])

        duplicate = task(resources=("cache", "cache"))
        rejected, port = self.route(
            duplicate,
            claims=(
                ResourceClaimPolicy(
                    ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
                ),
                ResourceClaimPolicy(
                    ResourceClaim("cache"), ResourceKey("gpu"), ResourceMode.SHARED
                ),
            ),
            known_keys=frozenset({ResourceKey("cache"), ResourceKey("gpu")}),
        )
        self.assertFalse(rejected.candidate)
        self.assertEqual(rejected.reason_code, "duplicate-resource-claim")
        self.assertEqual(port.requests, [])

    def test_reservation_authority_owner_epoch_and_token_mismatch_fail_closed(
        self,
    ) -> None:
        value = task(resources=("cache",))
        claim = ResourceClaimPolicy(
            ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
        )
        for foreign in (
            replace(AUTHORITY, owner_id="owner-2"),
            replace(AUTHORITY, lease_epoch=4),
            replace(AUTHORITY, fencing_token=8),
        ):
            with self.subTest(foreign=foreign):
                decision, port = self.route(
                    value,
                    claims=(claim,),
                    result=ResourceReservationResult(
                        ReservationStatus.RESERVED,
                        "reservation-1",
                        (ResourceKey("cache"),),
                        foreign,
                        value.task_id,
                        reservation_request(value, (claim,), AUTHORITY).request_digest,
                    ),
                )
                self.assertFalse(decision.candidate)
                self.assertEqual(decision.reason_code, "reservation-authority-mismatch")
                self.assertEqual(len(port.requests), 1)

        no_authority, port = self.route(
            value,
            claims=(claim,),
            result=ResourceReservationResult(
                ReservationStatus.RESERVED,
                "reservation-1",
                (ResourceKey("cache"),),
                None,
                value.task_id,
                reservation_request(value, (claim,), AUTHORITY).request_digest,
            ),
        )
        self.assertFalse(no_authority.candidate)
        self.assertEqual(no_authority.reason_code, "reservation-authority-mismatch")
        self.assertEqual(len(port.requests), 1)

    def test_forged_reservation_authority_is_typed_rejection(self) -> None:
        value = task(resources=("cache",))
        claim = ResourceClaimPolicy(
            ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
        )
        forged = object.__new__(ResourceReservationAuthority)
        object.__setattr__(forged, "owner_id", "")
        object.__setattr__(forged, "lease_epoch", -1)
        object.__setattr__(forged, "fencing_token", 0)
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-authority"):
            self.route(value, claims=(claim,), authority=forged)

    def test_reservation_digest_binds_task_claim_mode_and_authority(self) -> None:
        value = task(resources=("cache",))
        claim = ResourceClaimPolicy(
            ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
        )
        request = reservation_request(value, (claim,), AUTHORITY)
        self.assertEqual(len(request.request_digest), 64)

        foreign_task = replace(value, task_id=TaskId("foreign"))
        replay, port = self.route(
            foreign_task,
            claims=(claim,),
            result=ResourceReservationResult(
                ReservationStatus.RESERVED,
                "reservation-1",
                (ResourceKey("cache"),),
                AUTHORITY,
                value.task_id,
                request.request_digest,
            ),
        )
        self.assertFalse(replay.candidate)
        self.assertEqual(replay.reason_code, "reservation-identity")
        self.assertEqual(len(port.requests), 1)

        forged_policy = object.__new__(ResourceClaimPolicy)
        object.__setattr__(forged_policy, "claim", ResourceClaim("cache"))
        object.__setattr__(forged_policy, "key", ResourceKey("cache"))
        object.__setattr__(forged_policy, "mode", "bogus")
        with self.assertRaisesRegex(PathResourcePolicyError, "unknown-resource-mode"):
            adapt_resource_claims(
                value,
                (forged_policy,),
                known_keys=frozenset({ResourceKey("cache")}),
            )

    def test_claim_free_tasks_do_not_accept_or_synthesize_authority(self) -> None:
        value = task()
        rejected, port = self.route(
            value,
            authority=AUTHORITY,
            include_authority_for_claim_free=True,
        )
        self.assertFalse(rejected.candidate)
        self.assertEqual(rejected.reason_code, "unexpected-authority")
        self.assertEqual(port.requests, [])

    def test_reservation_unknown_conflict_stale_or_identity_mismatch_never_falls_back(
        self,
    ) -> None:
        value = task(resources=("cache",))
        claim = ResourceClaimPolicy(
            ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.SHARED
        )
        for status in (
            ReservationStatus.CONFLICT,
            ReservationStatus.UNKNOWN,
            ReservationStatus.STALE,
        ):
            with self.subTest(status=status):
                decision, port = self.route(
                    value,
                    claims=(claim,),
                    result=ResourceReservationResult(
                        status,
                        "reservation-1",
                        (ResourceKey("cache"),),
                        AUTHORITY,
                        value.task_id,
                        reservation_request(value, (claim,), AUTHORITY).request_digest,
                    ),
                )
                self.assertFalse(decision.candidate)
                self.assertEqual(decision.reservation.status, status)  # type: ignore[union-attr]
                self.assertEqual(len(port.requests), 1)

        mismatched, port = self.route(
            value,
            claims=(claim,),
            result=ResourceReservationResult(
                ReservationStatus.RESERVED,
                "other-reservation",
                (ResourceKey("cache"),),
                AUTHORITY,
                value.task_id,
                reservation_request(value, (claim,), AUTHORITY).request_digest,
            ),
        )
        self.assertFalse(mismatched.candidate)
        self.assertEqual(mismatched.reason_code, "reservation-identity")
        self.assertEqual(len(port.requests), 1)

    def test_express_is_small_single_exact_existing_write_with_same_gate(self) -> None:
        value = task(
            lane=TaskLane.EXPRESS,
            kind=TaskKind.SMALL_CHANGE,
            denied=(".git",),
        )
        decision, _ = self.route(value)
        self.assertTrue(decision.candidate)
        self.assertEqual(decision.dispatch_mode, DispatchMode.SERIAL)
        self.assertTrue(decision.serial_review_required)
        self.assertTrue(decision.completion_gate_required)
        self.assertFalse(decision.parallel_candidate)

        cases = (
            (replace(value, kind=TaskKind.IMPLEMENTATION), "express-kind"),
            (
                replace(value, dependencies=(TaskId("dependency"),)),
                "express-dependencies",
            ),
        )
        for invalid, code in cases:
            with self.subTest(code=code):
                rejected, port = self.route(invalid)
                self.assertFalse(rejected.candidate)
                self.assertEqual(rejected.reason_code, code)
                self.assertEqual(port.requests, [])

        directory = replace(value, allowed_paths=("src",))
        rejected, _ = self.route(
            directory,
            mutation=PathMutation(PathOperation.MODIFY, "src/file.txt", None),
            observations=(observation("src/file.txt"), root_observation()),
        )
        self.assertEqual(rejected.reason_code, "path-outside-allowed")

        exclusive = replace(value, resource_claims=(ResourceClaim("cache"),))
        rejected, _ = self.route(
            exclusive,
            claims=(
                ResourceClaimPolicy(
                    ResourceClaim("cache"), ResourceKey("cache"), ResourceMode.EXCLUSIVE
                ),
            ),
        )
        self.assertEqual(rejected.reason_code, "express-exclusive-resource")

    def test_research_requires_topology_read_only_and_never_reserves_or_completes_workspace_write(
        self,
    ) -> None:
        value = task(lane=TaskLane.RESEARCH, kind=TaskKind.RESEARCH)
        read_policy = policy_for(
            value,
            allowed=(PathClaim("src/file.txt", PathKind.EXACT, PathAccess.READ),),
        )
        port = FakeReservationPort(
            ResourceReservationResult(
                ReservationStatus.RESERVED, "unused", (), None, None, None
            )
        )
        decision = route_task(
            value,
            path_policy=read_policy,
            path_mutation=PathMutation(PathOperation.READ, "src/file.txt", None),
            path_observations=(
                root_observation(),
                observation("src", entry_kind=PathEntryKind.DIRECTORY),
                observation("src/file.txt"),
            ),
            resource_claims=(),
            known_keys=frozenset(),
            profile=profile(
                value,
                team=research_team_definition(),
            ),
            reservation_port=port,
            reservation_id="unused",
            reservation_authority=None,
        )
        self.assertTrue(decision.candidate)
        self.assertEqual(decision.dispatch_mode, DispatchMode.READ_ONLY)
        self.assertFalse(decision.serial_review_required)
        self.assertFalse(decision.completion_gate_required)
        self.assertFalse(decision.permits_workspace_write)
        self.assertFalse(decision.parallel_candidate)
        self.assertEqual(port.requests, [])

        write_path = replace(value, allowed_paths=("src/file.txt",))
        rejected, port = self.route(
            write_path,
            profile_value=profile(
                write_path,
                team=research_team_definition(),
            ),
            mutation=PathMutation(PathOperation.MODIFY, "src/file.txt", None),
        )
        self.assertFalse(rejected.candidate)
        self.assertEqual(rejected.reason_code, "research-write")
        self.assertEqual(port.requests, [])

    def test_research_recomputes_worker_permission_from_team_definition(self) -> None:
        value = task(lane=TaskLane.RESEARCH, kind=TaskKind.RESEARCH)
        readonly_policy = policy_for(
            value,
            allowed=(PathClaim("src/file.txt", PathKind.EXACT, PathAccess.READ),),
        )
        for team, reason in (
            (team_definition(), "research-profile"),
            (missing_worker_team_definition(), "profile-topology"),
        ):
            with self.subTest(reason=reason):
                port = FakeReservationPort(
                    ResourceReservationResult(
                        ReservationStatus.RESERVED, "unused", (), None, None, None
                    )
                )
                decision = route_task(
                    value,
                    path_policy=readonly_policy,
                    path_mutation=PathMutation(
                        PathOperation.READ, "src/file.txt", None
                    ),
                    path_observations=(
                        root_observation(),
                        observation("src", entry_kind=PathEntryKind.DIRECTORY),
                        observation("src/file.txt"),
                    ),
                    resource_claims=(),
                    known_keys=frozenset(),
                    profile=profile(value, team=team),
                    reservation_port=port,
                    reservation_id="unused",
                    reservation_authority=None,
                )
                self.assertFalse(decision.candidate)
                self.assertEqual(decision.reason_code, reason)
                self.assertEqual(port.requests, [])

        with self.assertRaises(TypeError):
            LaneProfileBinding(
                team_definition=team_definition(),
                worker_node=NodeId("worker"),
                reviewer_pair=None,
                verified_read_only=True,  # type: ignore[call-arg]
            )

    def test_normal_policy_and_profile_must_share_the_same_team_definition(
        self,
    ) -> None:
        value = task()
        review = serial_policy(value)
        foreign_team = replace(team_definition(), team_id=TeamId("foreign"))
        decision, port = self.route(
            value,
            profile_value=profile(value, team=foreign_team, review=review),
        )
        self.assertFalse(decision.candidate)
        self.assertEqual(decision.reason_code, "review-policy-mismatch")
        self.assertEqual(port.requests, [])

    def test_evil_worker_node_identity_with_recomputed_fingerprint_is_rejected(
        self,
    ) -> None:
        value = task()
        review = serial_policy(value)
        forged = object.__new__(SerialReviewPolicy)
        for field_name in review.__dataclass_fields__:
            object.__setattr__(forged, field_name, getattr(review, field_name))
        object.__setattr__(
            forged,
            "worker_node",
            cast(NodeId, EvilWorkerNodeString("worker")),
        )
        object.__setattr__(
            forged,
            "fingerprint",
            review_policy_module._policy_fingerprint(
                forged.task,
                forged.team_definition.team_id,
                forged.pair,
                forged.max_review_rounds,
                forged.dependency_states,
            ),
        )
        decision, port = self.route(
            value,
            profile_value=profile(value, review=forged),
        )
        self.assertFalse(decision.candidate)
        self.assertEqual(decision.reason_code, "review-policy-mismatch")
        self.assertEqual(port.requests, [])

    def test_evil_task_id_identity_is_rejected_even_with_matching_forged_policy(
        self,
    ) -> None:
        value = task()
        forged = object.__new__(TaskSpec)
        for field_name in value.__dataclass_fields__:
            object.__setattr__(forged, field_name, getattr(value, field_name))
        object.__setattr__(forged, "task_id", cast(TaskId, EvilString("task")))
        review = serial_policy(forged)
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-task"):
            self.route(forged, profile_value=profile(forged, review=review))

    def test_review_pair_and_policy_subclasses_are_not_authority(self) -> None:
        value = task()
        review = serial_policy(value)
        evil_pair = EvilReviewPair(NodeId("worker"), NodeId("reviewer"))
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-type"):
            profile(value, reviewer_pair=evil_pair, review=review)

        forged_policy = object.__new__(EvilSerialReviewPolicy)
        for field_name in review.__dataclass_fields__:
            object.__setattr__(forged_policy, field_name, getattr(review, field_name))
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-type"):
            profile(value, review=cast(SerialReviewPolicy, forged_policy))

        forged_profile = object.__new__(LaneProfileBinding)
        object.__setattr__(forged_profile, "team_definition", team_definition())
        object.__setattr__(forged_profile, "worker_node", NodeId("worker"))
        object.__setattr__(forged_profile, "reviewer_pair", review.pair)
        object.__setattr__(
            forged_profile,
            "serial_review_policy",
            cast(SerialReviewPolicy, forged_policy),
        )
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-type"):
            self.route(
                value,
                profile_value=forged_profile,
            )

    def test_evil_team_subclass_is_not_authority_for_normal_routing(self) -> None:
        value = task()
        review = serial_policy(value)
        base = team_definition()
        evil = EvilTeam(TeamId("foreign"), base.nodes, base.edges)
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-profile"):
            LaneProfileBinding(
                team_definition=evil,
                worker_node=NodeId("worker"),
                reviewer_pair=review.pair,
                serial_review_policy=review,
            )

    def test_forged_nested_topology_values_are_rejected(self) -> None:
        value = task()
        review = serial_policy(value)
        base = team_definition()
        forged_node = EvilAgentNode(
            NodeId("worker"), "Worker", base.nodes[1].profile, is_main=False
        )
        forged_team = replace(
            base,
            nodes=(base.nodes[0], forged_node, base.nodes[2]),
        )
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-profile"):
            profile(value, team=forged_team, review=review)

        forged_profile = object.__new__(ProfileRef)
        object.__setattr__(forged_profile, "provider", "worker")
        object.__setattr__(forged_profile, "transport", "direct")
        object.__setattr__(forged_profile, "permission", "bogus")
        invalid_profile_node = replace(base.nodes[1], profile=forged_profile)
        forged_team = replace(
            base,
            nodes=(base.nodes[0], invalid_profile_node, base.nodes[2]),
        )
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-permission"):
            profile(value, team=forged_team, review=review)

        forged_edge = object.__new__(Edge)
        object.__setattr__(forged_edge, "source", NodeId("main"))
        object.__setattr__(forged_edge, "target", NodeId("worker"))
        object.__setattr__(forged_edge, "kind", "bogus")
        forged_team = replace(
            base,
            edges=(forged_edge, base.edges[1]),
        )
        with self.assertRaisesRegex(PathResourcePolicyError, "invalid-profile"):
            profile(value, team=forged_team, review=review)

    def test_unknown_lane_or_profile_mismatch_does_not_fallback(self) -> None:
        value = task()
        forged = object.__new__(TaskSpec)
        for field_name in value.__dataclass_fields__:
            object.__setattr__(forged, field_name, getattr(value, field_name))
        object.__setattr__(forged, "lane", "unknown")
        with self.assertRaisesRegex(PathResourcePolicyError, "unknown-lane"):
            route_task(
                forged,
                path_policy=policy_for(value),
                path_mutation=PathMutation(PathOperation.MODIFY, "src/file.txt", None),
                path_observations=(
                    root_observation(),
                    observation("src", entry_kind=PathEntryKind.DIRECTORY),
                    observation("src/file.txt"),
                ),
                resource_claims=(),
                known_keys=frozenset(),
                profile=profile(value, review=serial_policy(value)),
                reservation_port=FakeReservationPort(
                    ResourceReservationResult(
                        ReservationStatus.RESERVED, "unused", (), None, None, None
                    )
                ),
                reservation_id="unused",
                reservation_authority=None,
            )

        bad_profile = profile(
            value,
            reviewer_pair=ReviewPair(NodeId("worker"), NodeId("reviewer")),
            team=wrong_reviewer_permission_team_definition(),
            review=serial_policy(value),
        )
        rejected, port = self.route(value, profile_value=bad_profile)
        self.assertFalse(rejected.candidate)
        self.assertEqual(rejected.reason_code, "review-profile")
        self.assertEqual(port.requests, [])

    def test_route_rejects_path_policy_from_a_different_task(self) -> None:
        value = task()
        foreign = task(allowed=("src/other.txt",))
        review = serial_policy(value)
        port = FakeReservationPort(
            ResourceReservationResult(
                ReservationStatus.RESERVED, "unused", (), None, None, None
            )
        )
        decision = route_task(
            value,
            path_policy=policy_for(foreign),
            path_mutation=PathMutation(PathOperation.MODIFY, "src/file.txt", None),
            path_observations=(observation("src/file.txt"), root_observation()),
            resource_claims=(),
            known_keys=frozenset(),
            profile=profile(value, review=review),
            reservation_port=port,
            reservation_id="unused",
            reservation_authority=None,
        )
        self.assertFalse(decision.candidate)
        self.assertEqual(decision.reason_code, "missing-claim")
        self.assertEqual(port.requests, [])

    def test_serial_policy_fingerprint_is_revalidated(self) -> None:
        value = task()
        review = serial_policy(value)
        forged = object.__new__(SerialReviewPolicy)
        for field_name in review.__dataclass_fields__:
            object.__setattr__(forged, field_name, getattr(review, field_name))
        object.__setattr__(forged, "fingerprint", "0" * 64)
        decision, port = self.route(
            value,
            profile_value=profile(value, review=forged),
        )
        self.assertFalse(decision.candidate)
        self.assertEqual(decision.reason_code, "review-policy-mismatch")
        self.assertEqual(port.requests, [])


if __name__ == "__main__":
    unittest.main()
