"""RED tests for the Issue #82 effect-arm transaction.

The fixture is the real close/reopen'd #81 ``REVIEW_PENDING + APPROVED`` pair
from ``test_verification_store_prepare``. SQL writes are limited to an explicit
post-prepare mixed-event rejection probe; the starting pair is never synthesized.
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast
from unittest import mock

import test_verification_gate as gate_fixtures
from test_verification_store_prepare import _prepare_fixture, _profile_and_snapshot
from verification_store_fixtures import actual_review_checkpoint_fixture

from agent_team import verification_gate as gate
from agent_team import verification_store
from agent_team import workflow_store as workflow
from agent_team.store import DATABASE_FILENAME, CoordinationStore, StoreIntegrityError
from agent_team.task_policy import VerificationProfileRef

_REJECTION_ERRORS = (
    AssertionError,
    KeyError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
)


def _state_projection(store: CoordinationStore) -> dict[str, object]:
    """Read the complete verification-relevant image without mutating it."""

    with store._workflow_read_snapshot() as connection:
        meta = {
            str(row["key"]): row["value"]
            for row in connection.execute(
                "SELECT key, value FROM store_meta ORDER BY key"
            ).fetchall()
        }
        task = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM task_policy_states ORDER BY root_key, task_id"
            ).fetchall()
        )
        operation = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM verification_operations "
                "ORDER BY root_key, verification_ref"
            ).fetchall()
        )
        receipts = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM verification_receipts ORDER BY root_key, receipt_ref"
            ).fetchall()
        )
        checkpoint = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM workflow_checkpoints ORDER BY root_key"
            ).fetchall()
        )
        events = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM workflow_events ORDER BY workflow_event_id"
            ).fetchall()
        )
    return {
        "meta": meta,
        "task": task,
        "operation": operation,
        "receipts": receipts,
        "checkpoint": checkpoint,
        "events": events,
    }


def _prepared(
    testcase: unittest.TestCase,
    fixture: Any,
) -> tuple[Any, Any, Any, Any, gate.VerificationHandle]:
    """Prepare one actual #81 pair through the public Gate start path."""

    context, snapshot, adapter, verification_gate = _prepare_fixture(testcase, fixture)
    handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
    return context, snapshot, adapter, verification_gate, handle


def _must_begin(
    testcase: unittest.TestCase,
    adapter: Any,
    verification_ref: gate.VerificationRef,
    request_digest: gate.ReceiptDigest,
) -> gate.VerificationEffectLease:
    """Turn an absent/unimplemented effect API into a RED failure, not an error."""

    try:
        effect = adapter.begin_effect_once(verification_ref, request_digest)
    except BaseException as exc:
        testcase.fail(f"begin_effect_once is unavailable: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable") from exc
    testcase.assertIs(
        type(effect),
        gate.VerificationEffectLease,
        "begin_effect_once must return an exact VerificationEffectLease",
    )
    return cast(gate.VerificationEffectLease, effect)


def _must_open(
    testcase: unittest.TestCase,
    state_root: Any,
) -> CoordinationStore:
    """Turn a non-empty-image open failure into a RED failure, not an error."""

    try:
        return CoordinationStore(state_root)
    except BaseException as exc:
        testcase.fail(f"fresh CoordinationStore reopen failed: {type(exc).__name__}")
        raise AssertionError("unreachable") from exc


def _profile_resolver(snapshot: Any) -> gate_fixtures.Resolver:
    approval = dict(snapshot.approved_review)
    profile = replace(
        gate_fixtures.profile(),
        ref=VerificationProfileRef(str(approval["profile_ref"])),
    )
    return gate_fixtures.Resolver(profile)


def _must_from_store(
    testcase: unittest.TestCase,
    store: CoordinationStore,
    fixture: Any,
    handle: gate.VerificationHandle,
    snapshot: Any,
) -> Any:
    try:
        return verification_store.StoreVerificationAdapter.from_store(
            store,
            workflow.WorkflowRootKey(fixture.root.root_key),
            gate.VerificationRef(handle.verification_ref),
            fixture.owner_id,
            _profile_resolver(snapshot),
        )
    except BaseException as exc:
        testcase.fail(
            "StoreVerificationAdapter.from_store is unavailable: "
            f"{type(exc).__name__}: {exc}"
        )
        raise AssertionError("unreachable") from exc


def _assert_rejected_without_mutation(
    testcase: unittest.TestCase,
    store: CoordinationStore,
    call: Callable[[], object],
    before: dict[str, object],
) -> None:
    with testcase.assertRaises(_REJECTION_ERRORS):
        call()
    testcase.assertEqual(before, _state_projection(store))


class VerificationStoreEffectRedTests(unittest.TestCase):
    def test_offline_mixed_prepare_event_wrapper_rejects_reopen(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            del handle
            fixture.store.close()
            connection = sqlite3.connect(fixture.state_root / DATABASE_FILENAME)
            connection.row_factory = sqlite3.Row
            try:
                triggers = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
                for trigger in triggers:
                    name = str(trigger[0]).replace('"', '""')
                    connection.execute(f'DROP TRIGGER "{name}"')
                operation = dict(
                    connection.execute(
                        "SELECT * FROM verification_operations"
                    ).fetchone()
                )
                event = dict(
                    connection.execute(
                        "SELECT * FROM workflow_events WHERE workflow_event_id = ?",
                        (operation["prepare_event_id"],),
                    ).fetchone()
                )
                event["request_digest"] = "sha256:" + "f" * 64
                event["event_digest"] = CoordinationStore._workflow_event_digest(event)
                connection.execute(
                    "UPDATE workflow_events SET request_digest = ?, event_digest = ? "
                    "WHERE workflow_event_id = ?",
                    (
                        event["request_digest"],
                        event["event_digest"],
                        operation["prepare_event_id"],
                    ),
                )
                operation["prepare_event_digest"] = event["event_digest"]
                operation["record_digest"] = (
                    verification_store._verification_record_digest(operation)
                )
                connection.execute(
                    "UPDATE verification_operations SET prepare_event_digest = ?, "
                    "record_digest = ?",
                    (
                        operation["prepare_event_digest"],
                        operation["record_digest"],
                    ),
                )
                for trigger in triggers:
                    if type(trigger[1]) is not str:
                        raise AssertionError("trigger SQL is missing")
                    connection.execute(trigger[1])
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(fixture.state_root)

    def test_begin_effect_changes_only_operation_and_global_floor(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            before = _state_projection(fixture.store)
            before_operation = before["operation"]
            self.assertIs(type(before_operation), tuple)
            if not isinstance(before_operation, tuple) or len(before_operation) != 1:
                raise AssertionError("prepared operation row is missing")
            before_row = cast(dict[str, object], before_operation[0])
            before_meta = cast(dict[str, object], before["meta"])

            _effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            after = _state_projection(fixture.store)
            after_operation = after["operation"]
            self.assertIs(type(after_operation), tuple)
            if not isinstance(after_operation, tuple) or len(after_operation) != 1:
                raise AssertionError("armed operation row is missing")
            after_row = cast(dict[str, object], after_operation[0])
            after_meta = cast(dict[str, object], after["meta"])

            self.assertEqual(before["task"], after["task"])
            self.assertEqual(before["checkpoint"], after["checkpoint"])
            self.assertEqual(before["events"], after["events"])
            self.assertEqual(before["receipts"], after["receipts"])
            self.assertEqual(
                before_meta["recovery_epoch"], after_meta["recovery_epoch"]
            )
            self.assertGreater(
                cast(int, after_meta["fencing_token_floor"]),
                cast(int, before_meta["fencing_token_floor"]),
            )
            self.assertEqual(
                cast(int, before_meta["fencing_token_floor"]) + 1,
                after_meta["fencing_token_floor"],
            )
            self.assertEqual(after_row["updated_ns"], after_meta["last_clock_ns"])
            self.assertLessEqual(
                cast(int, after_row["created_ns"]),
                cast(int, after_row["updated_ns"]),
            )

            changed = {
                name for name in before_row if before_row[name] != after_row[name]
            }
            self.assertEqual(
                {
                    "status",
                    "effect_owner",
                    "effect_attempt",
                    "effect_epoch",
                    "effect_fence",
                    "effect_nonce",
                    "record_digest",
                    "updated_ns",
                },
                changed,
            )

    def test_begin_effect_arms_exact_owner_epoch_fence_nonce_and_digest(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            before = _state_projection(fixture.store)
            effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            state = _state_projection(fixture.store)
            operation_rows = cast(tuple[dict[str, object], ...], state["operation"])
            if len(operation_rows) != 1:
                raise AssertionError("armed operation row is missing")
            operation = operation_rows[0]
            meta = cast(dict[str, object], state["meta"])

            self.assertIs(effect.status, gate.EffectBeginStatus.RUN_ONCE)
            self.assertEqual(handle.verification_ref, effect.verification_ref)
            self.assertEqual(handle.request_digest, effect.request_digest)
            self.assertEqual("EFFECT_PREPARED", operation["status"])
            self.assertEqual(snapshot.effect_owner, operation["effect_owner"])
            self.assertEqual(1, operation["effect_attempt"])
            self.assertEqual(meta["recovery_epoch"], operation["effect_epoch"])
            self.assertEqual(meta["recovery_epoch"], effect.lease_epoch)
            self.assertEqual(meta["fencing_token_floor"], operation["effect_fence"])
            self.assertEqual(meta["fencing_token_floor"], effect.fencing_token)
            self.assertIs(type(operation["effect_nonce"]), str)
            self.assertTrue(cast(str, operation["effect_nonce"]))
            self.assertRegex(cast(str, operation["effect_nonce"]), r"[0-9a-f]{32}\Z")
            self.assertEqual(effect.effect_nonce, operation["effect_nonce"])
            self.assertEqual(
                operation["record_digest"],
                verification_store._verification_record_digest(operation),
            )
            self.assertEqual(before["events"], state["events"])

    def test_repeated_begin_returns_unknown_and_writes_no_second_event(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            first = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            after_first = _state_projection(fixture.store)
            second = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            after_second = _state_projection(fixture.store)

            self.assertIs(first.status, gate.EffectBeginStatus.RUN_ONCE)
            self.assertIs(second.status, gate.EffectBeginStatus.UNKNOWN)
            self.assertEqual(first.effect_nonce, second.effect_nonce)
            self.assertEqual(first.lease_epoch, second.lease_epoch)
            self.assertEqual(first.fencing_token, second.fencing_token)
            self.assertEqual(after_first, after_second)

    def test_concurrent_store_begins_have_one_run_once_and_one_fence(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            del adapter
            state_root = fixture.state_root
            fixture.store.close()
            barrier = threading.Barrier(2)
            outcomes: list[gate.VerificationEffectLease] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def worker() -> None:
                store: CoordinationStore | None = None
                try:
                    store = _must_open(self, state_root)
                    state = _must_from_store(
                        self,
                        store,
                        fixture,
                        handle,
                        snapshot,
                    )
                    barrier.wait(timeout=10)
                    effect = state.begin_effect_once(
                        handle.verification_ref,
                        handle.request_digest,
                    )
                    if type(effect) is not gate.VerificationEffectLease:
                        raise AssertionError("concurrent begin returned wrong type")
                    with lock:
                        outcomes.append(effect)
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    with lock:
                        errors.append(exc)
                finally:
                    if store is not None:
                        store.close()

            threads = tuple(threading.Thread(target=worker) for _ in range(2))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive())
            if errors:
                self.fail(
                    "concurrent begin failed: "
                    + ", ".join(type(error).__name__ for error in errors)
                )
            self.assertEqual(2, len(outcomes))
            self.assertEqual(
                1,
                sum(
                    effect.status is gate.EffectBeginStatus.RUN_ONCE
                    for effect in outcomes
                ),
            )
            self.assertEqual(
                1,
                sum(
                    effect.status is gate.EffectBeginStatus.UNKNOWN
                    for effect in outcomes
                ),
            )

            readback = _must_open(self, state_root)
            try:
                state = _state_projection(readback)
                operation_rows = cast(tuple[dict[str, object], ...], state["operation"])
                self.assertEqual(1, len(operation_rows))
                self.assertEqual("EFFECT_PREPARED", operation_rows[0]["status"])
                event_rows = cast(tuple[dict[str, object], ...], state["events"])
                self.assertEqual(
                    1,
                    sum(
                        event["kind"] == workflow.TransitionKind.VERIFICATION.value
                        and event["actor"] == verification_store.VERIFICATION_ACTOR
                        for event in event_rows
                    ),
                )
                meta = cast(dict[str, object], state["meta"])
                self.assertEqual(
                    meta["fencing_token_floor"], operation_rows[0]["effect_fence"]
                )
            finally:
                readback.close()

    def test_concurrent_real_gate_resume_invokes_runner_at_most_once(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, _adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            state_root = fixture.state_root
            fixture.store.close()
            profile, before = _profile_and_snapshot(dict(snapshot.approved_review))
            barrier = threading.Barrier(2)
            terminals: list[gate.VerificationTerminalResult] = []
            recoveries: list[gate.RecoveryRequired] = []
            runner_calls: list[int] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def resume() -> None:
                store: CoordinationStore | None = None
                runner: gate_fixtures.FakeRunner | None = None
                try:
                    store = _must_open(self, state_root)
                    resolver = gate_fixtures.Resolver(profile)
                    adapter = verification_store.StoreVerificationAdapter.from_store(
                        store,
                        workflow.WorkflowRootKey(fixture.root.root_key),
                        handle.verification_ref,
                        fixture.owner_id,
                        resolver,
                    )
                    runner = gate_fixtures.FakeRunner()
                    current_gate = gate.VerificationGate(
                        adapter,
                        resolver,
                        gate_fixtures.SnapshotPort(before),
                        runner,
                        adapter,
                    )
                    current_handle = current_gate.start(handle.approval_ref)
                    barrier.wait(timeout=10)
                    terminal = current_gate.resume(current_handle)
                    with lock:
                        terminals.append(terminal)
                except gate.RecoveryRequired as exc:
                    with lock:
                        recoveries.append(exc)
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    with lock:
                        errors.append(exc)
                    barrier.abort()
                finally:
                    if runner is not None:
                        with lock:
                            runner_calls.append(runner.calls)
                    if store is not None:
                        store.close()

            threads = [threading.Thread(target=resume) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive())
            barrier.abort()
            self.assertEqual([], errors)
            self.assertEqual(2, len(terminals) + len(recoveries))
            self.assertEqual(
                1,
                sum(runner_calls),
                (
                    f"terminals={len(terminals)} "
                    f"recoveries={[item.reason_code for item in recoveries]}"
                ),
            )
            self.assertTrue(
                all(recovery.reason_code == "unknown-effect" for recovery in recoveries)
            )

            readback = _must_open(self, state_root)
            try:
                operation = cast(
                    tuple[dict[str, object], ...],
                    _state_projection(readback)["operation"],
                )[0]
                self.assertEqual("TERMINAL", operation["status"])
            finally:
                readback.close()

    def test_wrong_ref_request_owner_and_forged_capture_are_state_preserving(
        self,
    ) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            context, snapshot, adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            profiles = _profile_resolver(snapshot)
            baseline_before_prepare = _state_projection(fixture.store)
            staged_snapshot, staged_admission = (
                verification_store.capture_approval_binding(
                    fixture.store,
                    fixture.handoff,
                    context,
                    fixture.review_refs[-1],
                    fixture.completion_ref,
                )
            )
            _assert_rejected_without_mutation(
                self,
                fixture.store,
                lambda: verification_store.StoreVerificationAdapter.from_capture(
                    fixture.store,
                    replace(
                        staged_snapshot,
                        effect_owner="forged-effect-owner",
                    ),
                    # The staged value is intentionally the one issued with the
                    # unmodified snapshot; a forged snapshot must not be accepted.
                    staged_admission,
                    profiles,
                ),
                baseline_before_prepare,
            )

            wrong_context = fixture.store.read_verification_context(
                fixture.root.root_key,
                "wrong-effect-owner",
                fixture.final_review_binding,
            )
            wrong_snapshot, wrong_staged = verification_store.capture_approval_binding(
                fixture.store,
                fixture.handoff,
                wrong_context,
                fixture.review_refs[-1],
                fixture.completion_ref,
            )
            wrong_adapter = verification_store.StoreVerificationAdapter.from_capture(
                fixture.store,
                wrong_snapshot,
                wrong_staged,
                _profile_resolver(wrong_snapshot),
            )
            wrong_adapter.resolve(gate.ApprovalRef(wrong_snapshot.approval_ref))

            # Use the helper's staged adapter for the legitimate prepare.  The
            # context itself is only retained to make the fixture dependency
            # explicit; Gate owns the actual admission resolution.
            del context
            handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            baseline = _state_projection(fixture.store)
            _assert_rejected_without_mutation(
                self,
                fixture.store,
                lambda: adapter.begin_effect_once(
                    gate.VerificationRef(str(handle.verification_ref) + "-wrong"),
                    handle.request_digest,
                ),
                baseline,
            )
            _assert_rejected_without_mutation(
                self,
                fixture.store,
                lambda: adapter.begin_effect_once(
                    handle.verification_ref,
                    gate.ReceiptDigest("b" * 64),
                ),
                baseline,
            )
            _assert_rejected_without_mutation(
                self,
                fixture.store,
                lambda: wrong_adapter.begin_effect_once(
                    handle.verification_ref,
                    handle.request_digest,
                ),
                baseline,
            )

    def test_profile_drift_before_direct_begin_is_state_preserving(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            profiles = _profile_resolver(snapshot)
            fresh = verification_store.StoreVerificationAdapter.from_store(
                fixture.store,
                workflow.WorkflowRootKey(fixture.root.root_key),
                handle.verification_ref,
                fixture.owner_id,
                profiles,
            )
            before = _state_projection(fixture.store)
            profiles.value = replace(
                profiles.value,
                profile_identity=replace(
                    profiles.value.profile_identity,
                    probe_revision="profile-drift-probe",
                ),
            )
            _assert_rejected_without_mutation(
                self,
                fixture.store,
                lambda: fresh.begin_effect_once(
                    handle.verification_ref,
                    handle.request_digest,
                ),
                before,
            )

    def test_external_profile_error_and_copied_origin_are_bounded(self) -> None:
        class FailingResolver(gate_fixtures.Resolver):
            failure: BaseException | None = None

            def resolve(
                self,
                ref: VerificationProfileRef,
            ) -> gate.VerificationProfile:
                if self.failure is not None:
                    raise self.failure
                return super().resolve(ref)

        for copied_origin in (False, True):
            with (
                self.subTest(copied_origin=copied_origin),
                actual_review_checkpoint_fixture() as fixture,
            ):
                _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                    self, fixture
                )
                handle = verification_gate.start(
                    gate.ApprovalRef(snapshot.approval_ref)
                )
                resolver = FailingResolver(_profile_resolver(snapshot).value)
                fresh = verification_store.StoreVerificationAdapter.from_store(
                    fixture.store,
                    workflow.WorkflowRootKey(fixture.root.root_key),
                    handle.verification_ref,
                    fixture.owner_id,
                    resolver,
                )
                external = verification_store.VerificationStoreError(
                    "profile-secret-canary"
                )
                if copied_origin:
                    trusted = verification_store._context_error("trusted")
                    external._origin = trusted._origin
                resolver.failure = external
                before = _state_projection(fixture.store)
                with self.assertRaises(
                    verification_store.VerificationStoreError
                ) as raised:
                    fresh.begin_effect_once(
                        handle.verification_ref,
                        handle.request_digest,
                    )
                self.assertNotIn("canary", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(before, _state_projection(fixture.store))

    def test_stale_adapter_after_store_close_cannot_arm_new_effect(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                baseline = _state_projection(reopened)
                _assert_rejected_without_mutation(
                    self,
                    reopened,
                    lambda: adapter.begin_effect_once(
                        handle.verification_ref,
                        handle.request_digest,
                    ),
                    baseline,
                )
            finally:
                reopened.close()

    def test_effect_arm_fault_before_commit_rolls_back_operation_and_floor(
        self,
    ) -> None:
        def injector(target: str) -> tuple[list[str], Callable[[str], None]]:
            observed: list[str] = []

            def inject(point: str) -> None:
                observed.append(point)
                if point == target:
                    raise RuntimeError("effect-arm fault canary")

            return observed, inject

        for target in (
            "before_verification_effect_write",
            "after_verification_effect_write",
            "after_verification_effect_readback",
            "before_verification_effect_commit",
            "before_commit",
        ):
            with (
                self.subTest(target=target),
                actual_review_checkpoint_fixture() as fixture,
            ):
                _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                    self, fixture
                )
                before = _state_projection(fixture.store)
                seen, inject = injector(target)

                with (
                    mock.patch.object(fixture.store, "_fault", side_effect=inject),
                    self.assertRaises(verification_store.VerificationStoreError),
                ):
                    adapter.begin_effect_once(
                        handle.verification_ref,
                        handle.request_digest,
                    )
                self.assertIn(target, seen)
                self.assertEqual(before, _state_projection(fixture.store))

    def test_fresh_reopen_armed_marker_returns_unknown_without_runner_reexecution(
        self,
    ) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            first = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            self.assertIs(first.status, gate.EffectBeginStatus.RUN_ONCE)
            owner_calls = (
                fixture.owner_store.save_calls,
                fixture.owner_store.read_calls,
            )
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                fresh_adapter = _must_from_store(
                    self, reopened, fixture, handle, snapshot
                )
                fresh_effect = _must_begin(
                    self,
                    fresh_adapter,
                    handle.verification_ref,
                    handle.request_digest,
                )
                self.assertIs(fresh_effect.status, gate.EffectBeginStatus.UNKNOWN)
                self.assertEqual(first.effect_nonce, fresh_effect.effect_nonce)
                self.assertEqual(first.fencing_token, fresh_effect.fencing_token)

                fresh_runner = gate_fixtures.FakeRunner()
                profiles = _profile_resolver(snapshot)
                _profile, before_snapshot = _profile_and_snapshot(
                    dict(snapshot.approved_review)
                )
                fresh_gate = gate.VerificationGate(
                    fresh_adapter,
                    profiles,
                    gate_fixtures.SnapshotPort(before_snapshot),
                    fresh_runner,
                    fresh_adapter,
                )
                fresh_handle = fresh_gate.start(gate.ApprovalRef(handle.approval_ref))
                with self.assertRaises(gate.RecoveryRequired):
                    fresh_gate.resume(fresh_handle)
                self.assertEqual(0, fresh_runner.calls)
                self.assertEqual(
                    owner_calls,
                    (
                        fixture.owner_store.save_calls,
                        fixture.owner_store.read_calls,
                    ),
                )
                state = _state_projection(reopened)
                operation_rows = cast(tuple[dict[str, object], ...], state["operation"])
                operation = operation_rows[0]
                meta = cast(dict[str, object], state["meta"])
                self.assertEqual("EFFECT_PREPARED", operation["status"])
                self.assertEqual(snapshot.effect_owner, operation["effect_owner"])
                self.assertEqual(1, operation["effect_attempt"])
                self.assertEqual(meta["recovery_epoch"], operation["effect_epoch"])
                self.assertEqual(
                    meta["fencing_token_floor"],
                    operation["effect_fence"],
                )
                self.assertEqual(first.effect_nonce, operation["effect_nonce"])
                self.assertEqual(
                    operation["record_digest"],
                    verification_store._verification_record_digest(operation),
                )
                for name in (
                    "receipt_ref",
                    "receipt_digest",
                    "terminal_phase",
                    "terminal_receipt_ref",
                    "terminal_receipt_digest",
                    "unknown_code",
                    "unknown_evidence_digest",
                    "receipt_event_id",
                    "receipt_event_digest",
                    "terminal_event_id",
                    "terminal_event_digest",
                    "unknown_event_id",
                    "unknown_event_digest",
                ):
                    self.assertIsNone(operation[name], name)
                self.assertEqual(operation["updated_ns"], meta["last_clock_ns"])
            finally:
                reopened.close()

    def test_effect_commit_unknown_reopens_same_fence_without_second_arm(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )

            def inject(point: str) -> None:
                if point == "after_commit":
                    raise OSError("effect commit response loss canary")

            with (
                mock.patch.object(fixture.store, "_fault", side_effect=inject),
                self.assertRaises(verification_store.VerificationStoreError) as raised,
            ):
                adapter.begin_effect_once(
                    handle.verification_ref,
                    handle.request_digest,
                )
            self.assertIsNone(raised.exception.__cause__)
            retry_cleanup = getattr(raised.exception, "retry_cleanup", None)
            self.assertTrue(callable(retry_cleanup))
            if not callable(retry_cleanup):
                raise TypeError("effect cleanup capability is missing")
            retry_cleanup()

            reopened = _must_open(self, fixture.state_root)
            try:
                state_before = _state_projection(reopened)
                operation = cast(
                    tuple[dict[str, object], ...],
                    state_before["operation"],
                )[0]
                self.assertEqual("EFFECT_PREPARED", operation["status"])
                fresh = _must_from_store(self, reopened, fixture, handle, snapshot)
                replay = _must_begin(
                    self,
                    fresh,
                    handle.verification_ref,
                    handle.request_digest,
                )
                self.assertIs(replay.status, gate.EffectBeginStatus.UNKNOWN)
                self.assertEqual(operation["effect_nonce"], str(replay.effect_nonce))
                self.assertEqual(operation["effect_epoch"], replay.lease_epoch)
                self.assertEqual(operation["effect_fence"], replay.fencing_token)
                self.assertEqual(state_before, _state_projection(reopened))
            finally:
                reopened.close()

    def test_gate_effect_commit_unknown_preserves_cleanup_capability(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, _adapter, verification_gate, handle = _prepared(
                self, fixture
            )

            def inject(point: str) -> None:
                if point == "after_commit":
                    raise OSError("effect Gate commit response loss canary")

            with (
                mock.patch.object(fixture.store, "_fault", side_effect=inject),
                self.assertRaises(gate.RecoveryRequired) as raised,
            ):
                verification_gate.resume(handle)
            self.assertEqual("effect-response-loss", raised.exception.reason_code)
            self.assertTrue(callable(raised.exception.retry_cleanup))
            if not callable(raised.exception.retry_cleanup):
                raise TypeError("effect Gate cleanup capability is missing")
            raised.exception.retry_cleanup()


if __name__ == "__main__":
    unittest.main()
