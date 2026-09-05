"""RED tests for the package-private Issue #82 Gate recovery hooks.

The existing Gate tests intentionally use the public six-method state fake.  This
module adds a state double that exposes the two private capabilities required by
the schema-4 adapter without changing the public protocol:

* ``_read_with_status`` returns one record/status/revision observation;
* ``_mark_unknown`` records an effect-ambiguous recovery mutation.

These tests are deliberately written against the current Gate before its private
hook wiring exists.  They must fail with assertion failures (not collection or
runtime errors) until the implementation is added.
"""

from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from typing import Any

from test_verification_gate import (
    APPROVAL_REF,
    CleanupStatus,
    DurableRecordStatus,
    FakeRunner,
    FakeState,
    RecoveryRequired,
    SnapshotPort,
    VerificationGate,
    VerificationRunResult,
    make_gate,
    snapshot,
)

import agent_team.verification_gate as gate
from agent_team.task_policy import ReceiptRef, TaskPhase

_REVISION_DIGEST = "sha256:" + "d" * 64
_CANARY = "runner-response-secret-canary"
_READBACK_CANARY = "readback-secret-canary"


class HookState(FakeState):
    """Trace the private state seam while retaining the existing fake behavior."""

    def __init__(self, *, read_mode: str = "valid") -> None:
        super().__init__()
        self.read_mode = read_mode
        self.begin_response_loss = False
        self.begin_armed = False
        self.receipt_response_loss = False
        self.trace: list[str] = []
        self.read_with_status_calls = 0
        self.mark_unknown_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def prepare_once(
        self, request: gate.VerificationRequest
    ) -> gate.VerificationPrepareResult:
        self.trace.append("prepare")
        return super().prepare_once(request)

    def begin_effect_once(
        self,
        verification_ref: gate.VerificationRef,
        request_digest: gate.ReceiptDigest,
    ) -> gate.VerificationEffectLease:
        self.trace.append("begin")
        if self.begin_response_loss and not self.begin_armed:
            raise OSError(_CANARY)
        effect = super().begin_effect_once(verification_ref, request_digest)
        if self.begin_response_loss:
            self.begin_armed = True
            raise OSError(_CANARY)
        return effect

    def read(
        self, verification_ref: gate.VerificationRef
    ) -> gate.VerificationDurableRecord:
        self.trace.append("read")
        return super().read(verification_ref)

    def status(self, verification_ref: gate.VerificationRef) -> DurableRecordStatus:
        self.trace.append("status")
        return super().status(verification_ref)

    def record_receipt_once(
        self,
        verification_ref: gate.VerificationRef,
        effect: gate.VerificationEffectLease,
        result: gate.VerificationRunResult,
        before: gate.VerificationSnapshot,
        after: gate.VerificationSnapshot,
    ) -> gate.VerificationReceipt:
        self.trace.append("record_receipt")
        if self.receipt_response_loss:
            raise OSError(_CANARY)
        return super().record_receipt_once(
            verification_ref, effect, result, before, after
        )

    def apply_terminal_once(
        self,
        verification_ref: gate.VerificationRef,
        receipt_ref: ReceiptRef,
        receipt_digest: gate.ReceiptDigest,
    ) -> gate.VerificationTerminalResult:
        self.trace.append("apply_terminal")
        return super().apply_terminal_once(
            verification_ref, receipt_ref, receipt_digest
        )

    def _read_with_status(self, verification_ref: gate.VerificationRef) -> Any:
        self.trace.append("read_with_status")
        self.read_with_status_calls += 1
        if self.read_mode == "readback-unknown" and self.read_with_status_calls > 1:
            raise OSError(_READBACK_CANARY)
        with self.lock:
            record = self.record
            effect = self.effect
        if record is None:
            raise LookupError(verification_ref)
        if self.read_mode == "malformed":
            return (record, record.status)
        if self.read_mode == "status-mismatch":
            return record, DurableRecordStatus.RECEIPTED, _REVISION_DIGEST
        if self.read_mode == "revision-mismatch":
            return record, record.status, "not-a-revision-digest"
        if effect is not None and record.status is DurableRecordStatus.PREPARED:
            record = gate._make_record(
                record.verification_ref,
                record.approval_ref,
                record.request,
                DurableRecordStatus.PREPARED,
                effect,
                None,
            )
        return record, record.status, _REVISION_DIGEST

    def _mark_unknown(self, *args: Any, **kwargs: Any) -> object:
        self.trace.append("mark_unknown")
        self.mark_unknown_calls.append((args, dict(kwargs)))
        return object()


class RaisingRunner(FakeRunner):
    def run(
        self,
        request: gate.VerificationRequest,
        effect: gate.VerificationEffectLease,
    ) -> VerificationRunResult:
        self.calls += 1
        self.requests.append(request)
        raise OSError(_CANARY)


class InvalidResultRunner(FakeRunner):
    def run(
        self,
        request: gate.VerificationRequest,
        effect: gate.VerificationEffectLease,
    ) -> VerificationRunResult:
        result = super().run(request, effect)
        # Mutate after construction so the exact result type remains intact while
        # Gate's post-effect validator observes an invalid cleanup state.
        object.__setattr__(result, "cleanup", CleanupStatus.UNKNOWN)
        return result


class VerificationGatePrivateHookTest(unittest.TestCase):
    def test_public_signatures_and_protocol_remain_unchanged(self) -> None:
        self.assertFalse(hasattr(gate, "_attest_runner_result"))
        self.assertEqual(
            tuple(inspect.signature(VerificationGate.start).parameters),
            ("self", "approval_ref"),
        )
        self.assertEqual(
            tuple(inspect.signature(VerificationGate.resume).parameters),
            ("self", "handle"),
        )
        self.assertEqual(
            tuple(
                inspect.signature(gate.VerificationStatePort.prepare_once).parameters
            ),
            ("self", "request"),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    gate.VerificationStatePort.begin_effect_once
                ).parameters
            ),
            ("self", "verification_ref", "request_digest"),
        )
        self.assertEqual(
            tuple(inspect.signature(gate.VerificationStatePort.read).parameters),
            ("self", "verification_ref"),
        )
        self.assertEqual(
            tuple(inspect.signature(gate.VerificationStatePort.status).parameters),
            ("self", "verification_ref"),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    gate.VerificationStatePort.record_receipt_once
                ).parameters
            ),
            ("self", "verification_ref", "effect", "result", "before", "after"),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    gate.VerificationStatePort.apply_terminal_once
                ).parameters
            ),
            ("self", "verification_ref", "receipt_ref", "receipt_digest"),
        )
        self.assertNotIn("_read_with_status", gate.VerificationStatePort.__dict__)
        self.assertNotIn("_mark_unknown", gate.VerificationStatePort.__dict__)
        self.assertFalse(hasattr(VerificationGate, "execute"))
        self.assertFalse(hasattr(VerificationGate, "mark_unknown"))

    def test_resume_uses_one_revision_observation_before_separate_reads(self) -> None:
        state = HookState()
        verification_gate, _, _, _ = make_gate(state=state)
        handle = verification_gate.start(APPROVAL_REF)

        terminal = verification_gate.resume(handle)

        self.assertEqual(TaskPhase.COMPLETED, terminal.phase)
        self.assertEqual(
            [
                "prepare",
                "read_with_status",
                "begin",
                "record_receipt",
                "apply_terminal",
            ],
            state.trace,
        )
        self.assertEqual(1, state.read_with_status_calls)
        self.assertEqual(0, state.read_calls)
        self.assertEqual(0, state.status_calls)

    def test_malformed_or_mismatched_private_read_observation_requires_recovery(
        self,
    ) -> None:
        for mode in ("malformed", "status-mismatch", "revision-mismatch"):
            with self.subTest(mode=mode):
                state = HookState(read_mode=mode)
                verification_gate, _, _, _ = make_gate(state=state)
                handle = verification_gate.start(APPROVAL_REF)

                with self.assertRaises(RecoveryRequired):
                    verification_gate.resume(handle)

                self.assertEqual(["prepare", "read_with_status"], state.trace)
                self.assertEqual(1, state.read_with_status_calls)
                self.assertEqual(0, state.read_calls)
                self.assertEqual(0, state.status_calls)
                self.assertEqual(0, state.begin_calls)

    def _assert_post_arm_unknown(
        self,
        *,
        runner: FakeRunner,
        state: HookState,
        snapshots: SnapshotPort | None = None,
        reason_code: str,
    ) -> None:
        verification_gate, _, _, _ = make_gate(
            state=state,
            runner=runner,
            snapshots=snapshots,
        )
        handle = verification_gate.start(APPROVAL_REF)

        with self.assertRaises(RecoveryRequired) as caught:
            verification_gate.resume(handle)

        self.assertEqual(1, len(state.mark_unknown_calls))
        args, kwargs = state.mark_unknown_calls[0]
        self.assertEqual((handle.verification_ref, handle.request_digest), args)
        self.assertEqual(reason_code, kwargs.get("reason_code"))
        self.assertIs(state.effect, kwargs.get("effect"))
        evidence_digest = kwargs.get("evidence_digest")
        self.assertIsInstance(evidence_digest, str)
        assert isinstance(evidence_digest, str)
        self.assertTrue(evidence_digest.startswith("sha256:"))
        self.assertNotIn(_CANARY, str(caught.exception))
        self.assertNotIn(_CANARY, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(1, runner.calls)

    def test_runner_exception_after_effect_arm_marks_unknown_once(self) -> None:
        state = HookState()
        runner = RaisingRunner()
        self._assert_post_arm_unknown(
            runner=runner,
            state=state,
            reason_code="runner-response-loss",
        )
        self.assertEqual(
            [
                "prepare",
                "read_with_status",
                "begin",
                "read_with_status",
                "mark_unknown",
            ],
            state.trace,
        )

    def test_invalid_runner_result_after_effect_arm_marks_unknown_once(self) -> None:
        state = HookState()
        runner = InvalidResultRunner()
        self._assert_post_arm_unknown(
            runner=runner,
            state=state,
            reason_code="cleanup-unknown",
        )
        self.assertEqual(
            [
                "prepare",
                "read_with_status",
                "begin",
                "read_with_status",
                "mark_unknown",
            ],
            state.trace,
        )

    def test_after_snapshot_drift_after_effect_arm_marks_unknown_once(self) -> None:
        state = HookState()
        runner = FakeRunner()
        before = snapshot()
        drifted = replace(before, inode=99)
        snapshots = SnapshotPort(before, before, drifted)
        self._assert_post_arm_unknown(
            runner=runner,
            state=state,
            snapshots=snapshots,
            reason_code="snapshot-drift",
        )
        self.assertEqual(
            [
                "prepare",
                "read_with_status",
                "begin",
                "read_with_status",
                "mark_unknown",
            ],
            state.trace,
        )

    def test_receipt_response_loss_after_effect_arm_marks_unknown_once(self) -> None:
        state = HookState()
        state.receipt_response_loss = True
        runner = FakeRunner()
        self._assert_post_arm_unknown(
            runner=runner,
            state=state,
            reason_code="receipt-response-loss",
        )
        self.assertEqual(
            [
                "prepare",
                "read_with_status",
                "begin",
                "record_receipt",
                "read_with_status",
                "mark_unknown",
            ],
            state.trace,
        )

    def test_begin_response_loss_marks_unknown_only_when_arm_is_proven(self) -> None:
        for mode, expected_mark_count, expected_trace in (
            (
                "unarmed",
                0,
                ["prepare", "read_with_status", "begin", "read_with_status"],
            ),
            (
                "armed",
                1,
                [
                    "prepare",
                    "read_with_status",
                    "begin",
                    "read_with_status",
                    "mark_unknown",
                ],
            ),
            (
                "readback-unknown",
                0,
                ["prepare", "read_with_status", "begin", "read_with_status"],
            ),
        ):
            with self.subTest(mode=mode):
                state = HookState(
                    read_mode=(
                        "readback-unknown" if mode == "readback-unknown" else "valid"
                    )
                )
                state.begin_response_loss = True
                state.begin_armed = mode == "armed"
                verification_gate, _, _, _ = make_gate(
                    state=state,
                    runner=FakeRunner(),
                )
                handle = verification_gate.start(APPROVAL_REF)

                with self.assertRaises(RecoveryRequired) as caught:
                    verification_gate.resume(handle)

                self.assertEqual(expected_mark_count, len(state.mark_unknown_calls))
                self.assertEqual(expected_trace, state.trace)
                self.assertEqual(0, state.record_receipt_calls)
                self.assertEqual(0, state.apply_terminal_calls)
                self.assertNotIn(_CANARY, str(caught.exception))
                self.assertNotIn(_CANARY, repr(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    def test_known_receipt_and_terminal_never_downgrade_to_unknown(self) -> None:
        state = HookState()
        verification_gate, _, _, _ = make_gate(state=state)
        handle = verification_gate.start(APPROVAL_REF)

        record = state.record
        self.assertIsNotNone(record)
        assert record is not None
        effect = state.begin_effect_once(
            handle.verification_ref,
            handle.request_digest,
        )
        result = FakeRunner().run(record.request, effect)
        receipt = state.record_receipt_once(
            handle.verification_ref,
            effect,
            result,
            record.request.before_snapshot,
            snapshot(),
        )
        state.trace.clear()
        state.mark_unknown_calls.clear()
        state.read_with_status_calls = 0

        terminal = verification_gate.resume(handle)

        self.assertEqual(TaskPhase.COMPLETED, terminal.phase)
        self.assertEqual(["read_with_status", "apply_terminal"], state.trace)
        self.assertEqual(0, len(state.mark_unknown_calls))
        self.assertEqual(0, state.read_calls)
        self.assertEqual(0, state.status_calls)
        self.assertEqual(receipt.receipt_ref, terminal.receipt_ref)

        state.trace.clear()
        state.read_with_status_calls = 0
        replay = verification_gate.resume(handle)

        self.assertEqual(TaskPhase.COMPLETED, replay.phase)
        self.assertEqual(["read_with_status"], state.trace)
        self.assertEqual(0, len(state.mark_unknown_calls))
        self.assertEqual(0, state.read_calls)
        self.assertEqual(0, state.status_calls)


if __name__ == "__main__":
    unittest.main()
