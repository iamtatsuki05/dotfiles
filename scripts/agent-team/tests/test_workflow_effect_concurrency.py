"""Concurrent begin arbitration for one stable workflow effect identity."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing

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


class _BarrierStore(_StoreSpy):
    def __init__(
        self,
        store: CoordinationStore,
        calls: list[str],
        barrier: threading.Barrier,
    ) -> None:
        super().__init__(store, calls)
        self.barrier = barrier

    def begin_operation(
        self,
        intent: workflow.OperationIntent,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> workflow.OperationBegin | workflow.StoredReplay:
        self.barrier.wait(timeout=10)
        return super().begin_operation(
            intent,
            expected_workflow_sequence=expected_workflow_sequence,
            expected_task_sequence=expected_task_sequence,
        )


class WorkflowEffectConcurrencyTests(unittest.TestCase):
    def test_same_stable_start_identity_invokes_backend_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effect-concurrency-") as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            with CoordinationStore(state_root):
                pass
            calls: list[str] = []
            backend = _Backend(calls)
            projector = _Projector(calls)
            authority = _Authority(calls, expires_ns=10_000)
            barrier = threading.Barrier(2)
            outcomes: list[object] = []
            outcome_lock = threading.Lock()

            def run() -> None:
                try:
                    with CoordinationStore(state_root, clock=lambda: 100) as store:
                        runtime = adapter.WorkflowEffectAdapter(
                            _BarrierStore(store, calls, barrier),
                            backend,
                            authority,
                            projector,
                            clock=lambda: 100,
                        )
                        spec = _start_spec(root)
                        outcome: object = runtime.execute(
                            adapter.make_start_command(spec),
                            root=root,
                            payload=spec,
                        )
                except workflow.RecoveryRequired as exc:
                    outcome = exc
                except Exception as exc:  # noqa: BLE001 - retain unexpected thread outcome
                    outcome = exc
                with outcome_lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=run, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive(), "concurrent effect thread hung")

            self.assertEqual(2, len(outcomes))
            self.assertEqual(1, backend.execute_calls)
            self.assertEqual(
                1,
                sum(isinstance(item, adapter.AppliedEffect) for item in outcomes),
            )
            self.assertTrue(
                any(
                    isinstance(
                        item, (adapter.ReplayedEffect, workflow.RecoveryRequired)
                    )
                    for item in outcomes
                )
            )
            unexpected = [
                item
                for item in outcomes
                if not isinstance(
                    item,
                    (
                        adapter.AppliedEffect,
                        adapter.ReplayedEffect,
                        workflow.RecoveryRequired,
                    ),
                )
            ]
            self.assertEqual([], unexpected)
            with closing(
                sqlite3.connect(str(state_root / "coordination.sqlite3"))
            ) as db:
                self.assertEqual(
                    1,
                    db.execute("SELECT COUNT(*) FROM workflow_operations").fetchone()[
                        0
                    ],
                )
                self.assertEqual(
                    1,
                    db.execute("SELECT COUNT(*) FROM workflow_receipts").fetchone()[0],
                )
            applied = next(
                item for item in outcomes if isinstance(item, adapter.AppliedEffect)
            )
            assert isinstance(applied, adapter.AppliedEffect)
            execute_count = backend.execute_calls
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                runtime = adapter.WorkflowEffectAdapter(
                    _StoreSpy(store, calls),
                    backend,
                    authority,
                    projector,
                    clock=lambda: 100,
                )
                replayed = runtime.replay(
                    workflow.WorkflowOperationId(applied.operation_id)
                )
                self.assertIsInstance(replayed, adapter.ReplayedEffect)
            self.assertEqual(execute_count, backend.execute_calls)


if __name__ == "__main__":
    unittest.main()
