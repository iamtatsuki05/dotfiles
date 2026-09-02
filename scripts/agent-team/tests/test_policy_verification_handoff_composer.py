"""Contract tests for the #74 policy/verification handoff composer.

The owner records and the shared state implementation in this module are
test-only values.  They intentionally do not model SQLite, process restart,
provider execution, or provider-side exactly-once behavior.
"""

from __future__ import annotations

import copy
import inspect
import pickle
import threading
import unittest
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from functools import partial
from typing import Any, cast
from unittest.mock import patch

import test_policy_verification_handoff_authority as authority_fixtures
import test_verification_gate as gate_fixtures

import agent_team.policy_verification_handoff as handoff_module
import agent_team.verification_gate as gate
from agent_team.task_policy import ReceiptRef, TaskId, TaskPhase, VerificationProfileRef

_TASK_SEQUENCE = 7
_WORKFLOW_SEQUENCE = 11


@dataclass(frozen=True, slots=True)
class _PrepareRequest:
    expected_task_sequence: int
    expected_workflow_sequence: int
    verification_ref: str = "verification-1"
    operation_id: str = "operation-1"


@dataclass(frozen=True, slots=True)
class _PrepareResult:
    prepared: bool
    verification_ref: str | None
    task_sequence: int
    workflow_sequence: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _EffectLease:
    status: str
    verification_ref: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class _RunnerResult:
    outcome: str = "passed"


@dataclass(frozen=True, slots=True)
class _Receipt:
    receipt_ref: str
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class _Terminal:
    terminal_ref: str
    receipt_ref: str
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class _DurableState:
    verification_ref: str
    status: str
    task_sequence: int
    workflow_sequence: int
    receipt: _Receipt | None
    terminal: _Terminal | None


@dataclass(frozen=True, slots=True)
class _StateSnapshot:
    phase: str
    task_sequence: int
    workflow_sequence: int
    effect_status: str
    receipt_ref: str | None
    terminal_ref: str | None


class _StateFault(RuntimeError):
    pass


class _DeterministicSharedState:
    """Small, in-process model of the six-method #51 state-port contract."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.phase = "APPROVED"
        self.task_sequence = _TASK_SEQUENCE
        self.workflow_sequence = _WORKFLOW_SEQUENCE
        self.effect_status = "ready"
        self.receipt: _Receipt | None = None
        self.terminal: _Terminal | None = None
        self.calls: list[str] = []
        self.prepare_calls = 0
        self.begin_calls = 0
        self.record_receipt_calls = 0
        self.apply_terminal_calls = 0
        self.fail_prepare = False
        self.fail_receipt = False
        self.fail_terminal = False

    def snapshot(self) -> _StateSnapshot:
        with self._condition:
            return _StateSnapshot(
                phase=self.phase,
                task_sequence=self.task_sequence,
                workflow_sequence=self.workflow_sequence,
                effect_status=self.effect_status,
                receipt_ref=(
                    None if self.receipt is None else self.receipt.receipt_ref
                ),
                terminal_ref=(
                    None if self.terminal is None else self.terminal.terminal_ref
                ),
            )

    def prepare_once(self, request: _PrepareRequest) -> _PrepareResult:
        with self._condition:
            self.calls.append("prepare")
            self.prepare_calls += 1
            if self.fail_prepare:
                raise _StateFault("prepare fault before commit")
            if (
                request.expected_task_sequence != self.task_sequence
                or request.expected_workflow_sequence != self.workflow_sequence
            ):
                return _PrepareResult(
                    prepared=False,
                    verification_ref=None,
                    task_sequence=self.task_sequence,
                    workflow_sequence=self.workflow_sequence,
                    reason="stale-sequence",
                )
            if self.phase != "APPROVED":
                return _PrepareResult(
                    prepared=False,
                    verification_ref=None,
                    task_sequence=self.task_sequence,
                    workflow_sequence=self.workflow_sequence,
                    reason="already-prepared",
                )
            self.task_sequence += 1
            self.workflow_sequence += 1
            self.phase = "VERIFYING"
            self._condition.notify_all()
            return _PrepareResult(
                prepared=True,
                verification_ref=request.verification_ref,
                task_sequence=self.task_sequence,
                workflow_sequence=self.workflow_sequence,
            )

    def begin_effect_once(
        self, verification_ref: str, request_digest: str
    ) -> _EffectLease:
        with self._condition:
            self.calls.append("begin")
            self.begin_calls += 1
            if verification_ref != "verification-1":
                raise _StateFault("foreign verification ref")
            while self.effect_status == "running":
                if not self._condition.wait(timeout=3):
                    raise _StateFault("effect owner did not publish a receipt")
            if self.effect_status == "ready":
                self.effect_status = "running"
                return _EffectLease("run_once", verification_ref, request_digest)
            if self.effect_status == "receipted":
                return _EffectLease("receipted", verification_ref, request_digest)
            return _EffectLease("terminal", verification_ref, request_digest)

    def read(self, verification_ref: str) -> _DurableState:
        with self._condition:
            self.calls.append("read")
            if verification_ref != "verification-1":
                raise _StateFault("foreign verification ref")
            status = {
                "ready": "PREPARED",
                "running": "PREPARED",
                "receipted": "RECEIPTED",
                "terminal": "TERMINAL",
            }[self.effect_status]
            return _DurableState(
                verification_ref=verification_ref,
                status=status,
                task_sequence=self.task_sequence,
                workflow_sequence=self.workflow_sequence,
                receipt=self.receipt,
                terminal=self.terminal,
            )

    def status(self, verification_ref: str) -> str:
        with self._condition:
            self.calls.append("status")
            if verification_ref != "verification-1":
                raise _StateFault("foreign verification ref")
            return {
                "ready": "PREPARED",
                "running": "PREPARED",
                "receipted": "RECEIPTED",
                "terminal": "TERMINAL",
            }[self.effect_status]

    def record_receipt_once(
        self,
        verification_ref: str,
        effect: _EffectLease,
        result: _RunnerResult,
        before: str,
        after: str,
    ) -> _Receipt:
        del result, before, after
        with self._condition:
            self.calls.append("receipt")
            self.record_receipt_calls += 1
            if verification_ref != "verification-1" or effect.status != "run_once":
                raise _StateFault("invalid receipt fence")
            if self.receipt is not None:
                return self.receipt
            if self.fail_receipt:
                raise _StateFault("receipt fault before commit")
            if self.effect_status != "running":
                raise _StateFault("receipt without effect owner")
            self.receipt = _Receipt("receipt-1", "f" * 64)
            self.effect_status = "receipted"
            self.phase = "RECEIPTED"
            self._condition.notify_all()
            return self.receipt

    def apply_terminal_once(
        self, verification_ref: str, receipt_ref: str, receipt_digest: str
    ) -> _Terminal:
        with self._condition:
            self.calls.append("terminal")
            self.apply_terminal_calls += 1
            if verification_ref != "verification-1":
                raise _StateFault("foreign verification ref")
            if self.terminal is not None:
                if (
                    self.terminal.receipt_ref != receipt_ref
                    or self.terminal.receipt_digest != receipt_digest
                ):
                    raise _StateFault("terminal receipt mismatch")
                return self.terminal
            if self.fail_terminal:
                raise _StateFault("terminal fault before commit")
            if self.receipt is None or self.effect_status != "receipted":
                raise _StateFault("terminal without receipt")
            if (
                self.receipt.receipt_ref != receipt_ref
                or self.receipt.receipt_digest != receipt_digest
            ):
                raise _StateFault("terminal receipt mismatch")
            self.terminal = _Terminal(
                terminal_ref="terminal-1",
                receipt_ref=receipt_ref,
                receipt_digest=receipt_digest,
            )
            self.effect_status = "terminal"
            self.phase = "TERMINAL"
            self._condition.notify_all()
            return self.terminal


class _TraceGateState(gate_fixtures.FakeState):
    """Existing #51 fake with one cross-method state-port call trace."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def prepare_once(
        self, request: gate.VerificationRequest
    ) -> gate.VerificationPrepareResult:
        self.calls.append("prepare")
        return super().prepare_once(request)

    def begin_effect_once(
        self,
        verification_ref: gate.VerificationRef,
        request_digest: gate.ReceiptDigest,
    ) -> gate.VerificationEffectLease:
        self.calls.append("begin")
        return super().begin_effect_once(verification_ref, request_digest)

    def read(
        self, verification_ref: gate.VerificationRef
    ) -> gate.VerificationDurableRecord:
        self.calls.append("read")
        return super().read(verification_ref)

    def status(
        self, verification_ref: gate.VerificationRef
    ) -> gate.DurableRecordStatus:
        self.calls.append("status")
        return super().status(verification_ref)

    def record_receipt_once(
        self,
        verification_ref: gate.VerificationRef,
        effect: gate.VerificationEffectLease,
        result: gate.VerificationRunResult,
        before: gate.VerificationSnapshot,
        after: gate.VerificationSnapshot,
    ) -> gate.VerificationReceipt:
        self.calls.append("receipt")
        return super().record_receipt_once(
            verification_ref, effect, result, before, after
        )

    def apply_terminal_once(
        self,
        verification_ref: gate.VerificationRef,
        receipt_ref: ReceiptRef,
        receipt_digest: gate.ReceiptDigest,
    ) -> gate.VerificationTerminalResult:
        self.calls.append("terminal")
        return super().apply_terminal_once(
            verification_ref, receipt_ref, receipt_digest
        )


class _FakePolicyVerificationStore:
    """Fake owner registry plus the same injected shared state port."""

    def __init__(self) -> None:
        self._state_port: object = _DeterministicSharedState()
        self.review_records: dict[str, Any] = {}
        self.completion_records: dict[str, Any] = {}
        self.approval_records: dict[str, Any] = {}
        self.review_read_calls = 0
        self.completion_read_calls = 0
        self.review_save_calls = 0
        self.completion_save_calls = 0
        self.save_calls = 0
        self.read_calls = 0

    @staticmethod
    def _record_key(record: Any) -> str:
        reference = getattr(record, "reference", None)
        if type(reference) is not str:
            reference = getattr(record, "approval_ref", None)
        if type(reference) is not str:
            raise AssertionError("stored authority record has no reference")
        return reference

    @staticmethod
    def _save_once(records: dict[str, Any], record: Any) -> Any:
        key = _FakePolicyVerificationStore._record_key(record)
        existing = records.get(key)
        if existing is None:
            records[key] = record
            return record
        return existing

    def save_review_authority(self, record: Any) -> Any:
        self.review_save_calls += 1
        return self._save_once(self.review_records, record)

    def read_review_authority(self, reference: str) -> Any:
        self.review_read_calls += 1
        record = self.review_records.get(reference)
        if record is None:
            raise LookupError("review authority is not stored")
        return record

    def save_completion_admission(self, record: Any) -> Any:
        self.completion_save_calls += 1
        return self._save_once(self.completion_records, record)

    def read_completion_admission(self, reference: str) -> Any:
        self.completion_read_calls += 1
        record = self.completion_records.get(reference)
        if record is None:
            raise LookupError("completion admission is not stored")
        return record

    def save_approval(self, record: Any) -> Any:
        self.save_calls += 1
        return self._save_once(self.approval_records, record)

    def read_approval(self, approval_ref: str) -> Any:
        self.read_calls += 1
        record = self.approval_records.get(approval_ref)
        if record is None:
            raise LookupError("approval ref is not stored")
        return record

    def state_port(self) -> Any:
        return self._state_port


def _load_handoff(_testcase: unittest.TestCase) -> Any:
    return handoff_module


def _fixture(
    module: Any,
) -> tuple[Any, _FakePolicyVerificationStore, Any, Any]:
    store = _FakePolicyVerificationStore()
    handoff = module.PolicyVerificationHandoff(store)
    task = authority_fixtures._path_task()
    update, policy = authority_fixtures._review_path(task=task)
    review_ref = handoff.save_authority(update, policy)
    completion_ref = handoff.issue_completion_admission(
        **authority_fixtures._route_inputs(
            task,
            port=authority_fixtures.RecordingReservationPort(),
        )
    )
    store.save_calls = 0
    store.read_calls = 0
    return handoff, store, review_ref, completion_ref


def _state_from(
    testcase: unittest.TestCase,
    handoff: Any,
    store: _FakePolicyVerificationStore,
) -> _DeterministicSharedState:
    state = handoff.state_port()
    testcase.assertIs(state, store._state_port)
    return cast(_DeterministicSharedState, state)


def _assert_rejected(testcase: unittest.TestCase, module: Any, callback: Any) -> None:
    """Require the handoff's bounded typed rejection."""

    try:
        callback()
    except module.PolicyVerificationHandoffError as exc:
        testcase.assertTrue(
            type(getattr(exc, "code", None)) is str
            or type(getattr(exc, "reason_code", None)) is str
        )
        testcase.assertNotIn("handoff-authority-canary", str(exc))
    else:
        testcase.fail("handoff accepted a forged or mismatched authority")


def _forged_copy(value: Any, **changes: object) -> Any:
    forged = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(
            forged,
            item.name,
            changes.get(item.name, object.__getattribute__(value, item.name)),
        )
    return forged


class PolicyVerificationHandoffComposerTests(unittest.TestCase):
    def test_verification_gate_public_surface_remains_unchanged(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(gate.VerificationGate.start).parameters),
            ("self", "approval_ref"),
        )
        self.assertEqual(
            tuple(inspect.signature(gate.VerificationGate.resume).parameters),
            ("self", "handle"),
        )

    def test_handoff_exposes_composer_and_state_contract(self) -> None:
        module = _load_handoff(self)
        self.assertTrue(inspect.isclass(module.PolicyVerificationHandoffError))
        self.assertNotIn("PolicyVerificationStorePort", module.__all__)
        handoff, store, _, _ = _fixture(module)
        state = _state_from(self, handoff, store)
        self.assertIs(state, store._state_port)
        self.assertEqual(
            tuple(inspect.signature(type(handoff).compose).parameters),
            ("self", "review_ref", "completion_ref"),
        )
        self.assertEqual(
            tuple(inspect.signature(type(handoff).resolve).parameters),
            ("self", "approval_ref"),
        )

    def test_compose_resolves_bound_approval_and_reads_back_owner_binding(self) -> None:
        module = _load_handoff(self)
        handoff, store, review_ref, completion_ref = _fixture(module)
        review = store.review_records[review_ref.reference]
        completion = store.completion_records[completion_ref.reference]
        for name in (
            "run_id",
            "dispatch_id",
            "attempt_id",
            "worker_terminal_id",
            "reviewer_terminal_id",
            "review_round",
            "target_head",
            "target_tree_digest",
            "claim_ref",
        ):
            self.assertFalse(
                hasattr(completion, name),
                f"#50 fake record unexpectedly owns #49-only field {name}",
            )

        approval_ref = handoff.compose(review_ref, completion_ref)
        self.assertIs(type(approval_ref), str)
        self.assertEqual(store.save_calls, 1)
        self.assertEqual(store.read_calls, 1)
        stored = store.approval_records[approval_ref]
        self.assertEqual(stored.review_ref, review_ref)
        self.assertEqual(stored.completion_ref, completion_ref)
        self.assertEqual(stored.review_digest, review.digest)
        self.assertEqual(stored.completion_digest, completion.digest)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.assertRaises((TypeError, pickle.PicklingError)):
                operation(stored)
        bound = handoff.resolve(approval_ref)
        self.assertIsInstance(bound, gate._BoundApproval)
        self.assertEqual(bound.approved.run_id, review.run_id)
        self.assertEqual(bound.approved.dispatch_id, review.dispatch_id)
        self.assertEqual(bound.approved.attempt_id, review.attempt_id)
        self.assertEqual(bound.approved.routing_digest, completion.routing_digest)
        self.assertEqual(
            bound.approved.reservation_digest,
            completion.reservation_digest,
        )

    def test_existing_verification_gate_uses_handoff_and_same_state_port(self) -> None:
        module = _load_handoff(self)
        handoff, store, review_ref, completion_ref = _fixture(module)
        approval_ref = handoff.compose(review_ref, completion_ref)
        approved = handoff.resolve(approval_ref).approved
        state = _TraceGateState()
        store._state_port = state
        profile = replace(
            gate_fixtures.profile(),
            ref=VerificationProfileRef(approved.profile_ref),
        )
        runner = gate_fixtures.FakeRunner()
        verification_gate = gate.VerificationGate(
            handoff,
            gate_fixtures.Resolver(profile),
            gate_fixtures.SnapshotPort(gate_fixtures.snapshot(approved)),
            runner,
            handoff.state_port(),
        )
        expected_ref = gate.VerificationRef(approved.verification_id)
        with patch.object(gate_fixtures, "VERIFICATION_REF", expected_ref):
            handle = verification_gate.start(approval_ref)
            first = verification_gate.resume(handle)
            first_runner_calls = runner.calls
            first_begin_calls = state.begin_calls
            first_receipt_calls = state.record_receipt_calls
            first_terminal_calls = state.apply_terminal_calls
            replay = verification_gate.resume(handle)
        self.assertEqual(first.phase, replay.phase)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.calls, first_runner_calls)
        self.assertEqual(state.prepare_calls, 1)
        self.assertEqual(state.begin_calls, 1)
        self.assertEqual(state.begin_calls, first_begin_calls)
        self.assertEqual(state.record_receipt_calls, 1)
        self.assertEqual(state.record_receipt_calls, first_receipt_calls)
        self.assertEqual(state.apply_terminal_calls, 1)
        self.assertEqual(state.apply_terminal_calls, first_terminal_calls)
        self.assertEqual(
            state.calls,
            [
                "prepare",
                "read",
                "status",
                "begin",
                "receipt",
                "terminal",
                "read",
                "status",
            ],
        )

    def test_receipted_replay_uses_handoff_without_begin_or_runner(self) -> None:
        module = _load_handoff(self)
        handoff, store, review_ref, completion_ref = _fixture(module)
        approval_ref = handoff.compose(review_ref, completion_ref)
        approved = handoff.resolve(approval_ref).approved
        state = _TraceGateState()
        store._state_port = state
        profile = replace(
            gate_fixtures.profile(),
            ref=VerificationProfileRef(approved.profile_ref),
        )
        runner = gate_fixtures.FakeRunner()
        verification_gate = gate.VerificationGate(
            handoff,
            gate_fixtures.Resolver(profile),
            gate_fixtures.SnapshotPort(gate_fixtures.snapshot(approved)),
            runner,
            handoff.state_port(),
        )
        expected_ref = gate.VerificationRef(approved.verification_id)
        with patch.object(gate_fixtures, "VERIFICATION_REF", expected_ref):
            handle = verification_gate.start(approval_ref)
            record = cast(gate.VerificationDurableRecord, state.record)
            effect = state.begin_effect_once(
                record.verification_ref, record.request.request_digest
            )
            seed_runner = gate_fixtures.FakeRunner()
            result = seed_runner.run(record.request, effect)
            current = gate_fixtures.snapshot(approved)
            state.record_receipt_once(
                record.verification_ref,
                effect,
                result,
                current,
                current,
            )
            state.calls.clear()
            begin_calls = state.begin_calls
            receipt_calls = state.record_receipt_calls
            terminal_calls = state.apply_terminal_calls
            terminal = verification_gate.resume(handle)
        self.assertEqual(terminal.phase, TaskPhase.COMPLETED)
        self.assertEqual(runner.calls, 0)
        self.assertEqual(state.begin_calls, begin_calls)
        self.assertEqual(state.record_receipt_calls, receipt_calls)
        self.assertEqual(state.apply_terminal_calls, terminal_calls + 1)
        self.assertEqual(state.calls, ["read", "status", "terminal"])

    def test_composer_exactly_compares_only_overlap_and_owner_digests(self) -> None:
        module = _load_handoff(self)
        overlap = (
            "team_id",
            "task_id",
            "workspace",
            "profile_ref",
            "policy_fingerprint",
            "worker_node",
            "reviewer_node",
            "lane",
        )
        for owner in ("review", "completion"):
            for field in overlap:
                with self.subTest(owner=owner, field=field):
                    handoff, store, review_ref, completion_ref = _fixture(module)
                    if owner == "review":
                        record = store.review_records[review_ref.reference]
                        value = "express" if field == "lane" else f"changed-{field}"
                        store.review_records[review_ref.reference] = _forged_copy(
                            record, **{field: value}
                        )
                    else:
                        record = store.completion_records[completion_ref.reference]
                        value = "express" if field == "lane" else f"changed-{field}"
                        store.completion_records[completion_ref.reference] = (
                            _forged_copy(record, **{field: value})
                        )
                    state = _state_from(self, handoff, store)
                    before = state.snapshot()
                    _assert_rejected(
                        self,
                        module,
                        lambda handoff=handoff, review_ref=review_ref, completion_ref=completion_ref: (
                            handoff.compose(review_ref, completion_ref)
                        ),
                    )
                    self.assertEqual(state.snapshot(), before)
                    self.assertEqual(store.save_calls, 0)

    def test_owner_digest_mismatch_is_rejected_without_state_mutation(self) -> None:
        module = _load_handoff(self)
        cases = ("review-ref", "completion-ref", "review-record", "completion-record")
        for case in cases:
            with self.subTest(case=case):
                handoff, store, review_ref, completion_ref = _fixture(module)
                if case == "review-ref":
                    review_ref = _forged_copy(review_ref, digest="9" * 64)
                elif case == "completion-ref":
                    completion_ref = _forged_copy(completion_ref, digest="9" * 64)
                elif case == "review-record":
                    record = store.review_records[review_ref.reference]
                    store.review_records[review_ref.reference] = _forged_copy(
                        record, digest="9" * 64
                    )
                else:
                    record = store.completion_records[completion_ref.reference]
                    store.completion_records[completion_ref.reference] = _forged_copy(
                        record, digest="9" * 64
                    )
                state = _state_from(self, handoff, store)
                before = state.snapshot()
                _assert_rejected(
                    self,
                    module,
                    lambda handoff=handoff, review_ref=review_ref, completion_ref=completion_ref: (
                        handoff.compose(review_ref, completion_ref)
                    ),
                )
                self.assertEqual(state.snapshot(), before)
                self.assertEqual(store.save_calls, 0)

    def test_resolve_revalidates_both_owner_records_after_approval(self) -> None:
        module = _load_handoff(self)
        for owner in ("review", "completion"):
            with self.subTest(owner=owner):
                handoff, store, review_ref, completion_ref = _fixture(module)
                approval_ref = handoff.compose(review_ref, completion_ref)
                state = _state_from(self, handoff, store)
                before = state.snapshot()
                if owner == "review":
                    record = store.review_records[review_ref.reference]
                    store.review_records[review_ref.reference] = _forged_copy(
                        record, run_id="foreign-run"
                    )
                else:
                    record = store.completion_records[completion_ref.reference]
                    store.completion_records[completion_ref.reference] = _forged_copy(
                        record, routing_digest="9" * 64
                    )
                _assert_rejected(
                    self,
                    module,
                    partial(handoff.resolve, approval_ref),
                )
                self.assertEqual(state.snapshot(), before)

    def test_resolve_rejects_reissued_bound_with_changed_verification_id(self) -> None:
        module = _load_handoff(self)
        handoff, store, review_ref, completion_ref = _fixture(module)
        approval_ref = handoff.compose(review_ref, completion_ref)
        record = store.approval_records[approval_ref]
        approved = record.bound.approved
        changed = gate._make_approved(
            run_id=approved.run_id,
            team_id=approved.team_id,
            workspace=approved.workspace,
            task_id=approved.task_id,
            dispatch_id=approved.dispatch_id,
            attempt_id=approved.attempt_id,
            worker_node=approved.worker_node,
            reviewer_node=approved.reviewer_node,
            worker_terminal_id=approved.worker_terminal_id,
            reviewer_terminal_id=approved.reviewer_terminal_id,
            review_round=approved.review_round,
            target_head=approved.target_head,
            target_tree_digest=approved.target_tree_digest,
            claim_ref=approved.claim_ref,
            policy_fingerprint=approved.policy_fingerprint,
            routing_lane=approved.routing_lane,
            approval_ref=gate.ApprovalRef(approved.approval_ref),
            approval_sequence=approved.approval_sequence,
            profile_ref=approved.profile_ref,
            verification_id=gate.VerificationId("verification-attacker"),
            routing_digest=gate.ReceiptDigest(approved.routing_digest),
            reservation_digest=(
                None
                if approved.reservation_digest is None
                else gate.ReceiptDigest(approved.reservation_digest)
            ),
        )
        changed_bound = gate._make_bound_approval(
            gate.ApprovalRef(approved.approval_ref), changed
        )
        forged = _forged_copy(record, bound=changed_bound, digest="0" * 64)
        forged = _forged_copy(
            forged,
            digest=module._framed_digest(module._approval_parts(forged)),
        )
        store.approval_records[approval_ref] = forged
        _assert_rejected(
            self,
            module,
            partial(handoff.resolve, approval_ref),
        )

    def test_bare_foreign_and_mutated_owner_refs_are_rejected_before_prepare(
        self,
    ) -> None:
        module = _load_handoff(self)
        foreign_store = _FakePolicyVerificationStore()
        foreign_handoff = module.PolicyVerificationHandoff(foreign_store)
        foreign_task = replace(
            authority_fixtures._path_task(),
            task_id=TaskId("foreign-task"),
        )
        foreign_update, foreign_policy = authority_fixtures._review_path(
            task=foreign_task
        )
        foreign_review = foreign_handoff.save_authority(foreign_update, foreign_policy)
        foreign_completion = foreign_handoff.issue_completion_admission(
            **authority_fixtures._route_inputs(
                foreign_task,
                port=authority_fixtures.RecordingReservationPort(),
            )
        )
        cases: tuple[tuple[str, Callable[[Any, Any], tuple[Any, Any]]], ...] = (
            ("bare-review", lambda r, c: (r.reference, c)),
            ("foreign-review", lambda r, c: (foreign_review, c)),
            (
                "mutated-review",
                lambda r, c: (_forged_copy(r, reference="review-ref-mutated"), c),
            ),
            ("bare-completion", lambda r, c: (r, c.reference)),
            ("foreign-completion", lambda r, c: (r, foreign_completion)),
            (
                "mutated-completion",
                lambda r, c: (r, _forged_copy(c, digest="8" * 64)),
            ),
        )
        for name, mutate in cases:
            with self.subTest(case=name):
                handoff, store, review_ref, completion_ref = _fixture(module)
                candidate_review, candidate_completion = mutate(
                    review_ref, completion_ref
                )
                state = _state_from(self, handoff, store)
                before = state.snapshot()
                _assert_rejected(
                    self,
                    module,
                    lambda handoff=handoff, candidate_review=candidate_review, candidate_completion=candidate_completion: (
                        handoff.compose(candidate_review, candidate_completion)
                    ),
                )
                self.assertEqual(state.snapshot(), before)
                self.assertEqual(state.prepare_calls, 0)
                self.assertEqual(store.save_calls, 0)

    def test_same_store_foreign_handoff_completion_ref_is_rejected(self) -> None:
        module = _load_handoff(self)
        handoff, store, review_ref, _ = _fixture(module)
        task = authority_fixtures._path_task()
        foreign_handoff = module.PolicyVerificationHandoff(store)
        foreign_completion = foreign_handoff.issue_completion_admission(
            **authority_fixtures._route_inputs(
                task,
                port=authority_fixtures.RecordingReservationPort(),
            )
        )
        state = _state_from(self, handoff, store)
        before = state.snapshot()
        approval_count = len(store.approval_records)
        store.save_calls = 0

        _assert_rejected(
            self,
            module,
            lambda: handoff.compose(review_ref, foreign_completion),
        )

        self.assertEqual(before, state.snapshot())
        self.assertEqual(approval_count, len(store.approval_records))
        self.assertEqual(0, store.save_calls)
        self.assertEqual(0, state.prepare_calls)

    def test_dual_sequence_prepare_has_one_winner_and_no_partial_commit(self) -> None:
        module = _load_handoff(self)
        handoff, store, _, _ = _fixture(module)
        state = _state_from(self, handoff, store)
        request = _PrepareRequest(_TASK_SEQUENCE, _WORKFLOW_SEQUENCE)
        barrier = threading.Barrier(2)
        results: list[_PrepareResult] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def prepare() -> None:
            try:
                barrier.wait(timeout=3)
                result = state.prepare_once(request)
                with result_lock:
                    results.append(result)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=prepare, daemon=True) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
        finally:
            barrier.abort()
            for thread in threads:
                thread.join(timeout=1)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sum(result.prepared for result in results), 1)
        self.assertEqual(state.prepare_calls, 2)
        self.assertEqual(state.calls, ["prepare", "prepare"])
        after = state.snapshot()
        self.assertEqual(after.phase, "VERIFYING")
        self.assertEqual(after.task_sequence, _TASK_SEQUENCE + 1)
        self.assertEqual(after.workflow_sequence, _WORKFLOW_SEQUENCE + 1)

    def test_prepare_receipt_and_terminal_faults_leave_no_partial_commit(self) -> None:
        module = _load_handoff(self)
        handoff, store, _, _ = _fixture(module)
        state = _state_from(self, handoff, store)
        state.fail_prepare = True
        before = state.snapshot()
        with self.assertRaises(_StateFault):
            state.prepare_once(_PrepareRequest(_TASK_SEQUENCE, _WORKFLOW_SEQUENCE))
        self.assertEqual(state.snapshot(), before)
        self.assertEqual(state.calls, ["prepare"])

        handoff, store, _, _ = _fixture(module)
        state = _state_from(self, handoff, store)
        state.prepare_once(_PrepareRequest(_TASK_SEQUENCE, _WORKFLOW_SEQUENCE))
        effect = state.begin_effect_once("verification-1", "request-1")
        state.fail_receipt = True
        before = state.snapshot()
        with self.assertRaises(_StateFault):
            state.record_receipt_once(
                "verification-1", effect, _RunnerResult(), "before", "after"
            )
        self.assertEqual(state.snapshot(), before)
        self.assertIsNone(state.receipt)
        self.assertIsNone(state.terminal)
        self.assertEqual(state.calls, ["prepare", "begin", "receipt"])

        handoff, store, _, _ = _fixture(module)
        state = _state_from(self, handoff, store)
        state.prepare_once(_PrepareRequest(_TASK_SEQUENCE, _WORKFLOW_SEQUENCE))
        effect = state.begin_effect_once("verification-1", "request-1")
        receipt = state.record_receipt_once(
            "verification-1", effect, _RunnerResult(), "before", "after"
        )
        state.fail_terminal = True
        before = state.snapshot()
        with self.assertRaises(_StateFault):
            state.apply_terminal_once(
                "verification-1", receipt.receipt_ref, receipt.receipt_digest
            )
        self.assertEqual(state.snapshot(), before)
        self.assertIsNone(state.terminal)
        self.assertEqual(
            state.calls,
            ["prepare", "begin", "receipt", "terminal"],
        )


if __name__ == "__main__":
    unittest.main()
