"""RED tests for the Issue #82 ``_mark_unknown`` transaction.

The tests start from the actual reopened #81 review pair and reuse the
prepare/effect helpers from ``test_verification_store_effect``.  SQL is used
only through that read-back helper; no test creates or mutates ledger rows
through SQL.
"""

from __future__ import annotations

import dataclasses
import threading
import unittest
from collections.abc import Callable
from typing import Any, cast
from unittest import mock

from test_verification_store_effect import (
    _must_begin,
    _must_from_store,
    _must_open,
    _prepared,
    _state_projection,
)
from test_verification_store_receipt import _must_record, _receipt_inputs
from verification_store_fixtures import actual_review_checkpoint_fixture

from agent_team import verification_gate as gate
from agent_team import verification_store
from agent_team import workflow_store as workflow

_FIXED_UNKNOWN_CODES: tuple[str, ...] = (
    "effect-response-loss",
    "runner-response-loss",
    "runner-response-invalid",
    "cleanup-unknown",
    "snapshot-drift",
    "receipt-response-loss",
    "receipt-commit-unknown",
    "effect-fence-unknown",
)
_UNKNOWN_EVIDENCE = "sha256:" + "e" * 64
_OTHER_UNKNOWN_EVIDENCE = "sha256:" + "f" * 64
_UNKNOWN_STUB_MESSAGE = "verification unknown commit is unavailable"
_REJECTION_ERRORS = (
    AssertionError,
    KeyError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
    workflow.WorkflowStoreError,
)


def _must_mark_unknown(
    testcase: unittest.TestCase,
    adapter: Any,
    verification_ref: gate.VerificationRef,
    request_digest: gate.ReceiptDigest,
    *,
    reason_code: str,
    effect: gate.VerificationEffectLease | None,
    evidence_digest: str,
) -> object:
    """Turn the intentionally absent unknown writer into a RED failure."""

    try:
        return adapter._mark_unknown(
            verification_ref,
            request_digest,
            reason_code=reason_code,
            effect=effect,
            evidence_digest=evidence_digest,
        )
    except BaseException as exc:
        testcase.fail(f"_mark_unknown is unavailable: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable") from exc


def _assert_unknown_rejected_without_mutation(
    testcase: unittest.TestCase,
    store: Any,
    adapter: Any,
    verification_ref: gate.VerificationRef,
    request_digest: gate.ReceiptDigest,
    *,
    reason_code: str,
    effect: gate.VerificationEffectLease | None,
    evidence_digest: str,
) -> None:
    """Reject an invalid lifecycle/reason without accepting the RED stub."""

    before = _state_projection(store)
    try:
        adapter._mark_unknown(
            verification_ref,
            request_digest,
            reason_code=reason_code,
            effect=effect,
            evidence_digest=evidence_digest,
        )
    except _REJECTION_ERRORS as exc:
        if (
            type(exc) is verification_store.VerificationStoreError
            and str(exc) == _UNKNOWN_STUB_MESSAGE
        ):
            testcase.fail("_mark_unknown is still the RED stub")
    except BaseException as exc:  # noqa: BLE001 - unexpected boundary is a failure
        testcase.fail(f"unexpected unknown rejection: {type(exc).__name__}: {exc}")
    else:
        testcase.fail("invalid _mark_unknown input was accepted")
    testcase.assertEqual(before, _state_projection(store))


def _must_terminal(
    testcase: unittest.TestCase,
    adapter: Any,
    handle: gate.VerificationHandle,
    receipt: gate.VerificationReceipt,
) -> object:
    try:
        return adapter.apply_terminal_once(
            handle.verification_ref,
            receipt.receipt_ref,
            receipt.receipt_digest,
        )
    except BaseException as exc:
        testcase.fail(
            f"apply_terminal_once is unavailable: {type(exc).__name__}: {exc}"
        )
        raise AssertionError("unreachable") from exc


def _forged_effect(
    effect: gate.VerificationEffectLease,
    **changes: object,
) -> gate.VerificationEffectLease:
    forged = object.__new__(gate.VerificationEffectLease)
    for field in dataclasses.fields(gate.VerificationEffectLease):
        value = (
            changes[field.name]
            if field.name in changes
            else getattr(effect, field.name)
        )
        object.__setattr__(forged, field.name, value)
    return forged


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise AssertionError(f"{name} is not an exact integer")
    return value


class VerificationStoreUnknownRedTests(unittest.TestCase):
    def test_armed_effect_commits_unknown_recovery_edge_without_task_write(
        self,
    ) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            armed = _state_projection(fixture.store)
            armed_task = cast(tuple[dict[str, object], ...], armed["task"])[0]
            armed_checkpoint_row = cast(
                tuple[dict[str, object], ...], armed["checkpoint"]
            )[0]
            armed_operation = cast(tuple[dict[str, object], ...], armed["operation"])[0]
            armed_checkpoint = workflow.decode_checkpoint(
                cast(bytes, armed_checkpoint_row["checkpoint_bytes"])
            )

            _must_mark_unknown(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
                reason_code="runner-response-loss",
                effect=effect,
                evidence_digest=_UNKNOWN_EVIDENCE,
            )
            after = _state_projection(fixture.store)
            task_rows = cast(tuple[dict[str, object], ...], after["task"])
            checkpoint_rows = cast(tuple[dict[str, object], ...], after["checkpoint"])
            operation_rows = cast(tuple[dict[str, object], ...], after["operation"])
            event_rows = cast(tuple[dict[str, object], ...], after["events"])
            self.assertEqual(1, len(task_rows))
            self.assertEqual(1, len(checkpoint_rows))
            self.assertEqual(1, len(operation_rows))
            self.assertEqual(armed_task, task_rows[0])
            self.assertEqual(
                len(cast(tuple[dict[str, object], ...], armed["events"])) + 1,
                len(event_rows),
            )

            operation = operation_rows[0]
            self.assertEqual("UNKNOWN_EFFECT", operation["status"])
            self.assertEqual("runner-response-loss", operation["unknown_code"])
            self.assertEqual(_UNKNOWN_EVIDENCE, operation["unknown_evidence_digest"])
            for field in (
                "effect_owner",
                "effect_attempt",
                "effect_epoch",
                "effect_fence",
                "effect_nonce",
            ):
                self.assertEqual(armed_operation[field], operation[field], field)
            for field in (
                "receipt_ref",
                "receipt_digest",
                "terminal_phase",
                "terminal_receipt_ref",
                "terminal_receipt_digest",
                "receipt_event_id",
                "receipt_event_digest",
                "terminal_event_id",
                "terminal_event_digest",
            ):
                self.assertIsNone(operation[field], field)
            self.assertIsNotNone(operation["unknown_event_id"])
            self.assertIsNotNone(operation["unknown_event_digest"])
            self.assertEqual(
                operation["record_digest"],
                verification_store._verification_record_digest(operation),
            )

            checkpoint_row = checkpoint_rows[0]
            checkpoint = workflow.decode_checkpoint(
                cast(bytes, checkpoint_row["checkpoint_bytes"])
            )
            self.assertEqual(
                armed_checkpoint.workflow_sequence + 1,
                checkpoint.workflow_sequence,
            )
            self.assertEqual(armed_checkpoint.task_sequence, checkpoint.task_sequence)
            self.assertIs(
                checkpoint.workflow_state,
                workflow.CheckpointState.RECOVERY_REQUIRED,
            )
            for field in (
                "root",
                "run",
                "execution_mode",
                "task_sequence",
                "task_policy",
                "active_assignment",
                "pending_delivery",
                "replied_message_ids",
                "read_observed",
                "released",
                "review_authority",
                "last_operation",
            ):
                self.assertEqual(
                    getattr(armed_checkpoint, field),
                    getattr(checkpoint, field),
                    field,
                )
            authority = checkpoint.verification_authority
            self.assertIsNotNone(authority)
            if authority is None:
                raise AssertionError("unknown verification authority is missing")

            event = event_rows[-1]
            expected_request = verification_store._verification_request_wrapper(
                verification_store.VerificationStage.UNKNOWN,
                fixture.root.root_key,
                str(handle.verification_ref),
                _exact_int(
                    armed_operation["workflow_sequence_after"],
                    "armed workflow sequence",
                ),
                _exact_int(
                    operation["workflow_sequence_after"],
                    "unknown workflow sequence",
                ),
                _exact_int(
                    armed_operation["task_sequence_after"],
                    "armed task sequence",
                ),
                _exact_int(
                    operation["task_sequence_after"],
                    "unknown task sequence",
                ),
                str(handle.request_digest),
            )
            expected_evidence = verification_store._verification_evidence_wrapper(
                verification_store.VerificationStage.UNKNOWN,
                fixture.root.root_key,
                str(handle.verification_ref),
                _exact_int(
                    armed_operation["workflow_sequence_after"],
                    "armed workflow sequence",
                ),
                _exact_int(
                    operation["workflow_sequence_after"],
                    "unknown workflow sequence",
                ),
                _exact_int(
                    armed_operation["task_sequence_after"],
                    "armed task sequence",
                ),
                _exact_int(
                    operation["task_sequence_after"],
                    "unknown task sequence",
                ),
                "runner-response-loss",
                _UNKNOWN_EVIDENCE,
                _exact_int(effect.fencing_token, "effect fence"),
            )
            self.assertEqual(workflow.TransitionKind.VERIFICATION.value, event["kind"])
            self.assertIsNone(event["operation_id"])
            self.assertIsNone(event["receipt_id"])
            self.assertEqual(verification_store.VERIFICATION_ACTOR, event["actor"])
            self.assertEqual("VERIFYING", event["from_state"])
            self.assertEqual("RECOVERY_REQUIRED", event["to_state"])
            self.assertEqual(expected_request, event["request_digest"])
            self.assertEqual(expected_evidence, event["evidence_ref"])
            self.assertEqual(expected_evidence, authority.digest)
            self.assertEqual(str(handle.verification_ref), authority.reference)
            self.assertEqual(event["workflow_event_id"], operation["unknown_event_id"])
            self.assertEqual(event["event_digest"], operation["unknown_event_digest"])
            self.assertEqual(
                event["checkpoint_digest"], checkpoint_row["checkpoint_digest"]
            )
            self.assertEqual(event["event_digest"], workflow_event_digest(event))

    def test_unknown_code_is_the_closed_eight_value_set(self) -> None:
        for reason_code in _FIXED_UNKNOWN_CODES:
            with (
                self.subTest(reason_code=reason_code),
                actual_review_checkpoint_fixture() as fixture,
            ):
                _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                    self, fixture
                )
                effect = _must_begin(
                    self,
                    adapter,
                    handle.verification_ref,
                    handle.request_digest,
                )
                _must_mark_unknown(
                    self,
                    adapter,
                    handle.verification_ref,
                    handle.request_digest,
                    reason_code=reason_code,
                    effect=effect,
                    evidence_digest=_UNKNOWN_EVIDENCE,
                )
                operation = cast(
                    tuple[dict[str, object], ...],
                    _state_projection(fixture.store)["operation"],
                )[0]
                self.assertEqual("UNKNOWN_EFFECT", operation["status"])
                self.assertEqual(reason_code, operation["unknown_code"])
                self.assertEqual(
                    _UNKNOWN_EVIDENCE,
                    operation["unknown_evidence_digest"],
                )

    def test_restore_invalidation_is_not_an_issue_82_unknown_code(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            _assert_unknown_rejected_without_mutation(
                self,
                fixture.store,
                adapter,
                handle.verification_ref,
                handle.request_digest,
                reason_code="restore_invalidation",
                effect=effect,
                evidence_digest=_UNKNOWN_EVIDENCE,
            )

    def test_prepared_receipted_and_terminal_never_downgrade_to_unknown(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            _assert_unknown_rejected_without_mutation(
                self,
                fixture.store,
                adapter,
                handle.verification_ref,
                handle.request_digest,
                reason_code="runner-response-loss",
                effect=None,
                evidence_digest=_UNKNOWN_EVIDENCE,
            )

        with actual_review_checkpoint_fixture() as fixture:
            inputs = _receipt_inputs(self, fixture)
            receipt = _must_record(self, inputs)
            adapter = inputs["adapter"]
            handle = inputs["handle"]
            effect = inputs["effect"]
            _assert_unknown_rejected_without_mutation(
                self,
                fixture.store,
                adapter,
                handle.verification_ref,
                handle.request_digest,
                reason_code="runner-response-loss",
                effect=effect,
                evidence_digest=_UNKNOWN_EVIDENCE,
            )

        with actual_review_checkpoint_fixture() as fixture:
            inputs = _receipt_inputs(self, fixture)
            receipt = _must_record(self, inputs)
            _must_terminal(self, inputs["adapter"], inputs["handle"], receipt)
            _assert_unknown_rejected_without_mutation(
                self,
                fixture.store,
                inputs["adapter"],
                inputs["handle"].verification_ref,
                inputs["handle"].request_digest,
                reason_code="runner-response-loss",
                effect=inputs["effect"],
                evidence_digest=_UNKNOWN_EVIDENCE,
            )

    def test_unknown_replay_and_reason_evidence_fence_conflicts_preserve_state(
        self,
    ) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            _must_mark_unknown(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
                reason_code="runner-response-loss",
                effect=effect,
                evidence_digest=_UNKNOWN_EVIDENCE,
            )
            committed = _state_projection(fixture.store)
            _must_mark_unknown(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
                reason_code="runner-response-loss",
                effect=effect,
                evidence_digest=_UNKNOWN_EVIDENCE,
            )
            self.assertEqual(committed, _state_projection(fixture.store))
            for reason_code, evidence_digest, selected_effect in (
                ("runner-response-invalid", _UNKNOWN_EVIDENCE, effect),
                ("runner-response-loss", _OTHER_UNKNOWN_EVIDENCE, effect),
                (
                    "runner-response-loss",
                    _UNKNOWN_EVIDENCE,
                    _forged_effect(
                        effect,
                        fencing_token=effect.fencing_token + 1,
                    ),
                ),
            ):
                with self.subTest(
                    reason_code=reason_code,
                    evidence_digest=evidence_digest,
                    fence=selected_effect.fencing_token,
                ):
                    _assert_unknown_rejected_without_mutation(
                        self,
                        fixture.store,
                        adapter,
                        handle.verification_ref,
                        handle.request_digest,
                        reason_code=reason_code,
                        effect=selected_effect,
                        evidence_digest=evidence_digest,
                    )

    def test_fresh_unknown_readback_is_recovery_only_and_has_no_retry(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            _must_mark_unknown(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
                reason_code="runner-response-loss",
                effect=effect,
                evidence_digest=_UNKNOWN_EVIDENCE,
            )
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                fresh_adapter = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    handle,
                    snapshot,
                )
                try:
                    observed = fresh_adapter._read_with_status(handle.verification_ref)
                except BaseException as exc:
                    self.fail(
                        "fresh unknown readback is unavailable: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise AssertionError("unreachable") from exc
                self.assertIs(type(observed), tuple)
                if type(observed) is not tuple or len(observed) != 3:
                    raise AssertionError("unknown readback tuple is invalid")
                record, status, revision_digest = observed
                self.assertIs(type(record), gate.VerificationDurableRecord)
                self.assertIs(status, gate.DurableRecordStatus.UNKNOWN)
                self.assertIs(record.status, gate.DurableRecordStatus.UNKNOWN)
                self.assertRegex(cast(str, revision_digest), r"sha256:[0-9a-f]{64}\Z")
                _assert_unknown_rejected_without_mutation(
                    self,
                    reopened,
                    fresh_adapter,
                    handle.verification_ref,
                    handle.request_digest,
                    reason_code="runner-response-invalid",
                    effect=None,
                    evidence_digest=_OTHER_UNKNOWN_EVIDENCE,
                )
            finally:
                reopened.close()

    def test_two_writers_commit_one_unknown_event_and_one_replay(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            del effect
            fixture.store.close()
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def writer() -> None:
                store = None
                try:
                    store = _must_open(self, fixture.state_root)
                    fresh_adapter = _must_from_store(
                        self,
                        store,
                        fixture,
                        handle,
                        snapshot,
                    )
                    barrier.wait(timeout=10)
                    fresh_adapter._mark_unknown(
                        handle.verification_ref,
                        handle.request_digest,
                        reason_code="runner-response-loss",
                        effect=None,
                        evidence_digest=_UNKNOWN_EVIDENCE,
                    )
                    with lock:
                        outcomes.append("ok")
                except BaseException as exc:  # noqa: BLE001 - thread result is asserted
                    with lock:
                        errors.append(exc)
                finally:
                    if store is not None:
                        store.close()

            threads = tuple(threading.Thread(target=writer) for _ in range(2))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(["ok", "ok"], sorted(outcomes))

            reopened = _must_open(self, fixture.state_root)
            try:
                state = _state_projection(reopened)
                operation = cast(tuple[dict[str, object], ...], state["operation"])
                events = cast(tuple[dict[str, object], ...], state["events"])
                self.assertEqual(1, len(operation))
                self.assertEqual("UNKNOWN_EFFECT", operation[0]["status"])
                unknown_events = tuple(
                    event
                    for event in events
                    if event["to_state"] == "RECOVERY_REQUIRED"
                )
                self.assertEqual(1, len(unknown_events))
                self.assertEqual(
                    unknown_events[0]["workflow_event_id"],
                    operation[0]["unknown_event_id"],
                )
            finally:
                reopened.close()

    def test_closed_generation_effect_cannot_authorize_fresh_unknown(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            old_effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                fresh = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    handle,
                    snapshot,
                )
                _assert_unknown_rejected_without_mutation(
                    self,
                    reopened,
                    fresh,
                    handle.verification_ref,
                    handle.request_digest,
                    reason_code="runner-response-loss",
                    effect=old_effect,
                    evidence_digest=_UNKNOWN_EVIDENCE,
                )
            finally:
                reopened.close()

    def test_gate_unknown_commit_unknown_preserves_cleanup_capability(self) -> None:
        class RaisingRunner:
            def run(self, request: object, effect: object) -> object:
                del request, effect
                raise OSError("runner response loss canary")

        with actual_review_checkpoint_fixture() as fixture:
            _context, _snapshot, _adapter, verification_gate, handle = _prepared(
                self, fixture
            )
            object.__setattr__(verification_gate, "_runner", RaisingRunner())
            after_commits = 0

            def inject(point: str) -> None:
                nonlocal after_commits
                if point == "after_commit":
                    after_commits += 1
                    if after_commits == 2:
                        raise OSError("unknown Gate commit response loss canary")

            with (
                mock.patch.object(fixture.store, "_fault", side_effect=inject),
                self.assertRaises(gate.RecoveryRequired) as raised,
            ):
                verification_gate.resume(handle)
            self.assertEqual("runner-response-loss", raised.exception.reason_code)
            self.assertTrue(callable(raised.exception.retry_cleanup))
            if not callable(raised.exception.retry_cleanup):
                raise TypeError("unknown Gate cleanup capability is missing")
            raised.exception.retry_cleanup()

    def test_unknown_fault_rolls_back_and_commit_unknown_reopens_as_unknown(
        self,
    ) -> None:
        def injector(target: str) -> tuple[list[str], Callable[[str], None]]:
            observed: list[str] = []

            def inject(point: str) -> None:
                observed.append(point)
                if point == target:
                    raise RuntimeError("unknown-before-commit canary")

            return observed, inject

        for target in (
            "before_verification_unknown_checkpoint_write",
            "after_verification_unknown_checkpoint_write",
            "before_verification_unknown_event_write",
            "after_verification_unknown_event_write",
            "before_verification_unknown_operation_write",
            "after_verification_unknown_operation_write",
            "after_verification_unknown_readback",
            "before_verification_unknown_commit",
            "before_commit",
        ):
            with (
                self.subTest(target=target),
                actual_review_checkpoint_fixture() as fixture,
            ):
                _context, _snapshot, adapter, _verification_gate, handle = _prepared(
                    self, fixture
                )
                effect = _must_begin(
                    self,
                    adapter,
                    handle.verification_ref,
                    handle.request_digest,
                )
                seen, before_commit_fault = injector(target)

                with mock.patch.object(
                    fixture.store,
                    "_fault",
                    side_effect=before_commit_fault,
                ):
                    _assert_unknown_rejected_without_mutation(
                        self,
                        fixture.store,
                        adapter,
                        handle.verification_ref,
                        handle.request_digest,
                        reason_code="runner-response-loss",
                        effect=effect,
                        evidence_digest=_UNKNOWN_EVIDENCE,
                    )
                self.assertIn(target, seen)

        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate, handle = _prepared(
                self, fixture
            )
            effect = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            seen = []

            def after_commit_fault(point: str) -> None:
                seen.append(point)
                if point == "after_commit":
                    raise OSError("unknown-after-commit canary")

            with mock.patch.object(
                fixture.store,
                "_fault",
                side_effect=after_commit_fault,
            ):
                try:
                    adapter._mark_unknown(
                        handle.verification_ref,
                        handle.request_digest,
                        reason_code="runner-response-loss",
                        effect=effect,
                        evidence_digest=_UNKNOWN_EVIDENCE,
                    )
                except BaseException as exc:  # noqa: BLE001 - commit outcome is asserted
                    if (
                        type(exc) is verification_store.VerificationStoreError
                        and str(exc) == _UNKNOWN_STUB_MESSAGE
                    ):
                        self.fail("_mark_unknown is still the RED stub")
                    cleanup = getattr(exc, "retry_cleanup", None)
                    if callable(cleanup):
                        cleanup()
                    else:
                        fixture.store.close()
                else:
                    self.fail("unknown commit outcome was treated as successful")
            self.assertIn("after_commit", seen)

            reopened = _must_open(self, fixture.state_root)
            try:
                state = _state_projection(reopened)
                operation = cast(tuple[dict[str, object], ...], state["operation"])
                events = cast(tuple[dict[str, object], ...], state["events"])
                self.assertEqual(1, len(operation))
                self.assertEqual("UNKNOWN_EFFECT", operation[0]["status"])
                self.assertEqual("runner-response-loss", operation[0]["unknown_code"])
                self.assertEqual(
                    1,
                    sum(event["to_state"] == "RECOVERY_REQUIRED" for event in events),
                )
                fresh_adapter = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    handle,
                    snapshot,
                )
                observed = fresh_adapter._read_with_status(handle.verification_ref)
                self.assertIs(observed[1], gate.DurableRecordStatus.UNKNOWN)
            finally:
                reopened.close()


def workflow_event_digest(event: dict[str, object]) -> str:
    """Expose the existing Store event digest oracle without writing SQL."""

    from agent_team.store import CoordinationStore

    return CoordinationStore._workflow_event_digest(event)


if __name__ == "__main__":
    unittest.main()
