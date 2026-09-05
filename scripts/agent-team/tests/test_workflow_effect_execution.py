"""RED execution tests for the private durable workflow-effect adapter."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

import test_workflow_store_transaction as transaction_fixtures

from agent_team import workflow_effect_adapter as adapter
from agent_team import workflow_store as workflow
from agent_team.contracts import (
    Role,
    RoleWait,
    RunRef,
    StartResult,
    StartSpec,
    TerminalRef,
)
from agent_team.store import CoordinationStore


def _start_spec(root: workflow.RootIdentity) -> StartSpec:
    return StartSpec(
        team_id=root.team_id,
        workspace=Path(root.workspace_path),
        config_path=Path(root.config_path),
        state_path=Path(root.state_root_path),
        role_specs={},
    )


class _StoreSpy:
    def __init__(self, store: CoordinationStore, calls: list[str]) -> None:
        self.store = store
        self.calls = calls

    def load_checkpoint(
        self, key: workflow.WorkflowRootKey
    ) -> workflow.WorkflowCheckpointObservation | None:
        self.calls.append("store.load")
        return self.store.load_checkpoint(key)

    def begin_operation(
        self,
        intent: workflow.OperationIntent,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> workflow.OperationBegin | workflow.StoredReplay:
        self.calls.append("store.begin")
        return self.store.begin_operation(
            intent,
            expected_workflow_sequence=expected_workflow_sequence,
            expected_task_sequence=expected_task_sequence,
        )

    def _issue_workflow_receipt(self, **values: object) -> workflow.DurableReceipt:
        self.calls.append("store.issue")
        return self.store._issue_workflow_receipt(**values)  # type: ignore[arg-type]

    def commit_effect(
        self,
        operation: workflow.OperationHandle,
        receipt: workflow.DurableReceipt,
        next_checkpoint: workflow.WorkflowCheckpointDraft,
    ) -> workflow.WorkflowCommit | workflow.StoredReplay:
        self.calls.append("store.commit")
        return self.store.commit_effect(operation, receipt, next_checkpoint)

    def mark_unknown(
        self,
        operation: workflow.OperationHandle,
        *,
        reason: workflow.RecoveryCode,
    ) -> workflow.UnknownCommit:
        self.calls.append("store.unknown")
        return self.store.mark_unknown(operation, reason=reason)

    def _lookup_workflow_effect(
        self, operation_id: workflow.WorkflowOperationId
    ) -> workflow.WorkflowEffectSnapshot:
        self.calls.append("store.lookup-effect")
        return self.store._lookup_workflow_effect(operation_id)

    def _lookup_workflow_delivery_effect(
        self,
        root_key: workflow.WorkflowRootKey,
        delivery_id: str,
        consumer_generation: int,
    ) -> workflow.WorkflowEffectSnapshot:
        self.calls.append("store.lookup-delivery")
        return self.store._lookup_workflow_delivery_effect(
            root_key,
            delivery_id,
            consumer_generation,
        )


class _Authority:
    def __init__(self, calls: list[str], *, expires_ns: int = 1_000) -> None:
        self.calls = calls
        self.expires_ns = expires_ns

    def authorize(
        self, request: adapter.EffectRequestIdentity
    ) -> adapter.WorkflowEffectAuthority:
        self.calls.append("authority.authorize")
        return adapter._issue_workflow_effect_authority(
            request,
            backend_id="backend-1",
            provider_id="provider-1",
            owner="owner-1",
            lease_epoch=0,
            fencing_token=0,
            expires_ns=self.expires_ns,
            authority_ref="authority-1",
            proof_ref="authority-proof-1",
        )

    def validate(
        self,
        authority: adapter.WorkflowEffectAuthority,
        request: adapter.EffectRequestIdentity,
    ) -> None:
        self.calls.append("authority.validate")
        adapter.validate_authority(authority, request=request)

    def validate_observation(
        self,
        authority: adapter.WorkflowEffectAuthority,
        request: adapter.EffectRequestIdentity,
        observation: adapter.BackendEffectObservation,
    ) -> None:
        self.calls.append("authority.validate-observation")
        adapter.validate_observation(
            observation,
            request=request,
            identity=adapter.derive_effect_identity(request, authority),
            authority=authority,
        )

    def validate_lookup(
        self,
        snapshot: workflow.WorkflowEffectSnapshot,
        observation: adapter.BackendEffectObservation,
    ) -> None:
        self.calls.append("authority.validate-lookup")
        if (
            observation.backend_id != "backend-1"
            or observation.provider_id != "provider-1"
            or not observation.provider_proof_ref.startswith("provider-proof-")
            or observation.evidence_ref != snapshot.receipt.evidence_ref
        ):
            raise workflow.OperationIdentityConflict(
                "lookup authority provenance differs"
            )


class _Backend:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.execute_calls = 0
        self.lookup_calls = 0
        self.last_effect: adapter.BackendEffectRequest | None = None

    def durability_capabilities(self) -> adapter.DurableEffectCapabilities:
        self.calls.append("backend.capabilities")
        return adapter.DurableEffectCapabilities(
            version=1,
            effect_key_idempotency=True,
            pure_effect_lookup=True,
            attempt_fence_enforcement=True,
            consumer_generation=True,
            exact_delivery_lookup=True,
            exact_read_lookup=True,
            composite_stop=True,
        )

    def execute(
        self, effect: adapter.BackendEffectRequest
    ) -> adapter.BackendEffectObservation:
        self.calls.append("backend.execute")
        self.execute_calls += 1
        self.last_effect = effect
        if self.fail:
            raise RuntimeError("raw-body-canary")
        self.assert_start_payload(effect)
        run = workflow.RunIdentity("run-1", "terminal-main", 7)
        return adapter._issue_backend_effect_observation(
            effect.request,
            effect.identity,
            effect.authority,
            run=run,
            assignment=None,
            delivery=None,
            public_result=StartResult(
                effect.request.root.team_id,
                RunRef(run.run_id),
                TerminalRef(run.main_terminal_id),
                Path(effect.request.root.state_root_path),
            ),
            effect_ref="provider-effect-start",
            provider_proof_ref="provider-proof-start",
        )

    def lookup(
        self, snapshot: workflow.WorkflowEffectSnapshot
    ) -> adapter.BackendEffectObservation:
        del snapshot
        self.calls.append("backend.lookup")
        self.lookup_calls += 1
        raise AssertionError("START replay must not call backend lookup")

    @staticmethod
    def assert_start_payload(effect: adapter.BackendEffectRequest) -> None:
        if type(effect.payload) is not StartSpec:
            raise AssertionError("START payload was not handed off call-locally")
        if effect.command.action is not workflow.OperationAction.START:
            raise AssertionError("backend received the wrong action")


class _MutatingBackend(_Backend):
    def execute(
        self, effect: adapter.BackendEffectRequest
    ) -> adapter.BackendEffectObservation:
        observation = super().execute(effect)
        object.__setattr__(observation, "owner", "owner-foreign")
        return observation


class _CommitReplayStore(_StoreSpy):
    def commit_effect(
        self,
        operation: workflow.OperationHandle,
        receipt: workflow.DurableReceipt,
        next_checkpoint: workflow.WorkflowCheckpointDraft,
    ) -> workflow.WorkflowCommit | workflow.StoredReplay:
        committed = super().commit_effect(operation, receipt, next_checkpoint)
        if not isinstance(committed, workflow.WorkflowCommit):
            raise TypeError("commit replay fixture did not commit first")
        return workflow.StoredReplay(
            operation_id=receipt.operation_id,
            receipt=committed.receipt,
            checkpoint=committed.checkpoint,
        )


class _ForeignCommitReplayStore(_CommitReplayStore):
    def commit_effect(
        self,
        operation: workflow.OperationHandle,
        receipt: workflow.DurableReceipt,
        next_checkpoint: workflow.WorkflowCheckpointDraft,
    ) -> workflow.WorkflowCommit | workflow.StoredReplay:
        replay = super().commit_effect(operation, receipt, next_checkpoint)
        assert isinstance(replay, workflow.StoredReplay)
        return dataclasses.replace(replay, operation_id="operation-foreign")


class _ForeignBeginReplayStore(_StoreSpy):
    def load_checkpoint(
        self, key: workflow.WorkflowRootKey
    ) -> workflow.WorkflowCheckpointObservation | None:
        self.calls.append("store.load")
        del key
        return None

    def begin_operation(
        self,
        intent: workflow.OperationIntent,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> workflow.OperationBegin | workflow.StoredReplay:
        replay = super().begin_operation(
            intent,
            expected_workflow_sequence=expected_workflow_sequence,
            expected_task_sequence=expected_task_sequence,
        )
        if not isinstance(replay, workflow.StoredReplay):
            raise TypeError("begin replay fixture did not find committed operation")
        return dataclasses.replace(replay, operation_id="operation-foreign")


class _MissingMarkerBeginReplayStore(_ForeignBeginReplayStore):
    def begin_operation(
        self,
        intent: workflow.OperationIntent,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> workflow.OperationBegin | workflow.StoredReplay:
        replay = super().begin_operation(
            intent,
            expected_workflow_sequence=expected_workflow_sequence,
            expected_task_sequence=expected_task_sequence,
        )
        assert isinstance(replay, workflow.StoredReplay)
        object.__setattr__(replay, "operation_id", intent.operation_id)
        object.__setattr__(replay.checkpoint, "last_operation", None)
        return replay


class _ForeignMarkerBeginReplayStore(_ForeignBeginReplayStore):
    def begin_operation(
        self,
        intent: workflow.OperationIntent,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> workflow.OperationBegin | workflow.StoredReplay:
        replay = super().begin_operation(
            intent,
            expected_workflow_sequence=expected_workflow_sequence,
            expected_task_sequence=expected_task_sequence,
        )
        assert isinstance(replay, workflow.StoredReplay)
        object.__setattr__(replay, "operation_id", intent.operation_id)
        last = replay.checkpoint.last_operation
        if last is None:
            raise TypeError("foreign marker fixture lacks a checkpoint marker")
        object.__setattr__(
            replay.checkpoint,
            "last_operation",
            dataclasses.replace(last, operation_id="operation-other"),
        )
        return replay


class _Projector:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.project_calls = 0

    def project(
        self,
        current: workflow.WorkflowCheckpointObservation | None,
        request: adapter.EffectRequestIdentity,
        observation: adapter.BackendEffectObservation,
        receipt: workflow.DurableReceipt,
    ) -> adapter.EffectProjection:
        self.calls.append("projector.project")
        self.project_calls += 1
        if self.fail:
            raise RuntimeError("raw-output-canary")
        if current is not None:
            raise AssertionError("START projection received an existing checkpoint")
        run = workflow.RunIdentity(
            observation.run_id,
            observation.main_terminal_id,
            observation.consumer_generation,
        )
        draft = workflow.WorkflowCheckpointDraft(
            root=request.root,
            run=run,
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
                expected_workflow_sequence=request.expected_workflow_sequence,
                expected_task_sequence=request.expected_task_sequence,
                status=workflow.OperationStatus.COMMITTED,
                receipt_id=receipt.receipt_id,
                receipt_digest=workflow.durable_receipt_digest(receipt),
            ),
        )
        return adapter.EffectProjection(draft, observation.public_result)


class _RebindingProjector(_Projector):
    def __init__(self, calls: list[str], backend: _Backend) -> None:
        super().__init__(calls)
        self.backend = backend

    def project(
        self,
        current: workflow.WorkflowCheckpointObservation | None,
        request: adapter.EffectRequestIdentity,
        observation: adapter.BackendEffectObservation,
        receipt: workflow.DurableReceipt,
    ) -> adapter.EffectProjection:
        projection = super().project(current, request, observation, receipt)
        effect = self.backend.last_effect
        if effect is None:
            raise AssertionError("rebinding projector lacks backend authority")
        object.__setattr__(observation, "effect_ref", "provider-effect-rebound")
        object.__setattr__(
            observation,
            "evidence_ref",
            adapter._expected_observation_evidence(
                identity=effect.identity,
                authority=effect.authority,
                effect_ref=observation.effect_ref,
                provider_proof_ref=observation.provider_proof_ref,
                result_digest=observation.result_digest,
            ),
        )
        object.__setattr__(
            observation,
            "observation_digest",
            adapter._domain_digest(
                adapter._OBSERVATION_DOMAIN,
                adapter._observation_mapping(observation),
            ),
        )
        return projection


class WorkflowEffectExecutionTests(unittest.TestCase):
    def _fixture(
        self,
        *,
        expires_ns: int = 1_000,
        backend_fail: bool = False,
        projector_fail: bool = False,
    ) -> tuple[
        tempfile.TemporaryDirectory[str],
        Path,
        workflow.RootIdentity,
        list[str],
        CoordinationStore,
        _StoreSpy,
        _Authority,
        _Backend,
        _Projector,
    ]:
        temporary = tempfile.TemporaryDirectory(prefix="effect-execution-")
        state_root = transaction_fixtures._make_state_root(temporary.name)
        root = transaction_fixtures._make_root(state_root, temporary.name)
        calls: list[str] = []
        store = CoordinationStore(state_root, clock=lambda: 100)
        store_spy = _StoreSpy(store, calls)
        authority = _Authority(calls, expires_ns=expires_ns)
        backend = _Backend(calls, fail=backend_fail)
        projector = _Projector(calls, fail=projector_fail)
        return (
            temporary,
            state_root,
            root,
            calls,
            store,
            store_spy,
            authority,
            backend,
            projector,
        )

    def test_start_executes_once_and_returns_only_after_commit(self) -> None:
        fixture = self._fixture()
        temporary, _, root, calls, store, store_spy, authority, backend, projector = (
            fixture
        )
        try:
            runtime = adapter.WorkflowEffectAdapter(
                store_spy,
                backend,
                authority,
                projector,
                clock=lambda: 100,
            )
            spec = _start_spec(root)
            result = runtime.execute(
                adapter.make_start_command(spec),
                root=root,
                payload=spec,
            )
            self.assertIsInstance(result, adapter.AppliedEffect)
            assert isinstance(result, adapter.AppliedEffect)
            self.assertIsInstance(result.public_result, StartResult)
            self.assertEqual(1, backend.execute_calls)
            self.assertEqual(1, projector.project_calls)
            self.assertEqual(
                [
                    "backend.capabilities",
                    "store.load",
                    "authority.authorize",
                    "authority.validate",
                    "store.begin",
                    "authority.validate",
                    "backend.execute",
                    "authority.validate-observation",
                    "authority.validate",
                    "store.issue",
                    "authority.validate",
                    "projector.project",
                    "authority.validate",
                    "store.commit",
                ],
                calls,
            )
            loaded = store.load_checkpoint(workflow.WorkflowRootKey(root.root_key))
            self.assertEqual(result.checkpoint, loaded)
        finally:
            store.close()
            temporary.cleanup()

    def test_action_capability_and_expiry_reject_before_backend(self) -> None:
        fixture = self._fixture(expires_ns=100)
        temporary, _, root, calls, store, store_spy, authority, backend, projector = (
            fixture
        )
        try:
            runtime = adapter.WorkflowEffectAdapter(
                store_spy,
                backend,
                authority,
                projector,
                clock=lambda: 100,
            )
            with self.assertRaises(workflow.OperationIdentityConflict):
                runtime.execute(
                    adapter.make_start_command(_start_spec(root)),
                    root=root,
                    payload=_start_spec(root),
                )
            self.assertEqual(0, backend.execute_calls)
            self.assertNotIn("store.begin", calls)
        finally:
            store.close()
            temporary.cleanup()

        fixture = self._fixture()
        temporary, _, root, calls, store, store_spy, authority, backend, projector = (
            fixture
        )
        try:
            backend_caps = backend.durability_capabilities()
            object.__setattr__(backend_caps, "exact_delivery_lookup", False)
            backend.durability_capabilities = lambda: backend_caps  # type: ignore[method-assign]
            runtime = adapter.WorkflowEffectAdapter(
                store_spy,
                backend,
                authority,
                projector,
                clock=lambda: 100,
            )
            calls.clear()
            with self.assertRaises(adapter.DurabilityUnsupported):
                runtime.execute(
                    adapter.make_request_command(RoleWait(Role.WORKER, 1)),
                    root=root,
                )
            self.assertEqual([], calls)
            self.assertEqual(0, backend.execute_calls)
        finally:
            store.close()
            temporary.cleanup()

    def test_backend_or_projector_failure_marks_unknown_without_retry(self) -> None:
        for backend_fail, projector_fail in ((True, False), (False, True)):
            with self.subTest(backend_fail=backend_fail, projector_fail=projector_fail):
                fixture = self._fixture(
                    backend_fail=backend_fail,
                    projector_fail=projector_fail,
                )
                (
                    temporary,
                    _,
                    root,
                    calls,
                    store,
                    store_spy,
                    authority,
                    backend,
                    projector,
                ) = fixture
                try:
                    runtime = adapter.WorkflowEffectAdapter(
                        store_spy,
                        backend,
                        authority,
                        projector,
                        clock=lambda: 100,
                    )
                    spec = _start_spec(root)
                    with self.assertRaises(workflow.RecoveryRequired) as raised:
                        runtime.execute(
                            adapter.make_start_command(spec),
                            root=root,
                            payload=spec,
                        )
                    self.assertNotIn("raw-body-canary", str(raised.exception))
                    self.assertNotIn("raw-output-canary", str(raised.exception))
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertEqual(1, backend.execute_calls)
                    self.assertEqual(1, calls.count("store.unknown"))
                    self.assertEqual(0, calls.count("backend.lookup"))
                    checkpoint = store.load_checkpoint(
                        workflow.WorkflowRootKey(root.root_key)
                    )
                    self.assertIsInstance(checkpoint, workflow.WorkflowRootSeed)
                    assert isinstance(checkpoint, workflow.WorkflowRootSeed)
                    self.assertEqual(2, checkpoint.workflow_sequence)
                    self.assertIs(
                        workflow.OperationStatus.UNKNOWN_EFFECT,
                        checkpoint.operation_status,
                    )
                finally:
                    store.close()
                    temporary.cleanup()

    def test_observation_identity_mismatch_marks_unknown_before_projection(
        self,
    ) -> None:
        fixture = self._fixture()
        temporary, _, root, calls, store, store_spy, authority, _, projector = fixture
        backend = _MutatingBackend(calls)
        try:
            runtime = adapter.WorkflowEffectAdapter(
                store_spy,
                backend,
                authority,
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
            self.assertEqual(0, projector.project_calls)
            self.assertEqual(1, calls.count("store.unknown"))
            self.assertNotIn("store.issue", calls)
        finally:
            store.close()
            temporary.cleanup()

    def test_commit_stored_replay_is_success_not_recovery(self) -> None:
        fixture = self._fixture()
        temporary, _, root, calls, store, _, authority, backend, projector = fixture
        try:
            runtime = adapter.WorkflowEffectAdapter(
                _CommitReplayStore(store, calls),
                backend,
                authority,
                projector,
                clock=lambda: 100,
            )
            spec = _start_spec(root)
            result = runtime.execute(
                adapter.make_start_command(spec),
                root=root,
                payload=spec,
            )
            self.assertIsInstance(result, adapter.ReplayedEffect)
            self.assertEqual(1, backend.execute_calls)
            self.assertEqual(0, backend.lookup_calls)
        finally:
            store.close()
            temporary.cleanup()

    def test_stored_replay_response_identity_is_revalidated(self) -> None:
        for wrapper_type, precommit in (
            (_ForeignCommitReplayStore, False),
            (_ForeignBeginReplayStore, True),
            (_MissingMarkerBeginReplayStore, True),
            (_ForeignMarkerBeginReplayStore, True),
        ):
            with self.subTest(wrapper=wrapper_type.__name__):
                fixture = self._fixture()
                (
                    temporary,
                    _,
                    root,
                    calls,
                    store,
                    store_spy,
                    authority,
                    backend,
                    projector,
                ) = fixture
                try:
                    spec = _start_spec(root)
                    if precommit:
                        normal = adapter.WorkflowEffectAdapter(
                            store_spy,
                            backend,
                            authority,
                            projector,
                            clock=lambda: 100,
                        )
                        normal.execute(
                            adapter.make_start_command(spec),
                            root=root,
                            payload=spec,
                        )
                    execute_count = backend.execute_calls
                    runtime = adapter.WorkflowEffectAdapter(
                        wrapper_type(store, calls),
                        backend,
                        authority,
                        projector,
                        clock=lambda: 100,
                    )
                    with self.assertRaises(workflow.OperationIdentityConflict):
                        runtime.execute(
                            adapter.make_start_command(spec),
                            root=root,
                            payload=spec,
                        )
                    expected_execute_count = (
                        execute_count if precommit else execute_count + 1
                    )
                    self.assertEqual(expected_execute_count, backend.execute_calls)
                finally:
                    store.close()
                    temporary.cleanup()

    def test_projector_cannot_rebind_validated_observation(self) -> None:
        fixture = self._fixture()
        temporary, _, root, calls, store, store_spy, authority, backend, _ = fixture
        try:
            runtime = adapter.WorkflowEffectAdapter(
                store_spy,
                backend,
                authority,
                _RebindingProjector(calls, backend),
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
            self.assertEqual(1, calls.count("store.unknown"))
            self.assertNotIn("store.commit", calls)
        finally:
            store.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
