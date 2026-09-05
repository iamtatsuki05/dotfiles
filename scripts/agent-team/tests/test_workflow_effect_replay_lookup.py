"""Replay and pure lookup tests for committed durable workflow effects."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from unittest import mock

import test_workflow_store_transaction as transaction_fixtures
from test_workflow_effect_execution import _Authority, _start_spec, _StoreSpy
from test_workflow_effect_ladder import _LadderBackend, _LadderProjector

from agent_team import workflow_effect_adapter as adapter
from agent_team import workflow_store as workflow
from agent_team.contracts import (
    DeliveryAck,
    DeliveryRef,
    MessageRef,
    MessageReply,
    ReadReceipt,
    Role,
    RolePrompt,
    RoleRead,
    RoleWait,
    WaitReceipt,
)
from agent_team.store import CoordinationStore


class _TamperedOriginStore(_StoreSpy):
    def _lookup_workflow_delivery_effect(
        self,
        root_key: workflow.WorkflowRootKey,
        delivery_id: str,
        consumer_generation: int,
    ) -> workflow.WorkflowEffectSnapshot:
        snapshot = super()._lookup_workflow_delivery_effect(
            root_key,
            delivery_id,
            consumer_generation,
        )
        assignment = snapshot.checkpoint.active_assignment
        if assignment is None:
            raise AssertionError("origin fixture lacks an assignment")
        object.__setattr__(
            snapshot.checkpoint,
            "active_assignment",
            dataclasses.replace(assignment, attempt=assignment.attempt + 1),
        )
        return snapshot


class WorkflowEffectReplayLookupTests(unittest.TestCase):
    def test_committed_replay_and_wait_read_lookup_do_not_repeat_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effect-replay-") as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            calls: list[str] = []
            backend = _LadderBackend(calls)
            projector = _LadderProjector(calls)
            start_operation_id: str
            read_operation_id: str
            execute_count: int
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                runtime = adapter.WorkflowEffectAdapter(
                    _StoreSpy(store, calls),
                    backend,
                    _Authority(calls, expires_ns=10_000),
                    projector,
                    clock=lambda: 100,
                )
                spec = _start_spec(root)
                started = runtime.execute(
                    adapter.make_start_command(spec), root=root, payload=spec
                )
                assert isinstance(started, adapter.AppliedEffect)
                start_operation_id = started.operation_id
                runtime.execute(
                    adapter.make_request_command(
                        RolePrompt(Role.WORKER, "prompt-body-canary")
                    ),
                    root=root,
                    payload="prompt-body-canary",
                )
                start_command = adapter.make_start_command(spec)
                start_request = adapter.derive_effect_request_identity(
                    start_command,
                    root=root,
                    run=None,
                    assignment=None,
                    pending_delivery=None,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                start_authority = adapter._issue_workflow_effect_authority(
                    start_request,
                    backend_id="backend-1",
                    provider_id="provider-1",
                    owner="owner-1",
                    lease_epoch=0,
                    fencing_token=0,
                    expires_ns=10_000,
                    authority_ref="authority-1",
                    proof_ref="authority-proof-1",
                )
                start_identity = adapter.derive_effect_identity(
                    start_request,
                    start_authority,
                )
                later_replay = store.begin_operation(
                    adapter._operation_intent(
                        start_request,
                        start_identity,
                        start_authority,
                    ),
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(later_replay, workflow.StoredReplay)
                assert isinstance(later_replay, workflow.StoredReplay)
                self.assertGreater(later_replay.checkpoint.workflow_sequence, 2)
                adapter._validate_stored_replay_response(
                    later_replay,
                    start_request,
                    start_identity,
                )
                runtime.execute(
                    adapter.make_request_command(RoleWait(Role.WORKER, 1)),
                    root=root,
                )
                runtime.execute(
                    adapter.make_request_command(
                        MessageReply(MessageRef("message-1"), "reply-body-canary")
                    ),
                    root=root,
                    payload="reply-body-canary",
                )
                runtime.execute(
                    adapter.make_request_command(
                        DeliveryAck(DeliveryRef("delivery-1"))
                    ),
                    root=root,
                )

                execute_count = len(backend.execute_actions)
                before = (state_root / "coordination.sqlite3").read_bytes()
                wait = runtime.lookup_delivery(
                    root_key=workflow.WorkflowRootKey(root.root_key),
                    delivery_id="delivery-1",
                    consumer_generation=7,
                )
                self.assertIsInstance(wait, adapter.DurableDeliveryLookup)
                self.assertIsInstance(wait.result, WaitReceipt)
                self.assertEqual("question-body-canary", wait.result.events[0].body)
                self.assertEqual("delivery-1", wait.snapshot.receipt.delivery_id)
                with self.assertRaises(TypeError):
                    adapter.DurableDeliveryLookup(wait.snapshot, wait.result)
                self.assertEqual(execute_count, len(backend.execute_actions))
                self.assertEqual(
                    before, (state_root / "coordination.sqlite3").read_bytes()
                )
                lookup_count = calls.count("backend.lookup")
                with self.assertRaises(workflow.RecoveryRequired):
                    runtime.lookup_delivery(
                        root_key=workflow.WorkflowRootKey(root.root_key),
                        delivery_id="delivery-1",
                        consumer_generation=8,
                    )
                self.assertEqual(lookup_count, calls.count("backend.lookup"))

                replayed = runtime.replay(
                    workflow.WorkflowOperationId(start_operation_id)
                )
                self.assertIsInstance(replayed, adapter.ReplayedEffect)
                self.assertEqual(start_operation_id, replayed.operation_id)
                self.assertEqual(lookup_count, calls.count("backend.lookup"))
                self.assertEqual(execute_count, len(backend.execute_actions))

                runtime.execute(
                    adapter.make_request_command(RoleWait(Role.WORKER, 1)),
                    root=root,
                )
                read = runtime.execute(
                    adapter.make_request_command(RoleRead(Role.WORKER, 8)),
                    root=root,
                )
                assert isinstance(read, adapter.AppliedEffect)
                read_operation_id = read.operation_id
                self.assertIsInstance(read.public_result, ReadReceipt)
                execute_count = len(backend.execute_actions)

            with CoordinationStore(state_root, clock=lambda: 100) as reopened:
                resumed = adapter.WorkflowEffectAdapter(
                    _StoreSpy(reopened, calls),
                    backend,
                    _Authority(calls, expires_ns=10_000),
                    projector,
                    clock=lambda: 100,
                )
                before = (state_root / "coordination.sqlite3").read_bytes()
                with (
                    mock.patch.object(
                        backend,
                        "lookup",
                        side_effect=workflow.RecoveryRequired("raw-output-canary"),
                    ),
                    self.assertRaises(
                        adapter.EffectRecoveryRequired
                    ) as raw_lookup_raised,
                ):
                    resumed.lookup_read(workflow.WorkflowOperationId(read_operation_id))
                self.assertNotIn("raw-output-canary", str(raw_lookup_raised.exception))
                self.assertIsNone(raw_lookup_raised.exception.__cause__)
                read_lookup = resumed.lookup_read(
                    workflow.WorkflowOperationId(read_operation_id)
                )
                self.assertIsInstance(read_lookup, adapter.DurableReadLookup)
                self.assertEqual("read-output-canary\n", read_lookup.result.output)
                self.assertEqual(read_operation_id, read_lookup.snapshot.operation_id)
                with self.assertRaises(TypeError):
                    adapter.DurableReadLookup(
                        read_lookup.snapshot,
                        read_lookup.result,
                    )
                self.assertEqual(execute_count, len(backend.execute_actions))
                self.assertEqual(
                    before, (state_root / "coordination.sqlite3").read_bytes()
                )
                snapshot = reopened._lookup_workflow_effect(
                    workflow.WorkflowOperationId(read_operation_id)
                )
                observation = backend.observations[snapshot.receipt.effect_ref]
                original_backend = observation.backend_id
                object.__setattr__(observation, "backend_id", "backend-foreign")
                object.__setattr__(
                    observation,
                    "observation_digest",
                    adapter._domain_digest(
                        adapter._OBSERVATION_DOMAIN,
                        adapter._observation_mapping(observation),
                    ),
                )
                with self.assertRaises(workflow.RecoveryRequired):
                    resumed.lookup_read(workflow.WorkflowOperationId(read_operation_id))
                object.__setattr__(observation, "backend_id", original_backend)
                object.__setattr__(
                    observation,
                    "observation_digest",
                    adapter._domain_digest(
                        adapter._OBSERVATION_DOMAIN,
                        adapter._observation_mapping(observation),
                    ),
                )
                object.__setattr__(
                    observation,
                    "public_result",
                    ReadReceipt("tampered-output-canary"),
                )
                before = (state_root / "coordination.sqlite3").read_bytes()
                with self.assertRaises(
                    workflow.RecoveryRequired
                ) as tampered_lookup_raised:
                    resumed.lookup_read(workflow.WorkflowOperationId(read_operation_id))
                self.assertNotIn(
                    "tampered-output-canary",
                    str(tampered_lookup_raised.exception),
                )
                self.assertEqual(
                    before, (state_root / "coordination.sqlite3").read_bytes()
                )
                database = (state_root / "coordination.sqlite3").read_bytes()
                for canary in (
                    b"prompt-body-canary",
                    b"reply-body-canary",
                    b"question-body-canary",
                    b"read-output-canary",
                ):
                    self.assertNotIn(canary, database)

    def test_delivery_origin_assignment_mismatch_rejects_before_backend(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effect-origin-mismatch-") as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            calls: list[str] = []
            backend = _LadderBackend(calls)
            projector = _LadderProjector(calls)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                normal = adapter.WorkflowEffectAdapter(
                    _StoreSpy(store, calls),
                    backend,
                    _Authority(calls, expires_ns=10_000),
                    projector,
                    clock=lambda: 100,
                )
                spec = _start_spec(root)
                normal.execute(
                    adapter.make_start_command(spec), root=root, payload=spec
                )
                normal.execute(
                    adapter.make_request_command(
                        RolePrompt(Role.WORKER, "prompt-body-canary")
                    ),
                    root=root,
                    payload="prompt-body-canary",
                )
                normal.execute(
                    adapter.make_request_command(RoleWait(Role.WORKER, 1)),
                    root=root,
                )
                execute_count = len(backend.execute_actions)
                before = (state_root / "coordination.sqlite3").read_bytes()
                tampered = adapter.WorkflowEffectAdapter(
                    _TamperedOriginStore(store, calls),
                    backend,
                    _Authority(calls, expires_ns=10_000),
                    projector,
                    clock=lambda: 100,
                )
                with self.assertRaises(workflow.OperationIdentityConflict):
                    tampered.execute(
                        adapter.make_request_command(
                            MessageReply(
                                MessageRef("message-1"),
                                "reply-body-canary",
                            )
                        ),
                        root=root,
                        payload="reply-body-canary",
                    )
                self.assertEqual(execute_count, len(backend.execute_actions))
                self.assertEqual(
                    before,
                    (state_root / "coordination.sqlite3").read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
