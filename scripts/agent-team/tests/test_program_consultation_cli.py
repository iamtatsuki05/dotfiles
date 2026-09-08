from __future__ import annotations

import io
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from unittest import mock

from agent_team import cli
from agent_team.contracts import (
    BackendRequest,
    BackendResult,
    CoordinatorAttachReceipt,
    MessageReply,
    Role,
    RoleSpec,
    RunRef,
    RuntimeFailure,
    StartResult,
    StartSpec,
    StopResult,
    TaskConsultationReply,
    TaskStatusReceipt,
    TerminalRef,
)
from agent_team.named_graph import Coordination, GraphSpec
from agent_team.workflow import WorkflowEngine


def _graph(mode: str) -> GraphSpec:
    from agent_team.contracts import NodeRef

    worker = NodeRef("worker", Role.WORKER)
    reviewer = NodeRef("reviewer", Role.REVIEWER)
    return GraphSpec(
        nodes=(worker, reviewer),
        edges=(),
        coordination=Coordination(
            cast(Literal["agent", "program"], mode), ("worker",), "serial", 1
        ),
        routes=(),
    )


def _start_spec(graph: GraphSpec) -> StartSpec:
    worker = graph.node("worker")
    reviewer = graph.node("reviewer")
    return StartSpec(
        team_id="consultation-team",
        workspace=Path("/tmp/consultation-workspace"),
        config_path=Path("/tmp/consultation-config.toml"),
        state_path=Path("/tmp/consultation-state.json"),
        role_specs={
            worker: RoleSpec(
                provider="claude",
                transport="acp",
                model="worker-model",
                effort="medium",
                permission="workspace-write",
                instructions="worker",
                execution="background",
            ),
            reviewer: RoleSpec(
                provider="claude",
                transport="acp",
                model="reviewer-model",
                effort="medium",
                permission="read-only",
                instructions="reviewer",
                execution="background",
            ),
        },
        graph=graph,
        max_review_rounds=1,
    )


class _WorkflowBackend:
    def __init__(self) -> None:
        self.requests: list[BackendRequest] = []
        self.response: object = TaskStatusReceipt(
            "task-a",
            "consultation_required",
            {
                "consultation_answer": {
                    "consultation_id": "consult-1",
                    "body": "回答",
                    "body_sha256": "0" * 64,
                }
            },
        )

    def start(self, spec: StartSpec) -> StartResult:
        return StartResult(
            team_id=spec.team_id,
            run_id=RunRef("run-1"),
            main_terminal_id=None,
            coordinator_terminal_id=TerminalRef("coordinator"),
            state_path=spec.state_path,
        )

    def request(self, request: BackendRequest) -> BackendResult:
        self.requests.append(request)
        return cast(BackendResult, self.response)

    def stop(self) -> StopResult:
        return StopResult("consultation-team", RunRef("run-1"))


class _ManagementEngine:
    def __init__(self, receipt: TaskStatusReceipt) -> None:
        self.receipt = receipt
        self.started = False
        self.requests: list[object] = []

    def start(self, _spec: StartSpec) -> None:
        self.started = True

    def request(self, request: object) -> TaskStatusReceipt:
        self.requests.append(request)
        return self.receipt


class _ManagementBackend:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def request(self, request: object) -> object:
        self.requests.append(request)
        from agent_team.contracts import ReplyReceipt

        return ReplyReceipt(replied=True)


class ProgramConsultationWorkflowTest(unittest.TestCase):
    def test_consultation_reply_uses_typed_receipt_and_exact_answer_binding(
        self,
    ) -> None:
        backend = _WorkflowBackend()
        engine = WorkflowEngine(backend)
        spec = _start_spec(_graph("program"))
        engine.start(spec)

        receipt = engine.request(TaskConsultationReply("consult-1", "回答"))

        self.assertIsInstance(receipt, TaskStatusReceipt)
        self.assertEqual(backend.requests, [TaskConsultationReply("consult-1", "回答")])

        for request in (
            TaskConsultationReply("other-consultation", "回答"),
            TaskConsultationReply("consult-1", "別の回答"),
        ):
            with self.subTest(request=request):
                with self.assertRaises(RuntimeFailure) as caught:
                    engine.request(request)
                self.assertEqual(caught.exception.code.value, "IdentityMismatch")

    def test_consultation_reply_rejects_non_task_receipt(self) -> None:
        backend = _WorkflowBackend()
        engine = WorkflowEngine(backend)
        engine.start(_start_spec(_graph("program")))

        for response in (
            CoordinatorAttachReceipt(TerminalRef("wrong"), RunRef("run-1")),
            TaskStatusReceipt(
                "task-a", "consultation_required", cast(Mapping[str, object], None)
            ),
        ):
            with self.subTest(response=type(response).__name__):
                backend.response = response
                with self.assertRaises(RuntimeFailure) as caught:
                    engine.request(TaskConsultationReply("consult-1", "回答"))

                self.assertEqual(caught.exception.code.value, "BackendProtocolFailure")


class ProgramConsultationCliTest(unittest.TestCase):
    def test_answer_parser_makes_message_and_consultation_ids_mutually_exclusive(
        self,
    ) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            ["answer", "--consultation-id", "consult-1", "--body", "回答"]
        )
        self.assertEqual(args.consultation_id, "consult-1")
        self.assertIsNone(args.message_id)

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "answer",
                    "--message-id",
                    "message-1",
                    "--consultation-id",
                    "consult-1",
                    "--body",
                    "回答",
                ]
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["answer", "--body", "回答"])

    def test_legacy_native_consultation_fails_before_resuming_runtime(self):
        with (
            mock.patch.object(cli, "_ensure_orca_platform"),
            mock.patch.object(
                cli,
                "_start_spec",
                return_value=replace(_start_spec(_graph("agent")), graph=None),
            ),
            mock.patch.object(cli, "_runtime_engine") as runtime,
            self.assertRaisesRegex(RuntimeFailure, "named task graph"),
        ):
            cli.manage_team(
                "answer",
                {"runtime": "tmux"},
                None,
                consultation_id="consult-1",
                body="回答",
            )
        runtime.assert_not_called()

    def test_consultation_route_accepts_named_agent_and_program_without_body_output(
        self,
    ) -> None:
        for mode in ("agent", "program"):
            with self.subTest(mode=mode):
                engine = _ManagementEngine(
                    TaskStatusReceipt(
                        "task-a",
                        "consultation_required",
                        {
                            "consultation_answer": {
                                "consultation_id": "consult-1",
                                "body": "回答",
                                "body_sha256": "0" * 64,
                            }
                        },
                    )
                )
                backend = _ManagementBackend()
                plan = {"runtime": "tmux", "graph": _graph(mode)}
                with (
                    mock.patch.object(cli, "_ensure_orca_platform"),
                    mock.patch.object(
                        cli, "_start_spec", return_value=_start_spec(_graph(mode))
                    ),
                    mock.patch.object(
                        cli, "_runtime_engine", return_value=(engine, backend)
                    ),
                ):
                    result = cli.manage_team(
                        "answer",
                        plan,
                        None,
                        consultation_id="consult-1",
                        body="回答",
                    )

                self.assertEqual(
                    result,
                    {
                        "status": "answered",
                        "consultation_id": "consult-1",
                        "task_id": "task-a",
                    },
                )
                self.assertNotIn("body", result)
                self.assertEqual(
                    engine.requests, [TaskConsultationReply("consult-1", "回答")]
                )
                self.assertEqual(backend.requests, [])

    def test_message_id_route_remains_program_only(self) -> None:
        engine = _ManagementEngine(
            TaskStatusReceipt("task-a", "consultation_required", {})
        )
        backend = _ManagementBackend()
        plan = {"runtime": "tmux", "graph": _graph("agent")}
        with (
            mock.patch.object(cli, "_ensure_orca_platform"),
            mock.patch.object(
                cli, "_start_spec", return_value=_start_spec(_graph("agent"))
            ),
            mock.patch.object(cli, "_runtime_engine", return_value=(engine, backend)),
            self.assertRaises(RuntimeFailure),
        ):
            cli.manage_team("answer", plan, None, message_id="message-1", body="回答")

        self.assertFalse(engine.started)
        self.assertEqual(backend.requests, [])

    def test_message_id_program_route_keeps_regular_backend_reply(self) -> None:
        engine = _ManagementEngine(
            TaskStatusReceipt("task-a", "consultation_required", {})
        )
        backend = _ManagementBackend()
        plan = {"runtime": "tmux", "graph": _graph("program")}
        with (
            mock.patch.object(cli, "_ensure_orca_platform"),
            mock.patch.object(
                cli, "_start_spec", return_value=_start_spec(_graph("program"))
            ),
            mock.patch.object(cli, "_runtime_engine", return_value=(engine, backend)),
        ):
            result = cli.manage_team(
                "answer", plan, None, message_id="message-1", body="回答"
            )

        self.assertEqual(result, {"status": "answered", "message_id": "message-1"})
        self.assertEqual(len(backend.requests), 1)
        self.assertIsInstance(backend.requests[0], MessageReply)
        self.assertEqual(engine.requests, [])

    def test_invalid_consultation_arguments_are_rejected_before_runtime_effects(
        self,
    ) -> None:
        engine = _ManagementEngine(
            TaskStatusReceipt("task-a", "consultation_required", {})
        )
        backend = _ManagementBackend()
        plan = {"runtime": "tmux", "graph": _graph("program")}
        cases = (
            {"message_id": "message-1", "consultation_id": "consult-1"},
            {"message_id": None, "consultation_id": None},
            {"message_id": None, "consultation_id": "consult-1", "body": "   "},
        )
        for values in cases:
            with self.subTest(values=values):
                with (
                    mock.patch.object(cli, "_ensure_orca_platform"),
                    mock.patch.object(cli, "_start_spec") as start_spec,
                    mock.patch.object(
                        cli, "_runtime_engine", return_value=(engine, backend)
                    ) as runtime,
                    self.assertRaises(RuntimeFailure),
                ):
                    cli.manage_team(
                        "answer",
                        plan,
                        None,
                        message_id=values.get("message_id"),
                        consultation_id=values.get("consultation_id"),
                        body=values.get("body", "回答"),
                    )
                start_spec.assert_not_called()
                runtime.assert_not_called()
                self.assertFalse(engine.started)
                self.assertEqual(backend.requests, [])

    def test_consultation_route_rejects_named_orca_before_backend(self) -> None:
        engine = _ManagementEngine(
            TaskStatusReceipt("task-a", "consultation_required", {})
        )
        backend = _ManagementBackend()
        with (
            mock.patch.object(cli, "_ensure_orca_platform"),
            mock.patch.object(cli, "_start_spec") as start_spec,
            mock.patch.object(
                cli, "_runtime_engine", return_value=(engine, backend)
            ) as runtime,
            self.assertRaises(RuntimeFailure),
        ):
            cli.manage_team(
                "answer",
                {"runtime": "orca", "graph": _graph("agent")},
                None,
                consultation_id="consult-1",
                body="回答",
            )

        start_spec.assert_not_called()
        runtime.assert_not_called()
        self.assertFalse(engine.started)


if __name__ == "__main__":
    unittest.main()
