from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from typing import cast

import agent_team.verification_gate as gate
from agent_team.path_resource_policy import DispatchMode, LaneRoutingDecision
from agent_team.task_policy import (
    ClaimRef,
    GitObjectId,
    ReceiptRef,
    TaskLane,
    TaskPhase,
    TreeDigest,
    VerificationProfileRef,
    WorkspaceIdentity,
)

ApprovalAdmissionPort = gate.ApprovalAdmissionPort
ApprovalRef = gate.ApprovalRef
CleanupStatus = gate.CleanupStatus
DurableRecordStatus = gate.DurableRecordStatus
EffectBeginStatus = gate.EffectBeginStatus
EffectNonce = gate.EffectNonce
EnvName = gate.EnvName
PreparationStatus = gate.PreparationStatus
RecoveryRequired = gate.RecoveryRequired
ResultSchema = gate.ResultSchema
ResultSchemaId = gate.ResultSchemaId
VerificationExecutableIdentity = gate.VerificationExecutableIdentity
VerificationGate = gate.VerificationGate
VerificationGateError = gate.VerificationGateError
VerificationHandle = gate.VerificationHandle
VerificationOutcome = gate.VerificationOutcome
VerificationProfile = gate.VerificationProfile
VerificationProfileIdentity = gate.VerificationProfileIdentity
VerificationProfileResolver = gate.VerificationProfileResolver
VerificationReceipt = gate.VerificationReceipt
VerificationRequest = gate.VerificationRequest
VerificationRunResult = gate.VerificationRunResult
VerificationSnapshot = gate.VerificationSnapshot
VerificationStatePort = gate.VerificationStatePort
VerificationTerminalResult = gate.VerificationTerminalResult
VerificationRef = gate.VerificationRef
VerificationRunnerPort = gate.VerificationRunnerPort
WorkspaceSnapshotPort = gate.WorkspaceSnapshotPort

HEAD = GitObjectId("a" * 40)
TREE = TreeDigest("b" * 64)
WORKSPACE = WorkspaceIdentity("/repo")
CLAIM = ClaimRef("claim-1")
PROFILE_REF = VerificationProfileRef("verify")
APPROVAL_REF = ApprovalRef("approval-1")
VERIFICATION_REF = VerificationRef("verification-1")
ROUTING_DIGEST = gate.ReceiptDigest("c" * 64)


def routing_decision() -> LaneRoutingDecision:
    return LaneRoutingDecision(
        lane=TaskLane.NORMAL,
        candidate=True,
        dispatch_mode=DispatchMode.SERIAL,
        serial_review_required=True,
        completion_gate_required=True,
        permits_workspace_write=True,
        parallel_candidate=False,
        reservation=None,
        reason_code=None,
    )


def approved_review(
    reservation_digest: gate.ReceiptDigest | None = None,
) -> gate.ApprovedReview:
    return gate._make_approved(
        run_id="run-1",
        team_id="team-1",
        workspace="/repo",
        task_id="task-1",
        dispatch_id="dispatch-1",
        attempt_id="attempt-1",
        worker_node="worker",
        reviewer_node="reviewer",
        worker_terminal_id="worker-terminal",
        reviewer_terminal_id="reviewer-terminal",
        review_round=1,
        target_head="a" * 40,
        target_tree_digest="b" * 64,
        claim_ref="claim-1",
        policy_fingerprint="c" * 64,
        routing_lane=TaskLane.NORMAL,
        approval_ref=APPROVAL_REF,
        approval_sequence=4,
        profile_ref=PROFILE_REF,
        verification_id=gate.VerificationId("verification-1"),
        routing_digest=ROUTING_DIGEST,
        reservation_digest=reservation_digest,
    )


def profile() -> VerificationProfile:
    executable = VerificationExecutableIdentity(
        path="/usr/bin/verify-check",
        version="1.0.0",
        sha256="d" * 64,
    )
    template = (executable.path, "--workspace", "{workspace}")
    return VerificationProfile(
        ref=PROFILE_REF,
        profile_identity=VerificationProfileIdentity(
            harness_id="harness",
            permission="read-only",
            operating_system="darwin",
            architecture="arm64",
            probe_revision="probe-1",
            sandbox_policy_id="sandbox-1",
        ),
        executable=executable,
        argv_template=template,
        argv_template_digest=gate._argv_digest(template),
        cwd_policy="canonical-workspace",
        environment_allowlist=(EnvName("CI"), EnvName("LANG")),
        environment_values=("1", "C"),
        timeout_ms=10_000,
        output_limit_bytes=4_096,
        result_schema=ResultSchema(
            schema_id=ResultSchemaId("verification-result"),
            version=1,
            digest=gate.ReceiptDigest("e" * 64),
        ),
    )


def snapshot(approved: gate.ApprovedReview | None = None) -> VerificationSnapshot:
    value = approved or approved_review()
    return VerificationSnapshot(
        workspace=WorkspaceIdentity(value.workspace),
        canonical_path=value.workspace,
        device=1,
        inode=2,
        claim_ref=ClaimRef(value.claim_ref),
        target_head=GitObjectId(value.target_head),
        allowed_tree_digest=TreeDigest(value.target_tree_digest),
    )


class FakeAdmission(ApprovalAdmissionPort):
    def __init__(self, bound: gate._BoundApproval) -> None:
        self.bound = bound
        self.calls = 0

    def resolve(self, approval_ref: ApprovalRef) -> gate._BoundApproval:
        self.calls += 1
        return self.bound


class FakeState(VerificationStatePort):
    def __init__(self) -> None:
        self.record: gate.VerificationDurableRecord | None = None
        self.effect: gate.VerificationEffectLease | None = None
        self.receipt: VerificationReceipt | None = None
        self.terminal: VerificationTerminalResult | None = None
        self.prepare_calls = 0
        self.begin_calls = 0
        self.read_calls = 0
        self.status_calls = 0
        self.record_receipt_calls = 0
        self.apply_terminal_calls = 0
        self.lock = threading.Lock()

    def prepare_once(
        self, request: VerificationRequest
    ) -> gate.VerificationPrepareResult:
        with self.lock:
            self.prepare_calls += 1
            if self.record is None:
                self.record = gate._make_record(
                    VERIFICATION_REF,
                    ApprovalRef(request.approval_ref),
                    request,
                    DurableRecordStatus.PREPARED,
                    None,
                    None,
                )
            return gate._make_prepare(
                VERIFICATION_REF,
                ApprovalRef(request.approval_ref),
                request,
                PreparationStatus.PREPARED
                if self.prepare_calls == 1
                else PreparationStatus.EXISTING,
            )

    def begin_effect_once(
        self, verification_ref: VerificationRef, request_digest: gate.ReceiptDigest
    ) -> gate.VerificationEffectLease:
        with self.lock:
            self.begin_calls += 1
            if self.effect is None:
                self.effect = gate._make_effect(
                    verification_ref,
                    request_digest,
                    EffectNonce("effect-1"),
                    1,
                    1,
                    EffectBeginStatus.RUN_ONCE,
                )
            elif (
                self.record is not None
                and self.record.status is DurableRecordStatus.RECEIPTED
            ):
                return gate._make_effect(
                    verification_ref,
                    request_digest,
                    self.effect.effect_nonce,
                    self.effect.lease_epoch,
                    self.effect.fencing_token,
                    EffectBeginStatus.RECEIPTED,
                )
            else:
                return gate._make_effect(
                    verification_ref,
                    request_digest,
                    self.effect.effect_nonce,
                    self.effect.lease_epoch,
                    self.effect.fencing_token,
                    EffectBeginStatus.UNKNOWN,
                )
            return self.effect

    def read(self, verification_ref: VerificationRef) -> gate.VerificationDurableRecord:
        with self.lock:
            self.read_calls += 1
            if self.record is None:
                raise LookupError(verification_ref)
            return self.record

    def status(self, verification_ref: VerificationRef) -> DurableRecordStatus:
        with self.lock:
            self.status_calls += 1
            if self.record is None:
                raise LookupError(verification_ref)
            return self.record.status

    def record_receipt_once(
        self,
        verification_ref: VerificationRef,
        effect: gate.VerificationEffectLease,
        result: VerificationRunResult,
        before: VerificationSnapshot,
        after: VerificationSnapshot,
    ) -> VerificationReceipt:
        with self.lock:
            self.record_receipt_calls += 1
            if self.receipt is None:
                self.receipt = gate._make_receipt(
                    receipt_ref=ReceiptRef("receipt-1"),
                    request=self.record.request
                    if self.record
                    else cast(VerificationRequest, None),
                    result=result,
                    effect=effect,
                    after_snapshot=after,
                )
            assert self.record is not None
            stored_effect = gate._make_effect(
                effect.verification_ref,
                effect.request_digest,
                effect.effect_nonce,
                effect.lease_epoch,
                effect.fencing_token,
                EffectBeginStatus.RECEIPTED,
            )
            self.effect = stored_effect
            self.record = gate._make_record(
                self.record.verification_ref,
                self.record.approval_ref,
                self.record.request,
                DurableRecordStatus.RECEIPTED,
                stored_effect,
                self.receipt,
            )
            return self.receipt

    def apply_terminal_once(
        self,
        verification_ref: VerificationRef,
        receipt_ref: ReceiptRef,
        receipt_digest: gate.ReceiptDigest,
    ) -> VerificationTerminalResult:
        with self.lock:
            self.apply_terminal_calls += 1
            if self.terminal is None:
                assert self.receipt is not None
                phase = (
                    TaskPhase.COMPLETED
                    if self.receipt.outcome is VerificationOutcome.PASSED
                    else TaskPhase.VERIFICATION_FAILED
                )
                self.terminal = gate._make_terminal(
                    verification_ref, receipt_ref, receipt_digest, phase
                )
            assert self.record is not None
            terminal_effect = gate._make_effect(
                self.record.verification_ref,
                self.record.request.request_digest,
                self.record.effect.effect_nonce
                if self.record.effect
                else EffectNonce("effect-1"),
                self.record.effect.lease_epoch if self.record.effect else 1,
                self.record.effect.fencing_token if self.record.effect else 1,
                EffectBeginStatus.TERMINAL,
            )
            self.effect = terminal_effect
            self.record = gate._make_record(
                self.record.verification_ref,
                self.record.approval_ref,
                self.record.request,
                DurableRecordStatus.TERMINAL,
                terminal_effect,
                self.record.receipt,
            )
            return self.terminal


class FakeRunner(VerificationRunnerPort):
    def __init__(
        self, outcome: VerificationOutcome = VerificationOutcome.PASSED
    ) -> None:
        self.outcome = outcome
        self.calls = 0
        self.requests: list[VerificationRequest] = []

    def run(
        self, request: VerificationRequest, effect: gate.VerificationEffectLease
    ) -> VerificationRunResult:
        self.calls += 1
        self.requests.append(request)
        exit_code = (
            17
            if self.outcome is VerificationOutcome.FAILED
            else None
            if self.outcome
            in {
                VerificationOutcome.TIMEOUT,
                VerificationOutcome.OUTPUT_LIMIT,
            }
            else 0
        )
        executable_after = (
            None
            if self.outcome
            in {
                VerificationOutcome.RUNNER_UNAVAILABLE,
                VerificationOutcome.UNKNOWN_EFFECT,
            }
            else request.executable
        )
        cleanup = (
            CleanupStatus.NOT_STARTED
            if self.outcome is VerificationOutcome.RUNNER_UNAVAILABLE
            else CleanupStatus.UNKNOWN
            if self.outcome is VerificationOutcome.UNKNOWN_EFFECT
            else CleanupStatus.REAPED
        )
        return VerificationRunResult(
            verification_ref=VerificationRef(request.verification_id),
            request_digest=request.request_digest,
            profile_ref=request.profile_ref,
            profile_identity=request.profile_identity,
            profile_binding_digest=request.profile_binding_digest,
            executable_before=request.executable,
            executable_after=executable_after,
            effect_nonce=effect.effect_nonce,
            lease_epoch=effect.lease_epoch,
            fencing_token=effect.fencing_token,
            argv_digest=request.argv_digest,
            cwd=request.cwd,
            environment_names=request.environment_names,
            result_schema=request.result_schema,
            outcome=self.outcome,
            exit_code=None
            if self.outcome is VerificationOutcome.RUNNER_UNAVAILABLE
            else exit_code,
            stdout_sha256=None,
            stderr_sha256=None,
            stdout_bytes=0,
            stderr_bytes=0,
            cleanup=cleanup,
        )


def make_gate(
    *,
    resolver: VerificationProfileResolver | None = None,
    snapshots: WorkspaceSnapshotPort | None = None,
    runner: VerificationRunnerPort | None = None,
    state: FakeState | None = None,
    approved_value: gate.ApprovedReview | None = None,
) -> tuple[VerificationGate, FakeState, FakeAdmission, Resolver]:
    approved = approved_value or approved_review()
    bound = gate._make_bound_approval(APPROVAL_REF, approved)
    admission = FakeAdmission(bound)
    selected_resolver = resolver or Resolver(profile())
    selected_state = state or FakeState()
    selected_snapshots = snapshots or SnapshotPort(snapshot(approved))
    selected_runner = runner or FakeRunner()
    return (
        VerificationGate(
            admission,
            selected_resolver,
            selected_snapshots,
            selected_runner,
            selected_state,
        ),
        selected_state,
        admission,
        cast(Resolver, selected_resolver),
    )


class Resolver(VerificationProfileResolver):
    def __init__(self, value: VerificationProfile) -> None:
        self.value = value
        self.calls = 0

    def resolve(self, ref: VerificationProfileRef) -> VerificationProfile:
        self.calls += 1
        if ref != self.value.ref:
            raise LookupError(ref)
        return self.value


class SnapshotPort(WorkspaceSnapshotPort):
    def __init__(self, *values: VerificationSnapshot) -> None:
        self.values = list(values)
        self.calls = 0

    def capture(
        self, workspace: WorkspaceIdentity, claim_ref: ClaimRef
    ) -> VerificationSnapshot:
        self.calls += 1
        if not self.values:
            raise LookupError("snapshot")
        if len(self.values) == 1:
            return self.values[0]
        return self.values.pop(0)


class VerificationGateTest(unittest.TestCase):
    def test_secret_bearing_verification_values_have_redacted_repr(self) -> None:
        canary = "visible-environment-canary"
        approved = approved_review()
        bound = gate._make_bound_approval(APPROVAL_REF, approved)
        selected_profile = replace(
            profile(),
            environment_values=(canary, "C"),
        )
        request = gate._build_request(
            bound,
            selected_profile,
            snapshot(approved),
        )
        prepared = gate._make_prepare(
            gate.VerificationRef(request.verification_id),
            APPROVAL_REF,
            request,
            PreparationStatus.PREPARED,
        )
        for value in (selected_profile, request, prepared):
            self.assertNotIn(canary, repr(value))

    def test_public_seam_uses_opaque_ref_and_handle_only(self) -> None:
        verification_gate, _, _, _ = make_gate()
        self.assertEqual(
            tuple(__import__("inspect").signature(VerificationGate.start).parameters),
            ("self", "approval_ref"),
        )
        self.assertEqual(
            tuple(__import__("inspect").signature(VerificationGate.resume).parameters),
            ("self", "handle"),
        )
        handle = verification_gate.start(APPROVAL_REF)
        self.assertEqual(handle.verification_id, VERIFICATION_REF)
        with self.assertRaises(TypeError):
            verification_gate.start(APPROVAL_REF, routing_decision())  # type: ignore[call-arg]

    def test_start_requires_prepare_cas_before_handle(self) -> None:
        class UnknownState(FakeState):
            def prepare_once(
                self, request: VerificationRequest
            ) -> gate.VerificationPrepareResult:
                self.prepare_calls += 1
                raise RuntimeError("prepare response lost")

        state = UnknownState()
        verification_gate, _, _, _ = make_gate(state=state)
        with self.assertRaises(RecoveryRequired):
            verification_gate.start(APPROVAL_REF)
        self.assertEqual(state.prepare_calls, 1)

    def test_reserved_claim_admission_is_carried_by_private_bound_authority(
        self,
    ) -> None:
        approved = approved_review(gate.ReceiptDigest("a" * 64))
        state = FakeState()
        verification_gate, _, admission, _ = make_gate(
            state=state,
            approved_value=approved,
        )

        handle = verification_gate.start(APPROVAL_REF)

        self.assertEqual(handle.verification_id, VERIFICATION_REF)
        self.assertEqual(admission.calls, 1)
        self.assertEqual(state.prepare_calls, 1)

    def test_resume_runs_once_then_repeated_resume_is_terminal_without_runner(
        self,
    ) -> None:
        state = FakeState()
        runner = FakeRunner()
        verification_gate, _, _, _ = make_gate(state=state, runner=runner)
        handle = verification_gate.start(APPROVAL_REF)

        first = verification_gate.resume(handle)
        second = verification_gate.resume(handle)

        self.assertEqual(first.phase, TaskPhase.COMPLETED)
        self.assertEqual(second.phase, TaskPhase.COMPLETED)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(state.record_receipt_calls, 1)
        self.assertEqual(state.apply_terminal_calls, 1)

    def test_restart_resume_reconstructs_from_durable_state(self) -> None:
        state = FakeState()
        first_gate, _, _, _ = make_gate(state=state)
        handle = first_gate.start(APPROVAL_REF)

        restarted_gate, _, admission, resolver = make_gate(state=state)
        terminal = restarted_gate.resume(handle)

        self.assertEqual(terminal.phase, TaskPhase.COMPLETED)
        self.assertEqual(state.begin_calls, 1)
        self.assertEqual(state.record_receipt_calls, 1)
        self.assertGreaterEqual(admission.calls, 1)
        self.assertGreaterEqual(resolver.calls, 1)

    def test_prepared_effect_response_loss_is_not_retried(self) -> None:
        state = FakeState()
        first_gate, _, _, _ = make_gate(state=state)
        handle = first_gate.start(APPROVAL_REF)

        class LostRunner(FakeRunner):
            def run(
                self,
                request: VerificationRequest,
                effect: gate.VerificationEffectLease,
            ) -> VerificationRunResult:
                self.calls += 1
                raise OSError("runner response lost")

        lost_runner = LostRunner()
        failed_gate, _, _, _ = make_gate(state=state, runner=lost_runner)
        with self.assertRaises(RecoveryRequired):
            failed_gate.resume(handle)

        retried_runner = FakeRunner()
        retried_gate, _, _, _ = make_gate(state=state, runner=retried_runner)
        with self.assertRaises(RecoveryRequired):
            retried_gate.resume(handle)
        self.assertEqual(lost_runner.calls, 1)
        self.assertEqual(retried_runner.calls, 0)

    def test_concurrent_resume_has_one_run_once_effect(self) -> None:
        state = FakeState()
        runner = FakeRunner()
        verification_gate, _, _, _ = make_gate(state=state, runner=runner)
        handle = verification_gate.start(APPROVAL_REF)
        outcomes: list[object] = []

        def resume() -> None:
            try:
                outcomes.append(verification_gate.resume(handle))
            except Exception as exc:  # noqa: BLE001 - assertion captures typed recovery
                outcomes.append(exc)

        threads = [threading.Thread(target=resume) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(runner.calls, 1)
        self.assertEqual(state.record_receipt_calls, 1)
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(
            all(isinstance(item, VerificationTerminalResult) for item in outcomes)
        )

    def test_stale_before_snapshot_stops_before_runner(self) -> None:
        approved = approved_review()
        stale = replace(snapshot(approved), inode=99)
        snapshots = SnapshotPort(snapshot(approved), stale)
        state = FakeState()
        runner = FakeRunner()
        verification_gate, _, _, _ = make_gate(
            state=state, runner=runner, snapshots=snapshots
        )
        handle = verification_gate.start(APPROVAL_REF)
        with self.assertRaises(RecoveryRequired):
            verification_gate.resume(handle)
        self.assertEqual(runner.calls, 0)
        self.assertEqual(state.record_receipt_calls, 0)

    def test_profile_drift_after_receipt_requires_recovery_without_terminal_apply(
        self,
    ) -> None:
        state = FakeState()
        runner = FakeRunner()
        first = profile()
        changed = replace(
            first,
            executable=VerificationExecutableIdentity(
                "/usr/bin/verify-check", "2.0.0", "f" * 64
            ),
        )

        class SequenceResolver(Resolver):
            def __init__(self) -> None:
                super().__init__(first)
                self.values = [first, first, first, changed]

            def resolve(self, ref: VerificationProfileRef) -> VerificationProfile:
                self.calls += 1
                return self.values.pop(0) if self.values else changed

        resolver = SequenceResolver()
        verification_gate, _, _, _ = make_gate(
            resolver=resolver, state=state, runner=runner
        )
        handle = verification_gate.start(APPROVAL_REF)
        with self.assertRaises(RecoveryRequired):
            verification_gate.resume(handle)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(state.record_receipt_calls, 1)
        self.assertEqual(state.apply_terminal_calls, 0)

    def test_post_receipt_snapshot_drift_stops_before_terminal_cas(self) -> None:
        approved = approved_review()
        drifted = replace(snapshot(approved), inode=99)
        snapshots = SnapshotPort(
            snapshot(approved), snapshot(approved), snapshot(approved), drifted
        )
        state = FakeState()
        verification_gate, _, _, _ = make_gate(state=state, snapshots=snapshots)
        handle = verification_gate.start(APPROVAL_REF)

        with self.assertRaises(RecoveryRequired):
            verification_gate.resume(handle)
        self.assertEqual(snapshots.calls, 4)
        self.assertEqual(state.apply_terminal_calls, 0)

    def test_terminal_effect_hint_rechecks_record_and_applies_receipted_state(
        self,
    ) -> None:
        class TerminalHintState(FakeState):
            def begin_effect_once(
                self,
                verification_ref: VerificationRef,
                request_digest: gate.ReceiptDigest,
            ) -> gate.VerificationEffectLease:
                with self.lock:
                    self.begin_calls += 1
                    assert self.record is not None
                    effect = gate._make_effect(
                        verification_ref,
                        request_digest,
                        EffectNonce("effect-1"),
                        1,
                        1,
                        EffectBeginStatus.TERMINAL,
                    )
                    result = FakeRunner().run(self.record.request, effect)
                    self.receipt = gate._make_receipt(
                        receipt_ref=ReceiptRef("receipt-1"),
                        request=self.record.request,
                        result=result,
                        effect=effect,
                        after_snapshot=snapshot(),
                    )
                    self.effect = effect
                    self.record = gate._make_record(
                        self.record.verification_ref,
                        self.record.approval_ref,
                        self.record.request,
                        DurableRecordStatus.RECEIPTED,
                        effect,
                        self.receipt,
                    )
                    return effect

        state = TerminalHintState()
        verification_gate, _, _, _ = make_gate(state=state)
        handle = verification_gate.start(APPROVAL_REF)

        terminal = verification_gate.resume(handle)

        self.assertEqual(terminal.phase, TaskPhase.COMPLETED)
        self.assertEqual(state.apply_terminal_calls, 1)

    def test_unsafe_env_and_noncanonical_path_are_rejected(self) -> None:
        base = profile()
        for name in (
            "ORCA_ENDPOINT",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "HOME",
        ):
            with self.subTest(name=name), self.assertRaises(VerificationGateError):
                replace(
                    base,
                    environment_allowlist=(EnvName(name),),
                    environment_values=("x",),
                )
        with self.assertRaisesRegex(VerificationGateError, "noncanonical"):
            VerificationExecutableIdentity("/usr/bin//verify-check", "1.0", "d" * 64)

    def test_profile_argv_treats_shell_metacharacters_as_one_literal_argument(
        self,
    ) -> None:
        base = profile()
        literal = "$(touch /tmp/pwned);&&"
        fixed = replace(
            base,
            argv_template=(base.executable.path, literal, "{workspace}"),
            argv_template_digest=gate._argv_digest(
                (base.executable.path, literal, "{workspace}")
            ),
        )
        resolver = Resolver(fixed)
        runner = FakeRunner()
        verification_gate, _, _, _ = make_gate(
            resolver=resolver,
            runner=runner,
        )
        handle = verification_gate.start(APPROVAL_REF)
        verification_gate.resume(handle)
        self.assertEqual(runner.requests[0].argv[1], literal)

        with self.assertRaisesRegex(VerificationGateError, "placeholder"):
            replace(
                base,
                argv_template=(base.executable.path, "{untrusted}"),
                argv_template_digest=gate._argv_digest(
                    (base.executable.path, "{untrusted}")
                ),
            )

    def test_outcome_matrix_requires_reaped_spawn_or_explicit_not_started(self) -> None:
        cases = (
            VerificationOutcome.FAILED,
            VerificationOutcome.TIMEOUT,
            VerificationOutcome.OUTPUT_LIMIT,
            VerificationOutcome.SCHEMA_INVALID,
            VerificationOutcome.RUNNER_UNAVAILABLE,
        )
        for outcome in cases:
            with self.subTest(outcome=outcome):
                runner = FakeRunner(outcome)
                verification_gate, state, _, _ = make_gate(runner=runner)
                handle = verification_gate.start(APPROVAL_REF)
                terminal = verification_gate.resume(handle)
                self.assertEqual(
                    terminal.phase,
                    TaskPhase.VERIFICATION_FAILED,
                )
                self.assertEqual(state.record_receipt_calls, 1)

        class UnreapedRunner(FakeRunner):
            def run(
                self,
                request: VerificationRequest,
                effect: gate.VerificationEffectLease,
            ) -> VerificationRunResult:
                result = super().run(request, effect)
                return replace(result, cleanup=CleanupStatus.UNKNOWN)

        state = FakeState()
        runner = UnreapedRunner(VerificationOutcome.SCHEMA_INVALID)
        verification_gate, _, _, _ = make_gate(state=state, runner=runner)
        handle = verification_gate.start(APPROVAL_REF)
        with self.assertRaises(RecoveryRequired):
            verification_gate.resume(handle)
        self.assertEqual(state.record_receipt_calls, 0)

    def test_unissued_digest_value_is_rejected(self) -> None:
        forged: VerificationReceipt = object.__new__(VerificationReceipt)
        with self.assertRaises(VerificationGateError):
            gate._compute_receipt_digest(forged)

    def test_wrong_ref_and_receipt_digest_are_rejected(self) -> None:
        verification_gate, state, _, _ = make_gate()
        handle = verification_gate.start(APPROVAL_REF)
        with self.assertRaises(RecoveryRequired):
            forged: VerificationHandle = object.__new__(VerificationHandle)
            object.__setattr__(forged, "verification_ref", VerificationRef("foreign"))
            object.__setattr__(forged, "approval_ref", APPROVAL_REF)
            object.__setattr__(forged, "request_digest", handle.request_digest)
            object.__setattr__(forged, "_issuer", gate._HANDLE_ISSUER)
            verification_gate.resume(forged)
        self.assertEqual(state.begin_calls, 0)
