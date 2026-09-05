"""Fault barriers and restart handoff for the private effect adapter."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest

import test_workflow_store_transaction as transaction_fixtures
from test_workflow_effect_execution import (
    _Authority,
    _Backend,
    _Projector,
    _start_spec,
    _StoreSpy,
)

from agent_team import workflow_effect_adapter as adapter
from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore


class _FaultStore(CoordinationStore):
    fault_point: str | None = None

    def _fault(self, point: str) -> None:
        if point == self.fault_point:
            raise sqlite3.OperationalError("injected workflow commit fault")


class WorkflowEffectFaultTests(unittest.TestCase):
    def test_commit_faults_mark_unknown_without_backend_retry(self) -> None:
        for point in (
            "before_workflow_receipt_insert",
            "before_workflow_commit_checkpoint",
            "before_workflow_commit_event",
        ):
            with (
                self.subTest(point=point),
                tempfile.TemporaryDirectory(prefix="effect-commit-fault-") as temporary,
            ):
                state_root = transaction_fixtures._make_state_root(temporary)
                root = transaction_fixtures._make_root(state_root, temporary)
                calls: list[str] = []
                backend = _Backend(calls)
                projector = _Projector(calls)
                with _FaultStore(state_root, clock=lambda: 100) as store:
                    store.fault_point = point
                    runtime = adapter.WorkflowEffectAdapter(
                        _StoreSpy(store, calls),
                        backend,
                        _Authority(calls, expires_ns=10_000),
                        projector,
                        clock=lambda: 100,
                    )
                    spec = _start_spec(root)
                    with self.assertRaises(workflow.RecoveryRequired):
                        runtime.execute(
                            adapter.make_start_command(spec),
                            root=root,
                            payload=spec,
                        )
                    self.assertEqual(1, backend.execute_calls)
                    self.assertEqual(0, backend.lookup_calls)
                    checkpoint = store.load_checkpoint(
                        workflow.WorkflowRootKey(root.root_key)
                    )
                    self.assertIsInstance(checkpoint, workflow.WorkflowRootSeed)
                    assert isinstance(checkpoint, workflow.WorkflowRootSeed)
                    self.assertIs(
                        workflow.OperationStatus.UNKNOWN_EFFECT,
                        checkpoint.operation_status,
                    )

    def test_authority_expiry_after_begin_marks_unknown_before_backend(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="effect-expiry-after-begin-"
        ) as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            calls: list[str] = []
            backend = _Backend(calls)
            clock_values = iter((100, 1_000))
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                runtime = adapter.WorkflowEffectAdapter(
                    _StoreSpy(store, calls),
                    backend,
                    _Authority(calls, expires_ns=500),
                    _Projector(calls),
                    clock=lambda: next(clock_values),
                )
                spec = _start_spec(root)
                with self.assertRaises(workflow.RecoveryRequired):
                    runtime.execute(
                        adapter.make_start_command(spec),
                        root=root,
                        payload=spec,
                    )
                self.assertEqual(0, backend.execute_calls)
                self.assertEqual(1, calls.count("store.unknown"))
                checkpoint = store.load_checkpoint(
                    workflow.WorkflowRootKey(root.root_key)
                )
                self.assertIsInstance(checkpoint, workflow.WorkflowRootSeed)
                assert isinstance(checkpoint, workflow.WorkflowRootSeed)
                self.assertIs(
                    workflow.OperationStatus.UNKNOWN_EFFECT,
                    checkpoint.operation_status,
                )

    def test_authority_expiry_at_receipt_time_cannot_commit(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="effect-expiry-at-receipt-"
        ) as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            calls: list[str] = []
            backend = _Backend(calls)
            clock_values = iter((100, 100, 100, 1_000, 1_000, 1_000))
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                runtime = adapter.WorkflowEffectAdapter(
                    _StoreSpy(store, calls),
                    backend,
                    _Authority(calls, expires_ns=500),
                    _Projector(calls),
                    clock=lambda: next(clock_values),
                )
                spec = _start_spec(root)
                with self.assertRaises(workflow.RecoveryRequired):
                    runtime.execute(
                        adapter.make_start_command(spec),
                        root=root,
                        payload=spec,
                    )
                self.assertEqual(1, backend.execute_calls)
                self.assertEqual(1, calls.count("store.unknown"))
                self.assertNotIn("store.commit", calls)

    def test_authority_expiry_after_projection_cannot_commit(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="effect-expiry-after-projector-"
        ) as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            calls: list[str] = []
            backend = _Backend(calls)
            projector = _Projector(calls)
            clock_values = iter((100, 100, 100, 100, 100, 1_000))
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                runtime = adapter.WorkflowEffectAdapter(
                    _StoreSpy(store, calls),
                    backend,
                    _Authority(calls, expires_ns=500),
                    projector,
                    clock=lambda: next(clock_values),
                )
                spec = _start_spec(root)
                with self.assertRaises(workflow.RecoveryRequired):
                    runtime.execute(
                        adapter.make_start_command(spec),
                        root=root,
                        payload=spec,
                    )
                self.assertEqual(1, backend.execute_calls)
                self.assertEqual(1, projector.project_calls)
                self.assertEqual(1, calls.count("store.unknown"))
                self.assertNotIn("store.commit", calls)

    def test_restart_intent_handoff_never_synthesizes_handle_or_retries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effect-restart-intent-") as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            command = adapter.make_start_command(_start_spec(root))
            request = adapter.derive_effect_request_identity(
                command,
                root=root,
                run=None,
                assignment=None,
                pending_delivery=None,
                expected_workflow_sequence=0,
                expected_task_sequence=None,
            )
            authority = adapter._issue_workflow_effect_authority(
                request,
                backend_id="backend-1",
                provider_id="provider-1",
                owner="owner-1",
                lease_epoch=0,
                fencing_token=0,
                expires_ns=10_000,
                authority_ref="authority-1",
                proof_ref="authority-proof-1",
            )
            identity = adapter.derive_effect_identity(request, authority)
            intent = adapter._operation_intent(request, identity, authority)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                begun = store.begin_operation(
                    intent,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)

            calls: list[str] = []
            backend = _Backend(calls)
            with CoordinationStore(state_root, clock=lambda: 100) as reopened:
                runtime = adapter.WorkflowEffectAdapter(
                    _StoreSpy(reopened, calls),
                    backend,
                    _Authority(calls, expires_ns=10_000),
                    _Projector(calls),
                    clock=lambda: 100,
                )
                with self.assertRaises(workflow.RecoveryRequired):
                    runtime.replay(workflow.WorkflowOperationId(identity.operation_id))
            self.assertEqual(0, backend.execute_calls)
            self.assertEqual(0, backend.lookup_calls)
            self.assertNotIn("store.unknown", calls)


if __name__ == "__main__":
    unittest.main()
