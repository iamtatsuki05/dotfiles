"""Contract tests for the schema-4 review checkpoint producer.

The positive fixture uses the actual typed workflow Store and review-policy
reducer.  SQL is limited to negative corruption injection and rejection
snapshots; it never creates the accepted current pair or its event history.
"""

from __future__ import annotations

import importlib
import inspect
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from unittest import mock

import test_policy_verification_handoff_authority as authority_fixtures
import test_policy_verification_handoff_composer as composer_fixtures
import test_task_verification_schema as schema_fixtures
import test_workflow_store_transaction as workflow_fixtures

from agent_team import policy_verification_handoff as handoff_module
from agent_team import review_policy, verification_gate
from agent_team import store as store_module
from agent_team import task_verification_ledger as task_ledger
from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore, StoreError, StoreIntegrityError
from agent_team.task_policy import TaskPhase, TaskPolicyStateV4


def _producer_module() -> Any:
    try:
        return importlib.import_module("agent_team._review_workflow_store")
    except ModuleNotFoundError as exc:
        if exc.name != "agent_team._review_workflow_store":
            raise
        raise AssertionError(
            "missing-review-checkpoint-producer: typed producer is unavailable"
        ) from None


@dataclass(frozen=True, slots=True)
class _ReviewChain:
    policy: review_policy.SerialReviewPolicy
    assigned: review_policy.ReviewPolicyUpdate
    worker_done: review_policy.ReviewPolicyUpdate
    review_pending: review_policy.ReviewPolicyUpdate
    approved: review_policy.ReviewPolicyUpdate


def _review_chain(
    root: workflow.RootIdentity,
    *,
    attempt_id: str = "attempt-1",
    completion_explanation: str | None = None,
) -> _ReviewChain:
    task = authority_fixtures._path_task()
    policy = authority_fixtures._review_policy(task)
    pending = review_policy.initial_review_policy_state(
        review_policy.RunId("run-1"),
        authority_fixtures._review_state(
            team_id=root.team_id,
            workspace=root.workspace_path,
            task_id=str(task.task_id),
        ),
    )
    assignment = authority_fixtures._assignment(
        attempt=attempt_id,
        task_id=str(task.task_id),
    )
    assigned = review_policy.reduce_policy(
        pending,
        review_policy.AssignmentCommand(
            expected_sequence=0,
            assignment=assignment,
        ),
        policy,
    )
    completion = authority_fixtures._completion(assignment)
    if completion_explanation is not None:
        completion = replace(completion, explanation=completion_explanation)
    worker_done = review_policy.reduce_policy(
        assigned.next_state,
        completion,
        policy,
    )
    review_pending = review_policy.reduce_policy(
        worker_done.next_state,
        review_policy.ReviewRequest(
            expected_sequence=worker_done.next_state.task_state.sequence,
            completion=completion,
        ),
        policy,
    )
    approved = review_policy.reduce_policy(
        review_pending.next_state,
        authority_fixtures._review_decision(assignment),
        policy,
    )
    return _ReviewChain(policy, assigned, worker_done, review_pending, approved)


def _prompt_intent(
    root: workflow.RootIdentity,
    current: workflow.WorkflowCheckpointV4,
) -> workflow.OperationIntent:
    return workflow.OperationIntent(
        operation_id="operation-prompt-review",
        effect_key="effect/prompt-review",
        root_key=root.root_key,
        root=None,
        action=workflow.OperationAction.PROMPT,
        request_digest=workflow.digest_bounded_body(
            b"review-prompt-request",
            domain=workflow.REQUEST_DIGEST_DOMAIN,
        ),
        expected_workflow_sequence=current.workflow_sequence,
        expected_task_sequence=current.task_sequence,
        run_id=current.run.run_id,
        main_terminal_id=current.run.main_terminal_id,
        task_id=None,
        dispatch_id=None,
        attempt=None,
        terminal_id=None,
        delivery_id=None,
        message_id=None,
        consumer_generation=current.run.consumer_generation,
        owner="owner-review",
        lease_epoch=0,
        fencing_token=0,
        actor="actor-review",
        evidence_ref=None,
        next_task_sequence=1,
    )


def _prompt_assignment(
    state: TaskPolicyStateV4,
) -> workflow.ActiveAssignment:
    if state.worker_node is None or state.dispatch_id is None or state.task_id is None:
        raise AssertionError("assigned review state is incomplete")
    return workflow.ActiveAssignment(
        role=workflow.AssignmentRole.WORKER,
        worker_node=str(state.worker_node),
        task_id=str(state.task_id),
        attempt=1,
        dispatch_id=str(state.dispatch_id),
        terminal_id="worker-terminal",
        launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
        completion_identity=workflow.CompletionIdentity(
            run_id="run-1",
            task_id=str(state.task_id),
            dispatch_id=str(state.dispatch_id),
            sender_terminal_id="worker-terminal",
        ),
    )


def _commit_prompt(
    store: CoordinationStore,
    root: workflow.RootIdentity,
    current: workflow.WorkflowCheckpointV4,
    assigned_state: TaskPolicyStateV4,
) -> workflow.WorkflowCheckpointV4:
    begun = store.begin_operation(
        _prompt_intent(root, current),
        expected_workflow_sequence=current.workflow_sequence,
        expected_task_sequence=current.task_sequence,
    )
    if type(begun) is not workflow.OperationBegin:
        raise AssertionError("review prompt unexpectedly replayed")
    assignment = _prompt_assignment(assigned_state)
    receipt = store._issue_workflow_receipt(
        operation=begun.operation,
        receipt_id="receipt-prompt-review",
        run_id=current.run.run_id,
        main_terminal_id=current.run.main_terminal_id,
        consumer_generation=current.run.consumer_generation,
        task_id=assignment.task_id,
        dispatch_id=assignment.dispatch_id,
        attempt=assignment.attempt,
        terminal_id=assignment.terminal_id,
        delivery_id=None,
        message_id=None,
        effect_ref="backend/receipt-prompt-review",
        result_kind="assignment",
        result_digest=workflow.assignment_digest(assignment),
        evidence_ref="sha256:" + "3" * 64,
        issued_ns=20,
    )
    state_bytes = task_ledger.encode_task_state(assigned_state)
    state_digest = str(task_ledger.task_state_digest(state_bytes))
    draft = workflow.WorkflowCheckpointDraft(
        root=root,
        run=current.run,
        workflow_sequence=current.workflow_sequence + 2,
        task_sequence=assigned_state.sequence,
        execution_mode=current.execution_mode,
        workflow_state=workflow.CheckpointState.ACTIVE,
        task_policy=workflow.TaskPolicyReference(
            version=assigned_state.version,
            team_id=str(assigned_state.team_id),
            workspace=str(assigned_state.workspace),
            task_id=str(assigned_state.task_id),
            sequence=assigned_state.sequence,
            state_digest=state_digest,
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
            expected_workflow_sequence=current.workflow_sequence,
            expected_task_sequence=current.task_sequence,
            status=workflow.OperationStatus.COMMITTED,
            receipt_id=receipt.receipt_id,
            receipt_digest=workflow.durable_receipt_digest(receipt),
        ),
    )
    committed = store.commit_effect(begun.operation, receipt, draft)
    if type(committed) is not workflow.WorkflowCommit:
        raise AssertionError("review prompt unexpectedly replayed")
    return committed.checkpoint


def _commit_successful_wait(
    store: CoordinationStore,
    root: workflow.RootIdentity,
    current: workflow.WorkflowCheckpointV4,
) -> workflow.WorkflowCheckpointV4:
    begun = store.begin_operation(
        workflow_fixtures._wait_intent(root, current),
        expected_workflow_sequence=current.workflow_sequence,
        expected_task_sequence=current.task_sequence,
    )
    if type(begun) is not workflow.OperationBegin:
        raise AssertionError("review wait unexpectedly replayed")
    delivery = workflow_fixtures._wait_delivery(
        current,
        outcome=workflow.EventOutcome.SUCCEEDED,
    )
    receipt = workflow_fixtures._wait_receipt(
        store,
        begun.operation,
        current,
        delivery,
    )
    committed = store.commit_effect(
        begun.operation,
        receipt,
        workflow_fixtures._wait_draft(
            current,
            receipt,
            delivery,
            state=workflow.CheckpointState.WORKER_DONE,
        ),
    )
    if type(committed) is not workflow.WorkflowCommit:
        raise AssertionError("review wait unexpectedly replayed")
    return committed.checkpoint


def _database_snapshot(state_root: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(str(state_root / "coordination.sqlite3"))
    try:
        checkpoint = connection.execute(
            "SELECT checkpoint_bytes, checkpoint_digest FROM workflow_checkpoints"
        ).fetchall()
        tasks = connection.execute(
            "SELECT state_bytes, state_digest FROM task_policy_states"
        ).fetchall()
        events = connection.execute(
            "SELECT workflow_event_id, event_digest FROM workflow_events "
            "ORDER BY workflow_event_id"
        ).fetchall()
        verification_counts = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("verification_operations", "verification_receipts")
        )
        return tuple(checkpoint), tuple(tasks), tuple(events), verification_counts
    finally:
        connection.close()


def _replace_checkpoint_row(
    connection: sqlite3.Connection,
    checkpoint: workflow.WorkflowCheckpointV4,
) -> None:
    values = CoordinationStore._workflow_projection_values(checkpoint)
    columns = tuple(
        column
        for column in store_module._WORKFLOW_CHECKPOINT_ROW_COLUMNS
        if column != "root_key"
    )
    connection.execute(
        "UPDATE workflow_checkpoints SET "
        + ", ".join(f"{column} = ?" for column in columns)
        + " WHERE root_key = ?",
        (*(values[column] for column in columns), checkpoint.root.root_key),
    )


def _rewrite_event_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    **changes: object,
) -> None:
    values = dict(row)
    values.update(changes)
    values["event_digest"] = CoordinationStore._workflow_event_digest(values)
    columns = (*changes, "event_digest")
    connection.execute("DROP TRIGGER workflow_events_no_update")
    try:
        connection.execute(
            "UPDATE workflow_events SET "
            + ", ".join(f"{column} = ?" for column in columns)
            + " WHERE workflow_event_id = ?",
            (
                *(values[column] for column in columns),
                values["workflow_event_id"],
            ),
        )
    finally:
        connection.execute(store_module._WORKFLOW_EVENTS_NO_UPDATE_TRIGGER_SQL)


@dataclass(slots=True)
class _Fixture:
    temporary: tempfile.TemporaryDirectory[str]
    state_root: Path
    root: workflow.RootIdentity
    store: CoordinationStore
    current: workflow.WorkflowCheckpointV4
    chain: _ReviewChain
    handoff: handoff_module.PolicyVerificationHandoff
    authority_store: composer_fixtures._FakePolicyVerificationStore
    refs: tuple[review_policy.ReviewAuthorityRef, ...]


class _FaultStore(CoordinationStore):
    def __init__(self, state_root: Path) -> None:
        self.review_fault: str | None = None
        super().__init__(state_root)

    def _fault(self, point: str) -> None:
        if point == self.review_fault:
            raise RuntimeError("injected review policy fault")


class _RootDriftStore(CoordinationStore):
    def __init__(self, state_root: Path) -> None:
        self.drift_path: Path | None = None
        self.drift_point = "after_review_policy_checkpoint_write"
        self.drifted = False
        super().__init__(state_root)

    def _fault(self, point: str) -> None:
        if (
            point == self.drift_point
            and self.drift_path is not None
            and not self.drifted
        ):
            self.drift_path.write_bytes(
                self.drift_path.read_bytes() + b"\n# review config drift\n"
            )
            self.drifted = True


class _EventIdResultStore(CoordinationStore):
    @staticmethod
    def _workflow_insert_event(*args: object, **kwargs: object) -> Any:
        insert_event = cast(
            Callable[..., Any],
            CoordinationStore._workflow_insert_event,
        )
        row = insert_event(*args, **kwargs)
        values = dict(row)
        values["workflow_event_id"] += 1_000
        values["event_digest"] = CoordinationStore._workflow_event_digest(values)
        return values


class _StringSubclass(str):
    pass


class ReviewCheckpointProducerTests(unittest.TestCase):
    def _fixture(
        self,
        *,
        attempt_id: str = "attempt-1",
        store_type: type[CoordinationStore] = CoordinationStore,
    ) -> _Fixture:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-review-checkpoint-")
        self.addCleanup(temporary.cleanup)
        state_root = workflow_fixtures._make_state_root(temporary.name)
        root = replace(
            workflow_fixtures._make_root(state_root, temporary.name),
            team_id="team",
        )
        chain = _review_chain(root, attempt_id=attempt_id)
        workflow_fixtures._open_started_store(state_root, root)
        store = store_type(state_root)
        self.addCleanup(store.close)
        started = store.load_checkpoint(workflow.WorkflowRootKey(root.root_key))
        self.assertIs(type(started), workflow.WorkflowCheckpointV4)
        assert type(started) is workflow.WorkflowCheckpointV4
        prompted = _commit_prompt(
            store,
            root,
            started,
            chain.assigned.next_state.task_state,
        )
        current = _commit_successful_wait(store, root, prompted)
        self.assertIs(current.workflow_state, workflow.CheckpointState.WORKER_DONE)
        self.assertEqual(
            chain.assigned.next_state.task_state.sequence,
            current.task_sequence,
        )

        authority_store = composer_fixtures._FakePolicyVerificationStore()
        handoff = handoff_module.PolicyVerificationHandoff(authority_store)
        refs = tuple(
            handoff.save_authority(update, chain.policy)
            for update in (
                chain.worker_done,
                chain.review_pending,
                chain.approved,
            )
        )
        return _Fixture(
            temporary,
            state_root,
            root,
            store,
            current,
            chain,
            handoff,
            authority_store,
            refs,
        )

    @staticmethod
    def _producer(fixture: _Fixture) -> Any:
        module = _producer_module()
        producer_type = getattr(module, "ReviewCheckpointProducer", None)
        if not isinstance(producer_type, type):
            raise TypeError(
                "missing-review-checkpoint-producer: producer class is unavailable"
            )
        return producer_type(fixture.handoff, fixture.store)

    def _commit_chain(self, fixture: _Fixture) -> tuple[Any, Any, Any]:
        producer = self._producer(fixture)
        current = fixture.current
        results: list[Any] = []
        updates = (
            fixture.chain.worker_done,
            fixture.chain.review_pending,
            fixture.chain.approved,
        )
        for index, (update, reference) in enumerate(zip(updates, fixture.refs)):
            result = producer.commit(
                current,
                update,
                fixture.chain.policy,
                reference,
            )
            self.assertEqual(
                current.workflow_sequence + 1, result.checkpoint.workflow_sequence
            )
            self.assertEqual(update.next_state.task_state, result.task.state)
            self.assertEqual(
                task_ledger.encode_task_state(update.next_state.task_state),
                result.task.state_bytes,
            )
            self.assertEqual(
                str(task_ledger.task_state_digest(result.task.state_bytes)),
                result.task.state_digest,
            )
            self.assertEqual(
                result.task.state_digest, result.checkpoint.task_policy.state_digest
            )
            self.assertEqual(
                result.task.state.sequence, result.checkpoint.task_sequence
            )
            self.assertIsNone(result.event.operation_id)
            self.assertIsNone(result.event.receipt_id)
            self.assertEqual("policy_transition", result.event.kind)
            self.assertEqual(
                result.checkpoint.checkpoint_digest, result.event.checkpoint_digest
            )
            self.assertTrue(result.event.request_digest.startswith("sha256:"))
            self.assertTrue(result.event.evidence_ref.startswith("sha256:"))
            self.assertTrue(result.event.event_digest.startswith("sha256:"))
            self.assertEqual(
                reference.reference, result.checkpoint.review_authority.reference
            )
            self.assertIsNone(result.checkpoint.verification_authority)
            self.assertEqual(
                current.active_assignment, result.checkpoint.active_assignment
            )
            self.assertEqual(
                current.pending_delivery, result.checkpoint.pending_delivery
            )
            self.assertEqual(
                current.replied_message_ids, result.checkpoint.replied_message_ids
            )
            self.assertEqual(current.read_observed, result.checkpoint.read_observed)
            self.assertEqual(current.released, result.checkpoint.released)
            self.assertEqual(current.last_operation, result.checkpoint.last_operation)
            expected_intent = (
                fixture.chain.review_pending.effects[0] if index == 1 else None
            )
            self.assertEqual(expected_intent, result.reviewer_assignment)
            self.assertFalse(result.replayed)
            current = result.checkpoint
            results.append(result)
        return cast(tuple[Any, Any, Any], tuple(results))

    def test_three_actual_policy_edges_commit_as_separate_transactions(self) -> None:
        fixture = self._fixture()
        first, second, third = self._commit_chain(fixture)
        self.assertIs(
            first.checkpoint.workflow_state, workflow.CheckpointState.WORKER_DONE
        )
        self.assertIs(
            second.checkpoint.workflow_state, workflow.CheckpointState.REVIEW_PENDING
        )
        self.assertIs(
            third.checkpoint.workflow_state, workflow.CheckpointState.REVIEW_PENDING
        )
        self.assertIs(third.task.state.phase, TaskPhase.APPROVED)
        producer = self._producer(fixture)
        observed = producer.read(workflow.WorkflowRootKey(fixture.root.root_key))
        self.assertIsNotNone(observed)
        self.assertEqual(3, len(observed.events))
        self.assertEqual(
            (0, 0),
            (
                observed.verification_operation_count,
                observed.verification_receipt_count,
            ),
        )

    def test_producer_surface_has_no_projection_digest_or_effect_input(self) -> None:
        module = _producer_module()
        self.assertEqual(
            tuple(inspect.signature(module.ReviewCheckpointProducer.commit).parameters),
            ("self", "current", "update", "policy", "review_ref"),
        )
        self.assertEqual(
            set(module.ReviewPolicyCommitRequest.__slots__),
            {"current", "binding", "_issuer", "__weakref__"},
        )
        with self.assertRaises(TypeError):
            module.ReviewPolicyCommitRequest()

    def test_store_port_and_forged_request_errors_are_sanitized(self) -> None:
        fixture = self._fixture()
        module = _producer_module()

        class LeakyStore:
            def __init__(self) -> None:
                self.calls = 0

            def commit_review_policy(self, request: object) -> object:
                del request
                self.calls += 1
                raise RuntimeError("STORE_SECRET_CANARY")

            def load_review_checkpoint(self, key: object) -> object:
                del key
                self.calls += 1
                raise RuntimeError("READ_SECRET_CANARY")

        store = LeakyStore()
        producer = module.ReviewCheckpointProducer(
            fixture.handoff,
            cast(Any, store),
        )
        callbacks: tuple[Callable[[], object], ...] = (
            lambda: producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            ),
            lambda: producer.read(workflow.WorkflowRootKey(fixture.root.root_key)),
        )
        for callback in callbacks:
            with self.assertRaises(module.ReviewCheckpointError) as raised:
                callback()
            self.assertNotIn("CANARY", str(raised.exception))
            self.assertIsNone(raised.exception.__context__)
        self.assertEqual(0, store.calls)
        forged = object.__new__(module.ReviewPolicyCommitRequest)
        with self.assertRaises(workflow.OperationIdentityConflict) as raised:
            fixture.store.commit_review_policy(forged)
        self.assertNotIn("attribute", str(raised.exception).lower())
        real_producer = self._producer(fixture)
        legitimate = module._issue_review_policy_commit_request(
            real_producer,
            fixture.current,
            fixture.handoff._bind_review_authority(
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            ),
        )
        other_handoff = handoff_module.PolicyVerificationHandoff(
            fixture.authority_store
        )
        other_ref = other_handoff.save_authority(
            fixture.chain.worker_done,
            fixture.chain.policy,
        )
        foreign_binding = other_handoff._bind_review_authority(
            fixture.chain.worker_done,
            fixture.chain.policy,
            other_ref,
        )
        with self.assertRaises(module.ReviewCheckpointError):
            module._issue_review_policy_commit_request(
                real_producer,
                fixture.current,
                foreign_binding,
            )
        shaped_forged = object.__new__(module.ReviewPolicyCommitRequest)
        object.__setattr__(shaped_forged, "current", fixture.current)
        object.__setattr__(
            shaped_forged,
            "binding",
            foreign_binding,
        )
        object.__setattr__(
            shaped_forged,
            "_issuer",
            object.__getattribute__(legitimate, "_issuer"),
        )
        with self.assertRaises(workflow.OperationIdentityConflict):
            fixture.store.commit_review_policy(shaped_forged)

    def test_producer_handoff_and_store_binding_is_immutable(self) -> None:
        fixture = self._fixture()
        module = _producer_module()
        producer = self._producer(fixture)
        with self.assertRaises(TypeError):
            producer._handoff = handoff_module.PolicyVerificationHandoff(
                fixture.authority_store
            )
        other_handoff = handoff_module.PolicyVerificationHandoff(
            fixture.authority_store
        )
        before = _database_snapshot(fixture.state_root)
        object.__setattr__(producer, "_handoff", other_handoff)
        with self.assertRaises(module.ReviewCheckpointError):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))
        second = self._producer(fixture)
        object.__setattr__(second, "_store", object())
        with self.assertRaises(module.ReviewCheckpointError):
            second.read(workflow.WorkflowRootKey(fixture.root.root_key))

    def test_lower_store_request_revalidates_current_producer_binding(self) -> None:
        module = _producer_module()
        for attribute in ("_handoff", "_store"):
            with self.subTest(attribute=attribute):
                fixture = self._fixture()
                producer = self._producer(fixture)
                request = module._issue_review_policy_commit_request(
                    producer,
                    fixture.current,
                    fixture.handoff._bind_review_authority(
                        fixture.chain.worker_done,
                        fixture.chain.policy,
                        fixture.refs[0],
                    ),
                )
                replacement: object
                if attribute == "_handoff":
                    replacement = handoff_module.PolicyVerificationHandoff(
                        fixture.authority_store
                    )
                else:
                    replacement = object()
                object.__setattr__(producer, attribute, replacement)
                before = _database_snapshot(fixture.state_root)
                with self.assertRaises(workflow.OperationIdentityConflict):
                    fixture.store.commit_review_policy(request)
                self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_producer_cannot_be_reinitialized(self) -> None:
        fixture = self._fixture()
        module = _producer_module()
        producer = self._producer(fixture)
        other_handoff = handoff_module.PolicyVerificationHandoff(
            fixture.authority_store
        )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises(module.ReviewCheckpointError):
            producer.__init__(other_handoff, fixture.store)
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_producer_rejects_handoff_owner_store_rebinding(self) -> None:
        fixture = self._fixture()
        module = _producer_module()
        producer = self._producer(fixture)
        foreign_store = composer_fixtures._FakePolicyVerificationStore()
        handoff_module.PolicyVerificationHandoff.__init__(
            fixture.handoff,
            foreign_store,
        )
        foreign_ref = fixture.handoff.save_authority(
            fixture.chain.worker_done,
            fixture.chain.policy,
        )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises(module.ReviewCheckpointError):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                foreign_ref,
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_registered_store_results_are_correlated_to_the_request(self) -> None:
        fixture = self._fixture()
        foreign = self._fixture(attempt_id="attempt-2")
        module = _producer_module()
        foreign_producer = self._producer(foreign)
        foreign_result = foreign_producer.commit(
            foreign.current,
            foreign.chain.worker_done,
            foreign.chain.policy,
            foreign.refs[0],
        )
        foreign_observation = foreign_producer.read(
            workflow.WorkflowRootKey(foreign.root.root_key)
        )
        self.assertIsNotNone(foreign_observation)
        foreign_request = module._issue_review_policy_commit_request(
            foreign_producer,
            foreign.current,
            foreign.handoff._bind_review_authority(
                foreign.chain.worker_done,
                foreign.chain.policy,
                foreign.refs[0],
            ),
        )
        foreign_plan = module._plan_review_policy_request(
            foreign_request,
            foreign.store,
        )
        foreign_registration = module._registered_review_workflow_store(foreign.store)
        self.assertIsNotNone(foreign_registration)
        with self.assertRaises(module.ReviewCheckpointError):
            module._validate_review_policy_commit_result(
                replace(
                    foreign_result,
                    event=replace(
                        foreign_result.event,
                        event_digest="sha256:" + "f" * 64,
                    ),
                ),
                foreign_plan,
                foreign_registration,
            )

        producer = self._producer(fixture)
        request = module._issue_review_policy_commit_request(
            producer,
            fixture.current,
            fixture.handoff._bind_review_authority(
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            ),
        )
        plan = module._plan_review_policy_request(request, fixture.store)
        registration = module._registered_review_workflow_store(fixture.store)
        self.assertIsNotNone(registration)
        with self.assertRaises(module.ReviewCheckpointError):
            module._validate_review_policy_commit_result(
                foreign_result,
                plan,
                registration,
            )
        with self.assertRaises(module.ReviewCheckpointError):
            module._validate_review_checkpoint_observation_result(
                foreign_observation,
                workflow.WorkflowRootKey(fixture.root.root_key),
                registration,
            )

        before = _database_snapshot(fixture.state_root)
        with (
            mock.patch.object(
                fixture.store,
                "commit_review_policy",
                return_value=foreign_result,
            ),
            self.assertRaises(module.ReviewCheckpointError),
        ):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))

        with (
            mock.patch.object(
                fixture.store,
                "load_review_checkpoint",
                return_value=foreign_observation,
            ),
            self.assertRaises(module.ReviewCheckpointError),
        ):
            producer.read(workflow.WorkflowRootKey(fixture.root.root_key))

    def test_read_result_requires_complete_canonical_review_prefix(self) -> None:
        fixture = self._fixture()
        module = _producer_module()
        self._commit_chain(fixture)
        producer = self._producer(fixture)
        observation = producer.read(workflow.WorkflowRootKey(fixture.root.root_key))
        self.assertIsNotNone(observation)
        assert observation is not None
        registration = module._registered_review_workflow_store(fixture.store)
        self.assertIsNotNone(registration)
        root_key = workflow.WorkflowRootKey(fixture.root.root_key)

        altered_digest = "sha256:" + "f" * 64
        changed_request_event = replace(
            observation.events[0],
            request_digest=altered_digest,
        )
        changed_request_event = replace(
            changed_request_event,
            event_digest=module._review_policy_event_digest(changed_request_event),
        )
        mutations = (
            replace(observation, events=observation.events[-1:]),
            replace(
                observation,
                events=(
                    changed_request_event,
                    *observation.events[1:],
                ),
            ),
            replace(
                observation,
                events=(
                    replace(
                        observation.events[0],
                        checkpoint_digest=altered_digest,
                    ),
                    *observation.events[1:],
                ),
            ),
            replace(
                observation,
                events=(
                    replace(
                        observation.events[0],
                        event_digest=altered_digest,
                    ),
                    *observation.events[1:],
                ),
            ),
        )
        for mutated in mutations:
            with (
                self.subTest(event_count=len(mutated.events)),
                self.assertRaises(module.ReviewCheckpointError),
            ):
                module._validate_review_checkpoint_observation_result(
                    mutated,
                    root_key,
                    registration,
                )

        mutated_checkpoint = workflow.decode_checkpoint(
            workflow.encode_checkpoint(observation.checkpoint)
        )
        object.__setattr__(
            mutated_checkpoint,
            "verification_authority",
            mutated_checkpoint.review_authority,
        )
        with self.assertRaises(module.ReviewCheckpointError):
            module._validate_review_checkpoint_observation_result(
                replace(observation, checkpoint=mutated_checkpoint),
                root_key,
                registration,
            )

        for field in ("from_state", "to_state", "kind", "actor"):
            with (
                self.subTest(scalar_field=field),
                self.assertRaises(module.ReviewCheckpointError),
            ):
                replace(
                    observation.events[0],
                    **{field: _StringSubclass(getattr(observation.events[0], field))},
                )

    def test_commit_result_event_id_matches_the_inserted_row(self) -> None:
        fixture = self._fixture(store_type=_EventIdResultStore)
        result = self._producer(fixture).commit(
            fixture.current,
            fixture.chain.worker_done,
            fixture.chain.policy,
            fixture.refs[0],
        )
        with closing(
            sqlite3.connect(fixture.state_root / store_module.DATABASE_FILENAME)
        ) as db:
            row = db.execute(
                "SELECT workflow_event_id FROM workflow_events "
                "WHERE root_key = ? AND workflow_sequence = ?",
                (fixture.root.root_key, result.checkpoint.workflow_sequence),
            ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], result.event.workflow_event_id)

    def test_registered_store_arbitrary_cleanup_errors_are_sanitized(self) -> None:
        fixture = self._fixture()
        module = _producer_module()
        producer = self._producer(fixture)

        class LeakyRuntimeError(RuntimeError):
            def retry_cleanup(self) -> None:
                return None

        cases: tuple[tuple[str, Callable[[], object]], ...] = (
            (
                "commit_review_policy",
                lambda: producer.commit(
                    fixture.current,
                    fixture.chain.worker_done,
                    fixture.chain.policy,
                    fixture.refs[0],
                ),
            ),
            (
                "load_review_checkpoint",
                lambda: producer.read(workflow.WorkflowRootKey(fixture.root.root_key)),
            ),
        )
        for method, callback in cases:
            with self.subTest(method=method):
                caught: BaseException | None = None
                with mock.patch.object(
                    fixture.store,
                    method,
                    side_effect=LeakyRuntimeError("REGISTERED_SECRET_CANARY"),
                ):
                    try:
                        callback()
                    except (RuntimeError, ValueError) as error:
                        caught = error
                self.assertIs(type(caught), module.ReviewCheckpointError)
                assert caught is not None
                self.assertNotIn("CANARY", str(caught))
                self.assertIsNone(caught.__context__)

    def test_registered_store_error_without_cleanup_is_sanitized(self) -> None:
        fixture = self._fixture()
        module = _producer_module()
        producer = self._producer(fixture)
        callbacks: tuple[tuple[str, Callable[[], object]], ...] = (
            (
                "commit",
                lambda: producer.commit(
                    fixture.current,
                    fixture.chain.worker_done,
                    fixture.chain.policy,
                    fixture.refs[0],
                ),
            ),
            (
                "read",
                lambda: producer.read(workflow.WorkflowRootKey(fixture.root.root_key)),
            ),
        )
        for operation, callback in callbacks:
            target = "_fault" if operation == "commit" else "_workflow_validate_root"
            with self.subTest(operation=operation):
                caught: BaseException | None = None
                with mock.patch.object(
                    fixture.store,
                    target,
                    side_effect=StoreError("STORE_SECRET_CANARY"),
                ):
                    try:
                        callback()
                    except (RuntimeError, ValueError) as error:
                        caught = error
                self.assertIs(type(caught), module.ReviewCheckpointError)
                assert caught is not None
                self.assertNotIn("CANARY", str(caught))
                self.assertIsNone(caught.__context__)

    def test_cleanup_accessor_error_is_sanitized(self) -> None:
        fixture = self._fixture()
        module = _producer_module()
        producer = self._producer(fixture)

        class LeakyCleanupAccessorError(StoreError):
            @property
            def retry_cleanup(self) -> Callable[[], None]:
                raise RuntimeError("CLEANUP_ACCESSOR_SECRET_CANARY")

        error = LeakyCleanupAccessorError("bounded-body")
        error._attach_cleanup_capability(store_module._CleanupCapability(lambda: None))
        caught: BaseException | None = None
        with mock.patch.object(fixture.store, "_fault", side_effect=error):
            try:
                producer.commit(
                    fixture.current,
                    fixture.chain.worker_done,
                    fixture.chain.policy,
                    fixture.refs[0],
                )
            except (RuntimeError, ValueError) as raised:
                caught = raised
        self.assertIs(type(caught), module.ReviewCheckpointError)
        assert caught is not None
        self.assertNotIn("CANARY", str(caught))
        self.assertIsNone(caught.__context__)

    def test_constructor_store_introspection_error_is_sanitized(self) -> None:
        fixture = self._fixture()
        module = _producer_module()

        class LeakyPort:
            def __getattribute__(self, name: str) -> object:
                if name in ("commit_review_policy", "load_review_checkpoint"):
                    raise RuntimeError("CONSTRUCTOR_SECRET_CANARY")
                return object.__getattribute__(self, name)

        caught: BaseException | None = None
        try:
            module.ReviewCheckpointProducer(
                fixture.handoff,
                cast(Any, LeakyPort()),
            )
        except (RuntimeError, ValueError) as error:
            caught = error
        self.assertIs(type(caught), module.ReviewCheckpointError)
        assert caught is not None
        self.assertNotIn("CANARY", str(caught))
        self.assertIsNone(caught.__context__)

    def test_registered_store_subclass_cannot_override_review_methods(self) -> None:
        fixture = self._fixture()
        module = _producer_module()

        class CommitOverrideStore(CoordinationStore):
            def commit_review_policy(self, request: Any) -> Any:
                return super().commit_review_policy(request)

        class ReadOverrideStore(CoordinationStore):
            def load_review_checkpoint(self, key: Any) -> Any:
                return super().load_review_checkpoint(key)

        class EventOverrideStore(CoordinationStore):
            @staticmethod
            def _review_event_observation(row: Any) -> Any:
                return CoordinationStore._review_event_observation(row)

        for store_type in (
            CommitOverrideStore,
            ReadOverrideStore,
            EventOverrideStore,
        ):
            with (
                self.subTest(store_type=store_type.__name__),
                self.assertRaises(module.ReviewCheckpointError),
            ):
                store_type(fixture.state_root)

    def test_out_of_order_suffix_and_duplicate_edges_do_not_write(self) -> None:
        for update_name, ref_index in (
            ("review_pending", 1),
            ("approved", 2),
        ):
            with self.subTest(update=update_name):
                fixture = self._fixture()
                producer = self._producer(fixture)
                before = _database_snapshot(fixture.state_root)
                with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
                    producer.commit(
                        fixture.current,
                        getattr(fixture.chain, update_name),
                        fixture.chain.policy,
                        fixture.refs[ref_index],
                    )
                self.assertEqual(before, _database_snapshot(fixture.state_root))
        fixture = self._fixture()
        producer = self._producer(fixture)
        first = producer.commit(
            fixture.current,
            fixture.chain.worker_done,
            fixture.chain.policy,
            fixture.refs[0],
        )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            producer.commit(
                first.checkpoint,
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_review_commits_do_not_call_route_or_verification_gate(self) -> None:
        fixture = self._fixture()
        producer = self._producer(fixture)
        with (
            mock.patch.object(
                handoff_module,
                "route_task",
                side_effect=AssertionError("route must not run"),
            ) as route,
            mock.patch.object(
                verification_gate.VerificationGate,
                "start",
                side_effect=AssertionError("Gate must not start"),
            ) as gate_start,
            mock.patch.object(
                verification_gate.VerificationGate,
                "resume",
                side_effect=AssertionError("Gate must not resume"),
            ) as gate_resume,
            mock.patch.object(
                handoff_module.PolicyVerificationHandoff,
                "save_authority",
                side_effect=AssertionError("authority issuance must not run"),
            ) as save_authority,
            mock.patch.object(
                handoff_module.PolicyVerificationHandoff,
                "compose",
                side_effect=AssertionError("approval compose must not run"),
            ) as compose,
            mock.patch.object(
                handoff_module.PolicyVerificationHandoff,
                "resolve",
                side_effect=AssertionError("approval resolve must not run"),
            ) as resolve,
        ):
            current = fixture.current
            for update, reference in zip(
                (
                    fixture.chain.worker_done,
                    fixture.chain.review_pending,
                    fixture.chain.approved,
                ),
                fixture.refs,
            ):
                current = producer.commit(
                    current,
                    update,
                    fixture.chain.policy,
                    reference,
                ).checkpoint
        route.assert_not_called()
        gate_start.assert_not_called()
        gate_resume.assert_not_called()
        save_authority.assert_not_called()
        compose.assert_not_called()
        resolve.assert_not_called()

    def test_fresh_reopen_reads_review_pending_approved_pair(self) -> None:
        fixture = self._fixture()
        _, _, final = self._commit_chain(fixture)
        expected_checkpoint = final.checkpoint
        fixture.store.close()
        owner_reads = fixture.authority_store.review_read_calls
        with CoordinationStore(fixture.state_root) as reopened:
            observed = reopened.load_review_checkpoint(
                workflow.WorkflowRootKey(fixture.root.root_key)
            )
        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(expected_checkpoint, observed.checkpoint)
        self.assertIs(
            observed.checkpoint.workflow_state, workflow.CheckpointState.REVIEW_PENDING
        )
        self.assertEqual(
            fixture.chain.approved.next_state.task_state, observed.task.state
        )
        self.assertEqual(3, len(observed.events))
        self.assertEqual(owner_reads, fixture.authority_store.review_read_calls)

    def test_foreign_update_and_owner_ref_are_rejected_without_mutation(self) -> None:
        fixture = self._fixture()
        foreign_chain = _review_chain(fixture.root, attempt_id="attempt-2")
        foreign_ref = fixture.handoff.save_authority(
            foreign_chain.worker_done,
            foreign_chain.policy,
        )
        producer = self._producer(fixture)
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                foreign_ref,
            )
        real_ref = fixture.refs[0]
        forged_ref = object.__new__(review_policy.ReviewAuthorityRef)
        for name in ("reference", "digest", "_issuer"):
            object.__setattr__(
                forged_ref,
                name,
                object.__getattribute__(real_ref, name),
            )
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                forged_ref,
            )
        original_reference = real_ref.reference
        object.__setattr__(
            real_ref,
            "reference",
            _StringSubclass(original_reference),
        )
        with self.assertRaises(handoff_module.PolicyVerificationHandoffError):
            fixture.handoff._read_review(real_ref)
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                real_ref,
            )
        object.__setattr__(real_ref, "reference", original_reference)
        object.__setattr__(real_ref, "digest", "f" * 64)
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                real_ref,
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_text_identical_ref_from_another_handoff_is_rejected(self) -> None:
        fixture = self._fixture()
        other_handoff = handoff_module.PolicyVerificationHandoff(
            fixture.authority_store
        )
        other_ref = other_handoff.save_authority(
            fixture.chain.worker_done,
            fixture.chain.policy,
        )
        self.assertEqual(fixture.refs[0].reference, other_ref.reference)
        self.assertEqual(fixture.refs[0].digest, other_ref.digest)
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            self._producer(fixture).commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                other_ref,
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_stale_store_checkpoint_is_rejected_without_second_event(self) -> None:
        fixture = self._fixture()
        producer = self._producer(fixture)
        first = producer.commit(
            fixture.current,
            fixture.chain.worker_done,
            fixture.chain.policy,
            fixture.refs[0],
        )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            producer.commit(
                fixture.current,
                fixture.chain.review_pending,
                fixture.chain.policy,
                fixture.refs[1],
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))
        self.assertEqual(
            first.checkpoint,
            producer.read(workflow.WorkflowRootKey(fixture.root.root_key)).checkpoint,
        )

    def test_owner_record_drift_and_post_binding_mutation_do_not_write(self) -> None:
        fixture = self._fixture()
        producer = self._producer(fixture)
        before = _database_snapshot(fixture.state_root)
        original_ref = fixture.refs[0]
        original_record = fixture.authority_store.review_records[original_ref.reference]
        foreign_chain = _review_chain(fixture.root, attempt_id="attempt-2")
        foreign_ref = fixture.handoff.save_authority(
            foreign_chain.worker_done,
            foreign_chain.policy,
        )
        fixture.authority_store.review_records[original_ref.reference] = (
            fixture.authority_store.review_records[foreign_ref.reference]
        )
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            producer.commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                original_ref,
            )
        fixture.authority_store.review_records[original_ref.reference] = original_record
        module = _producer_module()
        binding = fixture.handoff._bind_review_authority(
            fixture.chain.worker_done,
            fixture.chain.policy,
            original_ref,
        )
        object.__setattr__(
            fixture.chain.worker_done.next_state.task_state,
            "target_head",
            "f" * 40,
        )
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            fixture.store.commit_review_policy(
                module._issue_review_policy_commit_request(
                    producer,
                    fixture.current,
                    binding,
                )
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_non_authoritative_explanation_is_not_in_store_digests(self) -> None:
        fixture = self._fixture()
        other_chain = _review_chain(
            fixture.root,
            completion_explanation="raw-explanation-secret-canary-81",
        )
        other_store = composer_fixtures._FakePolicyVerificationStore()
        other_handoff = handoff_module.PolicyVerificationHandoff(other_store)
        first_ref = fixture.refs[0]
        other_ref = other_handoff.save_authority(
            other_chain.worker_done,
            other_chain.policy,
        )
        self.assertEqual(first_ref.reference, other_ref.reference)
        self.assertEqual(first_ref.digest, other_ref.digest)
        module = _producer_module()
        first_producer = self._producer(fixture)
        other_producer = module.ReviewCheckpointProducer(
            other_handoff,
            fixture.store,
        )
        first_plan = module._plan_review_policy_request(
            module._issue_review_policy_commit_request(
                first_producer,
                fixture.current,
                fixture.handoff._bind_review_authority(
                    fixture.chain.worker_done,
                    fixture.chain.policy,
                    first_ref,
                ),
            ),
            fixture.store,
        )
        other_plan = module._plan_review_policy_request(
            module._issue_review_policy_commit_request(
                other_producer,
                fixture.current,
                other_handoff._bind_review_authority(
                    other_chain.worker_done,
                    other_chain.policy,
                    other_ref,
                ),
            ),
            fixture.store,
        )
        self.assertEqual(first_plan.authority, other_plan.authority)
        self.assertEqual(first_plan.request_digest, other_plan.request_digest)
        self.assertNotIn("secret-canary", first_plan.request_digest)

    def test_each_first_edge_fault_rolls_back_task_checkpoint_and_event(self) -> None:
        points = (
            "before_review_policy_task_preimage",
            "after_review_policy_task_preimage",
            "before_review_policy_task_write",
            "after_review_policy_task_write",
            "before_review_policy_checkpoint_write",
            "after_review_policy_checkpoint_write",
            "before_review_policy_event_write",
            "after_review_policy_event_write",
            "before_review_policy_commit",
        )
        for point in points:
            with self.subTest(point=point):
                fixture = self._fixture(store_type=_FaultStore)
                store = cast(_FaultStore, fixture.store)
                producer = self._producer(fixture)
                module = _producer_module()
                before = _database_snapshot(fixture.state_root)
                store.review_fault = point
                with self.assertRaises(module.ReviewCheckpointError) as raised:
                    producer.commit(
                        fixture.current,
                        fixture.chain.worker_done,
                        fixture.chain.policy,
                        fixture.refs[0],
                    )
                self.assertNotIn("injected", str(raised.exception))
                store.review_fault = None
                self.assertEqual(before, _database_snapshot(fixture.state_root))
                self.assertIsNone(
                    producer.read(workflow.WorkflowRootKey(fixture.root.root_key))
                )

    def test_commit_unknown_preserves_store_cleanup_capability(self) -> None:
        fixture = self._fixture(store_type=_FaultStore)
        store = cast(_FaultStore, fixture.store)
        store.review_fault = "after_commit"
        with self.assertRaises(store_module.StoreCommitUnknownError) as raised:
            self._producer(fixture).commit(
                fixture.current,
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            )
        self.assertTrue(callable(raised.exception.retry_cleanup))
        self.assertTrue(store._connection_cleanup_pending)
        raised.exception.retry_cleanup()

    def test_equal_durable_clock_is_not_reported_as_false_failure(self) -> None:
        fixture = self._fixture()
        fixture.store._clock = lambda: fixture.current.updated_ns
        fixture.store._clock_injected = True
        result = self._producer(fixture).commit(
            fixture.current,
            fixture.chain.worker_done,
            fixture.chain.policy,
            fixture.refs[0],
        )
        self.assertEqual(fixture.current.updated_ns, result.checkpoint.updated_ns)
        observed = self._producer(fixture).read(
            workflow.WorkflowRootKey(fixture.root.root_key)
        )
        self.assertIsNotNone(observed)
        self.assertEqual(result.checkpoint, observed.checkpoint)

    def test_config_drift_before_commit_rolls_back_review_transaction(self) -> None:
        module = _producer_module()
        for point in (
            "after_review_policy_checkpoint_write",
            "before_commit",
        ):
            with self.subTest(point=point):
                fixture = self._fixture(store_type=_RootDriftStore)
                store = cast(_RootDriftStore, fixture.store)
                config_path = Path(fixture.root.config_path)
                original_config = config_path.read_bytes()
                store.drift_path = config_path
                store.drift_point = point
                before = _database_snapshot(fixture.state_root)
                try:
                    with self.assertRaises(module.ReviewCheckpointError) as raised:
                        self._producer(fixture).commit(
                            fixture.current,
                            fixture.chain.worker_done,
                            fixture.chain.policy,
                            fixture.refs[0],
                        )
                finally:
                    config_path.write_bytes(original_config)
                self.assertEqual("store-failed", raised.exception.code)
                self.assertEqual(before, _database_snapshot(fixture.state_root))
                self.assertEqual(
                    fixture.current,
                    fixture.store.load_checkpoint(
                        workflow.WorkflowRootKey(fixture.root.root_key)
                    ),
                )

    def test_nonnull_verification_authority_blocks_first_review_edge(self) -> None:
        fixture = self._fixture()
        verification_authority = workflow.AuthorityReference(
            "verification-before-review",
            "sha256:" + "a" * 64,
        )
        transition = workflow.PolicyOrVerificationTransition(
            kind=workflow.TransitionKind.VERIFICATION,
            root_key=fixture.root.root_key,
            authority=verification_authority,
            expected_workflow_sequence=fixture.current.workflow_sequence,
            expected_task_sequence=fixture.current.task_sequence,
            next_task_sequence=fixture.current.task_sequence,
            actor="verification-before-review",
            request_digest="sha256:" + "d" * 64,
        )
        advanced = fixture.store.commit_transition(
            transition,
            replace(
                workflow.checkpoint_to_draft(fixture.current),
                workflow_sequence=fixture.current.workflow_sequence + 1,
                verification_authority=verification_authority,
            ),
            expected_workflow_sequence=fixture.current.workflow_sequence,
            expected_task_sequence=fixture.current.task_sequence,
        )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises((ValueError, workflow.WorkflowStoreError)):
            self._producer(fixture).commit(
                advanced,
                fixture.chain.worker_done,
                fixture.chain.policy,
                fixture.refs[0],
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_generic_transition_cannot_mutate_a_review_task_row(self) -> None:
        fixture = self._fixture()
        first = self._producer(fixture).commit(
            fixture.current,
            fixture.chain.worker_done,
            fixture.chain.policy,
            fixture.refs[0],
        )
        authority = workflow.AuthorityReference(
            "generic-after-review",
            "sha256:" + "b" * 64,
        )
        transition = workflow.PolicyOrVerificationTransition(
            kind=workflow.TransitionKind.POLICY,
            root_key=fixture.root.root_key,
            authority=authority,
            expected_workflow_sequence=first.checkpoint.workflow_sequence,
            expected_task_sequence=first.checkpoint.task_sequence,
            next_task_sequence=first.checkpoint.task_sequence,
            actor="generic-after-review",
            request_digest="sha256:" + "c" * 64,
        )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises(workflow.WorkflowStoreError):
            fixture.store.commit_transition(
                transition,
                replace(
                    workflow.checkpoint_to_draft(first.checkpoint),
                    workflow_sequence=first.checkpoint.workflow_sequence + 1,
                    review_authority=authority,
                ),
                expected_workflow_sequence=first.checkpoint.workflow_sequence,
                expected_task_sequence=first.checkpoint.task_sequence,
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))
        observed = fixture.store.load_review_checkpoint(
            workflow.WorkflowRootKey(fixture.root.root_key)
        )
        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(first.checkpoint, observed.checkpoint)

    def test_lifecycle_operation_cannot_mutate_a_review_task_row(self) -> None:
        fixture = self._fixture()
        first = self._producer(fixture).commit(
            fixture.current,
            fixture.chain.worker_done,
            fixture.chain.policy,
            fixture.refs[0],
        )
        self.assertIsNotNone(first.checkpoint.pending_delivery)
        assert first.checkpoint.pending_delivery is not None
        intent = replace(
            workflow_fixtures._wait_intent(fixture.root, first.checkpoint),
            operation_id="operation-read-after-review",
            effect_key="effect/read-after-review",
            action=workflow.OperationAction.READ,
            delivery_id=first.checkpoint.pending_delivery.delivery_id,
        )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises(workflow.WorkflowStoreError):
            fixture.store.begin_operation(
                intent,
                expected_workflow_sequence=first.checkpoint.workflow_sequence,
                expected_task_sequence=first.checkpoint.task_sequence,
            )
        self.assertEqual(before, _database_snapshot(fixture.state_root))
        observed = fixture.store.load_review_checkpoint(
            workflow.WorkflowRootKey(fixture.root.root_key)
        )
        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(first.checkpoint, observed.checkpoint)

    def test_review_request_fault_preserves_committed_worker_edge(self) -> None:
        points = (
            "after_review_policy_task_write",
            "after_review_policy_checkpoint_write",
            "after_review_policy_event_write",
            "before_review_policy_commit",
        )
        for point in points:
            with self.subTest(point=point):
                fixture = self._fixture(store_type=_FaultStore)
                store = cast(_FaultStore, fixture.store)
                producer = self._producer(fixture)
                module = _producer_module()
                first = producer.commit(
                    fixture.current,
                    fixture.chain.worker_done,
                    fixture.chain.policy,
                    fixture.refs[0],
                )
                before = _database_snapshot(fixture.state_root)
                store.review_fault = point
                with self.assertRaises(module.ReviewCheckpointError):
                    producer.commit(
                        first.checkpoint,
                        fixture.chain.review_pending,
                        fixture.chain.policy,
                        fixture.refs[1],
                    )
                store.review_fault = None
                self.assertEqual(before, _database_snapshot(fixture.state_root))
                observed = producer.read(
                    workflow.WorkflowRootKey(fixture.root.root_key)
                )
                self.assertIsNotNone(observed)
                self.assertEqual(first.checkpoint, observed.checkpoint)
                self.assertEqual(1, len(observed.events))

    def test_approved_decision_fault_preserves_review_pending_edge(self) -> None:
        points = (
            "after_review_policy_task_write",
            "after_review_policy_checkpoint_write",
            "after_review_policy_event_write",
            "before_review_policy_commit",
        )
        for point in points:
            with self.subTest(point=point):
                fixture = self._fixture(store_type=_FaultStore)
                store = cast(_FaultStore, fixture.store)
                producer = self._producer(fixture)
                module = _producer_module()
                current = fixture.current
                for update, reference in (
                    (fixture.chain.worker_done, fixture.refs[0]),
                    (fixture.chain.review_pending, fixture.refs[1]),
                ):
                    current = producer.commit(
                        current,
                        update,
                        fixture.chain.policy,
                        reference,
                    ).checkpoint
                before = _database_snapshot(fixture.state_root)
                store.review_fault = point
                with self.assertRaises(module.ReviewCheckpointError):
                    producer.commit(
                        current,
                        fixture.chain.approved,
                        fixture.chain.policy,
                        fixture.refs[2],
                    )
                store.review_fault = None
                self.assertEqual(before, _database_snapshot(fixture.state_root))
                observed = producer.read(
                    workflow.WorkflowRootKey(fixture.root.root_key)
                )
                self.assertIsNotNone(observed)
                self.assertEqual(current, observed.checkpoint)
                self.assertEqual(2, len(observed.events))

    def test_fresh_open_rejects_tampered_review_event_actor(self) -> None:
        fixture = self._fixture()
        self._commit_chain(fixture)
        fixture.store.close()
        with closing(
            sqlite3.connect(
                str(fixture.state_root / "coordination.sqlite3"),
                isolation_level=None,
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM workflow_events WHERE actor = ? "
                "ORDER BY workflow_sequence LIMIT 1",
                ("review-policy-producer-v1",),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            changed = dict(row)
            changed["actor"] = "review-policy-tampered"
            changed["event_digest"] = CoordinationStore._workflow_event_digest(changed)
            connection.execute("DROP TRIGGER workflow_events_no_update")
            try:
                connection.execute(
                    "UPDATE workflow_events SET actor = ?, event_digest = ? "
                    "WHERE workflow_event_id = ?",
                    (
                        changed["actor"],
                        changed["event_digest"],
                        changed["workflow_event_id"],
                    ),
                )
            finally:
                connection.execute(store_module._WORKFLOW_EVENTS_NO_UPDATE_TRIGGER_SQL)
        with self.assertRaises(StoreIntegrityError) as raised:
            CoordinationStore(fixture.state_root)
        self.assertIn("review policy", str(raised.exception))

    def test_fresh_open_rejects_tampered_review_request_digest(self) -> None:
        fixture = self._fixture()
        self._commit_chain(fixture)
        fixture.store.close()
        with closing(
            sqlite3.connect(
                str(fixture.state_root / "coordination.sqlite3"),
                isolation_level=None,
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM workflow_events WHERE actor = ? "
                "ORDER BY workflow_sequence LIMIT 1",
                ("review-policy-producer-v1",),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            changed = dict(row)
            changed["request_digest"] = "sha256:" + "e" * 64
            changed["event_digest"] = CoordinationStore._workflow_event_digest(changed)
            connection.execute("DROP TRIGGER workflow_events_no_update")
            try:
                connection.execute(
                    "UPDATE workflow_events SET request_digest = ?, event_digest = ? "
                    "WHERE workflow_event_id = ?",
                    (
                        changed["request_digest"],
                        changed["event_digest"],
                        changed["workflow_event_id"],
                    ),
                )
            finally:
                connection.execute(store_module._WORKFLOW_EVENTS_NO_UPDATE_TRIGGER_SQL)
        with self.assertRaises(StoreIntegrityError) as raised:
            CoordinationStore(fixture.state_root)
        self.assertIn("request", str(raised.exception))

    def test_fresh_open_rejects_nonnull_verification_authority(self) -> None:
        fixture = self._fixture()
        _, _, final = self._commit_chain(fixture)
        fixture.store.close()
        authority = workflow.AuthorityReference(
            "tampered-verification-authority",
            "sha256:" + "a" * 64,
        )
        with closing(
            sqlite3.connect(
                str(fixture.state_root / "coordination.sqlite3"),
                isolation_level=None,
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            changed = workflow._issue_checkpoint(
                replace(
                    workflow.checkpoint_to_draft(final.checkpoint),
                    verification_authority=authority,
                ),
                updated_ns=final.checkpoint.updated_ns,
                issuer=object(),
            )
            _replace_checkpoint_row(connection, changed)
            event = connection.execute(
                "SELECT * FROM workflow_events WHERE root_key = ? "
                "AND workflow_sequence = ?",
                (fixture.root.root_key, final.checkpoint.workflow_sequence),
            ).fetchone()
            self.assertIsNotNone(event)
            assert event is not None
            _rewrite_event_row(
                connection,
                event,
                checkpoint_bytes=workflow.encode_checkpoint(changed),
                checkpoint_digest=changed.checkpoint_digest,
            )
        with self.assertRaises(StoreIntegrityError) as raised:
            CoordinationStore(fixture.state_root)
        self.assertIn("review policy", str(raised.exception))

    def test_fresh_open_rejects_historical_task_digest_mutation(self) -> None:
        fixture = self._fixture()
        self._commit_chain(fixture)
        fixture.store.close()
        module = _producer_module()
        with closing(
            sqlite3.connect(
                str(fixture.state_root / "coordination.sqlite3"),
                isolation_level=None,
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            events = connection.execute(
                "SELECT * FROM workflow_events WHERE root_key = ? AND actor = ? "
                "ORDER BY workflow_sequence",
                (fixture.root.root_key, "review-policy-producer-v1"),
            ).fetchall()
            self.assertEqual(3, len(events))
            first_checkpoint = workflow.decode_checkpoint(events[0]["checkpoint_bytes"])
            self.assertIsNotNone(first_checkpoint.task_policy)
            assert first_checkpoint.task_policy is not None
            changed_first = workflow._issue_checkpoint(
                replace(
                    workflow.checkpoint_to_draft(first_checkpoint),
                    task_policy=replace(
                        first_checkpoint.task_policy,
                        state_digest="sha256:" + "e" * 64,
                    ),
                ),
                updated_ns=first_checkpoint.updated_ns,
                issuer=object(),
            )
            _rewrite_event_row(
                connection,
                events[0],
                checkpoint_bytes=workflow.encode_checkpoint(changed_first),
                checkpoint_digest=changed_first.checkpoint_digest,
            )
            second_checkpoint = workflow.decode_checkpoint(
                events[1]["checkpoint_bytes"]
            )
            self.assertIsNotNone(second_checkpoint.review_authority)
            assert second_checkpoint.review_authority is not None
            changed_request = module._review_policy_request_digest(
                module.ReviewPolicyEdge.REVIEW_REQUEST,
                changed_first,
                second_checkpoint.review_authority,
            )
            _rewrite_event_row(
                connection,
                events[1],
                request_digest=changed_request,
            )
        with self.assertRaises(StoreIntegrityError) as raised:
            CoordinationStore(fixture.state_root)
        self.assertIn("review policy", str(raised.exception))

    def test_nonempty_verification_operation_remains_fail_closed(self) -> None:
        fixture = self._fixture()
        _, _, final = self._commit_chain(fixture)
        fixture.store.close()
        (
            snapshot,
            _unused_task_state,
            _unused_task_bytes,
            _unused_task_digest,
            request,
            _receipt,
            request_bytes,
            request_digest,
            _receipt_bytes,
            _effect,
            _result,
        ) = schema_fixtures._typed_verification_payloads(
            replace(fixture.root, team_id="team-1")
        )
        with closing(
            sqlite3.connect(
                str(fixture.state_root / "coordination.sqlite3"),
                isolation_level=None,
            )
        ) as connection:
            schema_fixtures._insert_nonempty_ledger_row(
                connection,
                "verification_operations",
                schema_fixtures._operation_row_values(
                    connection,
                    fixture.root,
                    final.task.state,
                    final.task.state_digest,
                    snapshot,
                    request,
                    request_bytes,
                    request_digest,
                ),
            )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises(StoreIntegrityError) as raised:
            CoordinationStore(fixture.state_root)
        self.assertIn("verification ledger", str(raised.exception))
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_nonempty_verification_receipt_remains_fail_closed(self) -> None:
        fixture = self._fixture()
        _, _, final = self._commit_chain(fixture)
        fixture.store.close()
        (
            snapshot,
            _unused_task_state,
            _unused_task_bytes,
            _unused_task_digest,
            request,
            receipt,
            request_bytes,
            request_digest,
            receipt_bytes,
            _effect,
            _result,
        ) = schema_fixtures._typed_verification_payloads(
            replace(fixture.root, team_id="team-1")
        )
        with closing(
            sqlite3.connect(
                str(fixture.state_root / "coordination.sqlite3"),
                isolation_level=None,
            )
        ) as connection:
            operation = schema_fixtures._operation_row_values(
                connection,
                fixture.root,
                final.task.state,
                final.task.state_digest,
                snapshot,
                request,
                request_bytes,
                request_digest,
                status="RECEIPTED",
                changes={
                    "status": "RECEIPTED",
                    "effect_owner": "effect-owner",
                    "effect_attempt": 1,
                    "effect_epoch": 1,
                    "effect_fence": 1,
                    "effect_nonce": "effect-nonce",
                    "receipt_ref": str(receipt.receipt_ref),
                    "receipt_digest": str(receipt.receipt_digest),
                },
            )
            event = connection.execute(
                "SELECT workflow_event_id, event_digest FROM workflow_events "
                "WHERE root_key = ? ORDER BY workflow_event_id DESC LIMIT 1",
                (fixture.root.root_key,),
            ).fetchone()
            self.assertIsNotNone(event)
            assert event is not None
            operation["receipt_event_id"] = int(event[0])
            operation["receipt_event_digest"] = str(event[1])
            schema_fixtures._insert_nonempty_ledger_row(
                connection,
                "verification_operations",
                operation,
            )
            schema_fixtures._insert_nonempty_ledger_row(
                connection,
                "verification_receipts",
                schema_fixtures._receipt_row_values(
                    receipt,
                    receipt_bytes,
                    root_key=fixture.root.root_key,
                ),
            )
        before = _database_snapshot(fixture.state_root)
        with self.assertRaises(StoreIntegrityError) as raised:
            CoordinationStore(fixture.state_root)
        self.assertIn("verification ledger", str(raised.exception))
        self.assertEqual(before, _database_snapshot(fixture.state_root))

    def test_review_rows_do_not_enable_nonempty_image_validation(self) -> None:
        fixture = self._fixture()
        self._commit_chain(fixture)
        fixture.store.close()
        with (
            closing(
                sqlite3.connect(
                    str(fixture.state_root / "coordination.sqlite3"),
                    isolation_level=None,
                )
            ) as connection,
            self.assertRaises(StoreIntegrityError) as raised,
        ):
            store_module._validate_existing_schema(connection)
        self.assertIn("verification ledger", str(raised.exception))

    def test_image_validation_rejects_review_event_without_task_row(self) -> None:
        fixture = self._fixture()
        self._producer(fixture).commit(
            fixture.current,
            fixture.chain.worker_done,
            fixture.chain.policy,
            fixture.refs[0],
        )
        fixture.store.close()
        with closing(
            sqlite3.connect(
                str(fixture.state_root / "coordination.sqlite3"),
                isolation_level=None,
            )
        ) as connection:
            connection.execute("DELETE FROM task_policy_states")
        with (
            closing(
                sqlite3.connect(
                    str(fixture.state_root / "coordination.sqlite3"),
                    isolation_level=None,
                )
            ) as connection,
            self.assertRaises(StoreIntegrityError) as raised,
        ):
            store_module._validate_existing_schema(connection)
        self.assertIn("review policy", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
