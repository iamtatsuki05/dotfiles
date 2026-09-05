"""RED tests for the Issue #82 receipt transaction.

The happy path starts from the real reopened #81 review pair and uses the
current adapter to arm the effect.  SQL writes are limited to explicit
post-arm mixed-row rejection probes; tests never synthesize the starting pair.
"""

from __future__ import annotations

import dataclasses
import threading
import unittest
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from typing import Any, cast
from unittest import mock

import test_verification_gate as gate_fixtures
from test_verification_store_prepare import (
    _prepare_fixture,
    _profile_and_snapshot,
)
from verification_store_fixtures import actual_review_checkpoint_fixture

from agent_team import task_verification_ledger as ledger
from agent_team import verification_gate as gate
from agent_team import verification_store
from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore
from agent_team.task_policy import TaskPhase, VerificationProfileRef

_REJECTION_ERRORS = (
    AssertionError,
    KeyError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
)
_RECEIPT_STUB_MESSAGE = "verification receipt commit is unavailable"


@contextmanager
def _actual_fixture(testcase: unittest.TestCase) -> Any:
    """Make an incomplete parallel production seam an explicit RED failure."""

    manager = actual_review_checkpoint_fixture()
    try:
        fixture = manager.__enter__()
    except BaseException as exc:
        testcase.fail(f"actual #81 fixture is unavailable: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable") from exc
    try:
        yield fixture
    finally:
        manager.__exit__(None, None, None)


def _state_projection(store: CoordinationStore) -> dict[str, object]:
    """Read the verification image without creating or changing any row."""

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


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise AssertionError(f"{name} is not an exact integer")
    return value


def _prepared(
    testcase: unittest.TestCase,
    fixture: Any,
) -> tuple[Any, Any, Any, Any, gate.VerificationHandle]:
    """Run the real #81 pair through Gate.start and return its handle."""

    context, snapshot, adapter, verification_gate = _prepare_fixture(testcase, fixture)
    try:
        handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
    except BaseException as exc:
        testcase.fail(
            f"verification prepare is unavailable: {type(exc).__name__}: {exc}"
        )
        raise AssertionError("unreachable") from exc
    return context, snapshot, adapter, verification_gate, handle


def _must_begin(
    testcase: unittest.TestCase,
    adapter: Any,
    verification_ref: gate.VerificationRef,
    request_digest: gate.ReceiptDigest,
) -> gate.VerificationEffectLease:
    """Arm through the current adapter and turn a missing seam into RED."""

    try:
        effect = adapter.begin_effect_once(verification_ref, request_digest)
    except BaseException as exc:
        testcase.fail(f"begin_effect_once is unavailable: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable") from exc
    testcase.assertIs(type(effect), gate.VerificationEffectLease)
    return cast(gate.VerificationEffectLease, effect)


def _must_open(testcase: unittest.TestCase, state_root: Any) -> CoordinationStore:
    try:
        return CoordinationStore(state_root)
    except BaseException as exc:
        testcase.fail(f"fresh Store reopen failed: {type(exc).__name__}")
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
        testcase.fail(f"fresh adapter reopen failed: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable") from exc


def _receipt_inputs(testcase: unittest.TestCase, fixture: Any) -> dict[str, Any]:
    """Prepare and arm one real pair, then build a deterministic runner result."""

    context, snapshot, adapter, verification_gate, handle = _prepared(testcase, fixture)
    effect = _must_begin(
        testcase,
        adapter,
        handle.verification_ref,
        handle.request_digest,
    )
    bound = adapter.resolve(gate.ApprovalRef(snapshot.approval_ref))
    profile, before = _profile_and_snapshot(dict(snapshot.approved_review))
    request = gate._build_request(bound, profile, before)
    runner = gate_fixtures.FakeRunner()
    result = gate._run_and_attest(runner, request, effect)
    return {
        "context": context,
        "snapshot": snapshot,
        "adapter": adapter,
        "verification_gate": verification_gate,
        "handle": handle,
        "effect": effect,
        "request": request,
        "runner": runner,
        "result": result,
        "before": before,
        "after": before,
    }


def _must_record(
    testcase: unittest.TestCase,
    inputs: dict[str, Any],
) -> gate.VerificationReceipt:
    """Turn the intentionally absent receipt writer into a RED failure."""

    try:
        receipt = inputs["adapter"].record_receipt_once(
            inputs["handle"].verification_ref,
            inputs["effect"],
            inputs["result"],
            inputs["before"],
            inputs["after"],
        )
    except BaseException as exc:
        testcase.fail(
            f"record_receipt_once is unavailable: {type(exc).__name__}: {exc}"
        )
        raise AssertionError("unreachable") from exc
    testcase.assertIs(
        type(receipt),
        gate.VerificationReceipt,
        "record_receipt_once must return an exact VerificationReceipt",
    )
    try:
        gate._validate_receipt(receipt, verify_digest=True)
    except BaseException as exc:  # noqa: BLE001 - boundary validation is asserted
        testcase.fail(f"receipt is not canonical: {type(exc).__name__}: {exc}")
    return cast(gate.VerificationReceipt, receipt)


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


def _forged_result(
    result: gate.VerificationRunResult,
    **changes: object,
) -> gate.VerificationRunResult:
    forged = object.__new__(gate.VerificationRunResult)
    for field in dataclasses.fields(gate.VerificationRunResult):
        value = (
            changes[field.name]
            if field.name in changes
            else getattr(result, field.name)
        )
        object.__setattr__(forged, field.name, value)
    return forged


def _assert_rejected_without_mutation(
    testcase: unittest.TestCase,
    store: CoordinationStore,
    call: Callable[[], object],
    before: dict[str, object],
) -> None:
    missing_stub = False
    try:
        call()
    except _REJECTION_ERRORS as exc:
        missing_stub = (
            type(exc) is verification_store.VerificationStoreError
            and str(exc) == _RECEIPT_STUB_MESSAGE
        )
    except BaseException as exc:  # noqa: BLE001 - unexpected boundary is a failure
        testcase.fail(f"unexpected receipt rejection: {type(exc).__name__}: {exc}")
    else:
        testcase.fail("invalid receipt input was accepted")
    testcase.assertEqual(before, _state_projection(store))
    if missing_stub:
        testcase.fail("record_receipt_once is still the RED stub")


class VerificationStoreReceiptRedTests(unittest.TestCase):
    def test_mixed_effect_operation_is_rejected_before_receipt_mutation(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs = _receipt_inputs(self, fixture)
            with fixture.store._write_transaction() as connection:
                row = dict(
                    connection.execute(
                        "SELECT * FROM verification_operations"
                    ).fetchone()
                )
                row["approval_ref"] = "foreign-approval"
                row["record_digest"] = verification_store._verification_record_digest(
                    row
                )
                connection.execute(
                    "UPDATE verification_operations SET approval_ref = ?, "
                    "record_digest = ?",
                    (row["approval_ref"], row["record_digest"]),
                )
            before = _state_projection(fixture.store)
            _assert_rejected_without_mutation(
                self,
                fixture.store,
                partial(_record_with_values, inputs),
                before,
            )

    def test_record_receipt_commits_canonical_row_and_dual_state_transition(
        self,
    ) -> None:
        with _actual_fixture(self) as fixture:
            inputs = _receipt_inputs(self, fixture)
            before = _state_projection(fixture.store)
            before_task = cast(tuple[dict[str, object], ...], before["task"])[0]
            before_checkpoint = cast(
                tuple[dict[str, object], ...], before["checkpoint"]
            )[0]
            before_operations = cast(tuple[dict[str, object], ...], before["operation"])
            receipt = _must_record(self, inputs)
            after = _state_projection(fixture.store)

            operation_rows = cast(tuple[dict[str, object], ...], after["operation"])
            receipt_rows = cast(tuple[dict[str, object], ...], after["receipts"])
            task_rows = cast(tuple[dict[str, object], ...], after["task"])
            checkpoint_rows = cast(tuple[dict[str, object], ...], after["checkpoint"])
            event_rows = cast(tuple[dict[str, object], ...], after["events"])
            self.assertEqual(1, len(operation_rows))
            self.assertEqual(1, len(receipt_rows))
            self.assertEqual(1, len(task_rows))
            self.assertEqual(1, len(checkpoint_rows))
            self.assertEqual(
                len(cast(tuple[dict[str, object], ...], before["events"])) + 1,
                len(event_rows),
            )

            operation = operation_rows[0]
            stored_receipt = receipt_rows[0]
            task_row = task_rows[0]
            checkpoint_row = checkpoint_rows[0]
            event = event_rows[-1]
            self.assertEqual("RECEIPTED", operation["status"])
            self.assertEqual(str(receipt.receipt_ref), operation["receipt_ref"])
            self.assertEqual(str(receipt.receipt_digest), operation["receipt_digest"])
            self.assertEqual(event["workflow_event_id"], operation["receipt_event_id"])
            self.assertEqual(event["event_digest"], operation["receipt_event_digest"])
            self.assertEqual(
                operation["record_digest"],
                verification_store._verification_record_digest(operation),
            )
            for field in (
                "root_key",
                "verification_ref",
                "approval_ref",
                "request_digest",
                "run_id",
                "main_terminal_id",
                "task_id",
                "dispatch_id",
                "attempt_id",
                "effect_owner",
                "effect_attempt",
                "effect_epoch",
                "effect_fence",
                "effect_nonce",
                "prepare_event_id",
                "prepare_event_digest",
                "created_ns",
            ):
                self.assertEqual(before_operations[0][field], operation[field], field)

            projection = ledger.verification_receipt_projection_from_receipt(receipt)
            receipt_bytes = stored_receipt["receipt_bytes"]
            self.assertIs(type(receipt_bytes), bytes)
            if type(receipt_bytes) is not bytes:
                raise AssertionError("receipt bytes are missing")
            self.assertEqual(
                receipt_bytes,
                ledger.encode_verification_receipt_projection(projection),
            )
            self.assertEqual(
                projection,
                ledger.decode_verification_receipt_projection(receipt_bytes),
            )
            self.assertEqual(fixture.root.root_key, stored_receipt["root_key"])
            self.assertEqual(str(receipt.receipt_ref), stored_receipt["receipt_ref"])
            self.assertEqual(
                str(receipt.verification_ref), stored_receipt["verification_ref"]
            )
            self.assertEqual(1, stored_receipt["receipt_schema_version"])
            self.assertEqual(
                str(receipt.receipt_digest), stored_receipt["receipt_digest"]
            )

            task_state_bytes = task_row["state_bytes"]
            self.assertIs(type(task_state_bytes), bytes)
            if type(task_state_bytes) is not bytes:
                raise AssertionError("receipt task state bytes are missing")
            task_state = ledger.decode_task_state(task_state_bytes)
            self.assertIs(task_state.phase, TaskPhase.VERIFYING)
            self.assertEqual(
                _exact_int(before_task["sequence"], "before task sequence") + 1,
                task_state.sequence,
            )
            self.assertEqual(str(receipt.receipt_ref), str(task_state.receipt_ref))
            self.assertEqual(
                ledger.task_state_digest(task_state_bytes), task_row["state_digest"]
            )

            prepared_workflow_sequence = _exact_int(
                before_checkpoint["workflow_sequence"],
                "before workflow sequence",
            )
            self.assertEqual(
                prepared_workflow_sequence + 1, checkpoint_row["workflow_sequence"]
            )
            self.assertEqual(task_state.sequence, checkpoint_row["task_sequence"])
            self.assertEqual("VERIFYING", checkpoint_row["workflow_state"])
            checkpoint = workflow.decode_checkpoint(
                cast(bytes, checkpoint_row["checkpoint_bytes"])
            )
            self.assertIsNotNone(checkpoint.verification_authority)
            if checkpoint.verification_authority is None:
                raise AssertionError("receipt checkpoint authority is missing")
            self.assertEqual(
                str(receipt.verification_ref),
                checkpoint.verification_authority.reference,
            )
            self.assertEqual(
                event["evidence_ref"], checkpoint.verification_authority.digest
            )

            expected_request = verification_store._verification_request_wrapper(
                verification_store.VerificationStage.RECEIPT,
                fixture.root.root_key,
                str(receipt.verification_ref),
                prepared_workflow_sequence,
                prepared_workflow_sequence + 1,
                _exact_int(before_task["sequence"], "before task sequence"),
                task_state.sequence,
                str(inputs["request"].request_digest),
            )
            expected_evidence = verification_store._verification_evidence_wrapper(
                verification_store.VerificationStage.RECEIPT,
                fixture.root.root_key,
                str(receipt.verification_ref),
                prepared_workflow_sequence,
                prepared_workflow_sequence + 1,
                _exact_int(before_task["sequence"], "before task sequence"),
                task_state.sequence,
                str(receipt.receipt_digest),
            )
            self.assertIsNone(event["operation_id"])
            self.assertIsNone(event["receipt_id"])
            self.assertEqual(workflow.TransitionKind.VERIFICATION.value, event["kind"])
            self.assertEqual(verification_store.VERIFICATION_ACTOR, event["actor"])
            self.assertEqual("VERIFYING", event["from_state"])
            self.assertEqual("VERIFYING", event["to_state"])
            self.assertEqual(expected_request, event["request_digest"])
            self.assertEqual(expected_evidence, event["evidence_ref"])
            self.assertEqual(
                event["event_digest"], CoordinationStore._workflow_event_digest(event)
            )

    def test_record_receipt_rejects_effect_owner_profile_request_result_and_snapshots(
        self,
    ) -> None:
        mutations: tuple[
            tuple[str, Callable[[dict[str, Any]], dict[str, Any]]], ...
        ] = (
            (
                "effect-equal-clone",
                lambda values: {
                    **values,
                    "effect": _forged_effect(values["effect"]),
                },
            ),
            (
                "effect-owner-fence",
                lambda values: {
                    **values,
                    "effect": _forged_effect(
                        values["effect"],
                        effect_nonce=gate.EffectNonce("foreign-effect"),
                    ),
                },
            ),
            (
                "profile",
                lambda values: {
                    **values,
                    "result": dataclasses.replace(
                        values["result"],
                        profile_ref=VerificationProfileRef("foreign-profile"),
                    ),
                },
            ),
            (
                "request",
                lambda values: {
                    **values,
                    "result": dataclasses.replace(
                        values["result"],
                        request_digest=gate.ReceiptDigest("f" * 64),
                    ),
                },
            ),
            (
                "result-unattested-clone",
                lambda values: {
                    **values,
                    "result": dataclasses.replace(values["result"]),
                },
            ),
            (
                "result",
                lambda values: {
                    **values,
                    "result": _forged_result(
                        values["result"],
                        stdout_bytes="malformed-result",
                    ),
                },
            ),
            (
                "before",
                lambda values: {
                    **values,
                    "before": replace(
                        values["before"], inode=values["before"].inode + 1
                    ),
                },
            ),
            (
                "after",
                lambda values: {
                    **values,
                    "after": replace(values["after"], inode=values["after"].inode + 1),
                },
            ),
        )
        for name, mutate in mutations:
            with (
                self.subTest(field=name),
                _actual_fixture(self) as fixture,
            ):
                inputs = _receipt_inputs(self, fixture)
                before = _state_projection(fixture.store)
                mutated = mutate(inputs)
                _assert_rejected_without_mutation(
                    self,
                    fixture.store,
                    partial(_record_with_values, mutated),
                    before,
                )

    def test_same_receipt_plan_replays_without_second_row_or_event(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs = _receipt_inputs(self, fixture)
            first = _must_record(self, inputs)
            committed = _state_projection(fixture.store)
            second = _must_record(self, inputs)
            self.assertEqual(first, second)
            self.assertEqual(committed, _state_projection(fixture.store))

    def test_two_store_writers_commit_one_physical_receipt(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs = _receipt_inputs(self, fixture)
            state_root = fixture.state_root
            fixture.store.close()
            barrier = threading.Barrier(2)
            receipts: list[gate.VerificationReceipt] = []
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
                    record = adapter._read_with_status(
                        inputs["handle"].verification_ref
                    )[0]
                    fresh_effect = record.effect
                    if (
                        fresh_effect is None
                        or fresh_effect.status is not gate.EffectBeginStatus.RUN_ONCE
                    ):
                        raise AssertionError("fresh armed effect is unavailable")
                    local_result = gate._run_and_attest(
                        gate_fixtures.FakeRunner(),
                        inputs["request"],
                        fresh_effect,
                    )
                    barrier.wait(timeout=10)
                    receipt = adapter.record_receipt_once(
                        inputs["handle"].verification_ref,
                        fresh_effect,
                        local_result,
                        inputs["before"],
                        inputs["after"],
                    )
                    if type(receipt) is not gate.VerificationReceipt:
                        raise AssertionError("concurrent writer returned wrong type")
                    with lock:
                        receipts.append(receipt)
                except BaseException as exc:  # noqa: BLE001 - asserted below
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
                    "concurrent receipt writer failed: "
                    + ", ".join(type(error).__name__ for error in errors)
                )
            self.assertEqual(2, len(receipts))
            self.assertEqual(receipts[0], receipts[1])

            reopened = _must_open(self, state_root)
            try:
                state = _state_projection(reopened)
                self.assertEqual(
                    1, len(cast(tuple[dict[str, object], ...], state["receipts"]))
                )
                receipt_events = [
                    event
                    for event in cast(tuple[dict[str, object], ...], state["events"])
                    if event["kind"] == workflow.TransitionKind.VERIFICATION.value
                    and event["actor"] == verification_store.VERIFICATION_ACTOR
                    and event["from_state"] == "VERIFYING"
                ]
                self.assertEqual(1, len(receipt_events))
            finally:
                reopened.close()

    def test_receipt_fault_before_commit_rolls_back_everything(self) -> None:
        def injector(target: str) -> tuple[list[str], Callable[[str], None]]:
            observed: list[str] = []

            def inject(point: str) -> None:
                observed.append(point)
                if point == target:
                    raise RuntimeError("receipt fault canary")

            return observed, inject

        for target in (
            "before_verification_receipt_row_write",
            "after_verification_receipt_row_write",
            "after_verification_receipt_task_write",
            "after_verification_receipt_checkpoint_write",
            "after_verification_receipt_event_write",
            "after_verification_receipt_operation_write",
            "after_verification_receipt_readback",
            "before_verification_receipt_commit",
            "before_commit",
        ):
            with self.subTest(target=target), _actual_fixture(self) as fixture:
                inputs = _receipt_inputs(self, fixture)
                before = _state_projection(fixture.store)
                seen, inject = injector(target)

                with mock.patch.object(fixture.store, "_fault", side_effect=inject):
                    _assert_rejected_without_mutation(
                        self,
                        fixture.store,
                        partial(_record_with_values, inputs),
                        before,
                    )
                self.assertIn(target, seen)

    def test_receipt_commit_unknown_is_read_back_after_fresh_reopen(self) -> None:
        with _actual_fixture(self) as fixture:
            inputs = _receipt_inputs(self, fixture)

            def inject(point: str) -> None:
                if point == "after_commit":
                    raise OSError("receipt commit response loss canary")

            error: BaseException | None = None
            with mock.patch.object(fixture.store, "_fault", side_effect=inject):
                try:
                    _record_with_values(inputs)
                except _REJECTION_ERRORS as exc:
                    error = exc
                except BaseException as exc:  # noqa: BLE001 - boundary assertion
                    self.fail(f"unexpected receipt commit error: {type(exc).__name__}")
            if error is None:
                self.fail("receipt commit-unknown was reported as success")
            if (
                type(error) is verification_store.VerificationStoreError
                and str(error) == _RECEIPT_STUB_MESSAGE
            ):
                self.fail("record_receipt_once is still the RED stub")
            retry_cleanup = getattr(error, "retry_cleanup", None)
            self.assertTrue(callable(retry_cleanup))
            if not callable(retry_cleanup):
                raise TypeError("receipt commit-unknown cleanup capability is missing")
            retry_cleanup()

            reopened = _must_open(self, fixture.state_root)
            try:
                state = _state_projection(reopened)
                operation = cast(tuple[dict[str, object], ...], state["operation"])
                receipts = cast(tuple[dict[str, object], ...], state["receipts"])
                self.assertEqual(1, len(operation))
                self.assertEqual("RECEIPTED", operation[0]["status"])
                self.assertEqual(1, len(receipts))
                self.assertEqual(
                    operation[0]["receipt_ref"], receipts[0]["receipt_ref"]
                )
                fresh_adapter = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    inputs["handle"],
                    inputs["snapshot"],
                )
                record, status, _revision = fresh_adapter._read_with_status(
                    inputs["handle"].verification_ref
                )
                self.assertIs(status, gate.DurableRecordStatus.RECEIPTED)
                self.assertIsNotNone(record.receipt)
                if record.receipt is None:
                    raise AssertionError("fresh receipt readback is missing")
                self.assertEqual(
                    str(record.receipt.receipt_ref),
                    receipts[0]["receipt_ref"],
                )
            finally:
                reopened.close()

    def test_gate_receipt_commit_unknown_preserves_cleanup_and_reason(self) -> None:
        with _actual_fixture(self) as fixture:
            _context, snapshot, _adapter, verification_gate = _prepare_fixture(
                self, fixture
            )
            handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            after_commits = 0

            def inject(point: str) -> None:
                nonlocal after_commits
                if point == "after_commit":
                    after_commits += 1
                    if after_commits == 2:
                        raise OSError("receipt Gate commit response loss canary")

            with (
                mock.patch.object(fixture.store, "_fault", side_effect=inject),
                self.assertRaises(gate.RecoveryRequired) as raised,
            ):
                verification_gate.resume(handle)
            self.assertEqual("receipt-commit-unknown", raised.exception.reason_code)
            self.assertTrue(callable(raised.exception.retry_cleanup))
            if not callable(raised.exception.retry_cleanup):
                raise TypeError("receipt Gate cleanup capability is missing")
            raised.exception.retry_cleanup()


def _record_with_values(values: dict[str, Any]) -> object:
    return values["adapter"].record_receipt_once(
        values["handle"].verification_ref,
        values["effect"],
        values["result"],
        values["before"],
        values["after"],
    )


if __name__ == "__main__":
    unittest.main()
