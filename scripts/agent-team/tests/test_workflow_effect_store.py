"""RED tests for the private workflow-effect Store seams."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import test_workflow_store_lifecycle as lifecycle_fixtures
from test_workflow_store_transaction import (
    _DIGEST_1,
    _DIGEST_4,
    _commit_prompt,
    _make_root,
    _make_state_root,
    _open_started_store,
    _wait_delivery,
    _wait_draft,
    _wait_intent,
    _wait_receipt,
)

from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore


class WorkflowEffectStoreTests(unittest.TestCase):
    def test_committed_effect_lookup_returns_historical_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-store-"
        ) as temporary:
            state_root = _make_state_root(temporary)
            root = _make_root(state_root, temporary)
            _, committed = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                snapshot = store._lookup_workflow_effect(
                    workflow.WorkflowOperationId("operation-start")
                )
                self.assertIsInstance(snapshot, workflow.WorkflowEffectSnapshot)
                self.assertEqual("operation-start", snapshot.operation_id)
                self.assertEqual("receipt-start", snapshot.receipt.receipt_id)
                self.assertEqual(committed, snapshot.checkpoint)
                self.assertTrue(snapshot.event_digest.startswith("sha256:"))

    def test_wait_origin_uses_terminal_event_after_later_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-store-wait-"
        ) as temporary:
            state_root = _make_state_root(temporary)
            root = _make_root(state_root, temporary)
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
                delivery = _wait_delivery(current, outcome=None)
                receipt = _wait_receipt(store, begun.operation, current, delivery)
                committed = store.commit_effect(
                    begun.operation,
                    receipt,
                    _wait_draft(
                        current,
                        receipt,
                        delivery,
                        state=workflow.CheckpointState.QUESTION,
                    ),
                )
                self.assertIsInstance(committed, workflow.WorkflowCommit)
                assert isinstance(committed, workflow.WorkflowCommit)
                wait_sequence = committed.checkpoint.workflow_sequence

                authority = workflow.AuthorityReference("review-ref", _DIGEST_1)
                transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority,
                    expected_workflow_sequence=wait_sequence,
                    expected_task_sequence=committed.checkpoint.task_sequence,
                    next_task_sequence=committed.checkpoint.task_sequence,
                    actor="policy-authority",
                    request_digest=_DIGEST_4,
                )
                latest = store.commit_transition(
                    transition,
                    replace(
                        workflow.checkpoint_to_draft(committed.checkpoint),
                        workflow_sequence=wait_sequence + 1,
                        review_authority=authority,
                    ),
                    expected_workflow_sequence=wait_sequence,
                    expected_task_sequence=committed.checkpoint.task_sequence,
                )
                self.assertEqual(wait_sequence + 1, latest.workflow_sequence)

                snapshot = store._lookup_workflow_delivery_effect(
                    workflow.WorkflowRootKey(root.root_key),
                    delivery.delivery_id,
                    delivery.consumer_generation,
                )
                self.assertIsInstance(snapshot, workflow.WorkflowEffectSnapshot)
                self.assertEqual(wait_sequence, snapshot.checkpoint.workflow_sequence)
                self.assertEqual(delivery, snapshot.checkpoint.pending_delivery)
                self.assertEqual(receipt.receipt_id, snapshot.receipt.receipt_id)

    def test_wait_origin_rejects_wrong_generation_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-store-generation-"
        ) as temporary:
            state_root = _make_state_root(temporary)
            root = _make_root(state_root, temporary)
            _, started = _open_started_store(state_root, root)
            with CoordinationStore(state_root) as store:
                current = _commit_prompt(store, root, started)
                intent = _wait_intent(root, current)
                begun = store.begin_operation(
                    intent,
                    expected_workflow_sequence=current.workflow_sequence,
                    expected_task_sequence=current.task_sequence,
                )
                assert isinstance(begun, workflow.OperationBegin)
                delivery = _wait_delivery(current, outcome=None)
                receipt = _wait_receipt(store, begun.operation, current, delivery)
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
                database = Path(state_root) / "coordination.sqlite3"
                before = database.read_bytes()
                with self.assertRaises(workflow.RecoveryRequired):
                    store._lookup_workflow_delivery_effect(
                        workflow.WorkflowRootKey(root.root_key),
                        delivery.delivery_id,
                        delivery.consumer_generation + 1,
                    )
                self.assertEqual(before, database.read_bytes())

    def test_receipt_mutation_after_issuance_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-store-receipt-"
        ) as temporary:
            state_root = lifecycle_fixtures._state_root(temporary)
            root = lifecycle_fixtures._root(temporary, state_root)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                helper = lifecycle_fixtures.WorkflowStoreLifecycleTests(
                    methodName="runTest"
                )
                current = helper._bootstrap_wait(
                    store,
                    root,
                    outcome=workflow.EventOutcome.SUCCEEDED,
                )
                intent = lifecycle_fixtures._action_intent(
                    root,
                    current,
                    workflow.OperationAction.READ,
                    operation_id="operation-read-mutation",
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
                    receipt_id="receipt-read-mutation",
                    delivery_id=intent.delivery_id,
                )
                draft = helper._action_draft(
                    current,
                    intent,
                    receipt,
                )
                object.__setattr__(receipt, "result_digest", _DIGEST_4)
                assert draft.last_operation is not None
                mutated = replace(
                    draft,
                    last_operation=replace(
                        draft.last_operation,
                        receipt_digest=workflow.durable_receipt_digest(receipt),
                    ),
                )
                with self.assertRaises(workflow.RecoveryRequired):
                    store.commit_effect(begun.operation, receipt, mutated)


if __name__ == "__main__":
    unittest.main()
