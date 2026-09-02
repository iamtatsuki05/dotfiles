"""RED tests for the Issue #82 terminal transaction.

The cases start with the real reopened #81 review pair and use the existing
prepare/effect/receipt helpers.  SQL writes are limited to explicit mixed-row
rejection probes after a canonical receipt; the starting ledger is never synthesized.
"""

from __future__ import annotations

import threading
import unittest
from collections.abc import Callable
from functools import partial
from typing import Any, cast
from unittest import mock

import test_verification_gate as gate_fixtures
from test_verification_store_receipt import (
    _actual_fixture,
    _must_from_store,
    _must_open,
    _must_record,
    _receipt_inputs,
    _state_projection,
)

from agent_team import task_verification_ledger as ledger
from agent_team import verification_gate as gate
from agent_team import verification_store
from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore
from agent_team.task_policy import ReceiptRef, TaskPhase

_REJECTION_ERRORS = (
    AssertionError,
    KeyError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
)
_TERMINAL_STUB_MESSAGE = "verification terminal commit is unavailable"


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise AssertionError(f"{name} is not an exact integer")
    return value


def _terminal_inputs(
    testcase: unittest.TestCase,
    fixture: Any,
    *,
    outcome: gate.VerificationOutcome = gate.VerificationOutcome.PASSED,
) -> tuple[dict[str, Any], gate.VerificationReceipt]:
    inputs = _receipt_inputs(testcase, fixture)
    if outcome is not gate.VerificationOutcome.PASSED:
        inputs["result"] = gate._run_and_attest(
            gate_fixtures.FakeRunner(outcome),
            inputs["request"],
            inputs["effect"],
        )
    receipt = _must_record(testcase, inputs)
    return inputs, receipt


def _must_terminal(
    testcase: unittest.TestCase,
    inputs: dict[str, Any],
    receipt: gate.VerificationReceipt,
) -> gate.VerificationTerminalResult:
    try:
        terminal = inputs["adapter"].apply_terminal_once(
            inputs["handle"].verification_ref,
            receipt.receipt_ref,
            receipt.receipt_digest,
        )
    except BaseException as exc:  # noqa: BLE001 - boundary failure is asserted
        if type(exc) is verification_store.VerificationStoreError and str(exc) == (
            _TERMINAL_STUB_MESSAGE
        ):
            testcase.fail("apply_terminal_once is still the RED stub")
        testcase.fail(
            f"apply_terminal_once is unavailable: {type(exc).__name__}: {exc}"
        )
    testcase.assertIs(
        type(terminal),
        gate.VerificationTerminalResult,
        "apply_terminal_once must return an exact terminal result",
    )
    try:
        gate._validate_terminal(terminal)
    except BaseException as exc:  # noqa: BLE001 - boundary validation is asserted
        testcase.fail(f"terminal result is not canonical: {type(exc).__name__}: {exc}")
    return cast(gate.VerificationTerminalResult, terminal)


def _assert_terminal_projection(
    testcase: unittest.TestCase,
    fixture: Any,
    inputs: dict[str, Any],
    receipt: gate.VerificationReceipt,
    before: dict[str, object],
    terminal: gate.VerificationTerminalResult,
    expected_phase: TaskPhase,
) -> None:
    after = _state_projection(fixture.store)
    before_tasks = cast(tuple[dict[str, object], ...], before["task"])
    before_operations = cast(tuple[dict[str, object], ...], before["operation"])
    before_checkpoints = cast(tuple[dict[str, object], ...], before["checkpoint"])
    before_receipts = cast(tuple[dict[str, object], ...], before["receipts"])
    before_events = cast(tuple[dict[str, object], ...], before["events"])
    operations = cast(tuple[dict[str, object], ...], after["operation"])
    receipts = cast(tuple[dict[str, object], ...], after["receipts"])
    tasks = cast(tuple[dict[str, object], ...], after["task"])
    checkpoints = cast(tuple[dict[str, object], ...], after["checkpoint"])
    events = cast(tuple[dict[str, object], ...], after["events"])
    testcase.assertEqual(1, len(operations))
    testcase.assertEqual(1, len(receipts))
    testcase.assertEqual(1, len(tasks))
    testcase.assertEqual(1, len(checkpoints))
    testcase.assertEqual(len(before_events) + 1, len(events))
    testcase.assertEqual(before_receipts, receipts)

    operation = operations[0]
    before_operation = before_operations[0]
    task_row = tasks[0]
    before_task = before_tasks[0]
    checkpoint_row = checkpoints[0]
    before_checkpoint = before_checkpoints[0]
    event = events[-1]
    expected_phase_value = (
        "completed" if expected_phase is TaskPhase.COMPLETED else "verification_failed"
    )

    testcase.assertIs(terminal.phase, expected_phase)
    testcase.assertEqual(inputs["handle"].verification_ref, terminal.verification_ref)
    testcase.assertEqual(receipt.receipt_ref, terminal.receipt_ref)
    testcase.assertEqual(receipt.receipt_digest, terminal.receipt_digest)
    testcase.assertEqual("TERMINAL", operation["status"])
    testcase.assertEqual(str(receipt.receipt_ref), operation["receipt_ref"])
    testcase.assertEqual(str(receipt.receipt_digest), operation["receipt_digest"])
    testcase.assertEqual(expected_phase_value, operation["terminal_phase"])
    testcase.assertEqual(str(receipt.receipt_ref), operation["terminal_receipt_ref"])
    testcase.assertEqual(
        str(receipt.receipt_digest), operation["terminal_receipt_digest"]
    )
    testcase.assertEqual(event["workflow_event_id"], operation["terminal_event_id"])
    testcase.assertEqual(event["event_digest"], operation["terminal_event_digest"])
    testcase.assertEqual(
        operation["record_digest"],
        verification_store._verification_record_digest(operation),
    )
    for field in verification_store._VERIFICATION_RECORD_COLUMNS:
        if field not in {
            "status",
            "task_sequence_after",
            "task_digest_after",
            "workflow_sequence_after",
            "workflow_digest_after",
            "terminal_phase",
            "terminal_receipt_ref",
            "terminal_receipt_digest",
            "terminal_event_id",
            "terminal_event_digest",
            "record_digest",
            "updated_ns",
        }:
            testcase.assertEqual(before_operation[field], operation[field], field)
    for field in (
        "unknown_code",
        "unknown_evidence_digest",
        "unknown_event_id",
        "unknown_event_digest",
    ):
        testcase.assertIsNone(operation[field], field)

    task_bytes = task_row["state_bytes"]
    testcase.assertIs(type(task_bytes), bytes)
    if type(task_bytes) is not bytes:
        raise AssertionError("terminal task state bytes are missing")
    task_state = ledger.decode_task_state(task_bytes)
    testcase.assertIs(task_state.phase, expected_phase)
    testcase.assertEqual(
        _exact_int(before_task["sequence"], "before task sequence") + 1,
        task_state.sequence,
    )
    testcase.assertEqual(str(receipt.receipt_ref), str(task_state.receipt_ref))
    testcase.assertEqual(ledger.task_state_digest(task_bytes), task_row["state_digest"])
    testcase.assertEqual(task_state.sequence, operation["task_sequence_after"])
    testcase.assertEqual(
        ledger.task_state_digest(task_bytes), operation["task_digest_after"]
    )

    workflow_sequence_before = _exact_int(
        before_checkpoint["workflow_sequence"], "before workflow sequence"
    )
    testcase.assertEqual(
        workflow_sequence_before + 1,
        checkpoint_row["workflow_sequence"],
    )
    testcase.assertEqual(task_state.sequence, checkpoint_row["task_sequence"])
    testcase.assertEqual("VERIFYING", checkpoint_row["workflow_state"])
    checkpoint = workflow.decode_checkpoint(
        cast(bytes, checkpoint_row["checkpoint_bytes"])
    )
    testcase.assertIsNotNone(checkpoint.verification_authority)
    if checkpoint.verification_authority is None:
        raise AssertionError("terminal checkpoint authority is missing")
    testcase.assertEqual(
        str(receipt.verification_ref), checkpoint.verification_authority.reference
    )
    testcase.assertEqual(
        event["evidence_ref"], checkpoint.verification_authority.digest
    )
    testcase.assertEqual(task_state.sequence, operation["task_sequence_after"])
    testcase.assertEqual(
        workflow_sequence_before + 1,
        operation["workflow_sequence_after"],
    )

    expected_request = verification_store._verification_request_wrapper(
        verification_store.VerificationStage.TERMINAL,
        fixture.root.root_key,
        str(receipt.verification_ref),
        workflow_sequence_before,
        workflow_sequence_before + 1,
        _exact_int(before_task["sequence"], "before task sequence"),
        task_state.sequence,
        str(receipt.request_digest),
    )
    expected_evidence = verification_store._verification_evidence_wrapper(
        verification_store.VerificationStage.TERMINAL,
        fixture.root.root_key,
        str(receipt.verification_ref),
        workflow_sequence_before,
        workflow_sequence_before + 1,
        _exact_int(before_task["sequence"], "before task sequence"),
        task_state.sequence,
        expected_phase_value,
        str(receipt.receipt_ref),
        str(receipt.receipt_digest),
    )
    testcase.assertIsNone(event["operation_id"])
    testcase.assertIsNone(event["receipt_id"])
    testcase.assertEqual(workflow.TransitionKind.VERIFICATION.value, event["kind"])
    testcase.assertEqual(verification_store.VERIFICATION_ACTOR, event["actor"])
    testcase.assertEqual("VERIFYING", event["from_state"])
    testcase.assertEqual("VERIFYING", event["to_state"])
    testcase.assertEqual(expected_request, event["request_digest"])
    testcase.assertEqual(expected_evidence, event["evidence_ref"])
    testcase.assertEqual(
        event["event_digest"], CoordinationStore._workflow_event_digest(event)
    )
    testcase.assertEqual(event["checkpoint_digest"], checkpoint.checkpoint_digest)
    testcase.assertEqual(
        cast(bytes, event["checkpoint_bytes"]),
        cast(bytes, checkpoint_row["checkpoint_bytes"]),
    )


def _assert_terminal_rejected_without_mutation(
    testcase: unittest.TestCase,
    store: CoordinationStore,
    call: Callable[[], object],
    before: dict[str, object],
) -> None:
    try:
        call()
    except _REJECTION_ERRORS as exc:
        if type(exc) is verification_store.VerificationStoreError and str(exc) == (
            _TERMINAL_STUB_MESSAGE
        ):
            testcase.fail("apply_terminal_once is still the RED stub")
    except BaseException as exc:  # noqa: BLE001 - unexpected boundary is a failure
        testcase.fail(f"unexpected terminal rejection: {type(exc).__name__}: {exc}")
    else:
        testcase.fail("invalid terminal input was accepted")
    testcase.assertEqual(before, _state_projection(store))


class VerificationStoreTerminalRedTests(unittest.TestCase):
    def test_mixed_receipted_operation_is_rejected_before_terminal_mutation(
        self,
    ) -> None:
        with _actual_fixture(self) as fixture:
            inputs, receipt = _terminal_inputs(self, fixture)
            with fixture.store._write_transaction() as connection:
                row = dict(
                    connection.execute(
                        "SELECT * FROM verification_operations"
                    ).fetchone()
                )
                row["request_digest"] = "f" * 64
                row["record_digest"] = verification_store._verification_record_digest(
                    row
                )
                connection.execute(
                    "UPDATE verification_operations SET request_digest = ?, "
                    "record_digest = ?",
                    (row["request_digest"], row["record_digest"]),
                )
            before = _state_projection(fixture.store)
            _assert_terminal_rejected_without_mutation(
                self,
                fixture.store,
                partial(
                    inputs["adapter"].apply_terminal_once,
                    inputs["handle"].verification_ref,
                    receipt.receipt_ref,
                    receipt.receipt_digest,
                ),
                before,
            )

    def test_passed_receipt_terminal_commits_completed_n_plus_three_and_terminal_edge(
        self,
    ) -> None:
        with _actual_fixture(self) as fixture:
            inputs, receipt = _terminal_inputs(self, fixture)
            before = _state_projection(fixture.store)
            terminal = _must_terminal(self, inputs, receipt)
            _assert_terminal_projection(
                self,
                fixture,
                inputs,
                receipt,
                before,
                terminal,
                TaskPhase.COMPLETED,
            )

    def test_known_nonpassed_receipt_terminal_commits_verification_failed_n_plus_three(
        self,
    ) -> None:
        for outcome in (
            gate.VerificationOutcome.FAILED,
            gate.VerificationOutcome.TIMEOUT,
            gate.VerificationOutcome.OUTPUT_LIMIT,
            gate.VerificationOutcome.SCHEMA_INVALID,
            gate.VerificationOutcome.RUNNER_UNAVAILABLE,
        ):
            with self.subTest(outcome=outcome), _actual_fixture(self) as fixture:
                inputs, receipt = _terminal_inputs(
                    self,
                    fixture,
                    outcome=outcome,
                )
                before = _state_projection(fixture.store)
                terminal = _must_terminal(self, inputs, receipt)
                _assert_terminal_projection(
                    self,
                    fixture,
                    inputs,
                    receipt,
                    before,
                    terminal,
                    TaskPhase.VERIFICATION_FAILED,
                )

    def test_wrong_receipt_ref_or_digest_is_state_preserving(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs, receipt = _terminal_inputs(self, fixture)
            before = _state_projection(fixture.store)
            cases = (
                (
                    "receipt-ref",
                    ReceiptRef("foreign-receipt"),
                    receipt.receipt_digest,
                ),
                ("receipt-digest", receipt.receipt_ref, gate.ReceiptDigest("f" * 64)),
                (
                    "both",
                    ReceiptRef("foreign-receipt"),
                    gate.ReceiptDigest("f" * 64),
                ),
            )
            for name, receipt_ref, receipt_digest in cases:
                with self.subTest(field=name):
                    _assert_terminal_rejected_without_mutation(
                        self,
                        fixture.store,
                        partial(
                            inputs["adapter"].apply_terminal_once,
                            inputs["handle"].verification_ref,
                            receipt_ref,
                            receipt_digest,
                        ),
                        before,
                    )

    def test_same_terminal_plan_replays_without_second_event_or_row(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs, receipt = _terminal_inputs(self, fixture)
            first = _must_terminal(self, inputs, receipt)
            committed = _state_projection(fixture.store)
            second = _must_terminal(self, inputs, receipt)
            self.assertEqual(first, second)
            self.assertEqual(committed, _state_projection(fixture.store))

    def test_two_store_writers_commit_one_terminal_event(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs, receipt = _terminal_inputs(self, fixture)
            state_root = fixture.state_root
            fixture.store.close()
            barrier = threading.Barrier(2)
            terminals: list[gate.VerificationTerminalResult] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def worker() -> None:
                store: CoordinationStore | None = None
                try:
                    store = CoordinationStore(state_root)
                    adapter = _must_from_store(
                        self,
                        store,
                        fixture,
                        inputs["handle"],
                        inputs["snapshot"],
                    )
                    barrier.wait(timeout=10)
                    terminal = adapter.apply_terminal_once(
                        inputs["handle"].verification_ref,
                        receipt.receipt_ref,
                        receipt.receipt_digest,
                    )
                    if type(terminal) is not gate.VerificationTerminalResult:
                        raise AssertionError("concurrent writer returned wrong type")
                    with lock:
                        terminals.append(terminal)
                except BaseException as exc:  # noqa: BLE001 - assertion below owns result
                    with lock:
                        errors.append(exc)
                    barrier.abort()
                finally:
                    if store is not None:
                        store.close()

            threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            barrier.abort()
            for thread in threads:
                self.assertFalse(thread.is_alive())
            if errors:
                self.fail(
                    "concurrent terminal writer failed: "
                    + ", ".join(type(error).__name__ for error in errors)
                )
            self.assertEqual(2, len(terminals))
            self.assertEqual(terminals[0], terminals[1])
            self.assertIs(terminals[0].phase, TaskPhase.COMPLETED)

            reopened = _must_open(self, state_root)
            try:
                state = _state_projection(reopened)
                operation = cast(tuple[dict[str, object], ...], state["operation"])
                events = cast(tuple[dict[str, object], ...], state["events"])
                receipts = cast(tuple[dict[str, object], ...], state["receipts"])
                self.assertEqual(1, len(operation))
                self.assertEqual("TERMINAL", operation[0]["status"])
                self.assertEqual(1, len(receipts))
                terminal_events = [
                    event
                    for event in events
                    if event["kind"] == workflow.TransitionKind.VERIFICATION.value
                    and event["actor"] == verification_store.VERIFICATION_ACTOR
                    and event["workflow_event_id"] == operation[0]["terminal_event_id"]
                ]
                self.assertEqual(1, len(terminal_events))
                self.assertEqual(
                    terminal_events[0]["workflow_event_id"],
                    operation[0]["terminal_event_id"],
                )
            finally:
                reopened.close()

    def test_terminal_fault_rolls_back_dual_state_and_event(self) -> None:
        def injector(target: str) -> tuple[list[str], Callable[[str], None]]:
            observed: list[str] = []

            def inject(point: str) -> None:
                observed.append(point)
                if point == target:
                    raise RuntimeError("terminal rollback canary")

            return observed, inject

        for target in (
            "before_verification_terminal_task_write",
            "after_verification_terminal_task_write",
            "before_verification_terminal_checkpoint_write",
            "after_verification_terminal_checkpoint_write",
            "before_verification_terminal_event_write",
            "after_verification_terminal_event_write",
            "before_verification_terminal_operation_write",
            "after_verification_terminal_operation_write",
            "after_verification_terminal_readback",
            "before_verification_terminal_commit",
            "before_commit",
        ):
            with self.subTest(target=target), _actual_fixture(self) as fixture:
                inputs, receipt = _terminal_inputs(self, fixture)
                before = _state_projection(fixture.store)
                seen, inject = injector(target)

                with mock.patch.object(fixture.store, "_fault", side_effect=inject):
                    _assert_terminal_rejected_without_mutation(
                        self,
                        fixture.store,
                        partial(
                            inputs["adapter"].apply_terminal_once,
                            inputs["handle"].verification_ref,
                            receipt.receipt_ref,
                            receipt.receipt_digest,
                        ),
                        before,
                    )
                self.assertIn(target, seen)

    def test_terminal_commit_unknown_reopens_and_replays_without_duplicate(
        self,
    ) -> None:
        with _actual_fixture(self) as fixture:
            inputs, receipt = _terminal_inputs(self, fixture)

            def inject(point: str) -> None:
                if point == "after_commit":
                    raise OSError("terminal commit response loss canary")

            error: BaseException | None = None
            with mock.patch.object(fixture.store, "_fault", side_effect=inject):
                try:
                    inputs["adapter"].apply_terminal_once(
                        inputs["handle"].verification_ref,
                        receipt.receipt_ref,
                        receipt.receipt_digest,
                    )
                except _REJECTION_ERRORS as exc:
                    error = exc
                except BaseException as exc:  # noqa: BLE001 - boundary assertion
                    self.fail(f"unexpected terminal commit error: {type(exc).__name__}")
            if error is None:
                self.fail("terminal commit-unknown was reported as success")
            if type(error) is verification_store.VerificationStoreError and str(
                error
            ) == (_TERMINAL_STUB_MESSAGE):
                self.fail("apply_terminal_once is still the RED stub")
            retry_cleanup = getattr(error, "retry_cleanup", None)
            self.assertTrue(callable(retry_cleanup))
            if not callable(retry_cleanup):
                raise TypeError("terminal commit-unknown cleanup capability is missing")
            retry_cleanup()

            reopened = _must_open(self, fixture.state_root)
            try:
                state = _state_projection(reopened)
                operation = cast(tuple[dict[str, object], ...], state["operation"])
                events = cast(tuple[dict[str, object], ...], state["events"])
                receipts = cast(tuple[dict[str, object], ...], state["receipts"])
                self.assertEqual(1, len(operation))
                self.assertEqual("TERMINAL", operation[0]["status"])
                self.assertEqual(1, len(receipts))
                terminal_events = [
                    event
                    for event in events
                    if event["kind"] == workflow.TransitionKind.VERIFICATION.value
                    and event["actor"] == verification_store.VERIFICATION_ACTOR
                    and event["workflow_event_id"] == operation[0]["terminal_event_id"]
                ]
                self.assertEqual(1, len(terminal_events))
                fresh_adapter = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    inputs["handle"],
                    inputs["snapshot"],
                )
                replay = fresh_adapter.apply_terminal_once(
                    inputs["handle"].verification_ref,
                    receipt.receipt_ref,
                    receipt.receipt_digest,
                )
                self.assertEqual(receipt.receipt_ref, replay.receipt_ref)
                self.assertEqual(receipt.receipt_digest, replay.receipt_digest)
                self.assertIs(replay.phase, TaskPhase.COMPLETED)
                self.assertEqual(state, _state_projection(reopened))
            finally:
                reopened.close()

    def test_gate_terminal_commit_unknown_preserves_cleanup_capability(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs, _receipt = _terminal_inputs(self, fixture)

            def inject(point: str) -> None:
                if point == "after_commit":
                    raise OSError("terminal Gate commit response loss canary")

            with (
                mock.patch.object(fixture.store, "_fault", side_effect=inject),
                self.assertRaises(gate.RecoveryRequired) as raised,
            ):
                inputs["verification_gate"].resume(inputs["handle"])
            self.assertEqual("terminal-response-loss", raised.exception.reason_code)
            self.assertTrue(callable(raised.exception.retry_cleanup))
            if not callable(raised.exception.retry_cleanup):
                raise TypeError("terminal Gate cleanup capability is missing")
            raised.exception.retry_cleanup()


if __name__ == "__main__":
    unittest.main()
