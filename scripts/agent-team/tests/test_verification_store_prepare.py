"""RED tests for the Issue #82 initial prepare transaction.

Every case starts from the actual reopened #81 pair.  SQLite is queried only
for readback assertions; no test creates or changes the starting pair with SQL.
"""

from __future__ import annotations

import inspect
import threading
import types
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest import mock

import test_verification_gate as gate_fixtures
from verification_store_fixtures import actual_review_checkpoint_fixture

from agent_team import task_verification_ledger as ledger
from agent_team import verification_gate as gate
from agent_team import verification_store
from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore, _CleanupCapability
from agent_team.task_policy import (
    ClaimRef,
    GitObjectId,
    TaskPhase,
    TreeDigest,
    VerificationProfileRef,
    WorkspaceIdentity,
)

_AUTHORITY_ERRORS = (
    AssertionError,
    KeyError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
)
_PREPARE_FAULTS = (
    "before_verification_prepare_task_write",
    "after_verification_prepare_task_write",
    "before_verification_prepare_checkpoint_write",
    "after_verification_prepare_checkpoint_write",
    "before_verification_prepare_event_write",
    "after_verification_prepare_event_write",
    "before_verification_prepare_operation_write",
    "after_verification_prepare_operation_write",
    "before_verification_prepare_readback",
    "after_verification_prepare_readback",
    "before_verification_prepare_commit",
)


def _database_projection(store: CoordinationStore) -> tuple[object, ...]:
    queries = (
        "SELECT * FROM store_meta ORDER BY key",
        "SELECT * FROM task_policy_states ORDER BY root_key, task_id",
        "SELECT * FROM verification_operations ORDER BY root_key, verification_ref",
        "SELECT * FROM verification_receipts ORDER BY root_key, receipt_ref",
        "SELECT * FROM workflow_checkpoints ORDER BY root_key",
        "SELECT * FROM workflow_events ORDER BY workflow_event_id",
    )
    with store._workflow_read_snapshot() as connection:
        return tuple(
            tuple(tuple(row) for row in connection.execute(sql).fetchall())
            for sql in queries
        )


def _rows(store: CoordinationStore) -> tuple[dict[str, object], ...]:
    with store._workflow_read_snapshot() as connection:
        operations = connection.execute(
            "SELECT * FROM verification_operations"
        ).fetchall()
        tasks = connection.execute("SELECT * FROM task_policy_states").fetchall()
        events = connection.execute(
            "SELECT * FROM workflow_events ORDER BY workflow_sequence"
        ).fetchall()
    if len(operations) != 1 or len(tasks) != 1:
        raise AssertionError("prepare row cardinality differs")
    return dict(operations[0]), dict(tasks[0]), *(dict(row) for row in events)


def _profile_and_snapshot(
    approval: dict[str, object],
) -> tuple[gate.VerificationProfile, gate.VerificationSnapshot]:
    profile = replace(
        gate_fixtures.profile(),
        ref=VerificationProfileRef(str(approval["profile_ref"])),
    )
    workspace = Path(str(approval["workspace"]))
    metadata = workspace.stat()
    snapshot = gate.VerificationSnapshot(
        workspace=WorkspaceIdentity(str(workspace)),
        canonical_path=str(workspace),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        claim_ref=ClaimRef(str(approval["claim_ref"])),
        target_head=GitObjectId(str(approval["target_head"])),
        allowed_tree_digest=TreeDigest(str(approval["target_tree_digest"])),
    )
    return profile, snapshot


def _prepare_fixture(
    test: unittest.TestCase,
    fixture: Any,
    *,
    store: CoordinationStore | None = None,
    owner_id: str | None = None,
) -> tuple[Any, ...]:
    selected_store = fixture.store if store is None else store
    selected_owner = fixture.owner_id if owner_id is None else owner_id
    context_read = selected_store.read_verification_context(
        fixture.root.root_key,
        selected_owner,
        fixture.final_review_binding,
    )
    binding_snapshot, staged = verification_store.capture_approval_binding(
        selected_store,
        fixture.handoff,
        context_read,
        fixture.review_refs[-1],
        fixture.completion_ref,
    )
    adapter_type = getattr(verification_store, "StoreVerificationAdapter", None)
    test.assertTrue(
        inspect_is_class(adapter_type), "StoreVerificationAdapter is missing"
    )
    if not inspect_is_class(adapter_type):
        raise AssertionError("StoreVerificationAdapter is missing")
    factory = getattr(adapter_type, "from_capture", None)
    test.assertTrue(
        callable(factory), "StoreVerificationAdapter.from_capture is missing"
    )
    if not callable(factory):
        raise TypeError("StoreVerificationAdapter.from_capture is missing")
    approval = dict(binding_snapshot.approved_review)
    profile, before_snapshot = _profile_and_snapshot(approval)
    profiles = gate_fixtures.Resolver(profile)
    adapter = factory(selected_store, binding_snapshot, staged, profiles)
    verification_gate = gate.VerificationGate(
        adapter,
        profiles,
        gate_fixtures.SnapshotPort(before_snapshot),
        gate_fixtures.FakeRunner(),
        adapter,
    )
    return (
        context_read,
        binding_snapshot,
        adapter,
        verification_gate,
    )


def inspect_is_class(value: object) -> bool:
    return isinstance(value, type)


class VerificationStorePrepareRedTests(unittest.TestCase):
    def test_external_cleanup_duck_type_is_not_exposed(self) -> None:
        with actual_review_checkpoint_fixture():
            called = False

            def spoofed_cleanup() -> None:
                nonlocal called
                called = True

            for capability in (object(), _CleanupCapability(spoofed_cleanup)):
                source = RuntimeError("external cleanup canary")
                source._cleanup_capability = capability  # type: ignore[attr-defined]
                source.retry_cleanup = spoofed_cleanup  # type: ignore[attr-defined]
                wrapped = verification_store._context_boundary_error(
                    "verification boundary failed",
                    source,
                )
                self.assertIsNone(wrapped.retry_cleanup)
                self.assertFalse(wrapped._verification_cleanup_capability)
            self.assertFalse(called)

    def test_prepare_switches_same_adapter_to_store_backed_admission(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            live_bound = adapter.resolve(gate.ApprovalRef(snapshot.approval_ref))
            handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            durable_bound = adapter.resolve(handle.approval_ref)
            self.assertIsNot(live_bound, durable_bound)
            self.assertTrue(
                gate._same_approved(live_bound.approved, durable_bound.approved)
            )

    def test_adapter_factories_and_state_surface_are_exact(self) -> None:
        adapter_type = verification_store.StoreVerificationAdapter
        self.assertEqual(
            ["store", "snapshot", "staged_admission", "profile_resolver"],
            list(inspect.signature(adapter_type.from_capture).parameters),
        )
        self.assertEqual(
            [
                "store",
                "root_key",
                "verification_ref",
                "owner_id",
                "profile_resolver",
            ],
            list(inspect.signature(adapter_type.from_store).parameters),
        )
        for name in (
            "resolve",
            "prepare_once",
            "begin_effect_once",
            "read",
            "status",
            "record_receipt_once",
            "apply_terminal_once",
            "_read_with_status",
            "_mark_unknown",
        ):
            self.assertTrue(callable(getattr(adapter_type, name, None)), name)
        self.assertEqual(
            {
                "prepare_once",
                "begin_effect_once",
                "read",
                "status",
                "record_receipt_once",
                "apply_terminal_once",
            },
            {
                name
                for name, value in gate.VerificationStatePort.__dict__.items()
                if not name.startswith("_") and callable(value)
            },
        )

    def test_actual_gate_start_commits_prepare_atomically(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            w0 = fixture.current.workflow_sequence
            n = fixture.observation.task.state.sequence
            owner_calls = (
                fixture.owner_store.save_calls,
                fixture.owner_store.read_calls,
            )
            _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            owner_calls_after_capture = (
                fixture.owner_store.save_calls,
                fixture.owner_store.read_calls,
            )
            self.assertNotEqual(owner_calls, owner_calls_after_capture)
            trace: list[str] = []
            adapter_type = verification_store.StoreVerificationAdapter
            profiles = object.__getattribute__(verification_gate, "_profiles")
            snapshots = object.__getattribute__(verification_gate, "_snapshots")
            original_resolve = adapter_type.resolve
            original_prepare = adapter_type.prepare_once
            original_profile = type(profiles).resolve
            original_snapshot = type(snapshots).capture

            def traced_resolve(owner: Any, approval_ref: Any) -> Any:
                trace.append("adapter.resolve")
                return original_resolve(owner, approval_ref)

            def traced_profile(owner: Any, profile_ref: Any) -> Any:
                trace.append("profile.resolve")
                return original_profile(owner, profile_ref)

            def traced_snapshot(owner: Any, workspace: Any, claim_ref: Any) -> Any:
                trace.append("snapshot.capture")
                return original_snapshot(owner, workspace, claim_ref)

            def traced_prepare(owner: Any, request: Any) -> Any:
                trace.append("adapter.prepare_once")
                return original_prepare(owner, request)

            with (
                mock.patch.object(
                    adapter_type,
                    "resolve",
                    autospec=True,
                    side_effect=traced_resolve,
                ) as resolve,
                mock.patch.object(
                    type(profiles),
                    "resolve",
                    autospec=True,
                    side_effect=traced_profile,
                ) as profile_resolve,
                mock.patch.object(
                    type(snapshots),
                    "capture",
                    autospec=True,
                    side_effect=traced_snapshot,
                ) as snapshot_capture,
                mock.patch.object(
                    adapter_type,
                    "prepare_once",
                    autospec=True,
                    side_effect=traced_prepare,
                ) as prepare,
            ):
                handle = verification_gate.start(
                    gate.ApprovalRef(snapshot.approval_ref)
                )
            self.assertEqual(1, resolve.call_count)
            self.assertEqual(3, profile_resolve.call_count)
            self.assertEqual(1, snapshot_capture.call_count)
            self.assertEqual(1, prepare.call_count)
            self.assertEqual(
                [
                    "adapter.resolve",
                    "profile.resolve",
                    "snapshot.capture",
                    "adapter.prepare_once",
                    "profile.resolve",
                    "profile.resolve",
                ],
                trace,
            )

            self.assertEqual(snapshot.approval_ref, handle.approval_ref)
            current = fixture.store.load_checkpoint(
                workflow.WorkflowRootKey(fixture.root.root_key)
            )
            self.assertIs(type(current), workflow.WorkflowCheckpointV4)
            if type(current) is not workflow.WorkflowCheckpointV4:
                raise AssertionError("prepared checkpoint is missing")
            self.assertIs(current.workflow_state, workflow.CheckpointState.VERIFYING)
            self.assertEqual(w0 + 1, current.workflow_sequence)
            self.assertEqual(n + 1, current.task_sequence)
            operation, task_row, *events = _rows(fixture.store)
            state_bytes = task_row["state_bytes"]
            self.assertIs(type(state_bytes), bytes)
            task = ledger.decode_task_state(cast(bytes, state_bytes))
            self.assertIs(task.phase, TaskPhase.VERIFYING)
            self.assertEqual(n + 1, task.sequence)
            self.assertEqual("PREPARED", operation["status"])
            self.assertEqual(w0 + 1, len(events))
            self.assertEqual(fixture.current.root, current.root)
            self.assertEqual(fixture.current.run, current.run)
            self.assertEqual(
                fixture.current.execution_mode,
                current.execution_mode,
            )
            self.assertEqual(
                fixture.current.active_assignment,
                current.active_assignment,
            )
            self.assertEqual(
                fixture.current.pending_delivery,
                current.pending_delivery,
            )
            self.assertEqual(
                fixture.current.replied_message_ids,
                current.replied_message_ids,
            )
            self.assertEqual(fixture.current.read_observed, current.read_observed)
            self.assertEqual(fixture.current.released, current.released)
            self.assertEqual(
                fixture.current.review_authority,
                current.review_authority,
            )
            self.assertEqual(fixture.current.last_operation, current.last_operation)
            self.assertEqual(
                owner_calls_after_capture,
                (
                    fixture.owner_store.save_calls,
                    fixture.owner_store.read_calls,
                ),
            )

    def test_factory_rejects_subclass_override(self) -> None:
        class BypassStore(CoordinationStore):
            def commit_verification_prepare(self, request: Any) -> Any:
                del request
                raise AssertionError("bypass prepare must not be called")

        with actual_review_checkpoint_fixture() as fixture:
            fixture.store.close()
            bypass = BypassStore(fixture.state_root)
            try:
                with self.assertRaises(_AUTHORITY_ERRORS):
                    bypass.read_verification_context(
                        fixture.root.root_key,
                        fixture.owner_id,
                        fixture.final_review_binding,
                    )
            finally:
                bypass.close()

    def test_factory_rejects_internal_helper_and_instance_rebinding(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            before = _database_projection(fixture.store)
            called = False

            def forged_values(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal called
                del args, kwargs
                called = True
                raise AssertionError("forged prepare helper was called")

            fixture.store._verification_prepare_values = forged_values  # type: ignore[method-assign]
            try:
                with self.assertRaises(gate.RecoveryRequired):
                    verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
                self.assertFalse(called)
                self.assertEqual(before, _database_projection(fixture.store))
            finally:
                del fixture.store._verification_prepare_values

        with actual_review_checkpoint_fixture() as fixture:
            context = fixture.store.read_verification_context(
                fixture.root.root_key,
                fixture.owner_id,
                fixture.final_review_binding,
            )
            snapshot, staged = verification_store.capture_approval_binding(
                fixture.store,
                fixture.handoff,
                context,
                fixture.review_refs[-1],
                fixture.completion_ref,
            )

            def rebound(owner: object, request: object) -> object:
                del owner, request
                raise AssertionError("rebound prepare must not be called")

            object.__setattr__(
                fixture.store,
                "commit_verification_prepare",
                types.MethodType(rebound, fixture.store),
            )
            profile, _before = _profile_and_snapshot(dict(snapshot.approved_review))
            with self.assertRaises(_AUTHORITY_ERRORS):
                verification_store.StoreVerificationAdapter.from_capture(
                    fixture.store,
                    snapshot,
                    staged,
                    gate_fixtures.Resolver(profile),
                )

    def test_prepare_command_mutation_and_external_canary_are_rejected(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate = _prepare_fixture(
                self,
                fixture,
            )
            bound = adapter.resolve(gate.ApprovalRef(snapshot.approval_ref))
            profile, before_snapshot = _profile_and_snapshot(
                dict(snapshot.approved_review)
            )
            request = gate._build_request(bound, profile, before_snapshot)
            command = verification_store._issue_verification_prepare_request(
                adapter,
                request,
            )
            original_request_digest = command.request.request_digest
            object.__setattr__(
                command.request,
                "request_digest",
                gate.ReceiptDigest("0" * 64),
            )
            before = _database_projection(fixture.store)
            with self.assertRaises(_AUTHORITY_ERRORS):
                fixture.store.commit_verification_prepare(command)
            self.assertEqual(before, _database_projection(fixture.store))
            object.__setattr__(
                command.request,
                "request_digest",
                original_request_digest,
            )
            object.__setattr__(
                command.snapshot,
                "consumer_generation",
                command.snapshot.consumer_generation + 1,
            )
            object.__setattr__(
                command.snapshot,
                "binding_digest",
                ledger._snapshot_digest(
                    ledger._snapshot_payload_for_digest(command.snapshot)
                ),
            )
            with self.assertRaises(_AUTHORITY_ERRORS):
                fixture.store.commit_verification_prepare(command)
            self.assertEqual(before, _database_projection(fixture.store))

        class CanaryResolver:
            def resolve(self, profile_ref: VerificationProfileRef) -> Any:
                del profile_ref
                raise verification_store.VerificationStoreError("profile-secret-canary")

        with actual_review_checkpoint_fixture() as fixture:
            context = fixture.store.read_verification_context(
                fixture.root.root_key,
                fixture.owner_id,
                fixture.final_review_binding,
            )
            snapshot, staged = verification_store.capture_approval_binding(
                fixture.store,
                fixture.handoff,
                context,
                fixture.review_refs[-1],
                fixture.completion_ref,
            )
            adapter = verification_store.StoreVerificationAdapter.from_capture(
                fixture.store,
                snapshot,
                staged,
                CanaryResolver(),
            )
            bound = adapter.resolve(gate.ApprovalRef(snapshot.approval_ref))
            profile, before_snapshot = _profile_and_snapshot(
                dict(snapshot.approved_review)
            )
            request = gate._build_request(bound, profile, before_snapshot)
            with self.assertRaises(verification_store.VerificationStoreError) as raised:
                adapter.prepare_once(request)
            self.assertNotIn("canary", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_prepare_row_event_and_codecs_are_canonical(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            w0 = fixture.current.workflow_sequence
            n = fixture.observation.task.state.sequence
            _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            operation, task_row, *events = _rows(fixture.store)
            event = events[-1]
            current = fixture.store.load_checkpoint(
                workflow.WorkflowRootKey(fixture.root.root_key)
            )
            self.assertIs(type(current), workflow.WorkflowCheckpointV4)
            if type(current) is not workflow.WorkflowCheckpointV4:
                raise AssertionError("prepared checkpoint is missing")

            snapshot_bytes = operation["approval_binding_bytes"]
            self.assertIs(type(snapshot_bytes), bytes)
            self.assertEqual(
                snapshot,
                ledger.decode_approval_binding_snapshot(cast(bytes, snapshot_bytes)),
            )
            self.assertEqual(
                snapshot.binding_digest,
                operation["approval_binding_digest"],
            )
            request_bytes = operation["request_bytes"]
            self.assertIs(type(request_bytes), bytes)
            request = ledger.decode_verification_request_projection(
                cast(bytes, request_bytes)
            )
            approved = dict(snapshot.approved_review)
            self.assertEqual(fixture.root.root_key, operation["root_key"])
            self.assertEqual(handle.verification_ref, operation["verification_ref"])
            self.assertEqual(1, operation["record_version"])
            self.assertEqual(snapshot.version, operation["approval_binding_version"])
            self.assertEqual(request.version, operation["request_schema_version"])
            self.assertEqual(snapshot.approval_ref, operation["approval_ref"])
            self.assertEqual(snapshot.approval_digest, operation["approval_digest"])
            self.assertEqual(snapshot.review_ref, operation["review_ref"])
            self.assertEqual(snapshot.review_digest, operation["review_digest"])
            self.assertEqual(snapshot.completion_ref, operation["completion_ref"])
            self.assertEqual(
                snapshot.completion_digest,
                operation["completion_digest"],
            )
            self.assertEqual(snapshot.run_id, operation["run_id"])
            self.assertEqual(
                snapshot.main_terminal_id,
                operation["main_terminal_id"],
            )
            for row_name, approval_name in (
                ("task_id", "task_id"),
                ("dispatch_id", "dispatch_id"),
                ("attempt_id", "attempt_id"),
                ("worker_node", "worker_node"),
                ("reviewer_node", "reviewer_node"),
                ("worker_terminal_id", "worker_terminal_id"),
                ("reviewer_terminal_id", "reviewer_terminal_id"),
                ("team_id", "team_id"),
                ("workspace", "workspace"),
                ("review_round", "review_round"),
            ):
                self.assertEqual(approved[approval_name], operation[row_name])
            self.assertEqual(n, operation["task_sequence_before"])
            self.assertEqual(n + 1, operation["task_sequence_after"])
            self.assertEqual(w0, operation["workflow_sequence_before"])
            self.assertEqual(w0 + 1, operation["workflow_sequence_after"])
            self.assertEqual(handle.request_digest, request.request_digest)
            self.assertEqual(request.request_digest, operation["request_digest"])
            self.assertEqual(
                operation["record_digest"],
                verification_store._verification_record_digest(operation),
            )
            self.assertEqual(
                verification_store._VERIFICATION_RECORD_COLUMNS,
                tuple(operation),
            )
            self.assertEqual(
                {
                    "effect_owner": None,
                    "effect_attempt": None,
                    "effect_epoch": None,
                    "effect_fence": None,
                    "effect_nonce": None,
                    "receipt_ref": None,
                    "receipt_digest": None,
                    "terminal_phase": None,
                    "terminal_receipt_ref": None,
                    "terminal_receipt_digest": None,
                    "unknown_code": None,
                    "unknown_evidence_digest": None,
                },
                {
                    name: operation[name]
                    for name in (
                        "effect_owner",
                        "effect_attempt",
                        "effect_epoch",
                        "effect_fence",
                        "effect_nonce",
                        "receipt_ref",
                        "receipt_digest",
                        "terminal_phase",
                        "terminal_receipt_ref",
                        "terminal_receipt_digest",
                        "unknown_code",
                        "unknown_evidence_digest",
                    )
                },
            )
            request_wrapper = verification_store._verification_request_wrapper(
                verification_store.VerificationStage.PREPARE,
                fixture.root.root_key,
                str(handle.verification_ref),
                w0,
                w0 + 1,
                n,
                n + 1,
                request.request_digest,
            )
            evidence_wrapper = verification_store._verification_evidence_wrapper(
                verification_store.VerificationStage.PREPARE,
                fixture.root.root_key,
                str(handle.verification_ref),
                w0,
                w0 + 1,
                n,
                n + 1,
                snapshot.binding_digest,
            )
            self.assertEqual("verification_transition", event["kind"])
            self.assertEqual(verification_store.VERIFICATION_ACTOR, event["actor"])
            self.assertIsNone(event["operation_id"])
            self.assertIsNone(event["receipt_id"])
            self.assertEqual(request_wrapper, event["request_digest"])
            self.assertEqual(evidence_wrapper, event["evidence_ref"])
            self.assertEqual(event["workflow_event_id"], operation["prepare_event_id"])
            self.assertEqual(event["event_digest"], operation["prepare_event_digest"])
            self.assertIsNotNone(current.verification_authority)
            if current.verification_authority is None:
                raise AssertionError("verification authority is missing")
            self.assertEqual(evidence_wrapper, current.verification_authority.digest)
            self.assertEqual(
                str(handle.verification_ref), current.verification_authority.reference
            )
            self.assertEqual(task_row["state_digest"], operation["task_digest_after"])
            self.assertEqual(
                snapshot.task_state_digest,
                operation["task_digest_before"],
            )
            self.assertEqual(
                fixture.current.checkpoint_digest,
                operation["workflow_digest_before"],
            )
            self.assertEqual(
                current.checkpoint_digest,
                operation["workflow_digest_after"],
            )
            self.assertEqual(
                operation["created_ns"],
                operation["updated_ns"],
            )
            self.assertEqual(event["clock_ns"], operation["updated_ns"])
            self.assertEqual(current.updated_ns, operation["updated_ns"])
            self.assertIsNone(operation["receipt_event_id"])
            self.assertIsNone(operation["terminal_event_id"])
            self.assertIsNone(operation["unknown_event_id"])

    def test_same_plan_replays_and_old_context_is_rejected_after_prepare(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            context_read, snapshot, _adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            first = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            after_first = _database_projection(fixture.store)
            owner_calls = (
                fixture.owner_store.save_calls,
                fixture.owner_store.read_calls,
            )
            second = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            self.assertEqual(first.verification_ref, second.verification_ref)
            self.assertEqual(first.request_digest, second.request_digest)
            self.assertEqual(after_first, _database_projection(fixture.store))
            self.assertEqual(
                owner_calls,
                (
                    fixture.owner_store.save_calls,
                    fixture.owner_store.read_calls,
                ),
            )

            with self.assertRaises(_AUTHORITY_ERRORS):
                verification_store.capture_approval_binding(
                    fixture.store,
                    fixture.handoff,
                    context_read,
                    fixture.review_refs[-1],
                    fixture.completion_ref,
                )
            self.assertEqual(after_first, _database_projection(fixture.store))

            fixture.store.close()
            reopened = CoordinationStore(fixture.state_root)
            reopened.close()

    def test_two_store_writers_create_one_physical_prepare(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            fixture.store.close()
            barrier = threading.Barrier(2)
            handles: list[gate.VerificationHandle] = []
            snapshots: list[ledger.ApprovalBindingSnapshotV1] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()

            def run_start() -> None:
                store: CoordinationStore | None = None
                try:
                    store = CoordinationStore(fixture.state_root)
                    _context, snapshot, _adapter, selected_gate = _prepare_fixture(
                        self,
                        fixture,
                        store=store,
                    )
                    with result_lock:
                        snapshots.append(snapshot)
                    barrier.wait(timeout=5)
                    handle = selected_gate.start(
                        gate.ApprovalRef(snapshot.approval_ref)
                    )
                    with result_lock:
                        handles.append(handle)
                except BaseException as exc:  # noqa: BLE001 - test capture
                    with result_lock:
                        errors.append(exc)
                finally:
                    if store is not None:
                        store.close()

            threads = tuple(threading.Thread(target=run_start) for _ in range(2))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            self.assertEqual([], errors)
            self.assertEqual(2, len(snapshots))
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(2, len(handles))
            self.assertEqual(handles[0].verification_ref, handles[1].verification_ref)
            self.assertEqual(handles[0].request_digest, handles[1].request_digest)
            readback = CoordinationStore(fixture.state_root)
            try:
                with readback._workflow_read_snapshot() as connection:
                    operation_count = connection.execute(
                        "SELECT COUNT(*) FROM verification_operations"
                    ).fetchone()
                    event_count = connection.execute(
                        "SELECT COUNT(*) FROM workflow_events WHERE kind = ? AND actor = ?",
                        (
                            workflow.TransitionKind.VERIFICATION.value,
                            verification_store.VERIFICATION_ACTOR,
                        ),
                    ).fetchone()
                self.assertIsNotNone(operation_count)
                self.assertIsNotNone(event_count)
                if operation_count is None or event_count is None:
                    raise AssertionError("verification counts are unavailable")
                self.assertEqual(1, operation_count[0])
                self.assertEqual(1, event_count[0])
            finally:
                readback.close()

    def test_different_owner_plan_conflicts_without_last_write_wins(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _first_context, first_snapshot, _first_adapter, first_gate = (
                _prepare_fixture(
                    self,
                    fixture,
                    owner_id="verification-owner-a",
                )
            )
            _second_context, second_snapshot, _second_adapter, second_gate = (
                _prepare_fixture(
                    self,
                    fixture,
                    owner_id="verification-owner-b",
                )
            )
            self.assertNotEqual(
                first_snapshot.effect_owner,
                second_snapshot.effect_owner,
            )
            first_gate.start(gate.ApprovalRef(first_snapshot.approval_ref))
            committed = _database_projection(fixture.store)
            with self.assertRaises(gate.RecoveryRequired):
                second_gate.start(gate.ApprovalRef(second_snapshot.approval_ref))
            self.assertEqual(committed, _database_projection(fixture.store))

    def test_returned_context_mutation_cannot_rebind_prepare(self) -> None:
        mutations = (
            ("main_terminal_id", "forged-main-terminal"),
            ("worker_terminal_id", "forged-worker-terminal"),
            ("reviewer_terminal_id", "forged-reviewer-terminal"),
            ("consumer_generation", 999),
        )
        for field, value in mutations:
            with (
                self.subTest(field=field),
                actual_review_checkpoint_fixture() as fixture,
            ):
                context_read, snapshot, _adapter, verification_gate = _prepare_fixture(
                    self,
                    fixture,
                )
                object.__setattr__(context_read[0], field, value)
                before = _database_projection(fixture.store)
                with self.assertRaises(_AUTHORITY_ERRORS):
                    verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
                self.assertEqual(before, _database_projection(fixture.store))

    def test_prepare_faults_roll_back_all_durable_rows(self) -> None:
        for fault in _PREPARE_FAULTS:
            with (
                self.subTest(fault=fault),
                actual_review_checkpoint_fixture() as fixture,
            ):
                _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                    self, fixture
                )
                before = _database_projection(fixture.store)
                seen: list[str] = []

                def inject(
                    point: str,
                    expected: str = fault,
                    seen_points: list[str] = seen,
                ) -> None:
                    if point == expected:
                        seen_points.append(point)
                        raise RuntimeError("prepare fault canary")

                with (
                    mock.patch.object(fixture.store, "_fault", side_effect=inject),
                    self.assertRaises(_AUTHORITY_ERRORS),
                ):
                    verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
                self.assertEqual([fault], seen)
                self.assertEqual(before, _database_projection(fixture.store))

    def test_prepare_commit_unknown_preserves_cleanup_and_fresh_readback(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                self,
                fixture,
            )

            def inject(point: str) -> None:
                if point == "after_commit":
                    raise OSError("commit response loss canary")

            with (
                mock.patch.object(fixture.store, "_fault", side_effect=inject),
                self.assertRaises(gate.RecoveryRequired) as raised,
            ):
                verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            self.assertEqual("prepare-response-loss", raised.exception.reason_code)
            self.assertIsNone(raised.exception.__cause__)
            retry_cleanup = getattr(raised.exception, "retry_cleanup", None)
            self.assertTrue(callable(retry_cleanup))
            if not callable(retry_cleanup):
                raise TypeError("prepare cleanup capability is missing")
            retry_cleanup()

            reopened = CoordinationStore(fixture.state_root)
            try:
                operation, task_row, *events = _rows(reopened)
                self.assertEqual("PREPARED", operation["status"])
                state_bytes = task_row["state_bytes"]
                self.assertIs(type(state_bytes), bytes)
                self.assertIs(
                    ledger.decode_task_state(cast(bytes, state_bytes)).phase,
                    TaskPhase.VERIFYING,
                )
                self.assertEqual(
                    1,
                    sum(
                        event["kind"] == workflow.TransitionKind.VERIFICATION.value
                        for event in events
                    ),
                )
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
