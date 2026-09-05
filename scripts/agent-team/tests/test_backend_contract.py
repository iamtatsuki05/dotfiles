from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

from agent_team.contracts import (
    AckReceipt,
    Assignment,
    Attach,
    AttachReceipt,
    BackendPort,
    BackendRequest,
    BackendResult,
    CompletionIdentity,
    DeliveryAck,
    DeliveryRef,
    DispatchRef,
    ErrorCode,
    EventKind,
    LaunchMode,
    MessageRef,
    MessageReply,
    NormalizedEvent,
    Outcome,
    ReadReceipt,
    ReleaseReceipt,
    ReplyReceipt,
    Role,
    RoleGet,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleSpec,
    RoleStatusReceipt,
    RoleWait,
    RunRef,
    RuntimeFailure,
    RuntimeRequest,
    StartResult,
    StartSpec,
    Status,
    StatusReceipt,
    StopResult,
    TaskRef,
    TeamRuntime,
    TerminalRef,
    WaitReceipt,
    WorkflowState,
)
from agent_team.workflow import WorkflowEngine


def ref(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def sample_assignment(
    role: Role = Role.WORKER,
    *,
    run: str = "1",
    task: str = "1",
    dispatch: str = "1",
    terminal: str = "worker",
) -> Assignment:
    identity = CompletionIdentity(
        run_id=RunRef(ref("run", run)),
        task_id=TaskRef(ref("task", task)),
        dispatch_id=DispatchRef(ref("dispatch", dispatch)),
        sender_terminal_id=TerminalRef(ref("terminal", terminal)),
    )
    return Assignment(
        role=role,
        launch_mode=LaunchMode.SUPERVISED_DIRECT,
        task_id=TaskRef(ref("task", task)),
        dispatch_id=DispatchRef(ref("dispatch", dispatch)),
        terminal_id=TerminalRef(ref("terminal", terminal)),
        completion_identity=identity,
    )


def sample_spec() -> StartSpec:
    return StartSpec(
        team_id="agent-team-project-1",
        workspace=Path("/tmp/project"),
        config_path=Path("/tmp/config.toml"),
        state_path=Path("/tmp/state.json"),
        role_specs={
            role: RoleSpec(
                provider="codex",
                transport="direct",
                model="gpt-test",
                effort="medium",
                permission="read-only",
                instructions=role.value,
                execution="tui_direct",
            )
            for role in (Role.MAIN, Role.PLANNER, Role.WORKER, Role.REVIEWER)
        },
    )


class FakeBackend(BackendPort):
    def __init__(self) -> None:
        self.calls: list[BackendRequest] = []
        self.events: tuple[NormalizedEvent, ...] = ()
        self.assignment = sample_assignment()
        self.start_result = StartResult(
            team_id="agent-team-project-1",
            run_id=RunRef(ref("run", "1")),
            main_terminal_id=TerminalRef(ref("terminal", "main")),
            state_path=Path("/tmp/state.json"),
        )
        self.stop_result = StopResult(
            team_id="agent-team-project-1", run_id=RunRef(ref("run", "1"))
        )
        self.status_result = StatusReceipt(
            "running", "agent-team-project-1", RunRef(ref("run", "1"))
        )
        self.attach_result: AttachReceipt | None = None
        self.role_status_result: RoleStatusReceipt | None = None
        self.started = False
        self.start_count = 0

    def start(self, spec: StartSpec) -> StartResult:
        self.started = True
        self.start_count += 1
        return self.start_result

    def request(self, request: BackendRequest) -> BackendResult:
        self.calls.append(request)
        if isinstance(request, RolePrompt):
            if self.assignment.role is request.role:
                return self.assignment
            return sample_assignment(request.role)
        if isinstance(request, RoleWait):
            return WaitReceipt(
                delivery_id=self.events[0].delivery_id if self.events else None,
                events=self.events,
            )
        if isinstance(request, RoleRead):
            return ReadReceipt(output="verified output")
        if isinstance(request, RoleRelease):
            return ReleaseReceipt(state="released")
        if isinstance(request, DeliveryAck):
            return AckReceipt(acknowledged=True)
        if isinstance(request, MessageReply):
            return ReplyReceipt(replied=True)
        if isinstance(request, Status):
            return self.status_result
        if isinstance(request, Attach):
            return self.attach_result or AttachReceipt(
                request.role,
                TerminalRef(ref("terminal", "main")),
                RunRef(ref("run", "1")),
            )
        if isinstance(request, RoleGet):
            return self.role_status_result or RoleStatusReceipt(request.role, "running")
        raise AssertionError(f"unexpected request: {request!r}")

    def stop(self) -> StopResult:
        return self.stop_result


class ContractShapeTest(unittest.TestCase):
    def test_team_runtime_exposes_only_three_caller_entries(self) -> None:
        public = {
            name
            for name, value in TeamRuntime.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        self.assertEqual(public, {"start", "request", "stop"})

    def test_runtime_requests_are_frozen_and_typed(self) -> None:
        request: RuntimeRequest = RolePrompt(role=Role.WORKER, text="run tests")
        self.assertIsInstance(request, RolePrompt)
        with self.assertRaises(AttributeError):
            cast(RolePrompt, request).__setattr__("text", "mutated")

    def test_opaque_refs_hide_backend_tokens_from_repr_and_public_fields(self) -> None:
        reference = RunRef("orca-run-secret")

        self.assertNotIn("orca-run-secret", repr(reference))
        self.assertFalse(hasattr(reference, "token"))
        with self.assertRaises(FrozenInstanceError):
            reference._value = "changed"  # type: ignore[misc]

    def test_malformed_event_values_fail_during_construction(self) -> None:
        with self.assertRaises(TypeError):
            NormalizedEvent(
                cast(EventKind, "worker_done"),
                DeliveryRef(ref("delivery", "1")),
                identity=sample_assignment().completion_identity,
                outcome=Outcome.SUCCEEDED,
            )


class WorkflowEngineContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.engine = WorkflowEngine(self.backend)
        self.engine.start(sample_spec())

    def test_successful_worker_lifecycle_enforces_read_release_ack(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        self.backend.events = (
            NormalizedEvent.worker_done(
                identity=sample_assignment().completion_identity,
                outcome=Outcome.SUCCEEDED,
                body="done",
                delivery_id=DeliveryRef(ref("delivery", "1")),
            ),
        )
        self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))
        self.engine.request(RoleRead(role=Role.WORKER, lines=20))
        self.engine.request(RoleRelease(role=Role.WORKER))
        self.engine.request(DeliveryAck(delivery_id=DeliveryRef(ref("delivery", "1"))))

        self.assertEqual(
            [type(call) for call in self.backend.calls],
            [RolePrompt, RoleWait, RoleRead, RoleRelease, DeliveryAck],
        )
        self.assertEqual(self.engine.state, WorkflowState.IDLE)

    def test_start_twice_is_rejected_without_a_second_backend_effect(self) -> None:
        with self.assertRaises(RuntimeFailure) as error:
            self.engine.start(sample_spec())

        self.assertEqual(error.exception.code, ErrorCode.TEAM_ALREADY_RUNNING)
        self.assertEqual(self.backend.start_count, 1)

    def test_start_receipt_must_match_requested_team_and_state(self) -> None:
        backend = FakeBackend()
        backend.start_result = StartResult(
            team_id="foreign-team",
            run_id=RunRef(ref("run", "1")),
            main_terminal_id=TerminalRef(ref("terminal", "main")),
            state_path=Path("/tmp/foreign-state.json"),
        )
        engine = WorkflowEngine(backend)

        with self.assertRaises(RuntimeFailure) as error:
            engine.start(sample_spec())

        self.assertEqual(error.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_read_and_release_are_rejected_before_matching_worker_done(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        with self.assertRaises(RuntimeFailure) as read_error:
            self.engine.request(RoleRead(role=Role.WORKER, lines=20))
        self.assertEqual(read_error.exception.code, ErrorCode.COMPLETION_NOT_OBSERVED)
        with self.assertRaises(RuntimeFailure) as release_error:
            self.engine.request(RoleRelease(role=Role.WORKER))
        self.assertEqual(
            release_error.exception.code, ErrorCode.COMPLETION_NOT_OBSERVED
        )

    def test_timeout_does_not_advance_completion_or_allow_read(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))
        with self.assertRaises(RuntimeFailure) as error:
            self.engine.request(RoleRead(role=Role.WORKER, lines=20))
        self.assertEqual(error.exception.code, ErrorCode.COMPLETION_NOT_OBSERVED)
        retry = cast(
            WaitReceipt,
            self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000)),
        )
        self.assertIsNone(retry.delivery_id)

    def test_invalid_delivery_does_not_leave_pending_state(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        wrong = sample_assignment()
        wrong_identity = CompletionIdentity(
            run_id=wrong.completion_identity.run_id,
            task_id=wrong.completion_identity.task_id,
            dispatch_id=DispatchRef(ref("dispatch", "wrong")),
            sender_terminal_id=wrong.completion_identity.sender_terminal_id,
        )
        self.backend.events = (
            NormalizedEvent.worker_done(
                identity=wrong_identity,
                outcome=Outcome.SUCCEEDED,
                body="foreign",
                delivery_id=DeliveryRef(ref("delivery", "1")),
            ),
        )

        with self.assertRaises(RuntimeFailure) as error:
            self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))
        self.assertEqual(error.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(self.engine.state, WorkflowState.ACTIVE)

        self.backend.events = ()
        receipt = cast(
            WaitReceipt,
            self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000)),
        )
        self.assertEqual(receipt.events, ())

    def test_assignment_must_match_started_run_and_nested_identity(self) -> None:
        self.backend.assignment = sample_assignment(run="foreign")

        with self.assertRaises(RuntimeFailure) as error:
            self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        self.assertEqual(error.exception.code, ErrorCode.IDENTITY_MISMATCH)

        current = sample_assignment()
        self.backend.assignment = Assignment(
            role=Role.WORKER,
            launch_mode=LaunchMode.SUPERVISED_DIRECT,
            task_id=TaskRef(ref("task", "top-level")),
            dispatch_id=current.dispatch_id,
            terminal_id=current.terminal_id,
            completion_identity=current.completion_identity,
        )
        with self.assertRaises(RuntimeFailure) as nested_error:
            self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        self.assertEqual(nested_error.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_mixed_question_and_completion_batch_is_rejected_atomically(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        delivery = DeliveryRef(ref("delivery", "1"))
        self.backend.events = (
            NormalizedEvent.question(
                identity=sample_assignment().completion_identity,
                message_id=MessageRef(ref("message", "mixed")),
                delivery_id=delivery,
                body="question",
            ),
            NormalizedEvent.worker_done(
                identity=sample_assignment().completion_identity,
                outcome=Outcome.SUCCEEDED,
                body="done",
                delivery_id=delivery,
            ),
        )

        with self.assertRaises(RuntimeFailure) as error:
            self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))
        self.assertEqual(error.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(self.engine.state, WorkflowState.ACTIVE)

    def test_question_identity_and_message_ids_must_match_assignment(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        delivery = DeliveryRef(ref("delivery", "1"))
        foreign = NormalizedEvent.question(
            identity=sample_assignment(run="foreign").completion_identity,
            message_id=MessageRef(ref("message", "1")),
            delivery_id=delivery,
            body="foreign",
        )
        self.backend.events = (foreign,)
        with self.assertRaises(RuntimeFailure) as identity_error:
            self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))
        self.assertEqual(identity_error.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(self.engine.state, WorkflowState.ACTIVE)

        message = MessageRef(ref("message", "duplicate"))
        identity = sample_assignment().completion_identity
        self.backend.events = (
            NormalizedEvent.question(
                identity=identity,
                message_id=message,
                delivery_id=delivery,
                body="first",
            ),
            NormalizedEvent.question(
                identity=identity,
                message_id=message,
                delivery_id=delivery,
                body="duplicate",
            ),
        )
        with self.assertRaises(RuntimeFailure) as duplicate_error:
            self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))
        self.assertEqual(duplicate_error.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(self.engine.state, WorkflowState.ACTIVE)

    def test_question_requires_reply_before_delivery_ack(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        message = NormalizedEvent.question(
            identity=sample_assignment().completion_identity,
            message_id=MessageRef(ref("message", "1")),
            delivery_id=DeliveryRef(ref("delivery", "1")),
            body="which option?",
        )
        self.backend.events = (message,)
        self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))
        with self.assertRaises(RuntimeFailure) as error:
            self.engine.request(
                DeliveryAck(delivery_id=DeliveryRef(ref("delivery", "1")))
            )
        self.assertEqual(error.exception.code, ErrorCode.ORDER_VIOLATION)
        self.engine.request(
            MessageReply(message_id=MessageRef(ref("message", "1")), body="option A")
        )
        with self.assertRaises(RuntimeFailure) as duplicate:
            self.engine.request(
                MessageReply(
                    message_id=MessageRef(ref("message", "1")), body="option B"
                )
            )
        self.assertEqual(duplicate.exception.code, ErrorCode.ORDER_VIOLATION)
        self.engine.request(DeliveryAck(delivery_id=DeliveryRef(ref("delivery", "1"))))
        self.assertEqual(self.engine.state, WorkflowState.WAITING)
        self.backend.events = ()
        self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000))

    def test_stop_receipt_must_match_started_identity(self) -> None:
        self.backend.stop_result = StopResult(
            team_id="foreign-team", run_id=RunRef(ref("run", "foreign"))
        )
        with self.assertRaises(RuntimeFailure) as error:
            self.engine.stop()
        self.assertEqual(error.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_status_attach_and_role_get_receipts_are_correlated(self) -> None:
        self.backend.status_result = StatusReceipt(
            "running", "foreign-team", RunRef(ref("run", "foreign"))
        )
        with self.assertRaises(RuntimeFailure) as status_error:
            self.engine.request(Status())
        self.assertEqual(status_error.exception.code, ErrorCode.IDENTITY_MISMATCH)

        self.backend.attach_result = AttachReceipt(
            Role.REVIEWER,
            TerminalRef(ref("terminal", "reviewer")),
            RunRef(ref("run", "foreign")),
        )
        with self.assertRaises(RuntimeFailure) as attach_error:
            self.engine.request(Attach(Role.MAIN))
        self.assertEqual(attach_error.exception.code, ErrorCode.IDENTITY_MISMATCH)

        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        self.backend.role_status_result = RoleStatusReceipt(Role.REVIEWER, "running")
        with self.assertRaises(RuntimeFailure) as role_error:
            self.engine.request(RoleGet(Role.WORKER))
        self.assertEqual(role_error.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)

    def test_failed_worker_done_is_terminal_but_not_success(self) -> None:
        self.engine.request(RolePrompt(role=Role.WORKER, text="implement"))
        self.backend.events = (
            NormalizedEvent.worker_done(
                identity=sample_assignment().completion_identity,
                outcome=Outcome.FAILED,
                body="failed",
                delivery_id=DeliveryRef(ref("delivery", "1")),
            ),
        )
        wait = cast(
            WaitReceipt,
            self.engine.request(RoleWait(role=Role.WORKER, timeout_ms=1_000)),
        )
        self.assertEqual(wait.events[0].outcome, Outcome.FAILED)
        self.assertFalse(self.engine.completed_successfully)


if __name__ == "__main__":
    unittest.main()
