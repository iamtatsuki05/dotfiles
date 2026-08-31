"""Transaction tests for the durable workflow Store boundary.

The workflow value/codec tests live in ``test_workflow_store_contract.py``.
This module deliberately exercises the opaque facade on ``CoordinationStore``:
the operation, receipt, checkpoint, and journal rows must move together under
the Store's existing lifetime gate and SQLite transaction.

The receipt helper used below is a private production seam.  Tests must not
construct ``DurableReceipt`` directly or use the pure module's issuer helper;
the Store is the authority that binds a receipt to its local issuer registry.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from test_lease_provider import FakeClock, FakeProvider

from agent_team import workflow_store as workflow
from agent_team.doctor import ReadOnlyDoctor
from agent_team.store import (
    CoordinationStore,
    StoreBusyError,
    StoreIntegrityError,
)

_DIGEST_1 = "sha256:" + "1" * 64
_DIGEST_2 = "sha256:" + "2" * 64
_DIGEST_3 = "sha256:" + "3" * 64
_DIGEST_4 = "sha256:" + "4" * 64


def _database(state_root: Path) -> Path:
    return state_root / "coordination.sqlite3"


def _counts(state_root: Path) -> dict[str, int]:
    connection = sqlite3.connect(str(_database(state_root)))
    try:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in (
                "workflow_checkpoints",
                "workflow_operations",
                "workflow_receipts",
                "workflow_events",
            )
        }
    finally:
        connection.close()


def _workflow_rows(state_root: Path) -> dict[str, tuple[Any, ...]]:
    connection = sqlite3.connect(str(_database(state_root)))
    try:
        checkpoint = connection.execute(
            "SELECT root_key, run_id, main_terminal_id, workflow_sequence, "
            "workflow_state, checkpoint_bytes, checkpoint_digest, "
            "last_operation_id, last_operation_status, last_operation_receipt_id "
            "FROM workflow_checkpoints"
        ).fetchone()
        operation = connection.execute(
            "SELECT operation_id, effect_key, action, expected_workflow_sequence, "
            "expected_task_sequence, intent_sequence, run_id, main_terminal_id, "
            "status, receipt_id, intent_digest, receipt_digest "
            "FROM workflow_operations"
        ).fetchone()
        receipt = connection.execute(
            "SELECT receipt_id, operation_id, effect_key, action, request_digest, "
            "run_id, main_terminal_id, issued_ns FROM workflow_receipts"
        ).fetchone()
        events = tuple(
            connection.execute(
                "SELECT workflow_event_id, operation_id, workflow_sequence, "
                "task_sequence_before, task_sequence_after, from_state, to_state, "
                "kind, receipt_id, checkpoint_digest "
                "FROM workflow_events ORDER BY workflow_event_id"
            ).fetchall()
        )
        return {
            "checkpoint": tuple(checkpoint) if checkpoint is not None else (),
            "operation": tuple(operation) if operation is not None else (),
            "receipt": tuple(receipt) if receipt is not None else (),
            "events": events,
        }
    finally:
        connection.close()


def _operation_status(state_root: Path, operation_id: str) -> str | None:
    connection = sqlite3.connect(str(_database(state_root)))
    try:
        row = connection.execute(
            "SELECT status FROM workflow_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        connection.close()


def _make_state_root(parent: str, name: str = "state") -> Path:
    state_root = Path(os.path.realpath(parent)) / name
    state_root.mkdir()
    state_root.chmod(0o700)
    return state_root


def _make_root(state_root: Path, parent: str) -> workflow.RootIdentity:
    workspace = Path(parent) / "workspace"
    workspace.mkdir()
    workspace.chmod(0o700)
    config = workspace / "config.toml"
    config_bytes = b"[team]\nid = 'team-1'\n"
    config.write_bytes(config_bytes)
    config.chmod(0o600)
    workspace_stat = workspace.stat()
    config_stat = config.stat()
    state_stat = state_root.stat()
    return workflow.RootIdentity(
        root_key="root-1",
        team_id="team-1",
        workspace=workflow.PathIdentity(
            path=str(workspace),
            device=int(workspace_stat.st_dev),
            inode=int(workspace_stat.st_ino),
        ),
        config_path=str(config),
        config_device=int(config_stat.st_dev),
        config_inode=int(config_stat.st_ino),
        config_digest=workflow.config_content_digest(config_bytes),
        state_root=workflow.PathIdentity(
            path=str(state_root),
            device=int(state_stat.st_dev),
            inode=int(state_stat.st_ino),
        ),
    )


def _start_intent(root: workflow.RootIdentity) -> workflow.OperationIntent:
    return workflow.OperationIntent(
        operation_id="operation-start",
        effect_key="effect/start",
        root_key=root.root_key,
        root=root,
        action=workflow.OperationAction.START,
        request_digest=workflow.digest_bounded_body(
            b"start-request", domain=workflow.REQUEST_DIGEST_DOMAIN
        ),
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


def _prompt_intent(
    root: workflow.RootIdentity,
    *,
    operation_id: str = "operation-prompt",
    effect_key: str = "effect/prompt",
    expected_workflow_sequence: int = 2,
    expected_task_sequence: int | None = None,
    run_id: str = "run-1",
    main_terminal_id: str = "terminal-main",
    owner: str = "owner-1",
) -> workflow.OperationIntent:
    return workflow.OperationIntent(
        operation_id=operation_id,
        effect_key=effect_key,
        root_key=root.root_key,
        root=None,
        action=workflow.OperationAction.PROMPT,
        request_digest=workflow.digest_bounded_body(
            operation_id.encode("ascii"), domain=workflow.REQUEST_DIGEST_DOMAIN
        ),
        expected_workflow_sequence=expected_workflow_sequence,
        expected_task_sequence=expected_task_sequence,
        run_id=run_id,
        main_terminal_id=main_terminal_id,
        task_id=None,
        dispatch_id=None,
        attempt=None,
        terminal_id=None,
        delivery_id=None,
        message_id=None,
        consumer_generation=0,
        owner=owner,
        lease_epoch=0,
        fencing_token=0,
        actor=owner,
        evidence_ref=None,
        next_task_sequence=1 if expected_task_sequence is None else None,
    )


def _receipt(
    store: CoordinationStore,
    operation: workflow.OperationHandle,
    *,
    receipt_id: str,
    run_id: str = "run-1",
    main_terminal_id: str = "terminal-main",
    issued_ns: int = 20,
    result_kind: str = "started",
    assignment: bool = False,
) -> workflow.DurableReceipt:
    """Issue a genuine receipt through the Store-owned private seam.

    ``_issue_workflow_receipt`` is intentionally a narrow test/composition
    seam.  It is not a fallback to the pure value module: a receipt produced by
    another Store instance must fail the production provenance check.
    """

    assignment_value = (
        workflow.ActiveAssignment(
            role=workflow.AssignmentRole.WORKER,
            worker_node="worker-node-1",
            task_id="task-1",
            attempt=1,
            dispatch_id="dispatch-1",
            terminal_id="terminal-worker",
            launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
            completion_identity=workflow.CompletionIdentity(
                run_id=run_id,
                task_id="task-1",
                dispatch_id="dispatch-1",
                sender_terminal_id="terminal-worker",
            ),
        )
        if assignment
        else None
    )
    return store._issue_workflow_receipt(
        operation=operation,
        receipt_id=receipt_id,
        run_id=run_id,
        main_terminal_id=main_terminal_id,
        task_id=None if assignment_value is None else assignment_value.task_id,
        dispatch_id=(
            None if assignment_value is None else assignment_value.dispatch_id
        ),
        attempt=None if assignment_value is None else assignment_value.attempt,
        terminal_id=(
            None if assignment_value is None else assignment_value.terminal_id
        ),
        delivery_id=None,
        message_id=None,
        effect_ref=f"backend/{receipt_id}",
        result_kind=result_kind,
        result_digest=(
            _DIGEST_2
            if assignment_value is None
            else workflow.assignment_digest(assignment_value)
        ),
        evidence_ref=_DIGEST_3,
        issued_ns=issued_ns,
    )


def _start_draft(
    root: workflow.RootIdentity,
    receipt: workflow.DurableReceipt,
    *,
    run_id: str = "run-1",
    main_terminal_id: str = "terminal-main",
) -> workflow.WorkflowCheckpointDraft:
    return workflow.WorkflowCheckpointDraft(
        root=root,
        run=workflow.RunIdentity(
            run_id=run_id,
            main_terminal_id=main_terminal_id,
            consumer_generation=0,
        ),
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


def _open_started_store(
    state_root: Path, root: workflow.RootIdentity
) -> tuple[workflow.OperationHandle, workflow.WorkflowCheckpointV4]:
    """Create the common committed-start fixture through a closed Store."""

    with CoordinationStore(state_root) as store:
        started = store.begin_operation(
            _start_intent(root),
            expected_workflow_sequence=0,
            expected_task_sequence=None,
        )
        if not isinstance(started, workflow.OperationBegin):
            raise TypeError("fresh start unexpectedly returned a replay")
        start_receipt = _receipt(store, started.operation, receipt_id="receipt-start")
        committed = store.commit_effect(
            started.operation,
            start_receipt,
            _start_draft(root, start_receipt),
        )
        if not isinstance(committed, workflow.WorkflowCommit):
            raise TypeError("fresh start unexpectedly returned a replay")
        return started.operation, committed.checkpoint


def _fault_store_class(target: str) -> type[CoordinationStore]:
    class FaultStore(CoordinationStore):
        def _fault(self, point: str) -> None:
            if point == target:
                os.kill(os.getpid(), signal.SIGKILL)

    return FaultStore


class _BlockingWorkflowStore(CoordinationStore):
    def __init__(
        self,
        state_root: Path,
        ready: Any,
        release: Any,
    ) -> None:
        self._workflow_ready = ready
        self._workflow_release = release
        super().__init__(state_root, busy_timeout_ms=1000)

    def _fault(self, point: str) -> None:
        if point == "before_workflow_operation_insert":
            self._workflow_ready.set()
            if not self._workflow_release.wait(timeout=10):
                raise RuntimeError("workflow blocker release timed out")


def _kill_begin_worker(
    state_root: str,
    root: workflow.RootIdentity,
    target: str,
) -> None:
    fault_store = _fault_store_class(target)
    with fault_store(Path(state_root)) as store:
        store.begin_operation(
            _start_intent(root),
            expected_workflow_sequence=0,
            expected_task_sequence=None,
        )


def _kill_commit_worker(
    state_root: str,
    root: workflow.RootIdentity,
    target: str,
) -> None:
    fault_store = _fault_store_class(target)
    with fault_store(Path(state_root)) as store:
        begun = store.begin_operation(
            _prompt_intent(root),
            expected_workflow_sequence=2,
            expected_task_sequence=None,
        )
        if not isinstance(begun, workflow.OperationBegin):
            raise TypeError("prompt unexpectedly returned a replay")
        receipt = _receipt(
            store,
            begun.operation,
            receipt_id="receipt-prompt",
            assignment=True,
        )
        store.commit_effect(
            begun.operation,
            receipt,
            _prompt_draft(root, receipt),
        )


def _kill_unknown_worker(
    state_root: str,
    root: workflow.RootIdentity,
    target: str,
) -> None:
    fault_store = _fault_store_class(target)
    with fault_store(Path(state_root)) as store:
        begun = store.begin_operation(
            _prompt_intent(root),
            expected_workflow_sequence=2,
            expected_task_sequence=None,
        )
        if not isinstance(begun, workflow.OperationBegin):
            raise TypeError("prompt unexpectedly returned a replay")
        store.mark_unknown(
            begun.operation,
            reason=workflow.RecoveryCode.RESPONSE_LOST,
        )


def _blocking_begin_worker(
    state_root: str,
    root: workflow.RootIdentity,
    ready: Any,
    release: Any,
    result_queue: Any,
) -> None:
    try:
        with _BlockingWorkflowStore(Path(state_root), ready, release) as store:
            begun = store.begin_operation(
                _prompt_intent(root),
                expected_workflow_sequence=2,
                expected_task_sequence=None,
            )
            result_queue.put(
                "ok" if isinstance(begun, workflow.OperationBegin) else "replay"
            )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        result_queue.put(f"error:{type(exc).__name__}:{exc}")


def _concurrent_begin_worker(
    state_root: str,
    root: workflow.RootIdentity,
    owner: str,
    operation_id: str,
    barrier: Any,
    result_queue: Any,
) -> None:
    try:
        with CoordinationStore(Path(state_root), busy_timeout_ms=1000) as store:
            barrier.wait(timeout=10)
            begun = store.begin_operation(
                _prompt_intent(
                    root,
                    operation_id=operation_id,
                    effect_key=f"effect/{operation_id}",
                    owner=owner,
                ),
                expected_workflow_sequence=2,
                expected_task_sequence=None,
            )
            result_queue.put(
                "winner" if isinstance(begun, workflow.OperationBegin) else "replay"
            )
    except workflow.StateConflict:
        result_queue.put("conflict")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        result_queue.put(f"error:{type(exc).__name__}:{exc}")


def _prompt_draft(
    root: workflow.RootIdentity,
    receipt: workflow.DurableReceipt,
    *,
    workflow_sequence: int = 4,
    state: workflow.CheckpointState = workflow.CheckpointState.ACTIVE,
) -> workflow.WorkflowCheckpointDraft:
    assignment = workflow.ActiveAssignment(
        role=workflow.AssignmentRole.WORKER,
        worker_node="worker-node-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-1",
        terminal_id="terminal-worker",
        launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
        completion_identity=workflow.CompletionIdentity(
            run_id="run-1",
            task_id="task-1",
            dispatch_id="dispatch-1",
            sender_terminal_id="terminal-worker",
        ),
    )
    return workflow.WorkflowCheckpointDraft(
        root=root,
        run=workflow.RunIdentity("run-1", "terminal-main", 0),
        workflow_sequence=workflow_sequence,
        task_sequence=1,
        execution_mode=workflow.ExecutionMode.SERIAL,
        workflow_state=state,
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


def _commit_prompt(
    store: CoordinationStore,
    root: workflow.RootIdentity,
    started: workflow.WorkflowCheckpointV4,
) -> workflow.WorkflowCheckpointV4:
    begun = store.begin_operation(
        _prompt_intent(
            root,
            expected_workflow_sequence=started.workflow_sequence,
            expected_task_sequence=started.task_sequence,
        ),
        expected_workflow_sequence=started.workflow_sequence,
        expected_task_sequence=started.task_sequence,
    )
    if not isinstance(begun, workflow.OperationBegin):
        raise TypeError("prompt unexpectedly returned a replay")
    receipt = _receipt(
        store,
        begun.operation,
        receipt_id="receipt-prompt",
        assignment=True,
    )
    committed = store.commit_effect(
        begun.operation,
        receipt,
        _prompt_draft(root, receipt),
    )
    if not isinstance(committed, workflow.WorkflowCommit):
        raise TypeError("prompt unexpectedly returned a replay")
    return committed.checkpoint


def _wait_intent(
    root: workflow.RootIdentity,
    current: workflow.WorkflowCheckpointV4,
) -> workflow.OperationIntent:
    assignment = current.active_assignment
    if assignment is None:
        raise TypeError("wait fixture requires an assignment")
    return workflow.OperationIntent(
        operation_id="operation-wait",
        effect_key="effect/wait",
        root_key=root.root_key,
        root=None,
        action=workflow.OperationAction.WAIT,
        request_digest=workflow.digest_bounded_body(
            b"wait-request", domain=workflow.REQUEST_DIGEST_DOMAIN
        ),
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
    ack_status: workflow.AckStatus = workflow.AckStatus.PENDING,
    ack_operation_id: str | None = None,
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
        ack_operation_id=ack_operation_id,
        ack_status=ack_status,
    )


def _wait_receipt(
    store: CoordinationStore,
    operation: workflow.OperationHandle,
    current: workflow.WorkflowCheckpointV4,
    delivery: workflow.PendingDelivery,
) -> workflow.DurableReceipt:
    assignment = current.active_assignment
    if assignment is None:
        raise TypeError("wait fixture requires an assignment")
    return store._issue_workflow_receipt(
        operation=operation,
        receipt_id="receipt-wait",
        run_id=current.run.run_id,
        main_terminal_id=current.run.main_terminal_id,
        task_id=assignment.task_id,
        dispatch_id=assignment.dispatch_id,
        attempt=assignment.attempt,
        terminal_id=assignment.terminal_id,
        delivery_id=delivery.delivery_id,
        message_id=(
            None
            if not delivery.ordered_message_ids
            else delivery.ordered_message_ids[0]
        ),
        effect_ref="backend/receipt-wait",
        result_kind="delivery",
        result_digest=delivery.delivery_digest,
        evidence_ref=_DIGEST_3,
        issued_ns=30,
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
        review_authority=current.review_authority,
        verification_authority=current.verification_authority,
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


class WorkflowStoreTransactionTests(unittest.TestCase):
    def _fixture(
        self,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, workflow.RootIdentity]:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-workflow-tx-")
        state_root = _make_state_root(temporary.name)
        root = _make_root(state_root, temporary.name)
        return temporary, state_root, root

    def test_begin_start_atomically_persists_seed_intent_and_first_event(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                result = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(result, workflow.OperationBegin)
                assert isinstance(result, workflow.OperationBegin)
                self.assertEqual("operation-start", result.operation.operation_id)
                self.assertEqual(1, result.operation.intent_sequence)

                seed = store.load_checkpoint(workflow.WorkflowRootKey(root.root_key))
                self.assertIsInstance(seed, workflow.WorkflowRootSeed)
                assert isinstance(seed, workflow.WorkflowRootSeed)
                self.assertEqual(1, seed.workflow_sequence)
                self.assertEqual("operation-start", seed.operation_id)
                self.assertIs(
                    workflow.OperationStatus.INTENT,
                    seed.operation_status,
                )

                self.assertEqual(
                    {
                        "workflow_checkpoints": 1,
                        "workflow_operations": 1,
                        "workflow_receipts": 0,
                        "workflow_events": 1,
                    },
                    _counts(state_root),
                )
                rows = _workflow_rows(state_root)
                self.assertEqual("operation-start", rows["operation"][0])
                self.assertEqual("INTENT", rows["operation"][8])
                self.assertEqual(1, rows["operation"][5])
                self.assertEqual(1, rows["checkpoint"][3])
                self.assertEqual("STARTING", rows["checkpoint"][4])
                self.assertEqual(
                    seed.seed_digest,
                    rows["checkpoint"][6],
                )
                event = rows["events"][0]
                self.assertEqual("operation-start", event[1])
                self.assertEqual(1, event[2])
                self.assertEqual("start", event[7])
                self.assertEqual(seed.seed_digest, event[9])
        finally:
            temporary.cleanup()

    def test_doctor_reports_pending_workflow_intent_for_operator_review(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
            database_before = _database(state_root).read_bytes()
            report = ReadOnlyDoctor(
                marker_name="writer.marker",
                ledger_name="recovery.ledger",
            ).inspect(state_root, "operation-start")
            self.assertEqual("INTENT_ONLY", report.observed_state)
            self.assertEqual("HIGH", report.confidence)
            self.assertEqual("OPERATOR_REVIEW", report.safe_action)
            self.assertEqual(database_before, _database(state_root).read_bytes())
        finally:
            temporary.cleanup()

    def test_doctor_rejects_provider_workflow_operation_id_collision(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            provider = FakeProvider()
            clock = FakeClock(100)
            with CoordinationStore(state_root, clock=clock) as store:
                store.create_intent(
                    "operation-start",
                    effect_key="provider/effect-start",
                    provider_id="provider/test",
                    actor="provider-owner",
                    clock_ns=100,
                )
                claim = store.claim(
                    "operation-start",
                    owner="provider-owner",
                    provider_id="provider/test",
                    lease_ttl_ns=1_000,
                    now_ns=100,
                )
                claim = store.reserve_fence(claim, provider)
                provider_receipt = store.execute_effect(
                    claim,
                    provider,
                    now_ns=102,
                )
                store.complete(provider_receipt, now_ns=103)
                clock.set(104)
                store.begin_operation(
                    replace(
                        _start_intent(root),
                        lease_epoch=claim.lease_epoch,
                        fencing_token=claim.fencing_token,
                    ),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
            report = ReadOnlyDoctor(
                marker_name="writer.marker",
                ledger_name="recovery.ledger",
            ).inspect(state_root, "operation-start")
            self.assertEqual("UNREADABLE", report.observed_state)
            self.assertEqual("HIGH", report.confidence)
            self.assertEqual("OPERATOR_REVIEW", report.safe_action)
        finally:
            temporary.cleanup()

    def test_doctor_does_not_report_workflow_commit_as_provider_status(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _open_started_store(state_root, root)
            database_before = _database(state_root).read_bytes()
            report = ReadOnlyDoctor(
                marker_name="writer.marker",
                ledger_name="recovery.ledger",
            ).inspect(state_root, "operation-start")
            self.assertEqual("UNREADABLE", report.observed_state)
            self.assertEqual("HIGH", report.confidence)
            self.assertEqual("OPERATOR_REVIEW", report.safe_action)
            self.assertEqual(database_before, _database(state_root).read_bytes())
        finally:
            temporary.cleanup()

    def test_start_commit_atomically_binds_run_receipt_checkpoint_and_event(
        self,
    ) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(store, begun.operation, receipt_id="receipt-start")
                result = store.commit_effect(
                    begun.operation,
                    receipt,
                    _start_draft(root, receipt),
                )
                self.assertIsInstance(result, workflow.WorkflowCommit)
                assert isinstance(result, workflow.WorkflowCommit)
                self.assertEqual(2, result.checkpoint.workflow_sequence)
                self.assertEqual("run-1", result.checkpoint.run.run_id)
                self.assertEqual(
                    "terminal-main", result.checkpoint.run.main_terminal_id
                )
                self.assertEqual(receipt, result.receipt)

                checkpoint = store.load_checkpoint(
                    workflow.WorkflowRootKey(root.root_key)
                )
                self.assertIsInstance(checkpoint, workflow.WorkflowCheckpointV4)
                assert isinstance(checkpoint, workflow.WorkflowCheckpointV4)
                self.assertEqual(result.checkpoint, checkpoint)
                self.assertEqual(
                    {
                        "workflow_checkpoints": 1,
                        "workflow_operations": 1,
                        "workflow_receipts": 1,
                        "workflow_events": 2,
                    },
                    _counts(state_root),
                )
                rows = _workflow_rows(state_root)
                self.assertEqual("COMMITTED", rows["operation"][8])
                self.assertEqual("receipt-start", rows["operation"][9])
                self.assertEqual("receipt-start", rows["receipt"][0])
                self.assertEqual("run-1", rows["receipt"][5])
                self.assertEqual("terminal-main", rows["receipt"][6])
                self.assertEqual(2, rows["checkpoint"][3])
                self.assertEqual("IDLE", rows["checkpoint"][4])
                self.assertEqual("COMMITTED", rows["checkpoint"][8])
                self.assertEqual("receipt-start", rows["checkpoint"][9])
                self.assertEqual(
                    workflow.durable_receipt_digest(receipt), rows["operation"][11]
                )
                self.assertEqual(2, rows["events"][-1][2])
                self.assertEqual("receipt-start", rows["events"][-1][8])
                self.assertEqual(
                    result.checkpoint.checkpoint_digest,
                    rows["events"][-1][9],
                )
        finally:
            temporary.cleanup()

    def test_normal_begin_advances_workflow_only_and_preserves_task_projection(
        self,
    ) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as reopened:
                result = reopened.begin_operation(
                    _prompt_intent(root),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(result, workflow.OperationBegin)
                assert isinstance(result, workflow.OperationBegin)
                checkpoint = reopened.load_checkpoint(
                    workflow.WorkflowRootKey(root.root_key)
                )
                self.assertIsInstance(checkpoint, workflow.WorkflowCheckpointV4)
                assert isinstance(checkpoint, workflow.WorkflowCheckpointV4)
                self.assertEqual(3, checkpoint.workflow_sequence)
                self.assertIsNone(checkpoint.task_sequence)
                self.assertIsNone(checkpoint.task_policy)
                self.assertEqual(started.run, checkpoint.run)
                self.assertEqual(started.workflow_state, checkpoint.workflow_state)
                self.assertIsNotNone(checkpoint.last_operation)
                assert checkpoint.last_operation is not None
                self.assertEqual(
                    workflow.OperationStatus.INTENT,
                    checkpoint.last_operation.status,
                )
                self.assertEqual(
                    result.operation.operation_id,
                    checkpoint.last_operation.operation_id,
                )
                self.assertEqual(
                    {
                        "workflow_checkpoints": 1,
                        "workflow_operations": 2,
                        "workflow_receipts": 1,
                        "workflow_events": 3,
                    },
                    _counts(state_root),
                )
        finally:
            temporary.cleanup()

    def test_begin_rejects_stale_dual_cas_without_mutation(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as reopened:
                before = _workflow_rows(state_root)
                with self.assertRaises(workflow.StateConflict):
                    reopened.begin_operation(
                        _prompt_intent(
                            root,
                            operation_id="operation-stale",
                            effect_key="effect/stale",
                        ),
                        expected_workflow_sequence=started.workflow_sequence - 1,
                        expected_task_sequence=None,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_begin_rejects_action_state_generation_and_delivery_mismatch(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                base = _prompt_intent(root)
                candidates = (
                    replace(
                        base,
                        operation_id="operation-generation",
                        effect_key="effect/generation",
                        consumer_generation=1,
                    ),
                    replace(
                        base,
                        operation_id="operation-wait",
                        effect_key="effect/wait",
                        action=workflow.OperationAction.WAIT,
                        next_task_sequence=None,
                    ),
                    replace(
                        base,
                        operation_id="operation-reply",
                        effect_key="effect/reply",
                        action=workflow.OperationAction.REPLY,
                        delivery_id="delivery-foreign",
                        message_id="message-foreign",
                        next_task_sequence=None,
                    ),
                    replace(
                        base,
                        operation_id="operation-read",
                        effect_key="effect/read",
                        action=workflow.OperationAction.READ,
                        next_task_sequence=None,
                    ),
                    replace(
                        base,
                        operation_id="operation-release",
                        effect_key="effect/release",
                        action=workflow.OperationAction.RELEASE,
                        next_task_sequence=None,
                    ),
                    replace(
                        base,
                        operation_id="operation-ack",
                        effect_key="effect/ack",
                        action=workflow.OperationAction.ACK,
                        delivery_id="delivery-foreign",
                        next_task_sequence=None,
                    ),
                )
                before = _workflow_rows(state_root)
                for candidate in candidates:
                    with (
                        self.subTest(action=candidate.action.value),
                        self.assertRaises(
                            (
                                workflow.StateConflict,
                                workflow.OperationIdentityConflict,
                            )
                        ),
                    ):
                        store.begin_operation(
                            candidate,
                            expected_workflow_sequence=started.workflow_sequence,
                            expected_task_sequence=started.task_sequence,
                        )
                    self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_begin_rejects_stale_recovery_epoch_and_fencing_token(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                connection = store._connection
                assert connection is not None
                with store._write_transaction():
                    connection.execute(
                        "UPDATE store_meta SET value = 5 "
                        "WHERE key IN ('recovery_epoch', 'fencing_token_floor')"
                    )
                before = _workflow_rows(state_root)
                with self.assertRaises(workflow.OperationIdentityConflict):
                    store.begin_operation(
                        _prompt_intent(root),
                        expected_workflow_sequence=started.workflow_sequence,
                        expected_task_sequence=started.task_sequence,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_existing_image_rejects_inflight_generation_drift(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _prompt_intent(root),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
            forged = replace(_prompt_intent(root), consumer_generation=99)
            with closing(sqlite3.connect(str(_database(state_root)))) as connection:
                connection.execute(
                    "UPDATE workflow_operations SET consumer_generation = ?, "
                    "intent_digest = ? WHERE operation_id = ?",
                    (
                        99,
                        workflow.operation_intent_digest(
                            forged,
                            intent_sequence=3,
                        ),
                        forged.operation_id,
                    ),
                )
                connection.commit()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)
        finally:
            temporary.cleanup()

    def test_existing_image_rejects_operation_event_and_target_drift(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            forged_draft = replace(
                workflow.checkpoint_to_draft(started),
                workflow_state=workflow.CheckpointState.WAITING,
            )
            forged_checkpoint = workflow._issue_checkpoint(
                forged_draft,
                updated_ns=started.updated_ns,
                issuer=object(),
            )
            with closing(sqlite3.connect(str(_database(state_root)))) as connection:
                trigger_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'workflow_events_no_update'"
                    ).fetchone()[0]
                )
                connection.execute("DROP TRIGGER workflow_events_no_update")
                connection.execute(
                    "UPDATE workflow_events SET checkpoint_digest = ? "
                    "WHERE workflow_sequence = 1",
                    (_DIGEST_4,),
                )
                connection.execute(
                    "UPDATE workflow_events SET request_digest = ?, "
                    "evidence_ref = ?, to_state = ?, checkpoint_digest = ? "
                    "WHERE workflow_sequence = 2",
                    (
                        _DIGEST_4,
                        _DIGEST_4,
                        workflow.CheckpointState.WAITING.value,
                        forged_checkpoint.checkpoint_digest,
                    ),
                )
                connection.execute(
                    "UPDATE workflow_checkpoints SET workflow_state = ?, "
                    "checkpoint_bytes = ?, checkpoint_digest = ?",
                    (
                        workflow.CheckpointState.WAITING.value,
                        workflow.encode_checkpoint(forged_checkpoint),
                        forged_checkpoint.checkpoint_digest,
                    ),
                )
                connection.execute(trigger_sql)
                connection.commit()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)
        finally:
            temporary.cleanup()

    def test_existing_image_rejects_transition_actor_drift(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            authority = workflow.AuthorityReference("policy-ref", _DIGEST_1)
            with CoordinationStore(state_root) as store:
                transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority,
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                    next_task_sequence=started.task_sequence,
                    actor="policy-authority",
                    request_digest=_DIGEST_2,
                )
                store.commit_transition(
                    transition,
                    replace(
                        workflow.checkpoint_to_draft(started),
                        workflow_sequence=started.workflow_sequence + 1,
                        review_authority=authority,
                    ),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
            with closing(sqlite3.connect(str(_database(state_root)))) as connection:
                trigger_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'workflow_events_no_update'"
                    ).fetchone()[0]
                )
                connection.execute("DROP TRIGGER workflow_events_no_update")
                connection.execute(
                    "UPDATE workflow_events SET actor = 'forged-authority' "
                    "WHERE operation_id IS NULL"
                )
                connection.execute(trigger_sql)
                connection.commit()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)
        finally:
            temporary.cleanup()

    def test_existing_image_rejects_workflow_event_id_drift(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _open_started_store(state_root, root)
            with closing(sqlite3.connect(str(_database(state_root)))) as connection:
                trigger_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'workflow_events_no_update'"
                    ).fetchone()[0]
                )
                connection.execute("DROP TRIGGER workflow_events_no_update")
                connection.execute(
                    "UPDATE workflow_events "
                    "SET workflow_event_id = workflow_event_id + 100"
                )
                connection.execute(trigger_sql)
                connection.commit()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)
        finally:
            temporary.cleanup()

    def test_commit_effect_returns_stored_replay_for_exact_duplicate(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(store, begun.operation, receipt_id="receipt-start")
                draft = _start_draft(root, receipt)
                committed = store.commit_effect(begun.operation, receipt, draft)
                self.assertIsInstance(committed, workflow.WorkflowCommit)
                before = _workflow_rows(state_root)
                replay = store.commit_effect(begun.operation, receipt, draft)
                self.assertIsInstance(replay, workflow.StoredReplay)
                assert isinstance(replay, workflow.StoredReplay)
                self.assertEqual("operation-start", replay.operation_id)
                self.assertEqual(receipt, replay.receipt)
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_start_post_effect_projection_mismatch_is_unknown(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(
                    store,
                    begun.operation,
                    receipt_id="receipt-start",
                )
                invalid = replace(
                    _start_draft(root, receipt),
                    workflow_state=workflow.CheckpointState.WAITING,
                )
                with self.assertRaises(workflow.RecoveryRequired):
                    store.commit_effect(begun.operation, receipt, invalid)
                self.assertEqual(
                    "UNKNOWN_EFFECT",
                    _operation_status(state_root, "operation-start"),
                )
                checkpoint = store.load_checkpoint(
                    workflow.WorkflowRootKey(root.root_key)
                )
                self.assertIsInstance(checkpoint, workflow.WorkflowRootSeed)
                assert isinstance(checkpoint, workflow.WorkflowRootSeed)
                self.assertIs(
                    workflow.OperationStatus.UNKNOWN_EFFECT,
                    checkpoint.operation_status,
                )
        finally:
            temporary.cleanup()

    def test_prompt_commit_returns_stored_replay_for_exact_duplicate(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                intent = _prompt_intent(root)
                begun = store.begin_operation(
                    intent,
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(
                    store,
                    begun.operation,
                    receipt_id="receipt-prompt",
                    assignment=True,
                )
                draft = _prompt_draft(root, receipt)
                committed = store.commit_effect(
                    begun.operation,
                    receipt,
                    draft,
                )
                self.assertIsInstance(committed, workflow.WorkflowCommit)
                before = _workflow_rows(state_root)
                replay = store.commit_effect(
                    begun.operation,
                    receipt,
                    draft,
                )
                self.assertIsInstance(replay, workflow.StoredReplay)
                self.assertEqual(before, _workflow_rows(state_root))
                begin_replay = store.begin_operation(
                    intent,
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(begin_replay, workflow.StoredReplay)
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_commit_transition_atomically_compares_workflow_and_task_sequences(
        self,
    ) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as reopened:
                authority = workflow.AuthorityReference("policy-ref", _DIGEST_1)
                transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority,
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=None,
                    next_task_sequence=1,
                    actor="policy-authority",
                    request_digest=_DIGEST_2,
                )
                draft = workflow.WorkflowCheckpointDraft(
                    root=root,
                    run=started.run,
                    workflow_sequence=3,
                    task_sequence=1,
                    execution_mode=workflow.ExecutionMode.SERIAL,
                    workflow_state=workflow.CheckpointState.IDLE,
                    task_policy=workflow.TaskPolicyReference(
                        version=4,
                        team_id=root.team_id,
                        workspace=root.workspace_path,
                        task_id="task-1",
                        sequence=1,
                        state_digest=_DIGEST_3,
                    ),
                    active_assignment=None,
                    pending_delivery=None,
                    replied_message_ids=(),
                    read_observed=False,
                    released=False,
                    review_authority=authority,
                    verification_authority=None,
                    last_operation=started.last_operation,
                )
                result = reopened.commit_transition(
                    transition,
                    draft,
                    expected_workflow_sequence=2,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(result, workflow.WorkflowCheckpointV4)
                assert isinstance(result, workflow.WorkflowCheckpointV4)
                self.assertEqual(3, result.workflow_sequence)
                self.assertEqual(1, result.task_sequence)
                self.assertEqual(authority, result.review_authority)
                rows = _workflow_rows(state_root)
                self.assertEqual(3, rows["checkpoint"][3])
                self.assertEqual("IDLE", rows["checkpoint"][4])
                self.assertEqual(3, rows["events"][-1][2])
                self.assertIsNone(rows["events"][-1][3])
                self.assertEqual(1, rows["events"][-1][4])
                self.assertEqual("policy_transition", rows["events"][-1][7])
                before = _workflow_rows(state_root)
                replay = reopened.commit_transition(
                    transition,
                    draft,
                    expected_workflow_sequence=2,
                    expected_task_sequence=None,
                )
                self.assertEqual(result, replay)
                self.assertEqual(before, _workflow_rows(state_root))
                with self.assertRaises(workflow.OperationIdentityConflict):
                    reopened.commit_transition(
                        replace(
                            transition,
                            authority=workflow.AuthorityReference(
                                "policy-ref-other",
                                transition.authority.digest,
                            ),
                        ),
                        draft,
                        expected_workflow_sequence=2,
                        expected_task_sequence=None,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
                with self.assertRaises(workflow.StateConflict):
                    reopened.commit_transition(
                        transition,
                        draft,
                        expected_workflow_sequence=2,
                        expected_task_sequence=0,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_mark_unknown_is_atomic_idempotent_and_preserves_recovery_state(
        self,
    ) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as reopened:
                begun = reopened.begin_operation(
                    _prompt_intent(root),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                unknown = reopened.mark_unknown(
                    begun.operation,
                    reason=workflow.RecoveryCode.RESPONSE_LOST,
                )
                self.assertIsInstance(unknown, workflow.UnknownCommit)
                self.assertEqual(
                    workflow.OperationStatus.UNKNOWN_EFFECT, unknown.status
                )
                self.assertIsInstance(unknown.checkpoint, workflow.WorkflowCheckpointV4)
                assert isinstance(unknown.checkpoint, workflow.WorkflowCheckpointV4)
                self.assertEqual(
                    workflow.CheckpointState.RECOVERY_REQUIRED,
                    unknown.checkpoint.workflow_state,
                )
                self.assertEqual(4, unknown.checkpoint.workflow_sequence)
                before = _workflow_rows(state_root)
                repeated = reopened.mark_unknown(
                    begun.operation,
                    reason=workflow.RecoveryCode.RESPONSE_LOST,
                )
                self.assertEqual(unknown, repeated)
                self.assertEqual(before, _workflow_rows(state_root))
                rows = _workflow_rows(state_root)
                self.assertEqual(
                    "UNKNOWN_EFFECT",
                    _operation_status(state_root, "operation-prompt"),
                )
                self.assertEqual(4, len(rows["events"]))
                self.assertEqual("mark_unknown", rows["events"][-1][7])
                self.assertEqual(
                    workflow.CheckpointState.RECOVERY_REQUIRED.value,
                    rows["checkpoint"][4],
                )
        finally:
            temporary.cleanup()

    def test_mark_unknown_cannot_downgrade_committed_operation(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(store, begun.operation, receipt_id="receipt-start")
                store.commit_effect(
                    begun.operation,
                    receipt,
                    _start_draft(root, receipt),
                )
                before = _workflow_rows(state_root)
                with self.assertRaises(workflow.OperationIdentityConflict):
                    store.mark_unknown(
                        begun.operation,
                        reason=workflow.RecoveryCode.RESPONSE_LOST,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_post_effect_invalid_receipt_is_recovery_not_retryable_intent(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _prompt_intent(root),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                with self.assertRaises(workflow.RecoveryRequired):
                    store.commit_effect(
                        begun.operation,
                        object(),  # type: ignore[arg-type]
                        object(),  # type: ignore[arg-type]
                    )
                self.assertEqual(
                    "UNKNOWN_EFFECT",
                    _operation_status(state_root, "operation-prompt"),
                )
                checkpoint = store.load_checkpoint(
                    workflow.WorkflowRootKey(root.root_key)
                )
                self.assertIsInstance(checkpoint, workflow.WorkflowCheckpointV4)
                assert isinstance(checkpoint, workflow.WorkflowCheckpointV4)
                self.assertEqual(
                    workflow.CheckpointState.RECOVERY_REQUIRED,
                    checkpoint.workflow_state,
                )
        finally:
            temporary.cleanup()

    def test_prompt_receipt_cannot_commit_foreign_assignment_projection(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _prompt_intent(root),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(
                    store,
                    begun.operation,
                    receipt_id="receipt-prompt",
                    assignment=True,
                )
                valid = _prompt_draft(root, receipt)
                foreign_assignment = workflow.ActiveAssignment(
                    role=workflow.AssignmentRole.WORKER,
                    worker_node="worker-node-2",
                    task_id="task-2",
                    attempt=2,
                    dispatch_id="dispatch-2",
                    terminal_id="terminal-2",
                    launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
                    completion_identity=workflow.CompletionIdentity(
                        run_id="run-1",
                        task_id="task-2",
                        dispatch_id="dispatch-2",
                        sender_terminal_id="terminal-2",
                    ),
                )
                foreign = replace(
                    valid,
                    task_policy=workflow.TaskPolicyReference(
                        version=4,
                        team_id=root.team_id,
                        workspace=root.workspace_path,
                        task_id="task-2",
                        sequence=1,
                        state_digest=_DIGEST_4,
                    ),
                    active_assignment=foreign_assignment,
                )
                with self.assertRaises(workflow.RecoveryRequired):
                    store.commit_effect(begun.operation, receipt, foreign)
                self.assertEqual(
                    "UNKNOWN_EFFECT",
                    _operation_status(state_root, "operation-prompt"),
                )
        finally:
            temporary.cleanup()

    def test_commit_effect_rechecks_recovery_epoch_and_fencing_token(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _prompt_intent(root),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(
                    store,
                    begun.operation,
                    receipt_id="receipt-prompt",
                    assignment=True,
                )
                draft = _prompt_draft(root, receipt)
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                with store._write_transaction():
                    connection.execute(
                        "UPDATE store_meta SET value = value + 1 "
                        "WHERE key = 'fencing_token_floor'"
                    )
                with self.assertRaises(workflow.RecoveryRequired):
                    store.commit_effect(begun.operation, receipt, draft)
                self.assertEqual(
                    "UNKNOWN_EFFECT",
                    _operation_status(state_root, "operation-prompt"),
                )
        finally:
            temporary.cleanup()

    def test_wait_rejects_unissued_ack_intent_after_effect(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                current = _commit_prompt(store, root, started)
                begun = store.begin_operation(
                    _wait_intent(root, current),
                    expected_workflow_sequence=current.workflow_sequence,
                    expected_task_sequence=current.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                delivery = _wait_delivery(
                    current,
                    outcome=None,
                    ack_status=workflow.AckStatus.ACK_INTENT,
                    ack_operation_id="operation-unrelated-ack",
                )
                receipt = _wait_receipt(store, begun.operation, current, delivery)
                with self.assertRaises(workflow.RecoveryRequired):
                    store.commit_effect(
                        begun.operation,
                        receipt,
                        _wait_draft(
                            current,
                            receipt,
                            delivery,
                            state=workflow.CheckpointState.QUESTION,
                        ),
                    )
                self.assertEqual(
                    "UNKNOWN_EFFECT",
                    _operation_status(state_root, "operation-wait"),
                )
        finally:
            temporary.cleanup()

    def test_wait_commits_failed_worker_outcome_as_failed_checkpoint(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                current = _commit_prompt(store, root, started)
                begun = store.begin_operation(
                    _wait_intent(root, current),
                    expected_workflow_sequence=current.workflow_sequence,
                    expected_task_sequence=current.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                delivery = _wait_delivery(
                    current,
                    outcome=workflow.EventOutcome.FAILED,
                )
                receipt = _wait_receipt(store, begun.operation, current, delivery)
                result = store.commit_effect(
                    begun.operation,
                    receipt,
                    _wait_draft(
                        current,
                        receipt,
                        delivery,
                        state=workflow.CheckpointState.FAILED,
                    ),
                )
                self.assertIsInstance(result, workflow.WorkflowCommit)
                assert isinstance(result, workflow.WorkflowCommit)
                self.assertIs(
                    workflow.CheckpointState.FAILED,
                    result.checkpoint.workflow_state,
                )
        finally:
            temporary.cleanup()

    def test_wait_timeout_requires_canonical_result_digest(self) -> None:
        for valid in (True, False):
            with self.subTest(valid=valid):
                temporary, state_root, root = self._fixture()
                try:
                    _, started = _open_started_store(state_root, root)
                    with CoordinationStore(state_root) as store:
                        current = _commit_prompt(store, root, started)
                        intent = _wait_intent(root, current)
                        begun = store.begin_operation(
                            intent,
                            expected_workflow_sequence=current.workflow_sequence,
                            expected_task_sequence=current.task_sequence,
                        )
                        self.assertIsInstance(begun, workflow.OperationBegin)
                        assert isinstance(begun, workflow.OperationBegin)
                        assignment = current.active_assignment
                        self.assertIsNotNone(assignment)
                        assert assignment is not None
                        receipt = store._issue_workflow_receipt(
                            operation=begun.operation,
                            receipt_id="receipt-wait-timeout",
                            run_id=current.run.run_id,
                            main_terminal_id=current.run.main_terminal_id,
                            task_id=assignment.task_id,
                            dispatch_id=assignment.dispatch_id,
                            attempt=assignment.attempt,
                            terminal_id=assignment.terminal_id,
                            delivery_id=None,
                            message_id=None,
                            effect_ref="backend/wait-timeout",
                            result_kind="timeout",
                            result_digest=(
                                workflow.wait_timeout_digest() if valid else _DIGEST_4
                            ),
                            evidence_ref=_DIGEST_3,
                            issued_ns=30,
                        )
                        draft = replace(
                            workflow.checkpoint_to_draft(current),
                            workflow_sequence=current.workflow_sequence + 2,
                            workflow_state=workflow.CheckpointState.WAITING,
                            last_operation=workflow.LastOperation(
                                operation_id=receipt.operation_id,
                                effect_key=receipt.effect_key,
                                action=receipt.action,
                                request_digest=receipt.request_digest,
                                expected_workflow_sequence=(current.workflow_sequence),
                                expected_task_sequence=current.task_sequence,
                                status=workflow.OperationStatus.COMMITTED,
                                receipt_id=receipt.receipt_id,
                                receipt_digest=(
                                    workflow.durable_receipt_digest(receipt)
                                ),
                            ),
                        )
                        if valid:
                            result = store.commit_effect(
                                begun.operation,
                                receipt,
                                draft,
                            )
                            self.assertIsInstance(result, workflow.WorkflowCommit)
                            replay = store.commit_effect(
                                begun.operation,
                                receipt,
                                draft,
                            )
                            self.assertIsInstance(replay, workflow.StoredReplay)
                        else:
                            with self.assertRaises(workflow.RecoveryRequired):
                                store.commit_effect(
                                    begun.operation,
                                    receipt,
                                    draft,
                                )
                            self.assertEqual(
                                "UNKNOWN_EFFECT",
                                _operation_status(state_root, "operation-wait"),
                            )
                finally:
                    temporary.cleanup()

    def test_transition_cannot_promote_failed_delivery_to_success_state(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                current = _commit_prompt(store, root, started)
                begun = store.begin_operation(
                    _wait_intent(root, current),
                    expected_workflow_sequence=current.workflow_sequence,
                    expected_task_sequence=current.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                delivery = _wait_delivery(
                    current,
                    outcome=workflow.EventOutcome.FAILED,
                )
                receipt = _wait_receipt(store, begun.operation, current, delivery)
                committed = store.commit_effect(
                    begun.operation,
                    receipt,
                    _wait_draft(
                        current,
                        receipt,
                        delivery,
                        state=workflow.CheckpointState.FAILED,
                    ),
                )
                self.assertIsInstance(committed, workflow.WorkflowCommit)
                assert isinstance(committed, workflow.WorkflowCommit)
                failed = committed.checkpoint
                authority = workflow.AuthorityReference("policy-ref", _DIGEST_4)
                transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority,
                    expected_workflow_sequence=failed.workflow_sequence,
                    expected_task_sequence=failed.task_sequence,
                    next_task_sequence=failed.task_sequence,
                    actor="policy-authority",
                    request_digest=_DIGEST_1,
                )
                before = _workflow_rows(state_root)
                with self.assertRaises(workflow.OperationIdentityConflict):
                    store.commit_transition(
                        transition,
                        replace(
                            workflow.checkpoint_to_draft(failed),
                            workflow_sequence=failed.workflow_sequence + 1,
                            workflow_state=workflow.CheckpointState.WORKER_DONE,
                            review_authority=authority,
                        ),
                        expected_workflow_sequence=failed.workflow_sequence,
                        expected_task_sequence=failed.task_sequence,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_transition_preserves_effect_owned_and_non_target_projection(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                authority = workflow.AuthorityReference("policy-ref", _DIGEST_1)
                transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority,
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                    next_task_sequence=started.task_sequence,
                    actor="policy-authority",
                    request_digest=_DIGEST_2,
                )
                invalid = replace(
                    workflow.checkpoint_to_draft(started),
                    workflow_sequence=started.workflow_sequence + 1,
                    read_observed=True,
                    released=True,
                    review_authority=authority,
                    verification_authority=workflow.AuthorityReference(
                        "foreign-verification",
                        _DIGEST_3,
                    ),
                )
                before = _workflow_rows(state_root)
                with self.assertRaises(workflow.OperationIdentityConflict):
                    store.commit_transition(
                        transition,
                        invalid,
                        expected_workflow_sequence=started.workflow_sequence,
                        expected_task_sequence=started.task_sequence,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_generic_transition_cannot_change_workflow_state(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                authority = workflow.AuthorityReference("policy-ref", _DIGEST_1)
                transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority,
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=started.task_sequence,
                    next_task_sequence=started.task_sequence,
                    actor="policy-authority",
                    request_digest=_DIGEST_2,
                )
                before = _workflow_rows(state_root)
                for target in (
                    workflow.CheckpointState.REVIEW_PENDING,
                    workflow.CheckpointState.RECOVERY_REQUIRED,
                    workflow.CheckpointState.WORKER_DONE,
                ):
                    with (
                        self.subTest(target=target.value),
                        self.assertRaises(workflow.OperationIdentityConflict),
                    ):
                        store.commit_transition(
                            transition,
                            replace(
                                workflow.checkpoint_to_draft(started),
                                workflow_sequence=started.workflow_sequence + 1,
                                workflow_state=target,
                                review_authority=authority,
                            ),
                            expected_workflow_sequence=started.workflow_sequence,
                            expected_task_sequence=started.task_sequence,
                        )
                    self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_transition_cannot_replace_task_policy_without_sequence_advance(
        self,
    ) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                authority1 = workflow.AuthorityReference("policy-ref-1", _DIGEST_1)
                first_transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority1,
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=None,
                    next_task_sequence=1,
                    actor="policy-authority",
                    request_digest=_DIGEST_2,
                )
                policy1 = workflow.TaskPolicyReference(
                    version=4,
                    team_id=root.team_id,
                    workspace=root.workspace_path,
                    task_id="task-1",
                    sequence=1,
                    state_digest=_DIGEST_3,
                )
                first = store.commit_transition(
                    first_transition,
                    replace(
                        workflow.checkpoint_to_draft(started),
                        workflow_sequence=started.workflow_sequence + 1,
                        task_sequence=1,
                        workflow_state=workflow.CheckpointState.IDLE,
                        task_policy=policy1,
                        review_authority=authority1,
                    ),
                    expected_workflow_sequence=started.workflow_sequence,
                    expected_task_sequence=None,
                )
                authority2 = workflow.AuthorityReference("policy-ref-2", _DIGEST_4)
                second_transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority2,
                    expected_workflow_sequence=first.workflow_sequence,
                    expected_task_sequence=1,
                    next_task_sequence=1,
                    actor="policy-authority",
                    request_digest=_DIGEST_1,
                )
                invalid = replace(
                    workflow.checkpoint_to_draft(first),
                    workflow_sequence=first.workflow_sequence + 1,
                    task_policy=replace(policy1, state_digest=_DIGEST_4),
                    review_authority=authority2,
                )
                before = _workflow_rows(state_root)
                with self.assertRaises(workflow.OperationIdentityConflict):
                    store.commit_transition(
                        second_transition,
                        invalid,
                        expected_workflow_sequence=first.workflow_sequence,
                        expected_task_sequence=1,
                    )
                self.assertEqual(before, _workflow_rows(state_root))
        finally:
            temporary.cleanup()

    def test_receipt_issued_clock_advances_store_high_water(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root, clock=lambda: 1) as store:
                begun = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(
                    store,
                    begun.operation,
                    receipt_id="receipt-start",
                    issued_ns=20,
                )
                store.commit_effect(
                    begun.operation,
                    receipt,
                    _start_draft(root, receipt),
                )
            with CoordinationStore(state_root) as reopened:
                checkpoint = reopened.load_checkpoint(
                    workflow.WorkflowRootKey(root.root_key)
                )
                self.assertIsInstance(checkpoint, workflow.WorkflowCheckpointV4)
        finally:
            temporary.cleanup()

    def test_workflow_event_journal_is_append_only_after_transaction_commit(
        self,
    ) -> None:
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
            connection = sqlite3.connect(
                str(_database(state_root)), isolation_level=None
            )
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE workflow_events SET actor = 'attacker' "
                        "WHERE workflow_event_id = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM workflow_events WHERE workflow_event_id = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT OR REPLACE INTO workflow_events("
                        "workflow_event_id, workflow_event_schema_version, root_key, "
                        "operation_id, workflow_sequence, to_state, kind, actor, "
                        "clock_ns, request_digest, checkpoint_bytes, checkpoint_digest, "
                        "event_digest) "
                        "SELECT workflow_event_id, workflow_event_schema_version, "
                        "root_key, operation_id, workflow_sequence, to_state, kind, "
                        "actor, clock_ns, request_digest, checkpoint_bytes, "
                        "checkpoint_digest, event_digest "
                        "FROM workflow_events WHERE workflow_event_id = 1"
                    )
            finally:
                connection.close()
        finally:
            temporary.cleanup()

    def test_commit_effect_faults_leave_only_the_prior_intent_transaction(self) -> None:
        points = (
            "before_workflow_receipt_insert",
            "before_workflow_commit_checkpoint",
            "before_workflow_commit_event",
        )
        context = multiprocessing.get_context("spawn")
        for point in points:
            with self.subTest(point=point):
                temporary, state_root, root = self._fixture()
                try:
                    with CoordinationStore(state_root) as store:
                        begun = store.begin_operation(
                            _start_intent(root),
                            expected_workflow_sequence=0,
                            expected_task_sequence=None,
                        )
                        self.assertIsInstance(begun, workflow.OperationBegin)
                        assert isinstance(begun, workflow.OperationBegin)
                        receipt = _receipt(
                            store, begun.operation, receipt_id="receipt-start"
                        )
                        store.commit_effect(
                            begun.operation,
                            receipt,
                            _start_draft(root, receipt),
                        )
                    process = context.Process(
                        target=_kill_commit_worker,
                        args=(str(state_root), root, point),
                    )
                    process.start()
                    process.join(timeout=15)
                    if process.is_alive():
                        process.kill()
                        process.join()
                    self.assertEqual(-signal.SIGKILL, process.exitcode, point)
                    with CoordinationStore(state_root) as recovered:
                        checkpoint = recovered.load_checkpoint(
                            workflow.WorkflowRootKey(root.root_key)
                        )
                        self.assertIsInstance(
                            checkpoint, workflow.WorkflowCheckpointV4, point
                        )
                        assert isinstance(checkpoint, workflow.WorkflowCheckpointV4)
                        self.assertEqual(3, checkpoint.workflow_sequence, point)
                        self.assertIsNotNone(checkpoint.last_operation, point)
                        assert checkpoint.last_operation is not None
                        self.assertIs(
                            workflow.OperationStatus.INTENT,
                            checkpoint.last_operation.status,
                            point,
                        )
                    self.assertEqual(
                        {
                            "workflow_checkpoints": 1,
                            "workflow_operations": 2,
                            "workflow_receipts": 1,
                            "workflow_events": 3,
                        },
                        _counts(state_root),
                        point,
                    )
                    self.assertEqual(
                        "receipt-start",
                        _workflow_rows(state_root)["receipt"][0],
                    )
                finally:
                    temporary.cleanup()

    def test_begin_faults_are_all_or_none_before_intent_commit(self) -> None:
        points = (
            "before_workflow_seed_insert",
            "before_workflow_operation_insert",
            "before_workflow_checkpoint_update",
            "before_workflow_event_insert",
        )
        context = multiprocessing.get_context("spawn")
        for point in points:
            with self.subTest(point=point):
                temporary, state_root, root = self._fixture()
                try:
                    process = context.Process(
                        target=_kill_begin_worker,
                        args=(str(state_root), root, point),
                    )
                    process.start()
                    process.join(timeout=15)
                    if process.is_alive():
                        process.kill()
                        process.join()
                    self.assertEqual(-signal.SIGKILL, process.exitcode, point)
                    self.assertEqual(
                        {
                            "workflow_checkpoints": 0,
                            "workflow_operations": 0,
                            "workflow_receipts": 0,
                            "workflow_events": 0,
                        },
                        _counts(state_root),
                        point,
                    )
                finally:
                    temporary.cleanup()

    def test_mark_unknown_faults_are_all_or_none_before_unknown_commit(self) -> None:
        points = (
            "before_workflow_unknown_checkpoint",
            "before_workflow_unknown_event",
        )
        context = multiprocessing.get_context("spawn")
        for point in points:
            with self.subTest(point=point):
                temporary, state_root, root = self._fixture()
                try:
                    with CoordinationStore(state_root) as store:
                        begun = store.begin_operation(
                            _start_intent(root),
                            expected_workflow_sequence=0,
                            expected_task_sequence=None,
                        )
                        self.assertIsInstance(begun, workflow.OperationBegin)
                        assert isinstance(begun, workflow.OperationBegin)
                        receipt = _receipt(
                            store, begun.operation, receipt_id="receipt-start"
                        )
                        store.commit_effect(
                            begun.operation,
                            receipt,
                            _start_draft(root, receipt),
                        )
                    process = context.Process(
                        target=_kill_unknown_worker,
                        args=(str(state_root), root, point),
                    )
                    process.start()
                    process.join(timeout=15)
                    if process.is_alive():
                        process.kill()
                        process.join()
                    self.assertEqual(-signal.SIGKILL, process.exitcode, point)
                    rows = _workflow_rows(state_root)
                    self.assertEqual(
                        "INTENT",
                        _operation_status(state_root, "operation-prompt"),
                        point,
                    )
                    self.assertEqual("INTENT", rows["checkpoint"][8], point)
                    self.assertEqual(3, rows["checkpoint"][3], point)
                    self.assertEqual(3, len(rows["events"]), point)
                finally:
                    temporary.cleanup()

    def test_two_process_begin_has_one_cas_winner(self) -> None:
        context = multiprocessing.get_context("spawn")
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(store, begun.operation, receipt_id="receipt-start")
                store.commit_effect(
                    begun.operation,
                    receipt,
                    _start_draft(root, receipt),
                )
            barrier = context.Barrier(2)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_begin_worker,
                    args=(
                        str(state_root),
                        root,
                        f"owner-{index}",
                        f"operation-prompt-{index}",
                        barrier,
                        result_queue,
                    ),
                )
                for index in (1, 2)
            ]
            for process in processes:
                process.start()
            results = [result_queue.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join()
            self.assertEqual(["conflict", "winner"], sorted(results))
            self.assertEqual(
                {
                    "workflow_checkpoints": 1,
                    "workflow_operations": 2,
                    "workflow_receipts": 1,
                    "workflow_events": 3,
                },
                _counts(state_root),
            )
            with closing(sqlite3.connect(str(_database(state_root)))) as connection:
                checkpoint = connection.execute(
                    "SELECT workflow_sequence FROM workflow_checkpoints"
                ).fetchone()[0]
            self.assertEqual(3, checkpoint)
        finally:
            temporary.cleanup()

    def test_busy_writer_does_not_become_a_second_begin_winner(self) -> None:
        context = multiprocessing.get_context("spawn")
        temporary, state_root, root = self._fixture()
        try:
            with CoordinationStore(state_root) as store:
                begun = store.begin_operation(
                    _start_intent(root),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                receipt = _receipt(store, begun.operation, receipt_id="receipt-start")
                store.commit_effect(
                    begun.operation,
                    receipt,
                    _start_draft(root, receipt),
                )
            ready = context.Event()
            release = context.Event()
            result_queue = context.Queue()
            with CoordinationStore(state_root, busy_timeout_ms=20) as contender:
                blocker = context.Process(
                    target=_blocking_begin_worker,
                    args=(str(state_root), root, ready, release, result_queue),
                )
                blocker.start()
                try:
                    self.assertTrue(ready.wait(timeout=15))
                    with self.assertRaises(StoreBusyError):
                        contender.begin_operation(
                            _prompt_intent(
                                root,
                                operation_id="operation-contender",
                                effect_key="effect/contender",
                            ),
                            expected_workflow_sequence=2,
                            expected_task_sequence=None,
                        )
                finally:
                    release.set()
                    blocker.join(timeout=15)
                    if blocker.is_alive():
                        blocker.kill()
                        blocker.join()
            self.assertEqual(0, blocker.exitcode)
            self.assertEqual("ok", result_queue.get(timeout=10))
            self.assertEqual(
                {
                    "workflow_checkpoints": 1,
                    "workflow_operations": 2,
                    "workflow_receipts": 1,
                    "workflow_events": 3,
                },
                _counts(state_root),
            )
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
