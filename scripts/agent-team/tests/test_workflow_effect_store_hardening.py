"""Hardening tests for the private durable effect Store read seams."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import test_workflow_store_lifecycle as lifecycle_fixtures
import test_workflow_store_transaction as transaction_fixtures

from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore

_DIGEST_1 = transaction_fixtures._DIGEST_1
_DIGEST_3 = transaction_fixtures._DIGEST_3
_DIGEST_4 = transaction_fixtures._DIGEST_4


def _receipt_count(state_root: Path) -> int:
    with closing(
        sqlite3.connect(str(state_root / "coordination.sqlite3"))
    ) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM workflow_receipts").fetchone()[0]
        )


def _operation_status(state_root: Path, operation_id: str) -> str:
    with closing(
        sqlite3.connect(str(state_root / "coordination.sqlite3"))
    ) as connection:
        row = connection.execute(
            "SELECT status FROM workflow_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"operation {operation_id!r} is missing")
    return str(row[0])


def _issue_wait_receipt(
    store: CoordinationStore,
    operation: workflow.OperationHandle,
    current: workflow.WorkflowCheckpointV4,
    delivery: workflow.PendingDelivery,
    *,
    receipt_id: str,
) -> workflow.DurableReceipt:
    assignment = current.active_assignment
    if assignment is None:
        raise TypeError("wait fixture requires an assignment")
    return store._issue_workflow_receipt(
        operation=operation,
        receipt_id=receipt_id,
        run_id=current.run.run_id,
        main_terminal_id=current.run.main_terminal_id,
        consumer_generation=current.run.consumer_generation,
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
        effect_ref=f"backend/{receipt_id}",
        result_kind="delivery",
        result_digest=delivery.delivery_digest,
        evidence_ref=_DIGEST_3,
        issued_ns=30,
    )


class WorkflowEffectStoreHardeningTests(unittest.TestCase):
    """Prove historical lookups and receipt registration fail closed."""

    def _fixture(
        self,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, workflow.RootIdentity]:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-effect-hardening-")
        state_root = transaction_fixtures._make_state_root(temporary.name)
        root = transaction_fixtures._make_root(state_root, temporary.name)
        return temporary, state_root, root

    def _bootstrap_wait(
        self,
        store: CoordinationStore,
        root: workflow.RootIdentity,
        started: workflow.WorkflowCheckpointV4,
        *,
        outcome: workflow.EventOutcome | None,
    ) -> tuple[
        workflow.WorkflowCheckpointV4,
        workflow.PendingDelivery,
        workflow.DurableReceipt,
    ]:
        current = transaction_fixtures._commit_prompt(store, root, started)
        intent = transaction_fixtures._wait_intent(root, current)
        begun = store.begin_operation(
            intent,
            expected_workflow_sequence=current.workflow_sequence,
            expected_task_sequence=current.task_sequence,
        )
        self.assertIsInstance(begun, workflow.OperationBegin)
        assert isinstance(begun, workflow.OperationBegin)
        delivery = transaction_fixtures._wait_delivery(current, outcome=outcome)
        receipt = transaction_fixtures._wait_receipt(
            store, begun.operation, current, delivery
        )
        committed = store.commit_effect(
            begun.operation,
            receipt,
            transaction_fixtures._wait_draft(
                current,
                receipt,
                delivery,
                state=(
                    workflow.CheckpointState.QUESTION
                    if outcome is None
                    else workflow.CheckpointState.WORKER_DONE
                ),
            ),
        )
        self.assertIsInstance(committed, workflow.WorkflowCommit)
        assert isinstance(committed, workflow.WorkflowCommit)
        return committed.checkpoint, delivery, receipt

    def test_committed_effect_lookup_survives_store_reopen(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, expected_checkpoint = transaction_fixtures._open_started_store(
                state_root, root
            )
            operation_id = workflow.WorkflowOperationId("operation-start")
            with CoordinationStore(state_root) as store:
                first = store._lookup_workflow_effect(operation_id)
            with CoordinationStore(state_root) as reopened:
                second = reopened._lookup_workflow_effect(operation_id)
            self.assertEqual(expected_checkpoint, first.checkpoint)
            self.assertEqual(first.operation_id, second.operation_id)
            self.assertEqual(
                workflow.durable_receipt_digest(first.receipt),
                workflow.durable_receipt_digest(second.receipt),
            )
            self.assertEqual(first.checkpoint, second.checkpoint)
            self.assertEqual(first.event_digest, second.event_digest)
            self.assertEqual("receipt-start", second.receipt.receipt_id)
            self.assertEqual(2, second.checkpoint.workflow_sequence)
        finally:
            temporary.cleanup()

    def test_historical_wait_delivery_lookup_survives_question_ack(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = transaction_fixtures._open_started_store(state_root, root)
            helper = lifecycle_fixtures.WorkflowStoreLifecycleTests(
                methodName="runTest"
            )
            with CoordinationStore(state_root) as store:
                current, delivery, wait_receipt = self._bootstrap_wait(
                    store, root, started, outcome=None
                )
                reply_intent = lifecycle_fixtures._action_intent(
                    root,
                    current,
                    workflow.OperationAction.REPLY,
                    operation_id="operation-reply-before-hardening-ack",
                    message_id="message-1",
                )
                _, _, _, after_reply = helper._commit_action(
                    store,
                    current,
                    reply_intent,
                    receipt_id="receipt-reply-before-hardening-ack",
                )
                ack_intent = lifecycle_fixtures._action_intent(
                    root,
                    after_reply,
                    workflow.OperationAction.ACK,
                    operation_id="operation-ack-before-hardening-lookup",
                )
                _, _, _, after_ack = helper._commit_action(
                    store,
                    after_reply,
                    ack_intent,
                    receipt_id="receipt-ack-before-hardening-lookup",
                )
                self.assertIsNone(after_ack.pending_delivery)

                snapshot = store._lookup_workflow_delivery_effect(
                    workflow.WorkflowRootKey(root.root_key),
                    delivery.delivery_id,
                    delivery.consumer_generation,
                )
                self.assertEqual(wait_receipt.receipt_id, snapshot.receipt.receipt_id)
                self.assertEqual(delivery, snapshot.checkpoint.pending_delivery)
                self.assertEqual(
                    workflow.CheckpointState.QUESTION,
                    snapshot.checkpoint.workflow_state,
                )
                self.assertLess(
                    snapshot.checkpoint.workflow_sequence, after_ack.workflow_sequence
                )
        finally:
            temporary.cleanup()

    def test_duplicate_committed_wait_delivery_is_ambiguous_and_read_only(self) -> None:
        temporary, state_root, root = self._fixture()
        try:
            _, started = transaction_fixtures._open_started_store(state_root, root)
            helper = lifecycle_fixtures.WorkflowStoreLifecycleTests(
                methodName="runTest"
            )
            with CoordinationStore(state_root) as store:
                current, delivery, _ = self._bootstrap_wait(
                    store, root, started, outcome=None
                )
                reply_intent = lifecycle_fixtures._action_intent(
                    root,
                    current,
                    workflow.OperationAction.REPLY,
                    operation_id="operation-reply-before-duplicate-wait",
                    message_id="message-1",
                )
                _, _, _, after_reply = helper._commit_action(
                    store,
                    current,
                    reply_intent,
                    receipt_id="receipt-reply-before-duplicate-wait",
                )
                ack_intent = lifecycle_fixtures._action_intent(
                    root,
                    after_reply,
                    workflow.OperationAction.ACK,
                    operation_id="operation-ack-before-duplicate-wait",
                )
                _, _, _, after_ack = helper._commit_action(
                    store,
                    after_reply,
                    ack_intent,
                    receipt_id="receipt-ack-before-duplicate-wait",
                )

                duplicate_intent = replace(
                    transaction_fixtures._wait_intent(root, after_ack),
                    operation_id="operation-wait-duplicate",
                    effect_key="effect/wait-duplicate",
                    request_digest=workflow.digest_bounded_body(
                        b"wait-duplicate", domain=workflow.REQUEST_DIGEST_DOMAIN
                    ),
                )
                begun = store.begin_operation(
                    duplicate_intent,
                    expected_workflow_sequence=after_ack.workflow_sequence,
                    expected_task_sequence=after_ack.task_sequence,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                duplicate_delivery = transaction_fixtures._wait_delivery(
                    after_ack, outcome=None
                )
                self.assertEqual(delivery.delivery_id, duplicate_delivery.delivery_id)
                duplicate_receipt = _issue_wait_receipt(
                    store,
                    begun.operation,
                    after_ack,
                    duplicate_delivery,
                    receipt_id="receipt-wait-duplicate",
                )
                duplicate_commit = store.commit_effect(
                    begun.operation,
                    duplicate_receipt,
                    transaction_fixtures._wait_draft(
                        after_ack,
                        duplicate_receipt,
                        duplicate_delivery,
                        state=workflow.CheckpointState.QUESTION,
                    ),
                )
                self.assertIsInstance(duplicate_commit, workflow.WorkflowCommit)
                before = (state_root / "coordination.sqlite3").read_bytes()
                with self.assertRaises(workflow.OperationIdentityConflict):
                    store._lookup_workflow_delivery_effect(
                        workflow.WorkflowRootKey(root.root_key),
                        delivery.delivery_id,
                        delivery.consumer_generation,
                    )
                self.assertEqual(
                    before, (state_root / "coordination.sqlite3").read_bytes()
                )
        finally:
            temporary.cleanup()

    def test_delivery_lookup_missing_unknown_timeout_and_generation_mismatch_are_read_only(
        self,
    ) -> None:
        cases: tuple[tuple[str, Callable[[workflow.RootIdentity, Path], None]], ...] = (
            ("missing", self._assert_missing_delivery),
            ("unknown", self._assert_unknown_delivery),
            ("timeout", self._assert_timeout_delivery),
            ("wrong-generation", self._assert_wrong_generation),
        )
        for name, case in cases:
            with self.subTest(case=name):
                temporary, state_root, root = self._fixture()
                try:
                    case(root, state_root)
                finally:
                    # Each case owns its Store context; closing it here also
                    # verifies that a failed lookup never leaves a live reader.
                    temporary.cleanup()

    def _assert_missing_delivery(
        self, root: workflow.RootIdentity, state_root: Path
    ) -> None:
        transaction_fixtures._open_started_store(state_root, root)
        with CoordinationStore(state_root) as store:
            before = (state_root / "coordination.sqlite3").read_bytes()
            with self.assertRaises(workflow.RecoveryRequired):
                store._lookup_workflow_delivery_effect(
                    workflow.WorkflowRootKey(root.root_key), "missing-delivery", 0
                )
            self.assertEqual(before, (state_root / "coordination.sqlite3").read_bytes())
            self.assertEqual(1, _receipt_count(state_root))

    def _assert_unknown_delivery(
        self, root: workflow.RootIdentity, state_root: Path
    ) -> None:
        _, started = transaction_fixtures._open_started_store(state_root, root)
        with CoordinationStore(state_root) as store:
            current = transaction_fixtures._commit_prompt(store, root, started)
            intent = transaction_fixtures._wait_intent(root, current)
            begun = store.begin_operation(
                intent,
                expected_workflow_sequence=current.workflow_sequence,
                expected_task_sequence=current.task_sequence,
            )
            self.assertIsInstance(begun, workflow.OperationBegin)
            assert isinstance(begun, workflow.OperationBegin)
            store.mark_unknown(
                begun.operation, reason=workflow.RecoveryCode.UNKNOWN_EFFECT
            )
            before = (state_root / "coordination.sqlite3").read_bytes()
            with self.assertRaises(workflow.RecoveryRequired):
                store._lookup_workflow_delivery_effect(
                    workflow.WorkflowRootKey(root.root_key), "delivery-1", 0
                )
            self.assertEqual(before, (state_root / "coordination.sqlite3").read_bytes())
            self.assertEqual(2, _receipt_count(state_root))
            self.assertEqual(
                "UNKNOWN_EFFECT", _operation_status(state_root, intent.operation_id)
            )

    def _assert_timeout_delivery(
        self, root: workflow.RootIdentity, state_root: Path
    ) -> None:
        _, started = transaction_fixtures._open_started_store(state_root, root)
        with CoordinationStore(state_root) as store:
            current = transaction_fixtures._commit_prompt(store, root, started)
            intent = transaction_fixtures._wait_intent(root, current)
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
                consumer_generation=current.run.consumer_generation,
                task_id=assignment.task_id,
                dispatch_id=assignment.dispatch_id,
                attempt=assignment.attempt,
                terminal_id=assignment.terminal_id,
                delivery_id=None,
                message_id=None,
                effect_ref="backend/receipt-wait-timeout",
                result_kind="timeout",
                result_digest=workflow.wait_timeout_digest(),
                evidence_ref=_DIGEST_3,
                issued_ns=30,
            )
            timeout_draft = transaction_fixtures._wait_draft(
                current,
                receipt,
                transaction_fixtures._wait_delivery(current, outcome=None),
                state=workflow.CheckpointState.WAITING,
            )
            committed = store.commit_effect(
                begun.operation,
                receipt,
                replace(timeout_draft, pending_delivery=None),
            )
            self.assertIsInstance(committed, workflow.WorkflowCommit)
            # The timeout has no Delivery identity and is therefore never a
            # candidate for an exact Delivery lookup.
            before = (state_root / "coordination.sqlite3").read_bytes()
            with self.assertRaises(workflow.RecoveryRequired):
                store._lookup_workflow_delivery_effect(
                    workflow.WorkflowRootKey(root.root_key), "delivery-1", 0
                )
            self.assertEqual(before, (state_root / "coordination.sqlite3").read_bytes())
            self.assertEqual(3, _receipt_count(state_root))

    def _assert_wrong_generation(
        self, root: workflow.RootIdentity, state_root: Path
    ) -> None:
        _, started = transaction_fixtures._open_started_store(state_root, root)
        with CoordinationStore(state_root) as store:
            current, delivery, _ = self._bootstrap_wait(
                store, root, started, outcome=None
            )
            del current
            before = (state_root / "coordination.sqlite3").read_bytes()
            with self.assertRaises(workflow.RecoveryRequired):
                store._lookup_workflow_delivery_effect(
                    workflow.WorkflowRootKey(root.root_key),
                    delivery.delivery_id,
                    delivery.consumer_generation + 1,
                )
            self.assertEqual(before, (state_root / "coordination.sqlite3").read_bytes())
            self.assertEqual(3, _receipt_count(state_root))

    def test_all_receipt_fields_tampered_after_issuance_require_recovery(self) -> None:
        mutations: tuple[tuple[str, Callable[[workflow.DurableReceipt], None]], ...] = (
            (
                "receipt_id",
                lambda receipt: object.__setattr__(
                    receipt, "receipt_id", "receipt-tampered"
                ),
            ),
            (
                "operation_id",
                lambda receipt: object.__setattr__(
                    receipt, "operation_id", "operation-tampered"
                ),
            ),
            (
                "effect_key",
                lambda receipt: object.__setattr__(
                    receipt, "effect_key", "effect/tampered"
                ),
            ),
            (
                "request_digest",
                lambda receipt: object.__setattr__(
                    receipt, "request_digest", _DIGEST_4
                ),
            ),
            (
                "result_kind",
                lambda receipt: object.__setattr__(receipt, "result_kind", "tampered"),
            ),
            (
                "result_digest",
                lambda receipt: object.__setattr__(receipt, "result_digest", _DIGEST_4),
            ),
            (
                "effect_ref",
                lambda receipt: object.__setattr__(
                    receipt, "effect_ref", "backend/tampered"
                ),
            ),
            (
                "evidence_ref",
                lambda receipt: object.__setattr__(receipt, "evidence_ref", _DIGEST_1),
            ),
            ("issued_ns", lambda receipt: object.__setattr__(receipt, "issued_ns", 31)),
            (
                "consumer_generation",
                lambda receipt: object.__setattr__(receipt, "consumer_generation", 1),
            ),
            (
                "owner",
                lambda receipt: object.__setattr__(receipt, "owner", "owner-tampered"),
            ),
            (
                "lease_epoch",
                lambda receipt: object.__setattr__(receipt, "lease_epoch", 1),
            ),
            (
                "fencing_token",
                lambda receipt: object.__setattr__(receipt, "fencing_token", 1),
            ),
        )
        for index, (name, mutate) in enumerate(mutations):
            with self.subTest(field=name):
                temporary, state_root, root = self._fixture()
                try:
                    _, started = transaction_fixtures._open_started_store(
                        state_root, root
                    )
                    helper = lifecycle_fixtures.WorkflowStoreLifecycleTests(
                        methodName="runTest"
                    )
                    with CoordinationStore(state_root) as store:
                        current, _, _ = self._bootstrap_wait(
                            store,
                            root,
                            started,
                            outcome=workflow.EventOutcome.SUCCEEDED,
                        )
                        intent = lifecycle_fixtures._action_intent(
                            root,
                            current,
                            workflow.OperationAction.READ,
                            operation_id=f"operation-read-tamper-{index}",
                        )
                        begun = store.begin_operation(
                            intent,
                            expected_workflow_sequence=current.workflow_sequence,
                            expected_task_sequence=current.task_sequence,
                        )
                        self.assertIsInstance(begun, workflow.OperationBegin)
                        assert isinstance(begun, workflow.OperationBegin)
                        receipt = lifecycle_fixtures._receipt(
                            store,
                            begun.operation,
                            current,
                            receipt_id=f"receipt-read-tamper-{index}",
                            delivery_id=intent.delivery_id,
                        )
                        draft = helper._action_draft(current, intent, receipt)
                        mutate(receipt)
                        with self.assertRaises(workflow.RecoveryRequired):
                            store.commit_effect(begun.operation, receipt, draft)
                        self.assertEqual(3, _receipt_count(state_root))
                        self.assertEqual(
                            "UNKNOWN_EFFECT",
                            _operation_status(state_root, intent.operation_id),
                        )
                finally:
                    temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
