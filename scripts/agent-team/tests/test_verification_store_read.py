"""RED tests for the Issue #82 single-snapshot lifecycle read seam."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import test_verification_gate as gate_fixtures
from test_verification_store_effect import _must_begin, _must_from_store, _must_open
from test_verification_store_prepare import _prepare_fixture, _profile_and_snapshot
from test_verification_store_receipt import _must_record, _receipt_inputs
from verification_store_fixtures import actual_review_checkpoint_fixture

from agent_team import verification_gate as gate

_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _must_read(
    testcase: unittest.TestCase,
    adapter: Any,
    verification_ref: gate.VerificationRef,
) -> tuple[
    gate.VerificationDurableRecord,
    gate.DurableRecordStatus,
    str,
]:
    try:
        observation = adapter._read_with_status(verification_ref)
    except BaseException as exc:
        testcase.fail(f"_read_with_status is unavailable: {type(exc).__name__}: {exc}")
        raise AssertionError("unreachable") from exc
    testcase.assertIs(type(observation), tuple)
    testcase.assertEqual(3, len(observation))
    record, status, revision = observation
    testcase.assertIs(type(record), gate.VerificationDurableRecord)
    testcase.assertIs(type(status), gate.DurableRecordStatus)
    testcase.assertRegex(revision, _REVISION)
    return record, status, revision


class VerificationStoreReadRedTests(unittest.TestCase):
    def test_terminal_rehydrates_in_child_process_without_runner(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            inputs = _receipt_inputs(self, fixture)
            receipt = _must_record(self, inputs)
            inputs["adapter"].apply_terminal_once(
                inputs["handle"].verification_ref,
                receipt.receipt_ref,
                receipt.receipt_digest,
            )
            fixture.store.close()
            project_root = Path(__file__).resolve().parents[1]
            test_root = project_root / "tests"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(project_root), str(test_root))
            )
            program = """
import json
import sys
from test_verification_gate import FakeRunner, Resolver, SnapshotPort
from test_verification_store_prepare import _profile_and_snapshot
from agent_team import verification_gate as gate
from agent_team import verification_store
from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore

state_root, root_key, verification_ref, owner_id = sys.argv[1:]
store = CoordinationStore(state_root)
try:
    persisted = store.read_verification_reentry(
        root_key, verification_ref, owner_id
    )
    snapshot = persisted[2]
    profile, before = _profile_and_snapshot(dict(snapshot.approved_review))
    adapter = verification_store.StoreVerificationAdapter.from_store(
        store,
        workflow.WorkflowRootKey(root_key),
        gate.VerificationRef(verification_ref),
        owner_id,
        Resolver(profile),
    )
    runner = FakeRunner()
    verification_gate = gate.VerificationGate(
        adapter, Resolver(profile), SnapshotPort(before), runner, adapter
    )
    handle = verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
    terminal = verification_gate.resume(handle)
    print(json.dumps({"calls": runner.calls, "phase": terminal.phase.value}))
finally:
    store.close()
"""
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    program,
                    str(fixture.state_root),
                    fixture.root.root_key,
                    str(inputs["handle"].verification_ref),
                    fixture.owner_id,
                ],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                {"calls": 0, "phase": "completed"},
                json.loads(result.stdout),
            )

    def test_prepared_read_status_and_hook_are_one_exact_record(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate = _prepare_fixture(
                self,
                fixture,
            )
            handle = _verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            record, status, revision = _must_read(
                self,
                adapter,
                handle.verification_ref,
            )
            self.assertIs(status, gate.DurableRecordStatus.PREPARED)
            self.assertIs(record.status, status)
            self.assertIsNone(record.effect)
            self.assertIsNone(record.receipt)
            self.assertEqual(handle.verification_ref, record.verification_ref)
            self.assertEqual(handle.approval_ref, record.approval_ref)
            self.assertEqual(handle.request_digest, record.request.request_digest)
            self.assertEqual(record, adapter.read(handle.verification_ref))
            self.assertIs(status, adapter.status(handle.verification_ref))
            repeated = adapter._read_with_status(handle.verification_ref)
            self.assertEqual(revision, repeated[2])

    def test_armed_fresh_read_has_run_once_hint_but_resume_never_runs(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, snapshot, adapter, _verification_gate = _prepare_fixture(
                self,
                fixture,
            )
            handle = _verification_gate.start(gate.ApprovalRef(snapshot.approval_ref))
            armed = _must_begin(
                self,
                adapter,
                handle.verification_ref,
                handle.request_digest,
            )
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                fresh = _must_from_store(self, reopened, fixture, handle, snapshot)
                record, status, _revision = _must_read(
                    self,
                    fresh,
                    handle.verification_ref,
                )
                self.assertIs(status, gate.DurableRecordStatus.PREPARED)
                self.assertIsNotNone(record.effect)
                if record.effect is None:
                    raise AssertionError("armed durable effect is missing")
                self.assertIs(record.effect.status, gate.EffectBeginStatus.RUN_ONCE)
                self.assertEqual(armed.effect_nonce, record.effect.effect_nonce)
                self.assertEqual(armed.lease_epoch, record.effect.lease_epoch)
                self.assertEqual(armed.fencing_token, record.effect.fencing_token)

                profiles = gate_fixtures.Resolver(
                    _profile_and_snapshot(dict(snapshot.approved_review))[0]
                )
                _profile, before_snapshot = _profile_and_snapshot(
                    dict(snapshot.approved_review)
                )
                runner = gate_fixtures.FakeRunner()
                fresh_gate = gate.VerificationGate(
                    fresh,
                    profiles,
                    gate_fixtures.SnapshotPort(before_snapshot),
                    runner,
                    fresh,
                )
                fresh_handle = fresh_gate.start(gate.ApprovalRef(handle.approval_ref))
                with self.assertRaises(gate.RecoveryRequired) as raised:
                    fresh_gate.resume(fresh_handle)
                self.assertEqual("unknown-effect", raised.exception.reason_code)
                self.assertEqual(0, runner.calls)
            finally:
                reopened.close()

    def test_receipted_fresh_read_hydrates_exact_receipt_and_effect(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            inputs = _receipt_inputs(self, fixture)
            receipt = _must_record(self, inputs)
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                fresh = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    inputs["handle"],
                    inputs["snapshot"],
                )
                record, status, _revision = _must_read(
                    self,
                    fresh,
                    inputs["handle"].verification_ref,
                )
                self.assertIs(status, gate.DurableRecordStatus.RECEIPTED)
                self.assertIs(record.status, status)
                self.assertIsNotNone(record.effect)
                self.assertIsNotNone(record.receipt)
                self.assertEqual(receipt, record.receipt)
                self.assertEqual(receipt.receipt_ref, record.receipt_ref)
                self.assertEqual(receipt.receipt_digest, record.receipt_digest)
                if record.effect is None:
                    raise AssertionError("receipted effect is missing")
                self.assertIs(
                    record.effect.status,
                    gate.EffectBeginStatus.RECEIPTED,
                )
            finally:
                reopened.close()

    def test_receipted_fresh_public_gate_replays_without_runner(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            inputs = _receipt_inputs(self, fixture)
            receipt = _must_record(self, inputs)
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                fresh = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    inputs["handle"],
                    inputs["snapshot"],
                )
                profile, before = _profile_and_snapshot(
                    dict(inputs["snapshot"].approved_review)
                )
                runner = gate_fixtures.FakeRunner()
                fresh_gate = gate.VerificationGate(
                    fresh,
                    gate_fixtures.Resolver(profile),
                    gate_fixtures.SnapshotPort(before),
                    runner,
                    fresh,
                )
                fresh_handle = fresh_gate.start(inputs["handle"].approval_ref)
                terminal = fresh_gate.resume(fresh_handle)
                self.assertEqual(receipt.receipt_ref, terminal.receipt_ref)
                self.assertEqual(receipt.receipt_digest, terminal.receipt_digest)
                self.assertEqual(0, runner.calls)
            finally:
                reopened.close()

    def test_terminal_fresh_public_gate_replays_without_runner(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            inputs = _receipt_inputs(self, fixture)
            receipt = _must_record(self, inputs)
            expected = inputs["adapter"].apply_terminal_once(
                inputs["handle"].verification_ref,
                receipt.receipt_ref,
                receipt.receipt_digest,
            )
            fixture.store.close()
            reopened = _must_open(self, fixture.state_root)
            try:
                fresh = _must_from_store(
                    self,
                    reopened,
                    fixture,
                    inputs["handle"],
                    inputs["snapshot"],
                )
                profile, before = _profile_and_snapshot(
                    dict(inputs["snapshot"].approved_review)
                )
                runner = gate_fixtures.FakeRunner()
                fresh_gate = gate.VerificationGate(
                    fresh,
                    gate_fixtures.Resolver(profile),
                    gate_fixtures.SnapshotPort(before),
                    runner,
                    fresh,
                )
                fresh_handle = fresh_gate.start(inputs["handle"].approval_ref)
                self.assertEqual(expected, fresh_gate.resume(fresh_handle))
                self.assertEqual(0, runner.calls)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
