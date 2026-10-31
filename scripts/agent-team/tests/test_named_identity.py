from __future__ import annotations

import unittest
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
    LaunchMode,
    MessageRef,
    MessageReply,
    NodeRef,
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
    RoleTarget,
    RoleWait,
    RunRef,
    RuntimeFailure,
    StartResult,
    StartSpec,
    StopResult,
    TaskRef,
    TerminalRef,
    WaitReceipt,
    WorkflowState,
    role_id,
    role_kind,
)
from agent_team.workflow import WorkflowEngine


def ref(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def named(node_id: str = "worker-a", kind: Role = Role.WORKER) -> NodeRef:
    return NodeRef(node_id=node_id, kind=kind)


def sample_identity(
    *,
    run: str = "1",
    task: str = "1",
    dispatch: str = "1",
    terminal: str = "worker-a",
) -> CompletionIdentity:
    return CompletionIdentity(
        run_id=RunRef(ref("run", run)),
        task_id=TaskRef(ref("task", task)),
        dispatch_id=DispatchRef(ref("dispatch", dispatch)),
        sender_terminal_id=TerminalRef(ref("terminal", terminal)),
    )


def sample_assignment(role: RoleTarget | None = None) -> Assignment:
    role = named() if role is None else role
    identity = sample_identity()
    return Assignment(
        role=role,
        launch_mode=LaunchMode.SUPERVISED_DIRECT,
        task_id=identity.task_id,
        dispatch_id=identity.dispatch_id,
        terminal_id=identity.sender_terminal_id,
        completion_identity=identity,
    )


def sample_spec() -> StartSpec:
    return StartSpec(
        team_id="agent-team-project-1",
        workspace=Path("/tmp/project"),
        config_path=Path("/tmp/config.toml"),
        state_path=Path("/tmp/state.json"),
        role_specs={
            named("main", Role.MAIN): RoleSpec(
                provider="codex",
                transport="direct",
                model="gpt-test",
                effort="medium",
                permission="read-only",
                instructions="main",
                execution="tui_direct",
            ),
            named("worker-a"): RoleSpec(
                provider="codex",
                transport="direct",
                model="gpt-test",
                effort="medium",
                permission="workspace-write",
                instructions="worker-a",
                execution="tui_direct",
            ),
            named("worker-b"): RoleSpec(
                provider="codex",
                transport="direct",
                model="gpt-test",
                effort="medium",
                permission="workspace-write",
                instructions="worker-b",
                execution="tui_direct",
            ),
        },
        graph=None,
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
        self.attach_result: AttachReceipt | None = None
        self.role_status_result: RoleStatusReceipt | None = None

    def start(self, spec: StartSpec) -> StartResult:
        return self.start_result

    def request(self, request: BackendRequest) -> BackendResult:
        self.calls.append(request)
        if isinstance(request, RolePrompt):
            return self.assignment
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
        return StopResult(
            team_id="agent-team-project-1", run_id=RunRef(ref("run", "1"))
        )


class NodeRefContractTest(unittest.TestCase):
    def test_node_ref_exposes_strict_id_and_kind_without_value_alias(self) -> None:
        target = NodeRef("worker-a", Role.WORKER)

        self.assertEqual(target.node_id, "worker-a")
        self.assertIs(target.kind, Role.WORKER)
        self.assertFalse(hasattr(target, "value"))
        self.assertEqual(role_id(target), "worker-a")
        self.assertEqual(role_kind(target), Role.WORKER)
        self.assertEqual(role_id(Role.WORKER), "worker")
        self.assertIs(role_kind(Role.WORKER), Role.WORKER)
        self.assertEqual(target, NodeRef("worker-a", Role.WORKER))
        self.assertIsNot(target, NodeRef("worker-a", Role.WORKER))

    def test_node_ref_rejects_non_slug_ids_and_non_role_kinds(self) -> None:
        for node_id in (None, 1, "", "Worker-a", "worker/a", "../worker", "worker_"):
            with (
                self.subTest(node_id=node_id),
                self.assertRaises((TypeError, ValueError)),
            ):
                NodeRef(node_id, Role.WORKER)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            NodeRef("worker-a", "worker")  # type: ignore[arg-type]

    def test_named_role_specs_are_accepted_by_start_spec(self) -> None:
        spec = sample_spec()

        self.assertEqual(
            {role_id(target) for target in spec.role_specs},
            {"main", "worker-a", "worker-b"},
        )
        self.assertIsNone(spec.graph)


class WorkflowNamedIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.engine = WorkflowEngine(self.backend)
        self.engine.start(sample_spec())

    def test_same_named_id_and_kind_accepts_distinct_node_ref_instances(self) -> None:
        requested = NodeRef("worker-a", Role.WORKER)
        self.backend.assignment = sample_assignment(NodeRef("worker-a", Role.WORKER))

        result = self.engine.request(RolePrompt(role=requested, text="implement"))

        self.assertEqual(cast(Assignment, result).role, requested)

    def test_same_kind_with_different_named_id_is_rejected(self) -> None:
        self.backend.assignment = sample_assignment(NodeRef("worker-b", Role.WORKER))

        with self.assertRaises(RuntimeFailure) as error:
            self.engine.request(
                RolePrompt(role=NodeRef("worker-a", Role.WORKER), text="implement")
            )

        self.assertEqual(error.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)

    def test_same_named_id_with_different_kind_is_rejected(self) -> None:
        self.backend.assignment = sample_assignment(NodeRef("worker-a", Role.REVIEWER))

        with self.assertRaises(RuntimeFailure) as error:
            self.engine.request(
                RolePrompt(role=NodeRef("worker-a", Role.WORKER), text="implement")
            )

        self.assertEqual(error.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)

    def test_question_ack_keeps_named_assignment_for_completion_cleanup(self) -> None:
        requested = NodeRef("worker-a", Role.WORKER)
        self.backend.assignment = sample_assignment(NodeRef("worker-a", Role.WORKER))
        self.engine.request(RolePrompt(role=requested, text="implement"))
        identity = self.backend.assignment.completion_identity
        question_delivery = DeliveryRef(ref("delivery", "question"))
        message_id = MessageRef(ref("message", "question"))
        self.backend.events = (
            NormalizedEvent.question(
                identity=identity,
                message_id=message_id,
                delivery_id=question_delivery,
                body="which option?",
            ),
        )

        self.engine.request(
            RoleWait(role=NodeRef("worker-a", Role.WORKER), timeout_ms=1_000)
        )
        self.engine.request(MessageReply(message_id=message_id, body="option A"))
        self.engine.request(DeliveryAck(delivery_id=question_delivery))
        self.assertEqual(self.engine.state, WorkflowState.WAITING)

        completion_delivery = DeliveryRef(ref("delivery", "completion"))
        self.backend.events = (
            NormalizedEvent.worker_done(
                identity=identity,
                outcome=Outcome.SUCCEEDED,
                body="done",
                delivery_id=completion_delivery,
            ),
        )
        self.engine.request(
            RoleWait(role=NodeRef("worker-a", Role.WORKER), timeout_ms=1_000)
        )
        self.engine.request(RoleRead(role=NodeRef("worker-a", Role.WORKER), lines=20))
        self.engine.request(RoleRelease(role=NodeRef("worker-a", Role.WORKER)))
        self.engine.request(DeliveryAck(delivery_id=completion_delivery))

        self.assertEqual(self.engine.state, WorkflowState.IDLE)

    def test_attach_and_role_get_compare_named_values(self) -> None:
        requested = NodeRef("worker-a", Role.WORKER)
        self.backend.attach_result = AttachReceipt(
            NodeRef("worker-a", Role.WORKER),
            TerminalRef(ref("terminal", "worker-a")),
            RunRef(ref("run", "1")),
        )

        attach = self.engine.request(Attach(requested))

        self.assertEqual(cast(AttachReceipt, attach).role, requested)

        self.backend.assignment = sample_assignment(NodeRef("worker-a", Role.WORKER))
        self.engine.request(RolePrompt(role=requested, text="implement"))
        self.backend.role_status_result = RoleStatusReceipt(
            NodeRef("worker-a", Role.WORKER), "running"
        )
        status = self.engine.request(RoleGet(NodeRef("worker-a", Role.WORKER)))

        self.assertEqual(cast(RoleStatusReceipt, status).role, requested)


if __name__ == "__main__":
    unittest.main()
