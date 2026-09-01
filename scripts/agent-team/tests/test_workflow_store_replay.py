"""Replay and readback tests for the durable workflow Store facade."""

from __future__ import annotations

import copy
import operator
import os
import pickle
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from agent_team import workflow_store as workflow
from agent_team.store import (
    CoordinationStore,
    StoreIntegrityError,
)

_WORKFLOW_FACADE_METHODS = (
    "load_checkpoint",
    "begin_operation",
    "commit_effect",
    "lookup_operation",
    "mark_unknown",
)
_RECOVERY_ERRORS = (
    workflow.RecoveryRequired,
    workflow.OperationIdentityConflict,
    workflow.StateConflict,
    StoreIntegrityError,
)


class _EffectProbe:
    """Fake effect boundary used to prove that readback stays side-effect free."""

    def __init__(self) -> None:
        self.execute_calls = 0
        self.status_calls = 0
        self.retry_calls = 0
        self.release_calls = 0
        self.ack_calls = 0
        self.cleanup_calls = 0

    def execute(self) -> None:
        self.execute_calls += 1

    def status(self) -> None:
        self.status_calls += 1

    def retry(self) -> None:
        self.retry_calls += 1

    def release(self) -> None:
        self.release_calls += 1

    def ack(self) -> None:
        self.ack_calls += 1

    def cleanup(self) -> None:
        self.cleanup_calls += 1

    def assert_quiescent(self, test_case: unittest.TestCase) -> None:
        test_case.assertEqual(
            {
                "execute": self.execute_calls,
                "status": self.status_calls,
                "retry": self.retry_calls,
                "release": self.release_calls,
                "ack": self.ack_calls,
                "cleanup": self.cleanup_calls,
            },
            {
                "execute": 0,
                "status": 0,
                "retry": 0,
                "release": 0,
                "ack": 0,
                "cleanup": 0,
            },
        )


class WorkflowStoreReplayTests(unittest.TestCase):
    """Read-only replay and recovery properties for Issue #72."""

    @staticmethod
    def _digest(value: str, *, domain: bytes = workflow.REQUEST_DIGEST_DOMAIN) -> str:
        return workflow.digest_bounded_body(value.encode("utf-8"), domain=domain)

    @staticmethod
    def _state_root(parent: str) -> Path:
        state_root = Path(os.path.realpath(parent)) / "state"
        state_root.mkdir()
        state_root.chmod(0o700)
        return state_root

    @staticmethod
    def _path_identity(path: Path) -> workflow.PathIdentity:
        metadata = path.stat()
        return workflow.PathIdentity(
            path=str(path),
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
        )

    def _root(self, parent: str, state_root: Path) -> workflow.RootIdentity:
        workspace = Path(os.path.realpath(parent)) / "workspace"
        workspace.mkdir()
        workspace.chmod(0o700)
        config = Path(os.path.realpath(parent)) / "agent-team-config.toml"
        config_bytes = b"team = 'team-1'\n"
        config.write_bytes(config_bytes)
        config.chmod(0o600)
        config_metadata = config.stat()
        return workflow.RootIdentity(
            root_key="root-1",
            team_id="team-1",
            workspace=self._path_identity(workspace),
            config_path=str(config),
            config_device=int(config_metadata.st_dev),
            config_inode=int(config_metadata.st_ino),
            config_digest=workflow.config_content_digest(config_bytes),
            state_root=self._path_identity(state_root),
        )

    def _require_facade(self, store: CoordinationStore) -> None:
        missing = tuple(
            name
            for name in _WORKFLOW_FACADE_METHODS
            if not callable(getattr(store, name, None))
        )
        self.assertFalse(
            missing,
            "intended workflow-store facade is missing on CoordinationStore: "
            + ", ".join(missing),
        )

    @staticmethod
    def _workflow_snapshot(
        store: CoordinationStore,
    ) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
        connection = store._connection
        if connection is None:
            return ()
        result: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
        for table in (
            "workflow_checkpoints",
            "workflow_operations",
            "workflow_receipts",
            "workflow_events",
        ):
            rows = tuple(
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            )
            result.append((table, rows))
        return tuple(result)

    def _start_intent(self, root: workflow.RootIdentity) -> workflow.OperationIntent:
        return workflow.OperationIntent(
            operation_id="operation-start",
            effect_key="effect-start",
            root_key=root.root_key,
            root=root,
            action=workflow.OperationAction.START,
            request_digest=self._digest("start-request"),
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
            evidence_ref=None,
        )

    @staticmethod
    def _committed_checkpoint(
        root: workflow.RootIdentity,
        receipt: workflow.DurableReceipt,
    ) -> workflow.WorkflowCheckpointDraft:
        return workflow.WorkflowCheckpointDraft(
            root=root,
            run=workflow.RunIdentity(
                run_id="run-1",
                main_terminal_id="terminal-main",
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

    def _commit_start(
        self,
        store: CoordinationStore,
        root: workflow.RootIdentity,
    ) -> tuple[
        workflow.OperationIntent,
        workflow.OperationHandle,
        workflow.DurableReceipt,
        workflow.WorkflowCheckpointV4,
    ]:
        intent = self._start_intent(root)
        begun = store.begin_operation(
            intent,
            expected_workflow_sequence=0,
            expected_task_sequence=None,
        )
        self.assertIsInstance(begun, workflow.OperationBegin)
        assert isinstance(begun, workflow.OperationBegin)
        receipt = store._issue_workflow_receipt(
            operation=begun.operation,
            receipt_id="receipt-start",
            run_id="run-1",
            main_terminal_id="terminal-main",
            consumer_generation=0,
            task_id=None,
            dispatch_id=None,
            attempt=None,
            terminal_id=None,
            delivery_id=None,
            message_id=None,
            effect_ref="effect-ref-start",
            result_kind="started",
            result_digest=self._digest(
                "start-result", domain=workflow.DELIVERY_DIGEST_DOMAIN
            ),
            evidence_ref=self._digest(
                "start-evidence", domain=workflow.EVENT_BODY_DIGEST_DOMAIN
            ),
            issued_ns=2,
        )
        committed = store.commit_effect(
            begun.operation,
            receipt,
            self._committed_checkpoint(root, receipt),
        )
        self.assertIsInstance(committed, workflow.WorkflowCommit)
        assert isinstance(committed, workflow.WorkflowCommit)
        return intent, begun.operation, receipt, committed.checkpoint

    def test_lookup_missing_operation_is_recovery_and_quiescent(self) -> None:
        """Missing lookup is not an effect-absence proof or retry permission."""

        probe = _EffectProbe()
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                before = self._workflow_snapshot(store)
                with self.assertRaises(workflow.RecoveryRequired):
                    store.lookup_operation(
                        workflow.WorkflowOperationId("missing-operation")
                    )
                self.assertEqual(before, self._workflow_snapshot(store))
        probe.assert_quiescent(self)

    def test_exact_committed_operation_returns_stored_replay_without_effect(
        self,
    ) -> None:
        probe = _EffectProbe()
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                intent, _, receipt, checkpoint = self._commit_start(store, root)
                before = self._workflow_snapshot(store)

            with CoordinationStore(state_root) as resumed:
                self._require_facade(resumed)
                replay = resumed.begin_operation(
                    intent,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
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
                lookup = resumed.lookup_operation(
                    workflow.WorkflowOperationId(intent.operation_id)
                )
                self.assertIsInstance(lookup, workflow.OperationLookup)
                assert lookup is not None
                self.assertEqual(workflow.OperationStatus.COMMITTED, lookup.status)
                self.assertEqual(receipt.receipt_id, lookup.receipt_id)
                self.assertEqual(
                    workflow.durable_receipt_digest(receipt), lookup.receipt_digest
                )
                self.assertEqual(before, self._workflow_snapshot(resumed))
        probe.assert_quiescent(self)

    def test_intent_and_unknown_replay_require_recovery_without_status_query(
        self,
    ) -> None:
        probe = _EffectProbe()
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                intent = self._start_intent(root)
                begun = store.begin_operation(
                    intent,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                before_intent = self._workflow_snapshot(store)
                with self.assertRaises(workflow.RecoveryRequired):
                    store.begin_operation(
                        intent,
                        expected_workflow_sequence=0,
                        expected_task_sequence=None,
                    )
                self.assertEqual(before_intent, self._workflow_snapshot(store))

                unknown = store.mark_unknown(
                    begun.operation,
                    reason=workflow.RecoveryCode.UNKNOWN_EFFECT,
                )
                self.assertIsInstance(unknown, workflow.UnknownCommit)
                self.assertEqual(
                    workflow.OperationStatus.UNKNOWN_EFFECT, unknown.status
                )
                before_unknown = self._workflow_snapshot(store)
                with self.assertRaises(workflow.RecoveryRequired):
                    store.begin_operation(
                        intent,
                        expected_workflow_sequence=0,
                        expected_task_sequence=None,
                    )
                with self.assertRaises(workflow.RecoveryRequired):
                    store.lookup_operation(
                        workflow.WorkflowOperationId(intent.operation_id)
                    )
                self.assertEqual(before_unknown, self._workflow_snapshot(store))
        probe.assert_quiescent(self)

    def test_old_committed_operation_replays_after_a_later_transition(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                intent, handle, receipt, checkpoint = self._commit_start(store, root)
                authority = workflow.AuthorityReference(
                    "review-ref", self._digest("review-authority")
                )
                transition = workflow.PolicyOrVerificationTransition(
                    kind=workflow.TransitionKind.POLICY,
                    root_key=root.root_key,
                    authority=authority,
                    expected_workflow_sequence=2,
                    expected_task_sequence=None,
                    next_task_sequence=None,
                    actor="policy-authority",
                    request_digest=self._digest("policy-transition"),
                )
                next_draft = workflow.WorkflowCheckpointDraft(
                    root=root,
                    run=checkpoint.run,
                    workflow_sequence=3,
                    task_sequence=None,
                    execution_mode=workflow.ExecutionMode.SERIAL,
                    workflow_state=workflow.CheckpointState.IDLE,
                    task_policy=None,
                    active_assignment=None,
                    pending_delivery=None,
                    replied_message_ids=(),
                    read_observed=False,
                    released=False,
                    review_authority=authority,
                    verification_authority=None,
                    last_operation=checkpoint.last_operation,
                )
                latest = store.commit_transition(
                    transition,
                    next_draft,
                    expected_workflow_sequence=2,
                    expected_task_sequence=None,
                )
                replay = store.begin_operation(
                    intent,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(replay, workflow.StoredReplay)
                assert isinstance(replay, workflow.StoredReplay)
                self.assertEqual(3, replay.checkpoint.workflow_sequence)
                self.assertEqual(latest, replay.checkpoint)
                self.assertEqual(
                    workflow.durable_receipt_digest(receipt),
                    workflow.durable_receipt_digest(replay.receipt),
                )
                commit_replay = store.commit_effect(
                    handle,
                    receipt,
                    self._committed_checkpoint(root, receipt),
                )
                self.assertIsInstance(commit_replay, workflow.StoredReplay)
                assert isinstance(commit_replay, workflow.StoredReplay)
                self.assertEqual(latest, commit_replay.checkpoint)

    def test_changed_digest_identity_generation_or_order_rejects_state_unchanged(
        self,
    ) -> None:
        probe = _EffectProbe()
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                intent, _, _, _ = self._commit_start(store, root)
                before = self._workflow_snapshot(store)
                changed_digest = workflow.OperationIntent(
                    operation_id=intent.operation_id,
                    effect_key=intent.effect_key,
                    root_key=intent.root_key,
                    root=intent.root,
                    action=intent.action,
                    request_digest=self._digest("changed-request"),
                    expected_workflow_sequence=intent.expected_workflow_sequence,
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
                    owner=intent.owner,
                    lease_epoch=0,
                    fencing_token=0,
                    actor=intent.actor,
                    evidence_ref=None,
                )
                changed_generation = workflow.OperationIntent(
                    operation_id=intent.operation_id,
                    effect_key=intent.effect_key,
                    root_key=intent.root_key,
                    root=intent.root,
                    action=intent.action,
                    request_digest=intent.request_digest,
                    expected_workflow_sequence=intent.expected_workflow_sequence,
                    expected_task_sequence=None,
                    run_id=None,
                    main_terminal_id=None,
                    task_id=None,
                    dispatch_id=None,
                    attempt=None,
                    terminal_id=None,
                    delivery_id=None,
                    message_id=None,
                    consumer_generation=1,
                    owner=intent.owner,
                    lease_epoch=0,
                    fencing_token=0,
                    actor=intent.actor,
                    evidence_ref=None,
                )
                changed_identity = workflow.OperationIntent(
                    operation_id=intent.operation_id,
                    effect_key=intent.effect_key,
                    root_key=intent.root_key,
                    root=intent.root,
                    action=intent.action,
                    request_digest=intent.request_digest,
                    expected_workflow_sequence=intent.expected_workflow_sequence,
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
                    owner="owner-2",
                    lease_epoch=0,
                    fencing_token=0,
                    actor=intent.actor,
                    evidence_ref=None,
                )
                changed_order = workflow.OperationIntent(
                    operation_id=intent.operation_id,
                    effect_key=intent.effect_key,
                    root_key=intent.root_key,
                    root=None,
                    action=workflow.OperationAction.PROMPT,
                    request_digest=self._digest("order-changed"),
                    expected_workflow_sequence=2,
                    expected_task_sequence=None,
                    run_id="run-1",
                    main_terminal_id="terminal-main",
                    task_id=None,
                    dispatch_id=None,
                    attempt=None,
                    terminal_id=None,
                    delivery_id="delivery-1",
                    message_id="message-2",
                    consumer_generation=1,
                    owner=intent.owner,
                    lease_epoch=0,
                    fencing_token=0,
                    actor=intent.actor,
                    evidence_ref=None,
                    next_task_sequence=1,
                )
                for candidate in (
                    changed_digest,
                    changed_generation,
                    changed_identity,
                    changed_order,
                ):
                    with self.subTest(
                        operation=candidate.action.value,
                        digest=candidate.request_digest,
                        generation=candidate.consumer_generation,
                        message=candidate.message_id,
                    ):
                        with self.assertRaises(_RECOVERY_ERRORS):
                            store.begin_operation(
                                candidate,
                                expected_workflow_sequence=candidate.expected_workflow_sequence,
                                expected_task_sequence=candidate.expected_task_sequence,
                            )
                        self.assertEqual(before, self._workflow_snapshot(store))
        probe.assert_quiescent(self)

    def test_receipt_checkpoint_event_tamper_cannot_produce_stored_replay(self) -> None:
        probe = _EffectProbe()
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                intent, _, receipt, _ = self._commit_start(store, root)
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                before = self._workflow_snapshot(store)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE workflow_receipts SET result_digest = ? "
                        "WHERE receipt_id = ?",
                        (self._digest("forged-result"), receipt.receipt_id),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE workflow_events SET actor = 'forged-actor' "
                        "WHERE operation_id = ?",
                        (intent.operation_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM workflow_events WHERE operation_id = ?",
                        (intent.operation_id,),
                    )
                self.assertEqual(before, self._workflow_snapshot(store))

            database = state_root / "coordination.sqlite3"
            with closing(sqlite3.connect(str(database))) as connection:
                connection.execute(
                    "UPDATE workflow_checkpoints SET checkpoint_digest = ? "
                    "WHERE root_key = ?",
                    (self._digest("forged-checkpoint"), root.root_key),
                )
                connection.commit()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)
        probe.assert_quiescent(self)

    def test_mark_unknown_is_idempotent_and_cannot_downgrade_committed(self) -> None:
        probe = _EffectProbe()
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                intent = self._start_intent(root)
                begun = store.begin_operation(
                    intent,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                first = store.mark_unknown(
                    begun.operation,
                    reason=workflow.RecoveryCode.RESPONSE_LOST,
                )
                before_second = self._workflow_snapshot(store)
                second = store.mark_unknown(
                    begun.operation,
                    reason=workflow.RecoveryCode.RESPONSE_LOST,
                )
                self.assertEqual(first, second)
                self.assertEqual(before_second, self._workflow_snapshot(store))

            with tempfile.TemporaryDirectory(
                prefix="agent-team-workflow-replay-committed-"
            ) as committed_temporary:
                committed_root_path = self._state_root(committed_temporary)
                committed_root = self._root(committed_temporary, committed_root_path)
                with CoordinationStore(committed_root_path) as committed_store:
                    committed_intent, committed_handle, _, _ = self._commit_start(
                        committed_store, committed_root
                    )
                    before_downgrade = self._workflow_snapshot(committed_store)
                    with self.assertRaises(_RECOVERY_ERRORS):
                        committed_store.mark_unknown(
                            committed_handle,
                            reason=workflow.RecoveryCode.UNKNOWN_EFFECT,
                        )
                    self.assertEqual(
                        before_downgrade,
                        self._workflow_snapshot(committed_store),
                    )
                    lookup = committed_store.lookup_operation(
                        workflow.WorkflowOperationId(committed_intent.operation_id)
                    )
                    self.assertIsInstance(lookup, workflow.OperationLookup)
                    assert lookup is not None
                    self.assertEqual(
                        workflow.OperationStatus.COMMITTED,
                        lookup.status,
                    )
        probe.assert_quiescent(self)

    def test_restart_uses_stable_operation_id_and_rejects_serialized_handle(
        self,
    ) -> None:
        probe = _EffectProbe()
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                intent = self._start_intent(root)
                begun = store.begin_operation(
                    intent,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(begun, workflow.OperationBegin)
                assert isinstance(begun, workflow.OperationBegin)
                with self.assertRaises(TypeError):
                    copy.copy(begun.operation)
                with self.assertRaises(TypeError):
                    copy.deepcopy(begun.operation)
                with self.assertRaises(TypeError):
                    pickle.dumps(begun.operation)

            with CoordinationStore(state_root) as resumed:
                self._require_facade(resumed)
                # The stable ID is the only restart input.  The unresolved
                # intent must remain recovery-required; no synthetic handle or
                # automatic effect/status call is allowed.
                with self.assertRaises(workflow.RecoveryRequired):
                    resumed.lookup_operation(
                        workflow.WorkflowOperationId(intent.operation_id)
                    )
                with self.assertRaises(TypeError):
                    resumed.commit_effect(
                        object(),  # type: ignore[arg-type]
                        object(),  # type: ignore[arg-type]
                        object(),  # type: ignore[arg-type]
                    )
        probe.assert_quiescent(self)

    def test_raw_body_replay_is_explicitly_unavailable_and_never_persisted(
        self,
    ) -> None:
        probe = _EffectProbe()
        hostile_body = "raw-body-must-not-be-replayed-or-persisted"
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-replay-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._require_facade(store)
                intent, _, receipt, checkpoint = self._commit_start(store, root)
                replay = store.begin_operation(
                    intent,
                    expected_workflow_sequence=0,
                    expected_task_sequence=None,
                )
                self.assertIsInstance(replay, workflow.StoredReplay)
                assert isinstance(replay, workflow.StoredReplay)
                self.assertNotIn("body", workflow.DurableReceipt.__slots__)
                self.assertNotIn("response", workflow.DurableReceipt.__slots__)
                self.assertNotIn(hostile_body, repr(receipt))
                self.assertNotIn(hostile_body, repr(checkpoint))
                self.assertNotIn(hostile_body, repr(replay))
                with self.assertRaises(AttributeError):
                    operator.attrgetter("body")(replay.receipt)
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                for table in (
                    "workflow_checkpoints",
                    "workflow_operations",
                    "workflow_receipts",
                    "workflow_events",
                ):
                    for row in connection.execute(f"SELECT * FROM {table}"):
                        self.assertNotIn(hostile_body, repr(tuple(row)))
        probe.assert_quiescent(self)

    def test_normal_open_rejects_changed_config_content(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-config-drift-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._commit_start(store, root)
            Path(root.config_path).write_text(
                "team = 'changed-team'\n",
                encoding="utf-8",
            )
            with self.assertRaises(workflow.OperationIdentityConflict):
                CoordinationStore(state_root)

    def test_normal_open_rejects_semantically_rewritten_event_chain(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-event-tamper-"
        ) as temporary:
            state_root = self._state_root(temporary)
            root = self._root(temporary, state_root)
            with CoordinationStore(state_root) as store:
                self._commit_start(store, root)
            database = state_root / "coordination.sqlite3"
            with closing(sqlite3.connect(str(database))) as connection:
                trigger_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'workflow_events_no_update'"
                ).fetchone()[0]
                connection.execute("DROP TRIGGER workflow_events_no_update")
                connection.execute(
                    "UPDATE workflow_events SET from_state = 'FAILED', "
                    "to_state = 'FAILED' WHERE workflow_sequence = 1"
                )
                connection.execute(
                    "UPDATE workflow_events SET from_state = 'FAILED' "
                    "WHERE workflow_sequence = 2"
                )
                connection.execute(str(trigger_sql))
                connection.commit()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)


if __name__ == "__main__":
    unittest.main()
