"""Lifecycle coverage for the typed workflow Store facade.

The reducer and external effect adapter are intentionally not part of this
module.  Each test drives the Store's typed begin/commit boundary with a
Store-issued receipt and checks the durable checkpoint projection that the
facade accepts.  The fake effect counter represents the caller's one attempt;
the Store must not cause a second attempt when an exact operation is replayed.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore

_DIGEST_1 = "sha256:" + "1" * 64
_DIGEST_2 = "sha256:" + "2" * 64
_DIGEST_3 = "sha256:" + "3" * 64
_DIGEST_4 = "sha256:" + "4" * 64


def _digest(value: str, *, domain: bytes = workflow.REQUEST_DIGEST_DOMAIN) -> str:
    return workflow.digest_bounded_body(value.encode("utf-8"), domain=domain)


def _state_root(parent: str) -> Path:
    state_root = Path(os.path.realpath(parent)) / "state"
    state_root.mkdir()
    state_root.chmod(0o700)
    return state_root


def _path_identity(path: Path) -> workflow.PathIdentity:
    metadata = path.stat()
    return workflow.PathIdentity(
        path=str(path), device=int(metadata.st_dev), inode=int(metadata.st_ino)
    )


def _root(parent: str, state_root: Path) -> workflow.RootIdentity:
    parent_path = Path(os.path.realpath(parent))
    workspace = parent_path / "workspace"
    workspace.mkdir()
    workspace.chmod(0o700)
    config = parent_path / "agent-team-config.toml"
    config_bytes = b"team = 'team-1'\n"
    config.write_bytes(config_bytes)
    config.chmod(0o600)
    config_metadata = config.stat()
    return workflow.RootIdentity(
        root_key="root-1",
        team_id="team-1",
        workspace=_path_identity(workspace),
        config_path=str(config),
        config_device=int(config_metadata.st_dev),
        config_inode=int(config_metadata.st_ino),
        config_digest=workflow.config_content_digest(config_bytes),
        state_root=_path_identity(state_root),
    )


def _start_intent(root: workflow.RootIdentity) -> workflow.OperationIntent:
    return workflow.OperationIntent(
        operation_id="operation-start",
        effect_key="effect/start",
        root_key=root.root_key,
        root=root,
        action=workflow.OperationAction.START,
        request_digest=_digest("start-request"),
        expected_workflow_sequence=0,
        expected_task_sequence=None,
        run_id=None,
        main_terminal_id=None,
        task_id=None,
        dispatch_id=None,
        attempt=None,
        terminal_id=None,
        delivery_id=None,
        message_id=None,
        consumer_generation=0,
        owner="owner-1",
        lease_epoch=0,
        fencing_token=0,
        actor="actor-1",
        evidence_ref=_DIGEST_1,
    )


def _assignment() -> workflow.ActiveAssignment:
    completion = workflow.CompletionIdentity(
        run_id="run-1",
        task_id="task-1",
        dispatch_id="dispatch-1",
        sender_terminal_id="terminal-worker",
    )
    return workflow.ActiveAssignment(
        role=workflow.AssignmentRole.WORKER,
        worker_node="worker-node-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-1",
        terminal_id="terminal-worker",
        launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
        completion_identity=completion,
    )


def _start_draft(
    root: workflow.RootIdentity, receipt: workflow.DurableReceipt
) -> workflow.WorkflowCheckpointDraft:
    return workflow.WorkflowCheckpointDraft(
        root=root,
        run=workflow.RunIdentity("run-1", "terminal-main", 0),
        workflow_sequence=2,
        task_sequence=None,
        execution_mode=workflow.ExecutionMode.SERIAL,
        workflow_state=workflow.CheckpointState.IDLE,
        task_policy=None,
        active_assignment=None,
        pending_delivery=None,
        replied_message_ids=(),
        read_observed=False,
        released=False,
        review_authority=None,
        verification_authority=None,
        last_operation=workflow.LastOperation(
            operation_id=receipt.operation_id,
            effect_key=receipt.effect_key,
            action=receipt.action,
            request_digest=receipt.request_digest,
            expected_workflow_sequence=0,
            expected_task_sequence=None,
            status=workflow.OperationStatus.COMMITTED,
            receipt_id=receipt.receipt_id,
            receipt_digest=workflow.durable_receipt_digest(receipt),
        ),
    )


def _prompt_intent(
    root: workflow.RootIdentity, *, operation_id: str = "operation-prompt"
) -> workflow.OperationIntent:
    return workflow.OperationIntent(
        operation_id=operation_id,
        effect_key=f"effect/{operation_id}",
        root_key=root.root_key,
        root=None,
        action=workflow.OperationAction.PROMPT,
        request_digest=_digest(operation_id),
        expected_workflow_sequence=2,
        expected_task_sequence=None,
        run_id="run-1",
        main_terminal_id="terminal-main",
        task_id=None,
        dispatch_id=None,
        attempt=None,
        terminal_id=None,
        delivery_id=None,
        message_id=None,
        consumer_generation=0,
        owner="owner-1",
        lease_epoch=0,
        fencing_token=0,
        actor="owner-1",
        evidence_ref=None,
        next_task_sequence=1,
    )


def _prompt_draft(
    root: workflow.RootIdentity, receipt: workflow.DurableReceipt
) -> workflow.WorkflowCheckpointDraft:
    assignment = _assignment()
    return workflow.WorkflowCheckpointDraft(
        root=root,
        run=workflow.RunIdentity("run-1", "terminal-main", 0),
        workflow_sequence=4,
        task_sequence=1,
        execution_mode=workflow.ExecutionMode.SERIAL,
        workflow_state=workflow.CheckpointState.ACTIVE,
        task_policy=workflow.TaskPolicyReference(
            version=4,
            team_id=root.team_id,
            workspace=root.workspace_path,
            task_id="task-1",
            sequence=1,
            state_digest=_DIGEST_4,
        ),
        active_assignment=assignment,
        pending_delivery=None,
        replied_message_ids=(),
        read_observed=False,
        released=False,
        review_authority=None,
        verification_authority=None,
        last_operation=workflow.LastOperation(
            operation_id=receipt.operation_id,
            effect_key=receipt.effect_key,
            action=receipt.action,
            request_digest=receipt.request_digest,
            expected_workflow_sequence=2,
            expected_task_sequence=None,
            status=workflow.OperationStatus.COMMITTED,
            receipt_id=receipt.receipt_id,
            receipt_digest=workflow.durable_receipt_digest(receipt),
        ),
    )


def _wait_intent(
    root: workflow.RootIdentity,
    current: workflow.WorkflowCheckpointV4,
    *,
    operation_id: str = "operation-wait",
) -> workflow.OperationIntent:
    assignment = current.active_assignment
    if assignment is None:
        raise TypeError("wait fixture requires an assignment")
    return workflow.OperationIntent(
        operation_id=operation_id,
        effect_key=f"effect/{operation_id}",
        root_key=root.root_key,
        root=None,
        action=workflow.OperationAction.WAIT,
        request_digest=_digest(operation_id),
        expected_workflow_sequence=current.workflow_sequence,
        expected_task_sequence=current.task_sequence,
        run_id=current.run.run_id,
        main_terminal_id=current.run.main_terminal_id,
        task_id=assignment.task_id,
        dispatch_id=assignment.dispatch_id,
        attempt=assignment.attempt,
        terminal_id=assignment.terminal_id,
        delivery_id=None,
        message_id=None,
        consumer_generation=current.run.consumer_generation,
        owner="owner-1",
        lease_epoch=0,
        fencing_token=0,
        actor="owner-1",
        evidence_ref=None,
        next_task_sequence=None,
    )


def _wait_delivery(
    current: workflow.WorkflowCheckpointV4,
    *,
    outcome: workflow.EventOutcome | None,
) -> workflow.PendingDelivery:
    assignment = current.active_assignment
    if assignment is None:
        raise TypeError("wait fixture requires an assignment")
    if outcome is None:
        kind = workflow.EventProjectionKind.QUESTION
        message_id = "message-1"
    else:
        kind = workflow.EventProjectionKind.WORKER_DONE
        message_id = None
    projection = workflow.EventProjection(
        kind=kind,
        message_id=message_id,
        completion_identity=assignment.completion_identity,
        outcome=outcome,
        body_digest=_DIGEST_1,
    )
    return workflow.PendingDelivery(
        delivery_id="delivery-1",
        consumer_generation=current.run.consumer_generation,
        ordered_message_ids=() if message_id is None else (message_id,),
        ordered_event_projection=(projection,),
        delivery_digest=workflow.delivery_content_digest(
            delivery_id="delivery-1",
            consumer_generation=current.run.consumer_generation,
            ordered_message_ids=() if message_id is None else (message_id,),
            ordered_event_projection=(projection,),
        ),
        ack_operation_id=None,
        ack_status=workflow.AckStatus.PENDING,
    )


def _wait_draft(
    current: workflow.WorkflowCheckpointV4,
    receipt: workflow.DurableReceipt,
    delivery: workflow.PendingDelivery,
    *,
    state: workflow.CheckpointState,
) -> workflow.WorkflowCheckpointDraft:
    return workflow.WorkflowCheckpointDraft(
        root=current.root,
        run=current.run,
        workflow_sequence=current.workflow_sequence + 2,
        task_sequence=current.task_sequence,
        execution_mode=current.execution_mode,
        workflow_state=state,
        task_policy=current.task_policy,
        active_assignment=current.active_assignment,
        pending_delivery=delivery,
        replied_message_ids=(),
        read_observed=False,
        released=False,
        review_authority=None,
        verification_authority=None,
        last_operation=workflow.LastOperation(
            operation_id=receipt.operation_id,
            effect_key=receipt.effect_key,
            action=receipt.action,
            request_digest=receipt.request_digest,
            expected_workflow_sequence=current.workflow_sequence,
            expected_task_sequence=current.task_sequence,
            status=workflow.OperationStatus.COMMITTED,
            receipt_id=receipt.receipt_id,
            receipt_digest=workflow.durable_receipt_digest(receipt),
        ),
    )


def _action_intent(
    root: workflow.RootIdentity,
    current: workflow.WorkflowCheckpointV4,
    action: workflow.OperationAction,
    *,
    operation_id: str,
    message_id: str | None = None,
) -> workflow.OperationIntent:
    assignment = current.active_assignment
    needs_assignment = action is not workflow.OperationAction.STOP
    if needs_assignment and assignment is None:
        raise TypeError("action fixture requires an assignment")
    if assignment is not None:
        task_id = assignment.task_id
        dispatch_id = assignment.dispatch_id
        attempt = assignment.attempt
        terminal_id = assignment.terminal_id
    else:
        task_id = dispatch_id = attempt = terminal_id = None
    delivery = current.pending_delivery
    delivery_id = (
        None
        if action
        not in (
            workflow.OperationAction.REPLY,
            workflow.OperationAction.READ,
            workflow.OperationAction.RELEASE,
            workflow.OperationAction.ACK,
        )
        else None
        if delivery is None
        else delivery.delivery_id
    )
    return workflow.OperationIntent(
        operation_id=operation_id,
        effect_key=f"effect/{operation_id}",
        root_key=root.root_key,
        root=None,
        action=action,
        request_digest=_digest(operation_id),
        expected_workflow_sequence=current.workflow_sequence,
        expected_task_sequence=current.task_sequence,
        run_id=current.run.run_id,
        main_terminal_id=current.run.main_terminal_id,
        task_id=task_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        terminal_id=terminal_id,
        delivery_id=delivery_id,
        message_id=message_id if action is workflow.OperationAction.REPLY else None,
        consumer_generation=current.run.consumer_generation,
        owner="owner-1",
        lease_epoch=0,
        fencing_token=0,
        actor="owner-1",
        evidence_ref=None,
        next_task_sequence=None,
    )


def _receipt(
    store: CoordinationStore,
    operation: workflow.OperationHandle,
    current: workflow.WorkflowCheckpointV4 | None,
    *,
    receipt_id: str,
    result_digest: str = _DIGEST_3,
    delivery_id: str | None = None,
    message_id: str | None = None,
    assignment_override: workflow.ActiveAssignment | None = None,
) -> workflow.DurableReceipt:
    if current is None:
        run_id = "run-1"
        main_terminal_id = "terminal-main"
        assignment = None
    else:
        run_id = current.run.run_id
        main_terminal_id = current.run.main_terminal_id
        assignment = current.active_assignment
    if assignment_override is not None:
        assignment = assignment_override
    return store._issue_workflow_receipt(
        operation=operation,
        receipt_id=receipt_id,
        run_id=run_id,
        main_terminal_id=main_terminal_id,
        task_id=None if assignment is None else assignment.task_id,
        dispatch_id=None if assignment is None else assignment.dispatch_id,
        attempt=None if assignment is None else assignment.attempt,
        terminal_id=None if assignment is None else assignment.terminal_id,
        delivery_id=delivery_id,
        message_id=message_id,
        effect_ref=f"backend/{receipt_id}",
        result_kind="lifecycle",
        result_digest=result_digest,
        evidence_ref=_DIGEST_4,
        issued_ns=50,
    )


def _db_rows(state_root: Path) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    database = state_root / "coordination.sqlite3"
    with closing(sqlite3.connect(str(database))) as connection:
        return tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    ).fetchall()
                ),
            )
            for table in (
                "workflow_checkpoints",
                "workflow_operations",
                "workflow_receipts",
                "workflow_events",
            )
        )


def _ack_begin_worker(
    state_root: str,
    root: workflow.RootIdentity,
    operation_id: str,
    barrier: Any,
    result_queue: Any,
) -> None:
    try:
        with CoordinationStore(Path(state_root), busy_timeout_ms=1000) as store:
            checkpoint = store.load_checkpoint(workflow.WorkflowRootKey(root.root_key))
            if type(checkpoint) is not workflow.WorkflowCheckpointV4:
                raise TypeError("ack fixture is not a committed checkpoint")
            intent = _action_intent(
                root,
                checkpoint,
                workflow.OperationAction.ACK,
                operation_id=operation_id,
            )
            barrier.wait(timeout=15)
            result = store.begin_operation(
                intent,
                expected_workflow_sequence=checkpoint.workflow_sequence,
                expected_task_sequence=checkpoint.task_sequence,
            )
            result_queue.put(
                (
                    "winner"
                    if isinstance(result, workflow.OperationBegin)
                    else "replay",
                    operation_id,
                )
            )
    except workflow.StateConflict:
        result_queue.put(("conflict", operation_id))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        result_queue.put((f"error:{type(exc).__name__}:{exc}", operation_id))


class _EffectProbe:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.retry_calls = 0

    def execute(self) -> None:
        self.execute_calls += 1

    def retry(self) -> None:
        self.retry_calls += 1


class WorkflowStoreLifecycleTests(unittest.TestCase):
    """Positive, replay, mismatch, and CAS coverage for lifecycle actions."""

    def _bootstrap_start(
        self, store: CoordinationStore, root: workflow.RootIdentity
    ) -> workflow.WorkflowCheckpointV4:
        begun = store.begin_operation(
            _start_intent(root),
            expected_workflow_sequence=0,
            expected_task_sequence=None,
        )
        self.assertIsInstance(begun, workflow.OperationBegin)
        assert isinstance(begun, workflow.OperationBegin)
        receipt = _receipt(store, begun.operation, None, receipt_id="receipt-start")
        committed = store.commit_effect(
            begun.operation, receipt, _start_draft(root, receipt)
        )
        self.assertIsInstance(committed, workflow.WorkflowCommit)
        assert isinstance(committed, workflow.WorkflowCommit)
        return committed.checkpoint

    def _bootstrap_wait(
        self,
        store: CoordinationStore,
        root: workflow.RootIdentity,
        *,
        outcome: workflow.EventOutcome | None,
    ) -> workflow.WorkflowCheckpointV4:
        started = self._bootstrap_start(store, root)
        prompt_begun = store.begin_operation(
            _prompt_intent(root),
            expected_workflow_sequence=started.workflow_sequence,
            expected_task_sequence=started.task_sequence,
        )
        self.assertIsInstance(prompt_begun, workflow.OperationBegin)
        assert isinstance(prompt_begun, workflow.OperationBegin)
        assignment = _assignment()
        prompt_receipt = _receipt(
            store,
            prompt_begun.operation,
            started,
            receipt_id="receipt-prompt",
            result_digest=workflow.assignment_digest(assignment),
            assignment_override=assignment,
        )
        prompt_committed = store.commit_effect(
            prompt_begun.operation,
            prompt_receipt,
            _prompt_draft(root, prompt_receipt),
        )
        self.assertIsInstance(prompt_committed, workflow.WorkflowCommit)
        assert isinstance(prompt_committed, workflow.WorkflowCommit)
        current = prompt_committed.checkpoint

        wait_begun = store.begin_operation(
            _wait_intent(root, current),
            expected_workflow_sequence=current.workflow_sequence,
            expected_task_sequence=current.task_sequence,
        )
        self.assertIsInstance(wait_begun, workflow.OperationBegin)
        assert isinstance(wait_begun, workflow.OperationBegin)
        delivery = _wait_delivery(current, outcome=outcome)
        wait_receipt = _receipt(
            store,
            wait_begun.operation,
            current,
            receipt_id="receipt-wait",
            result_digest=delivery.delivery_digest,
            delivery_id=delivery.delivery_id,
            message_id=(
                None
                if not delivery.ordered_message_ids
                else delivery.ordered_message_ids[0]
            ),
        )
        wait_state = (
            workflow.CheckpointState.QUESTION
            if outcome is None
            else workflow.CheckpointState.WORKER_DONE
        )
        wait_committed = store.commit_effect(
            wait_begun.operation,
            wait_receipt,
            _wait_draft(
                current,
                wait_receipt,
                delivery,
                state=wait_state,
            ),
        )
        self.assertIsInstance(wait_committed, workflow.WorkflowCommit)
        assert isinstance(wait_committed, workflow.WorkflowCommit)
        self.assertEqual(wait_state, wait_committed.checkpoint.workflow_state)
        self.assertEqual(
            current.workflow_sequence + 2,
            wait_committed.checkpoint.workflow_sequence,
        )
        self.assertEqual(
            delivery,
            wait_committed.checkpoint.pending_delivery,
        )
        self.assertEqual(
            current.active_assignment,
            wait_committed.checkpoint.active_assignment,
        )
        return wait_committed.checkpoint

    def _action_draft(
        self,
        current: workflow.WorkflowCheckpointV4,
        intent: workflow.OperationIntent,
        receipt: workflow.DurableReceipt,
    ) -> workflow.WorkflowCheckpointDraft:
        action = intent.action
        assignment = current.active_assignment
        pending = current.pending_delivery
        state = current.workflow_state
        replies = current.replied_message_ids
        read_observed = current.read_observed
        released = current.released
        if action is workflow.OperationAction.REPLY:
            if intent.message_id is None:
                raise TypeError("reply fixture lacks a message")
            replies = (*replies, intent.message_id)
            state = workflow.CheckpointState.QUESTION
        elif action is workflow.OperationAction.READ:
            read_observed = True
        elif action is workflow.OperationAction.RELEASE:
            read_observed = True
            released = True
            state = workflow.CheckpointState.AWAITING_ACK
        elif action is workflow.OperationAction.ACK:
            pending = None
            replies = ()
            read_observed = False
            released = False
            if current.pending_delivery is None:
                raise TypeError("ack fixture lacks a delivery")
            kind = current.pending_delivery.ordered_event_projection[0].kind
            if kind is workflow.EventProjectionKind.QUESTION:
                state = workflow.CheckpointState.WAITING
            else:
                assignment = None
                state = workflow.CheckpointState.IDLE
        elif action is workflow.OperationAction.STOP:
            assignment = None
            pending = None
            replies = ()
            read_observed = False
            released = False
            state = workflow.CheckpointState.STOPPED
        return workflow.WorkflowCheckpointDraft(
            root=current.root,
            run=current.run,
            workflow_sequence=current.workflow_sequence + 2,
            task_sequence=current.task_sequence,
            execution_mode=current.execution_mode,
            workflow_state=state,
            task_policy=current.task_policy,
            active_assignment=assignment,
            pending_delivery=pending,
            replied_message_ids=replies,
            read_observed=read_observed,
            released=released,
            review_authority=current.review_authority,
            verification_authority=current.verification_authority,
            last_operation=workflow.LastOperation(
                operation_id=intent.operation_id,
                effect_key=intent.effect_key,
                action=intent.action,
                request_digest=intent.request_digest,
                expected_workflow_sequence=intent.expected_workflow_sequence,
                expected_task_sequence=intent.expected_task_sequence,
                status=workflow.OperationStatus.COMMITTED,
                receipt_id=receipt.receipt_id,
                receipt_digest=workflow.durable_receipt_digest(receipt),
            ),
        )

    def _commit_action(
        self,
        store: CoordinationStore,
        current: workflow.WorkflowCheckpointV4,
        intent: workflow.OperationIntent,
        *,
        receipt_id: str,
        probe: _EffectProbe | None = None,
    ) -> tuple[
        workflow.OperationHandle,
        workflow.DurableReceipt,
        workflow.WorkflowCheckpointDraft,
        workflow.WorkflowCheckpointV4,
    ]:
        begun = store.begin_operation(
            intent,
            expected_workflow_sequence=current.workflow_sequence,
            expected_task_sequence=current.task_sequence,
        )
        self.assertIsInstance(begun, workflow.OperationBegin)
        assert isinstance(begun, workflow.OperationBegin)
        if probe is not None:
            probe.execute()
        receipt = _receipt(
            store,
            begun.operation,
            current,
            receipt_id=receipt_id,
            delivery_id=intent.delivery_id,
            message_id=intent.message_id,
        )
        draft = self._action_draft(current, intent, receipt)
        committed = store.commit_effect(begun.operation, receipt, draft)
        self.assertIsInstance(committed, workflow.WorkflowCommit)
        assert isinstance(committed, workflow.WorkflowCommit)
        return begun.operation, receipt, draft, committed.checkpoint

    def _assert_projection(
        self,
        store: CoordinationStore,
        root: workflow.RootIdentity,
        expected: workflow.WorkflowCheckpointDraft,
        actual: workflow.WorkflowCheckpointV4,
    ) -> None:
        for field_name in (
            "root",
            "run",
            "workflow_sequence",
            "task_sequence",
            "execution_mode",
            "workflow_state",
            "task_policy",
            "active_assignment",
            "pending_delivery",
            "replied_message_ids",
            "read_observed",
            "released",
            "review_authority",
            "verification_authority",
            "last_operation",
        ):
            self.assertEqual(
                getattr(expected, field_name),
                getattr(actual, field_name),
                field_name,
            )
        self.assertEqual(4, actual.checkpoint_version)
        self.assertEqual(3, actual.store_schema)
        self.assertEqual(
            actual.checkpoint_digest,
            workflow.compute_checkpoint_digest(workflow.encode_checkpoint(actual)),
        )
        self.assertEqual(100, actual.updated_ns)
        loaded = store.load_checkpoint(workflow.WorkflowRootKey(root.root_key))
        self.assertEqual(actual, loaded)

    def _assert_replay(
        self,
        store: CoordinationStore,
        state_root: Path,
        intent: workflow.OperationIntent,
        handle: workflow.OperationHandle,
        receipt: workflow.DurableReceipt,
        draft: workflow.WorkflowCheckpointDraft,
        checkpoint: workflow.WorkflowCheckpointV4,
        probe: _EffectProbe,
    ) -> None:
        before = _db_rows(state_root)
        replay = store.begin_operation(
            intent,
            expected_workflow_sequence=intent.expected_workflow_sequence,
            expected_task_sequence=intent.expected_task_sequence,
        )
        self.assertIsInstance(replay, workflow.StoredReplay)
        assert isinstance(replay, workflow.StoredReplay)
        self.assertEqual(intent.operation_id, replay.operation_id)
        self.assertEqual(receipt.receipt_id, replay.receipt.receipt_id)
        self.assertEqual(
            workflow.durable_receipt_digest(receipt),
            workflow.durable_receipt_digest(replay.receipt),
        )
        self.assertEqual(checkpoint, replay.checkpoint)
        commit_replay = store.commit_effect(handle, receipt, draft)
        self.assertIsInstance(commit_replay, workflow.StoredReplay)
        assert isinstance(commit_replay, workflow.StoredReplay)
        self.assertEqual(checkpoint, commit_replay.checkpoint)
        self.assertEqual(before, _db_rows(state_root))
        self.assertEqual(1, probe.execute_calls)
        self.assertEqual(0, probe.retry_calls)

    def test_reply_commit_projection_and_exact_duplicate_replay(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_wait(store, root, outcome=None)
                intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.REPLY,
                    operation_id="operation-reply",
                    message_id="message-1",
                )
                probe = _EffectProbe()
                handle, receipt, draft, checkpoint = self._commit_action(
                    store,
                    current,
                    intent,
                    receipt_id="receipt-reply",
                    probe=probe,
                )
                self._assert_projection(store, root, draft, checkpoint)
                self._assert_replay(
                    store, state_root, intent, handle, receipt, draft, checkpoint, probe
                )
                self.assertEqual(("message-1",), checkpoint.replied_message_ids)
                self.assertIs(
                    workflow.CheckpointState.QUESTION, checkpoint.workflow_state
                )

    def test_committed_lifecycle_begin_replays_after_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-reopen-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            probe = _EffectProbe()
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_wait(store, root, outcome=None)
                intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.REPLY,
                    operation_id="operation-reply-reopen",
                    message_id="message-1",
                )
                _, receipt, _, checkpoint = self._commit_action(
                    store,
                    current,
                    intent,
                    receipt_id="receipt-reply-reopen",
                    probe=probe,
                )
            with CoordinationStore(state_root) as reopened:
                replay = reopened.begin_operation(
                    intent,
                    expected_workflow_sequence=intent.expected_workflow_sequence,
                    expected_task_sequence=intent.expected_task_sequence,
                )
                self.assertIsInstance(replay, workflow.StoredReplay)
                assert isinstance(replay, workflow.StoredReplay)
                self.assertEqual(checkpoint, replay.checkpoint)
                self.assertEqual(
                    workflow.durable_receipt_digest(receipt),
                    workflow.durable_receipt_digest(replay.receipt),
                )
            self.assertEqual(1, probe.execute_calls)
            self.assertEqual(0, probe.retry_calls)

    def test_read_commit_projection_and_exact_duplicate_replay(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_wait(
                    store, root, outcome=workflow.EventOutcome.SUCCEEDED
                )
                intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.READ,
                    operation_id="operation-read",
                )
                probe = _EffectProbe()
                handle, receipt, draft, checkpoint = self._commit_action(
                    store,
                    current,
                    intent,
                    receipt_id="receipt-read",
                    probe=probe,
                )
                self._assert_projection(store, root, draft, checkpoint)
                self._assert_replay(
                    store, state_root, intent, handle, receipt, draft, checkpoint, probe
                )
                self.assertTrue(checkpoint.read_observed)
                self.assertFalse(checkpoint.released)

    def test_release_commit_projection_and_exact_duplicate_replay(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_wait(
                    store, root, outcome=workflow.EventOutcome.SUCCEEDED
                )
                read_intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.READ,
                    operation_id="operation-read-before-release",
                )
                _, _, _, current = self._commit_action(
                    store,
                    current,
                    read_intent,
                    receipt_id="receipt-read-before-release",
                )
                intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.RELEASE,
                    operation_id="operation-release",
                )
                probe = _EffectProbe()
                handle, receipt, draft, checkpoint = self._commit_action(
                    store,
                    current,
                    intent,
                    receipt_id="receipt-release",
                    probe=probe,
                )
                self._assert_projection(store, root, draft, checkpoint)
                self._assert_replay(
                    store, state_root, intent, handle, receipt, draft, checkpoint, probe
                )
                self.assertIs(
                    workflow.CheckpointState.AWAITING_ACK, checkpoint.workflow_state
                )
                self.assertTrue(checkpoint.read_observed)
                self.assertTrue(checkpoint.released)
                self.assertIsNotNone(checkpoint.pending_delivery)

    def test_question_ack_commit_projection_and_exact_duplicate_replay(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_wait(store, root, outcome=None)
                reply_intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.REPLY,
                    operation_id="operation-reply-before-ack",
                    message_id="message-1",
                )
                _, _, _, current = self._commit_action(
                    store,
                    current,
                    reply_intent,
                    receipt_id="receipt-reply-before-ack",
                )
                intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.ACK,
                    operation_id="operation-ack-question",
                )
                probe = _EffectProbe()
                handle, receipt, draft, checkpoint = self._commit_action(
                    store,
                    current,
                    intent,
                    receipt_id="receipt-ack-question",
                    probe=probe,
                )
                self._assert_projection(store, root, draft, checkpoint)
                self._assert_replay(
                    store, state_root, intent, handle, receipt, draft, checkpoint, probe
                )
                self.assertIs(
                    workflow.CheckpointState.WAITING, checkpoint.workflow_state
                )
                self.assertIsNone(checkpoint.pending_delivery)
                self.assertIsNotNone(checkpoint.active_assignment)

    def test_worker_ack_commit_releases_assignment_and_replays_exactly(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_wait(
                    store, root, outcome=workflow.EventOutcome.SUCCEEDED
                )
                read_intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.READ,
                    operation_id="operation-read-before-worker-ack",
                )
                _, _, _, current = self._commit_action(
                    store,
                    current,
                    read_intent,
                    receipt_id="receipt-read-before-worker-ack",
                )
                release_intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.RELEASE,
                    operation_id="operation-release-before-worker-ack",
                )
                _, _, _, current = self._commit_action(
                    store,
                    current,
                    release_intent,
                    receipt_id="receipt-release-before-worker-ack",
                )
                intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.ACK,
                    operation_id="operation-ack-worker",
                )
                probe = _EffectProbe()
                handle, receipt, draft, checkpoint = self._commit_action(
                    store,
                    current,
                    intent,
                    receipt_id="receipt-ack-worker",
                    probe=probe,
                )
                self._assert_projection(store, root, draft, checkpoint)
                self._assert_replay(
                    store, state_root, intent, handle, receipt, draft, checkpoint, probe
                )
                self.assertIs(workflow.CheckpointState.IDLE, checkpoint.workflow_state)
                self.assertIsNone(checkpoint.active_assignment)
                self.assertIsNone(checkpoint.pending_delivery)

    def test_stop_commit_projection_and_exact_duplicate_replay(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_start(store, root)
                intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.STOP,
                    operation_id="operation-stop",
                )
                probe = _EffectProbe()
                handle, receipt, draft, checkpoint = self._commit_action(
                    store,
                    current,
                    intent,
                    receipt_id="receipt-stop",
                    probe=probe,
                )
                self._assert_projection(store, root, draft, checkpoint)
                self._assert_replay(
                    store, state_root, intent, handle, receipt, draft, checkpoint, probe
                )
                self.assertIs(
                    workflow.CheckpointState.STOPPED, checkpoint.workflow_state
                )
                self.assertIsNone(checkpoint.active_assignment)
                self.assertIsNone(checkpoint.pending_delivery)

    def _prepare_mismatch(
        self,
        store: CoordinationStore,
        root: workflow.RootIdentity,
        action: workflow.OperationAction,
    ) -> tuple[
        workflow.WorkflowCheckpointV4,
        workflow.OperationIntent,
        workflow.OperationHandle,
        workflow.DurableReceipt,
        workflow.WorkflowCheckpointDraft,
        workflow.WorkflowCheckpointDraft,
    ]:
        if action is workflow.OperationAction.STOP:
            current = self._bootstrap_start(store, root)
        else:
            current = self._bootstrap_wait(
                store,
                root,
                outcome=(
                    None
                    if action
                    in (workflow.OperationAction.REPLY, workflow.OperationAction.ACK)
                    else workflow.EventOutcome.SUCCEEDED
                ),
            )
        if action is workflow.OperationAction.REPLY:
            message_id = "message-1"
        else:
            message_id = None
        if action is workflow.OperationAction.ACK:
            reply_intent = _action_intent(
                root,
                current,
                workflow.OperationAction.REPLY,
                operation_id="operation-reply-before-mismatch-ack",
                message_id="message-1",
            )
            _, _, _, current = self._commit_action(
                store,
                current,
                reply_intent,
                receipt_id="receipt-reply-before-mismatch-ack",
            )
        if action is workflow.OperationAction.RELEASE:
            read_intent = _action_intent(
                root,
                current,
                workflow.OperationAction.READ,
                operation_id="operation-read-before-mismatch-release",
            )
            _, _, _, current = self._commit_action(
                store,
                current,
                read_intent,
                receipt_id="receipt-read-before-mismatch-release",
            )
        intent = _action_intent(
            root,
            current,
            action,
            operation_id=f"operation-mismatch-{action.value}",
            message_id=message_id,
        )
        begun = store.begin_operation(
            intent,
            expected_workflow_sequence=current.workflow_sequence,
            expected_task_sequence=current.task_sequence,
        )
        self.assertIsInstance(begun, workflow.OperationBegin)
        assert isinstance(begun, workflow.OperationBegin)
        receipt = _receipt(
            store,
            begun.operation,
            current,
            receipt_id=f"receipt-mismatch-{action.value}",
            delivery_id=intent.delivery_id,
            message_id=intent.message_id,
        )
        valid = self._action_draft(current, intent, receipt)
        if action is workflow.OperationAction.REPLY:
            invalid = replace(valid, replied_message_ids=())
        elif action is workflow.OperationAction.READ:
            invalid = replace(valid, read_observed=False)
        elif action is workflow.OperationAction.RELEASE:
            invalid = replace(
                valid,
                workflow_state=workflow.CheckpointState.WORKER_DONE,
                released=False,
            )
        elif action is workflow.OperationAction.ACK:
            invalid = replace(valid, pending_delivery=current.pending_delivery)
        else:
            invalid = replace(valid, workflow_state=workflow.CheckpointState.IDLE)
        return current, intent, begun.operation, receipt, valid, invalid

    def test_post_effect_projection_mismatch_is_unknown_for_every_lifecycle_action(
        self,
    ) -> None:
        for action in (
            workflow.OperationAction.REPLY,
            workflow.OperationAction.READ,
            workflow.OperationAction.RELEASE,
            workflow.OperationAction.ACK,
            workflow.OperationAction.STOP,
        ):
            with (
                self.subTest(action=action.value),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-workflow-lifecycle-mismatch-"
                ) as temporary,
            ):
                state_root = _state_root(temporary)
                root = _root(temporary, state_root)
                with CoordinationStore(state_root, clock=lambda: 100) as store:
                    _, intent, handle, receipt, _, invalid = self._prepare_mismatch(
                        store, root, action
                    )
                    probe = _EffectProbe()
                    probe.execute()
                    with self.assertRaises(workflow.RecoveryRequired):
                        store.commit_effect(handle, receipt, invalid)
                    checkpoint = store.load_checkpoint(
                        workflow.WorkflowRootKey(root.root_key)
                    )
                    self.assertIsInstance(checkpoint, workflow.WorkflowCheckpointV4)
                    assert isinstance(checkpoint, workflow.WorkflowCheckpointV4)
                    self.assertIs(
                        workflow.CheckpointState.RECOVERY_REQUIRED,
                        checkpoint.workflow_state,
                    )
                    self.assertIsNotNone(checkpoint.last_operation)
                    assert checkpoint.last_operation is not None
                    self.assertIs(
                        workflow.OperationStatus.UNKNOWN_EFFECT,
                        checkpoint.last_operation.status,
                    )
                    with closing(
                        sqlite3.connect(str(state_root / "coordination.sqlite3"))
                    ) as connection:
                        status = connection.execute(
                            "SELECT status FROM workflow_operations WHERE operation_id = ?",
                            (intent.operation_id,),
                        ).fetchone()[0]
                        receipt_count = connection.execute(
                            "SELECT COUNT(*) FROM workflow_receipts WHERE operation_id = ?",
                            (intent.operation_id,),
                        ).fetchone()[0]
                    self.assertEqual("UNKNOWN_EFFECT", status)
                    self.assertEqual(0, receipt_count)
                    self.assertEqual(1, probe.execute_calls)
                    self.assertEqual(0, probe.retry_calls)
                    with self.assertRaises(workflow.RecoveryRequired):
                        store.begin_operation(
                            intent,
                            expected_workflow_sequence=intent.expected_workflow_sequence,
                            expected_task_sequence=intent.expected_task_sequence,
                        )

    def test_ack_begin_has_one_fixed_barrier_cas_winner(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-lifecycle-ack-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                current = self._bootstrap_wait(
                    store, root, outcome=workflow.EventOutcome.SUCCEEDED
                )
                read_intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.READ,
                    operation_id="operation-read-before-ack-race",
                )
                _, _, _, current = self._commit_action(
                    store,
                    current,
                    read_intent,
                    receipt_id="receipt-read-before-ack-race",
                )
                release_intent = _action_intent(
                    root,
                    current,
                    workflow.OperationAction.RELEASE,
                    operation_id="operation-release-before-ack-race",
                )
                self._commit_action(
                    store,
                    current,
                    release_intent,
                    receipt_id="receipt-release-before-ack-race",
                )
            barrier = context.Barrier(2)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_ack_begin_worker,
                    args=(
                        str(state_root),
                        root,
                        f"operation-ack-race-{index}",
                        barrier,
                        result_queue,
                    ),
                )
                for index in (1, 2)
            ]
            for process in processes:
                process.start()
            results = [result_queue.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join()
            statuses = [status for status, _ in results]
            self.assertEqual(["conflict", "winner"], sorted(statuses))
            winner = next(
                operation_id for status, operation_id in results if status == "winner"
            )
            with CoordinationStore(state_root) as reopened:
                checkpoint = reopened.load_checkpoint(
                    workflow.WorkflowRootKey(root.root_key)
                )
                self.assertIsInstance(checkpoint, workflow.WorkflowCheckpointV4)
                assert isinstance(checkpoint, workflow.WorkflowCheckpointV4)
                self.assertIsNotNone(checkpoint.pending_delivery)
                assert checkpoint.pending_delivery is not None
                self.assertIs(
                    workflow.AckStatus.ACK_INTENT,
                    checkpoint.pending_delivery.ack_status,
                )
                self.assertEqual(winner, checkpoint.pending_delivery.ack_operation_id)
            with closing(
                sqlite3.connect(str(state_root / "coordination.sqlite3"))
            ) as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM workflow_operations WHERE action = 'ack'"
                    ).fetchone()[0],
                )


if __name__ == "__main__":
    unittest.main()
