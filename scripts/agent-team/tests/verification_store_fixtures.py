"""Reusable real schema-4 current-pair fixture for Issue #82.

The fixture deliberately enters the verification seam only after the actual
Issue #81 producer has created and reopened its three-edge review suffix.  It
also creates the retained #50 completion reference through the real route and
reservation owner before any #81 edge is committed.  No SQL is used to create
the current pair or its event history.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import test_policy_verification_handoff_authority as authority_fixtures
import test_policy_verification_handoff_composer as composer_fixtures
import test_review_checkpoint_producer as producer_fixtures
import test_workflow_store_transaction as workflow_fixtures

from agent_team import policy_verification_handoff as handoff_module
from agent_team import workflow_store as workflow
from agent_team.path_resource_policy import (
    PathAccess,
    PathClaim,
    PathClaimPolicy,
    PathEntryKind,
    PathKind,
    PathObservation,
    WorkspaceObservation,
)
from agent_team.store import CoordinationStore
from agent_team.task_policy import WorkspaceIdentity

OWNER_ID = "verification-owner"


@dataclass(slots=True)
class ActualReviewCheckpointFixture:
    """An actual #81 current pair plus retained #50/#74 owner values."""

    temporary: tempfile.TemporaryDirectory[str]
    state_root: Path
    root: workflow.RootIdentity
    store: CoordinationStore
    chain: Any
    handoff: handoff_module.PolicyVerificationHandoff
    owner_store: Any
    completion_ref: Any
    review_refs: tuple[Any, Any, Any]
    final_review_binding: Any
    current: workflow.WorkflowCheckpointV4
    observation: Any
    predecessor: workflow.WorkflowCheckpointV4
    reservation_port: Any
    owner_id: str = OWNER_ID

    def close(self) -> None:
        """Close the fresh Store and release the temporary workspace."""

        self.store.close()
        self.temporary.cleanup()


def _actual_path_observations(workspace: Path) -> tuple[PathObservation, ...]:
    source = workspace / "src"
    target = source / "file.txt"
    workspace_stat = workspace.stat()
    source_stat = source.stat()
    target_stat = target.stat()
    return (
        PathObservation(
            relative_path=".",
            canonical_path=str(workspace),
            entry_kind=PathEntryKind.DIRECTORY,
            device=int(workspace_stat.st_dev),
            inode=int(workspace_stat.st_ino),
            nlink=int(workspace_stat.st_nlink),
            parent_device=None,
            parent_inode=None,
            ancestor_symlink=False,
        ),
        PathObservation(
            relative_path="src",
            canonical_path=str(source),
            entry_kind=PathEntryKind.DIRECTORY,
            device=int(source_stat.st_dev),
            inode=int(source_stat.st_ino),
            nlink=int(source_stat.st_nlink),
            parent_device=int(workspace_stat.st_dev),
            parent_inode=int(workspace_stat.st_ino),
            ancestor_symlink=False,
        ),
        PathObservation(
            relative_path="src/file.txt",
            canonical_path=str(target),
            entry_kind=PathEntryKind.REGULAR,
            device=int(target_stat.st_dev),
            inode=int(target_stat.st_ino),
            nlink=int(target_stat.st_nlink),
            parent_device=int(source_stat.st_dev),
            parent_inode=int(source_stat.st_ino),
            ancestor_symlink=False,
        ),
    )


def _actual_workspace_policy(
    task: Any, workspace: Path
) -> tuple[WorkspaceObservation, PathClaimPolicy, tuple[PathObservation, ...]]:
    workspace_stat = workspace.stat()
    observed_workspace = WorkspaceObservation(
        workspace=WorkspaceIdentity(str(workspace)),
        canonical_path=str(workspace),
        device=int(workspace_stat.st_dev),
        inode=int(workspace_stat.st_ino),
        case_sensitive=True,
    )
    observations = _actual_path_observations(workspace)
    policy = PathClaimPolicy.from_task_spec(
        task,
        workspace=observed_workspace,
        allowed=(PathClaim("src/file.txt", PathKind.EXACT, PathAccess.WRITE),),
        denied=(),
        reserved_roots=(),
    )
    return observed_workspace, policy, observations


def _issue_actual_completion_ref(
    handoff: handoff_module.PolicyVerificationHandoff,
    root: workflow.RootIdentity,
) -> tuple[Any, Any]:
    """Issue #50 once from a real temporary workspace and return its port."""

    workspace = Path(root.workspace_path)
    source = workspace / "src"
    source.mkdir()
    target = source / "file.txt"
    target.write_text("verification fixture\n", encoding="utf-8")
    _observed_workspace, path_policy, observations = _actual_workspace_policy(
        authority_fixtures._path_task(), workspace
    )
    reservation_port = authority_fixtures.RecordingReservationPort()
    route = authority_fixtures._route_inputs(
        authority_fixtures._path_task(),
        port=reservation_port,
        policy=path_policy,
        observations=observations,
    )
    completion_ref = handoff.issue_completion_admission(**route)
    return completion_ref, reservation_port


def issue_foreign_handoff_completion_ref(
    fixture: ActualReviewCheckpointFixture,
) -> tuple[handoff_module.PolicyVerificationHandoff, Any, Any]:
    """Issue an otherwise-valid completion ref from another exact Handoff."""

    workspace = Path(fixture.root.workspace_path)
    _observed_workspace, path_policy, observations = _actual_workspace_policy(
        authority_fixtures._path_task(), workspace
    )
    reservation_port = authority_fixtures.RecordingReservationPort()
    route = authority_fixtures._route_inputs(
        authority_fixtures._path_task(),
        port=reservation_port,
        policy=path_policy,
        observations=observations,
    )
    foreign = handoff_module.PolicyVerificationHandoff(fixture.owner_store)
    return foreign, foreign.issue_completion_admission(**route), reservation_port


def _assert_reopened_review_pair(
    observation: Any,
    chain: Any,
    review_refs: tuple[Any, Any, Any],
) -> workflow.WorkflowCheckpointV4:
    """Check the producer's complete review prefix and approved current pair."""

    checkpoint = observation.checkpoint
    if checkpoint.workflow_state is not workflow.CheckpointState.REVIEW_PENDING:
        raise AssertionError("reopened #81 checkpoint is not REVIEW_PENDING")
    if checkpoint.task_sequence is None:
        raise AssertionError("reopened #81 checkpoint has no task sequence")
    if observation.task.state.phase.value != "approved":
        raise AssertionError("reopened #81 task is not APPROVED")
    if observation.task.state.sequence != checkpoint.task_sequence:
        raise AssertionError("task/checkpoint sequences differ after reopen")
    if len(observation.events) != 3:
        raise AssertionError("reopened #81 suffix is not the three actual edges")
    if observation.predecessor_checkpoint_bytes == b"":
        raise AssertionError("#81 predecessor checkpoint bytes are empty")

    predecessor = workflow.decode_checkpoint(observation.predecessor_checkpoint_bytes)
    if predecessor.workflow_state is not workflow.CheckpointState.WORKER_DONE:
        raise AssertionError("#81 predecessor is not WORKER_DONE")
    if predecessor.workflow_sequence + 1 != observation.events[0].workflow_sequence:
        raise AssertionError("#81 predecessor is not adjacent to the suffix")
    expected_edges = (
        (workflow.CheckpointState.WORKER_DONE, workflow.CheckpointState.WORKER_DONE),
        (workflow.CheckpointState.WORKER_DONE, workflow.CheckpointState.REVIEW_PENDING),
        (
            workflow.CheckpointState.REVIEW_PENDING,
            workflow.CheckpointState.REVIEW_PENDING,
        ),
    )
    previous = predecessor
    for event, (from_state, to_state) in zip(
        observation.events, expected_edges, strict=True
    ):
        decoded = workflow.decode_checkpoint(event.checkpoint_bytes)
        if event.from_state != from_state.value or event.to_state != to_state.value:
            raise AssertionError("#81 review suffix edge differs")
        if decoded.workflow_sequence != event.workflow_sequence:
            raise AssertionError("#81 event/checkpoint workflow sequence differs")
        if decoded.workflow_sequence != previous.workflow_sequence + 1:
            raise AssertionError("#81 review suffix is not a complete prefix")
        if decoded.task_sequence != event.task_sequence_after:
            raise AssertionError("#81 event/checkpoint task sequence differs")
        if event.checkpoint_digest != decoded.checkpoint_digest:
            raise AssertionError("#81 event checkpoint digest differs")
        previous = decoded
    if previous != checkpoint:
        raise AssertionError("#81 suffix does not terminate at current checkpoint")

    authority = checkpoint.review_authority
    if authority is None:
        raise AssertionError("#81 current checkpoint has no review authority")
    final_ref = review_refs[-1]
    if authority.reference != final_ref.reference:
        raise AssertionError("#81 final review authority differs from retained ref")
    if authority.digest != observation.events[-1].evidence_ref:
        raise AssertionError("#81 final Store authority differs from event evidence")
    if chain.approved.next_state.task_state.phase.value != "approved":
        raise AssertionError("#49 approved reducer output is not approved")
    return predecessor


@contextmanager
def actual_review_checkpoint_fixture() -> Iterator[ActualReviewCheckpointFixture]:
    """Create and reopen a real #81 ``REVIEW_PENDING + APPROVED`` pair."""

    temporary = tempfile.TemporaryDirectory(prefix="agent-team-verification-store-")
    state_root = workflow_fixtures._make_state_root(temporary.name)
    root = replace(
        workflow_fixtures._make_root(state_root, temporary.name),
        team_id="team",
    )
    chain = producer_fixtures._review_chain(root)
    workflow_fixtures._open_started_store(state_root, root)
    store = CoordinationStore(state_root)
    owner_store = composer_fixtures._FakePolicyVerificationStore()
    handoff = handoff_module.PolicyVerificationHandoff(owner_store)
    reservation_port: Any | None = None
    active_store: CoordinationStore | None = store
    try:
        started = store.load_checkpoint(workflow.WorkflowRootKey(root.root_key))
        if type(started) is not workflow.WorkflowCheckpointV4:
            raise AssertionError("started Store checkpoint is unavailable")
        prompted = producer_fixtures._commit_prompt(
            store,
            root,
            started,
            chain.assigned.next_state.task_state,
        )
        current = producer_fixtures._commit_successful_wait(store, root, prompted)
        if current.workflow_state is not workflow.CheckpointState.WORKER_DONE:
            raise AssertionError("#81 producer precondition is not WORKER_DONE")

        completion_ref, reservation_port = _issue_actual_completion_ref(handoff, root)
        review_refs = tuple(
            handoff.save_authority(update, chain.policy)
            for update in (
                chain.worker_done,
                chain.review_pending,
                chain.approved,
            )
        )
        if len(review_refs) != 3:
            raise AssertionError("#81 did not issue three review refs")
        producer = producer_fixtures._producer_module().ReviewCheckpointProducer(
            handoff, store
        )
        for update, review_ref in zip(
            (
                chain.worker_done,
                chain.review_pending,
                chain.approved,
            ),
            review_refs,
            strict=True,
        ):
            result = producer.commit(current, update, chain.policy, review_ref)
            current = result.checkpoint
        store.close()
        store = CoordinationStore(state_root)
        active_store = store
        reopened_producer = (
            producer_fixtures._producer_module().ReviewCheckpointProducer(
                handoff, store
            )
        )
        observation = reopened_producer.read(workflow.WorkflowRootKey(root.root_key))
        if observation is None:
            raise AssertionError("#81 current pair did not reopen")
        predecessor = _assert_reopened_review_pair(observation, chain, review_refs)
        final_review_binding = handoff._bind_review_authority(
            chain.approved,
            chain.policy,
            review_refs[-1],
        )
        yield ActualReviewCheckpointFixture(
            temporary=temporary,
            state_root=state_root,
            root=root,
            store=store,
            chain=chain,
            handoff=handoff,
            owner_store=owner_store,
            completion_ref=completion_ref,
            review_refs=review_refs,
            final_review_binding=final_review_binding,
            current=observation.checkpoint,
            observation=observation,
            predecessor=predecessor,
            reservation_port=reservation_port,
        )
    finally:
        if active_store is not None:
            active_store.close()
        temporary.cleanup()


__all__ = [
    "OWNER_ID",
    "ActualReviewCheckpointFixture",
    "actual_review_checkpoint_fixture",
    "issue_foreign_handoff_completion_ref",
]
