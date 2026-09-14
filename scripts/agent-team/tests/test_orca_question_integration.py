from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import cast
from unittest import mock

import test_orca_tasks as fixture_module

from agent_team import cli, mcp_server, orca_questions, runtime_mcp
from agent_team.adapters import ExecutionError, ProcessResult
from agent_team.backend import OrcaBackend, OrcaClient
from agent_team.contracts import EventKind, RuntimeFailure
from agent_team.native_question_channel import QuestionField, QuestionRequest
from agent_team.runtime import read_state, write_state
from agent_team.workflow import WorkflowEngine


class OrcaQuestionIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.OrcaTasksTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path

    @staticmethod
    def _read_frame(
        connection: socket.socket, *, timeout_seconds: float = 3.0
    ) -> dict[str, object]:
        connection.settimeout(timeout_seconds)
        raw = bytearray()
        while True:
            chunk = connection.recv(16 * 1024)
            if not chunk:
                raise AssertionError("question peer closed before a complete frame")
            raw.extend(chunk)
            if raw.endswith(b"\n"):
                break
        decoded = json.loads(bytes(raw[:-1]).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise TypeError("question frame was not an object")
        return cast(dict[str, object], decoded)

    def _wait_until(
        self, predicate: Callable[[], bool], *, timeout_seconds: float = 3.0
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for the named Orca question state")

    def _runtime_session(self) -> runtime_mcp.RuntimeMcpSession:
        resume_backend = OrcaBackend(
            cast(OrcaClient, self.fixture.fixture.client),
            main_command_factory=lambda _socket_path: "selected-main",
            resume_existing=True,
        )
        engine = WorkflowEngine(resume_backend)
        with mock.patch.object(
            cli, "_runtime_engine", return_value=(engine, resume_backend)
        ):
            return runtime_mcp.RuntimeMcpSession(self.path, read_state(self.path))

    def test_question_ipc_roundtrip_waits_for_main_ack_and_preserves_ids(self) -> None:
        provider = tempfile.TemporaryDirectory(
            prefix="agent-team-provider-", dir="/tmp"
        )
        self.addCleanup(provider.cleanup)
        provider_root = Path(provider.name).resolve()
        state = read_state(self.path)
        roles = state["roles"]
        self.assertIsInstance(roles, dict)
        assignment = roles["worker-a"]
        self.assertIsInstance(assignment, dict)
        assignment["provider_private_root"] = str(provider_root)
        write_state(self.path, state, require_existing=True)

        identity = {
            field: cast(str, assignment[field])
            for field in (
                "role",
                "role_kind",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
            )
        }
        identity["run_id"] = cast(str, state["run_id"])
        socket_path = provider_root / "q.sock"
        request = QuestionRequest(
            "acp-session-question-integration",
            "tool-call-four-fields",
            tuple(
                QuestionField(f"question_{index}_custom", f"Question body {index}")
                for index in range(4)
            ),
        )
        question_body = orca_questions.question_json(request)
        answers = {
            field: f"answer-{index}-" + ("あ" * (20_000 - len(f"answer-{index}-")))
            for index, field in enumerate(
                question.field for question in request.questions
            )
        }
        answer_body = json.dumps(
            dict(reversed(tuple(answers.items()))), ensure_ascii=False, indent=2
        )
        question_message_id = "question-integration-1"
        answer_message_id = "answer-integration-1"
        delivery_id = "delivery-integration-1"

        runner_calls: list[tuple[str, ...]] = []
        runner_lock = threading.Lock()
        first_pending = threading.Event()
        second_pending_started = threading.Event()
        allow_delayed_pending = threading.Event()
        worker_context_entered = threading.Event()
        worker_context_done = threading.Event()
        worker_errors: list[BaseException] = []

        pending_result = {
            "answer": None,
            "messageId": question_message_id,
            "threadId": question_message_id,
            "timedOut": True,
            "cancelled": False,
            "connectionLost": False,
            "timeoutMs": 500,
        }
        success_result = {
            "answer": answer_body,
            "messageId": question_message_id,
            "answerMessageId": answer_message_id,
            "threadId": question_message_id,
            "timedOut": False,
            "cancelled": False,
            "connectionLost": False,
            "timeoutMs": 500,
        }

        def ask_process(
            _runner: object, argv: list[str], **_kwargs: object
        ) -> ProcessResult:
            call = tuple(argv)
            with runner_lock:
                runner_calls.append(call)
                call_number = len(runner_calls)
            if call_number == 1:
                first_pending.set()
                return ProcessResult(1, json.dumps(pending_result), "")
            if call_number == 2:
                second_pending_started.set()
                if not allow_delayed_pending.wait(timeout=5):
                    raise AssertionError("delayed pending ask was not released")
                return ProcessResult(1, json.dumps(pending_result), "")
            if call_number == 3:
                return ProcessResult(0, json.dumps(success_result), "")
            raise AssertionError(f"unexpected extra Orca ask invocation: {argv!r}")

        main_calls: list[tuple[str, ...]] = []

        def fake_run_orca(
            _state: dict[str, object], args: list[str], **_kwargs: object
        ) -> dict[str, object]:
            call = tuple(args)
            main_calls.append(call)
            operation = args[1] if len(args) > 1 else ""
            if operation == "check" and "--wait" in args:
                return {
                    "deliveryId": delivery_id,
                    "messages": [
                        {
                            "id": question_message_id,
                            "type": "question",
                            "from_handle": "dispatch:dispatch_1",
                            "run_id": "run_1",
                            "body": question_body,
                            "payload": {
                                "taskId": "task_worker",
                                "dispatchId": "dispatch_1",
                            },
                        }
                    ],
                }
            if operation == "reply":
                self.assertEqual(args[args.index("--id") + 1], question_message_id)
                self.assertEqual(args[args.index("--body") + 1], answer_body)
                return {
                    "duplicate": False,
                    "message": {
                        "id": answer_message_id,
                        "thread_id": question_message_id,
                        "run_id": "run_1",
                        "body": answer_body,
                    },
                    "question": {
                        "message_id": question_message_id,
                        "run_id": "run_1",
                        "dispatch_id": "dispatch_1",
                        "status": "answered",
                        "answer_message_id": answer_message_id,
                        "answer_body": answer_body,
                    },
                }
            if operation == "check" and "--ack" in args:
                self.assertEqual(args[args.index("--ack") + 1], delivery_id)
                return {"acknowledged": delivery_id}
            raise AssertionError(f"unexpected Main Orca operation: {args!r}")

        def worker() -> None:
            try:
                with orca_questions.question_context(
                    self.path,
                    socket_path,
                    identity,
                    timeout_ms=500,
                ):
                    worker_context_entered.set()
                    worker_context_done.wait(timeout=5)
            except (AssertionError, ExecutionError) as exc:
                worker_errors.append(exc)

        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(
            mock.patch.object(
                orca_questions, "orca_executable", return_value="orca-test"
            )
        )
        patches.enter_context(
            mock.patch.object(orca_questions._AskProcessRunner, "run", new=ask_process)
        )
        patches.enter_context(
            mock.patch.object(mcp_server, "run_orca", side_effect=fake_run_orca)
        )
        session = self._runtime_session()
        worker_thread = threading.Thread(
            target=worker, name="orca-question-integration-worker"
        )
        worker_thread.start()
        client: socket.socket | None = None
        try:
            self.assertTrue(worker_context_entered.wait(timeout=3))
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(socket_path))
            client.sendall(
                json.dumps(
                    request.as_dict(), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
            )
            self.assertTrue(first_pending.wait(timeout=3))

            def message_identity_saved() -> bool:
                current = read_state(self.path)
                current_roles = current.get("roles")
                current_assignment = (
                    current_roles.get("worker-a")
                    if isinstance(current_roles, dict)
                    else None
                )
                question = (
                    current_assignment.get("orca_question")
                    if isinstance(current_assignment, dict)
                    else None
                )
                return (
                    isinstance(question, dict)
                    and question.get("phase") == "asking"
                    and question.get("message_id") == question_message_id
                    and question.get("thread_id") == question_message_id
                )

            self._wait_until(message_identity_saved)
            wait_result = session.execute(
                "role_wait", {"role": "worker-a", "timeout_ms": 1_000}
            )
            self.assertEqual(wait_result["delivery_id"], delivery_id)
            events = wait_result["events"]
            self.assertIsInstance(events, list)
            self.assertEqual(events[0]["kind"], EventKind.QUESTION.value)
            self.assertEqual(events[0]["message_id"], question_message_id)
            observed = read_state(self.path)
            observed_assignment = observed["roles"]["worker-a"]
            self.assertIsInstance(observed_assignment, dict)
            observed_question = observed_assignment["orca_question"]
            self.assertIsInstance(observed_question, dict)
            self.assertEqual(observed_question["phase"], "observed")
            self.assertEqual(observed_question["message_id"], question_message_id)
            self.assertEqual(observed_question["thread_id"], question_message_id)
            self.assertEqual(observed["pending_delivery_id"], delivery_id)
            self.assertEqual(observed["pending_question_ids"], [question_message_id])
            self.assertTrue(second_pending_started.wait(timeout=3))

            reply_result = session.execute(
                "message_reply",
                {"message_id": question_message_id, "body": answer_body},
            )
            self.assertEqual(reply_result, {"replied": True})
            replied = read_state(self.path)
            replied_assignment = replied["roles"]["worker-a"]
            self.assertIsInstance(replied_assignment, dict)
            replied_question = replied_assignment["orca_question"]
            self.assertIsInstance(replied_question, dict)
            self.assertEqual(replied_question["phase"], "replied")
            self.assertEqual(replied_question["answer_message_id"], answer_message_id)
            self.assertEqual(replied_question["answers"], answers)

            client.settimeout(0.25)
            try:
                early_frame = client.recv(1)
            except TimeoutError:
                pass
            else:
                self.fail(f"answer frame arrived before Main ACK: {early_frame!r}")

            ack_result = session.execute("delivery_ack", {"delivery_id": delivery_id})
            self.assertEqual(ack_result, {"acknowledged": True})
            acked = read_state(self.path)
            acked_assignment = acked["roles"]["worker-a"]
            self.assertIsInstance(acked_assignment, dict)
            acked_question = acked_assignment["orca_question"]
            self.assertIsInstance(acked_question, dict)
            self.assertEqual(acked_question["phase"], "acked")
            self.assertEqual(acked_question["answer_message_id"], answer_message_id)
            self.assertEqual(acked_assignment["acp_session_id"], request.session_id)
            allow_delayed_pending.set()

            answer_frame = self._read_frame(client)
            self.assertEqual(answer_frame["kind"], "answer")
            self.assertEqual(answer_frame["answers"], answers)
            client.sendall(
                json.dumps(
                    {
                        "kind": "received",
                        "session_id": request.session_id,
                        "tool_call_id": request.tool_call_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            recorded_frame = self._read_frame(client)
            self.assertEqual(recorded_frame["kind"], "recorded")
            self.assertEqual(recorded_frame["session_id"], request.session_id)
            self.assertEqual(recorded_frame["tool_call_id"], request.tool_call_id)
            worker_context_done.set()
        finally:
            allow_delayed_pending.set()
            worker_context_done.set()
            if client is not None:
                client.close()
            worker_thread.join(timeout=5)
            if socket_path.exists():
                socket_path.unlink()

        self.assertFalse(worker_thread.is_alive())
        self.assertEqual(worker_errors, [])
        final_state = read_state(self.path)
        final_assignment = final_state["roles"]["worker-a"]
        self.assertIsInstance(final_assignment, dict)
        final_question = final_assignment["orca_question"]
        self.assertIsInstance(final_question, dict)
        self.assertEqual(final_question["phase"], "recorded")
        self.assertEqual(final_question["message_id"], question_message_id)
        self.assertEqual(final_question["thread_id"], question_message_id)
        self.assertEqual(final_question["answer_message_id"], answer_message_id)
        self.assertEqual(final_question["answers"], answers)
        self.assertEqual(final_assignment["acp_session_id"], request.session_id)
        self.assertEqual(final_question["session_id"], request.session_id)
        self.assertFalse(socket_path.exists())
        client_calls = self.fixture.fixture.client.calls
        self.assertIn("worktree-show", [call[0] for call in client_calls])
        self.assertIn("run-show", [call[0] for call in client_calls])
        self.assertIn("worker-show", [call[0] for call in client_calls])
        self.assertIn("terminal-show", [call[0] for call in client_calls])
        self.assertEqual([call[1] for call in main_calls], ["check", "reply", "check"])
        with runner_lock:
            observed_runner_calls = list(runner_calls)
        self.assertEqual(len(observed_runner_calls), 3)
        self.assertEqual(
            observed_runner_calls[0][0:4],
            ("orca-test", "orchestration", "ask", "--question"),
        )
        self.assertEqual(
            observed_runner_calls[1][0:4],
            ("orca-test", "orchestration", "ask", "--resume"),
        )
        self.assertEqual(
            observed_runner_calls[2][0:4],
            ("orca-test", "orchestration", "ask", "--resume"),
        )
        self.assertEqual(observed_runner_calls[1][4], question_message_id)
        self.assertEqual(observed_runner_calls[2][4], question_message_id)
        for call in observed_runner_calls:
            self.assertEqual(
                call[-7:],
                (
                    "--run",
                    "run_1",
                    "--from",
                    "term_planner",
                    "--timeout-ms",
                    "500",
                    "--json",
                ),
            )
        question_argument = observed_runner_calls[0][4]
        self.assertEqual(json.loads(question_argument), request.as_dict())

    def test_first_pending_ask_after_main_ack_preserves_answer_identity(self) -> None:
        provider = tempfile.TemporaryDirectory(
            prefix="agent-team-provider-", dir="/tmp"
        )
        self.addCleanup(provider.cleanup)
        provider_root = Path(provider.name).resolve()
        state = read_state(self.path)
        roles = state["roles"]
        self.assertIsInstance(roles, dict)
        assignment = roles["worker-a"]
        self.assertIsInstance(assignment, dict)
        assignment["provider_private_root"] = str(provider_root)
        write_state(self.path, state, require_existing=True)

        identity = {
            field: cast(str, assignment[field])
            for field in (
                "role",
                "role_kind",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
            )
        }
        identity["run_id"] = cast(str, state["run_id"])
        socket_path = provider_root / "q.sock"
        request = QuestionRequest(
            "acp-session-first-pending",
            "tool-call-first-pending",
            (QuestionField("question_0_custom", "Reply after Main ACK."),),
        )
        question_body = orca_questions.question_json(request)
        answer_body = '{\n  "question_0_custom": "after Main ACK"\n}'
        question_message_id = "question-first-pending"
        answer_message_id = "answer-first-pending"
        delivery_id = "delivery-first-pending"

        runner_calls: list[tuple[str, ...]] = []
        first_ask_started = threading.Event()
        release_first_pending = threading.Event()
        second_ask_started = threading.Event()
        worker_context_entered = threading.Event()
        worker_context_done = threading.Event()
        worker_errors: list[BaseException] = []
        pending_result = {
            "answer": None,
            "messageId": question_message_id,
            "threadId": question_message_id,
            "timedOut": True,
            "cancelled": False,
            "connectionLost": False,
            "timeoutMs": 500,
        }
        success_result = {
            "answer": answer_body,
            "messageId": question_message_id,
            "answerMessageId": answer_message_id,
            "threadId": question_message_id,
            "timedOut": False,
            "cancelled": False,
            "connectionLost": False,
            "timeoutMs": 500,
        }

        def ask_process(
            _runner: object, argv: list[str], **_kwargs: object
        ) -> ProcessResult:
            runner_calls.append(tuple(argv))
            if len(runner_calls) == 1:
                first_ask_started.set()
                if not release_first_pending.wait(timeout=5):
                    raise AssertionError("first pending ask was not released")
                return ProcessResult(1, json.dumps(pending_result), "")
            if len(runner_calls) == 2:
                second_ask_started.set()
                return ProcessResult(0, json.dumps(success_result), "")
            raise AssertionError(f"unexpected extra Orca ask invocation: {argv!r}")

        main_calls: list[tuple[str, ...]] = []

        def fake_run_orca(
            _state: dict[str, object], args: list[str], **_kwargs: object
        ) -> dict[str, object]:
            main_calls.append(tuple(args))
            operation = args[1] if len(args) > 1 else ""
            if operation == "check" and "--wait" in args:
                return {
                    "deliveryId": delivery_id,
                    "messages": [
                        {
                            "id": question_message_id,
                            "type": "question",
                            "from_handle": "dispatch:dispatch_1",
                            "run_id": "run_1",
                            "body": question_body,
                            "payload": {
                                "taskId": "task_worker",
                                "dispatchId": "dispatch_1",
                            },
                        }
                    ],
                }
            if operation == "reply":
                self.assertEqual(args[args.index("--id") + 1], question_message_id)
                self.assertEqual(args[args.index("--body") + 1], answer_body)
                return {
                    "duplicate": False,
                    "message": {
                        "id": answer_message_id,
                        "thread_id": question_message_id,
                        "run_id": "run_1",
                        "body": answer_body,
                    },
                    "question": {
                        "message_id": question_message_id,
                        "run_id": "run_1",
                        "dispatch_id": "dispatch_1",
                        "status": "answered",
                        "answer_message_id": answer_message_id,
                        "answer_body": answer_body,
                    },
                }
            if operation == "check" and "--ack" in args:
                self.assertEqual(args[args.index("--ack") + 1], delivery_id)
                return {"acknowledged": delivery_id}
            raise AssertionError(f"unexpected Main Orca operation: {args!r}")

        def worker() -> None:
            try:
                with orca_questions.question_context(
                    self.path,
                    socket_path,
                    identity,
                    timeout_ms=500,
                ):
                    worker_context_entered.set()
                    worker_context_done.wait(timeout=5)
            except (AssertionError, ExecutionError) as exc:
                worker_errors.append(exc)

        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(
            mock.patch.object(
                orca_questions, "orca_executable", return_value="orca-test"
            )
        )
        patches.enter_context(
            mock.patch.object(orca_questions._AskProcessRunner, "run", new=ask_process)
        )
        patches.enter_context(
            mock.patch.object(mcp_server, "run_orca", side_effect=fake_run_orca)
        )
        session = self._runtime_session()
        worker_thread = threading.Thread(
            target=worker, name="orca-question-first-pending-worker"
        )
        worker_thread.start()
        client: socket.socket | None = None
        try:
            self.assertTrue(worker_context_entered.wait(timeout=3))
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(socket_path))
            client.sendall(
                json.dumps(
                    request.as_dict(), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
            )
            self.assertTrue(first_ask_started.wait(timeout=3))

            def placeholder_saved() -> bool:
                current = read_state(self.path)
                current_roles = current.get("roles")
                current_assignment = (
                    current_roles.get("worker-a")
                    if isinstance(current_roles, dict)
                    else None
                )
                question = (
                    current_assignment.get("orca_question")
                    if isinstance(current_assignment, dict)
                    else None
                )
                return (
                    isinstance(question, dict)
                    and question.get("phase") == "asking"
                    and question.get("message_id") is None
                )

            self._wait_until(placeholder_saved)
            wait_result = session.execute(
                "role_wait", {"role": "worker-a", "timeout_ms": 1_000}
            )
            self.assertEqual(wait_result["delivery_id"], delivery_id)
            reply_result = session.execute(
                "message_reply",
                {"message_id": question_message_id, "body": answer_body},
            )
            self.assertEqual(reply_result, {"replied": True})
            ack_result = session.execute("delivery_ack", {"delivery_id": delivery_id})
            self.assertEqual(ack_result, {"acknowledged": True})
            acked = read_state(self.path)
            acked_assignment = acked["roles"]["worker-a"]
            self.assertIsInstance(acked_assignment, dict)
            acked_question = acked_assignment["orca_question"]
            self.assertIsInstance(acked_question, dict)
            self.assertEqual(acked_question["phase"], "acked")
            self.assertEqual(acked_question["message_id"], question_message_id)
            self.assertEqual(acked_question["answer_message_id"], answer_message_id)

            release_first_pending.set()
            if not second_ask_started.wait(timeout=3):
                failure_state = read_state(self.path)
                failure_assignment = failure_state["roles"]["worker-a"]
                failure_question = (
                    failure_assignment.get("orca_question")
                    if isinstance(failure_assignment, dict)
                    else None
                )
                worker_context_done.set()
                worker_thread.join(timeout=5)
                self.assertEqual(
                    worker_errors,
                    [],
                    "first pending ask failed after Main ACK: "
                    f"question={failure_question!r}, worker={worker_errors!r}",
                )
                self.fail("resume ask did not start after the first pending response")

            answer_frame = self._read_frame(client)
            self.assertEqual(answer_frame["kind"], "answer")
            self.assertEqual(
                answer_frame["answers"], {"question_0_custom": "after Main ACK"}
            )
            client.sendall(
                json.dumps(
                    {
                        "kind": "received",
                        "session_id": request.session_id,
                        "tool_call_id": request.tool_call_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            recorded_frame = self._read_frame(client)
            self.assertEqual(recorded_frame["kind"], "recorded")
            worker_context_done.set()
        finally:
            release_first_pending.set()
            worker_context_done.set()
            if client is not None:
                client.close()
            worker_thread.join(timeout=5)
            if socket_path.exists():
                socket_path.unlink()

        self.assertFalse(worker_thread.is_alive())
        self.assertEqual(worker_errors, [])
        final_state = read_state(self.path)
        final_assignment = final_state["roles"]["worker-a"]
        self.assertIsInstance(final_assignment, dict)
        final_question = final_assignment["orca_question"]
        self.assertIsInstance(final_question, dict)
        self.assertEqual(final_question["phase"], "recorded")
        self.assertEqual(final_question["message_id"], question_message_id)
        self.assertEqual(final_question["thread_id"], question_message_id)
        self.assertEqual(final_question["answer_message_id"], answer_message_id)
        self.assertEqual(final_assignment["acp_session_id"], request.session_id)
        self.assertEqual([call[1] for call in main_calls], ["check", "reply", "check"])
        self.assertEqual(runner_calls[0][3], "--question")
        self.assertEqual(runner_calls[1][3], "--resume")
        self.assertEqual(runner_calls[1][4], question_message_id)

    def test_remote_reply_waits_for_local_answer_identity_commit(self) -> None:
        provider = tempfile.TemporaryDirectory(
            prefix="agent-team-provider-", dir="/tmp"
        )
        self.addCleanup(provider.cleanup)
        provider_root = Path(provider.name).resolve()
        state = read_state(self.path)
        roles = state["roles"]
        self.assertIsInstance(roles, dict)
        assignment = roles["worker-a"]
        self.assertIsInstance(assignment, dict)
        assignment["provider_private_root"] = str(provider_root)
        write_state(self.path, state, require_existing=True)

        identity = {
            field: cast(str, assignment[field])
            for field in (
                "role",
                "role_kind",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
            )
        }
        identity["run_id"] = cast(str, state["run_id"])
        socket_path = provider_root / "q.sock"
        request = QuestionRequest(
            "acp-session-reply-race",
            "tool-call-reply-race",
            (QuestionField("question_0_custom", "Reply from Main."),),
        )
        question_body = orca_questions.question_json(request)
        answer_body = '{"question_0_custom":"remote answer"}'
        question_message_id = "question-reply-race"
        answer_message_id = "answer-reply-race"
        delivery_id = "delivery-reply-race"

        runner_calls: list[tuple[str, ...]] = []
        first_identity_saved = threading.Event()
        resume_ask_started = threading.Event()
        server_answer_ready = threading.Event()
        worker_success_ready = threading.Event()
        worker_local_check = threading.Event()
        worker_context_entered = threading.Event()
        worker_context_done = threading.Event()
        answer_ready = threading.Event()
        worker_errors: list[BaseException] = []
        reader_result: dict[str, object] = {}

        pending_result = {
            "answer": None,
            "messageId": question_message_id,
            "threadId": question_message_id,
            "timedOut": True,
            "cancelled": False,
            "connectionLost": False,
            "timeoutMs": 500,
        }
        success_result = {
            "answer": answer_body,
            "messageId": question_message_id,
            "answerMessageId": answer_message_id,
            "threadId": question_message_id,
            "timedOut": False,
            "cancelled": False,
            "connectionLost": False,
            "timeoutMs": 500,
        }

        def ask_process(
            _runner: object, argv: list[str], **_kwargs: object
        ) -> ProcessResult:
            runner_calls.append(tuple(argv))
            if len(runner_calls) == 1:
                return ProcessResult(1, json.dumps(pending_result), "")
            if len(runner_calls) == 2:
                resume_ask_started.set()
                if not server_answer_ready.wait(timeout=5):
                    raise AssertionError("server reply was not made available")
                worker_success_ready.set()
                return ProcessResult(0, json.dumps(success_result), "")
            raise AssertionError(f"unexpected extra Orca ask invocation: {argv!r}")

        main_calls: list[tuple[str, ...]] = []

        def fake_run_orca(
            _state: dict[str, object], args: list[str], **_kwargs: object
        ) -> dict[str, object]:
            main_calls.append(tuple(args))
            operation = args[1] if len(args) > 1 else ""
            if operation == "check" and "--wait" in args:
                return {
                    "deliveryId": delivery_id,
                    "messages": [
                        {
                            "id": question_message_id,
                            "type": "question",
                            "from_handle": "dispatch:dispatch_1",
                            "run_id": "run_1",
                            "body": question_body,
                            "payload": {
                                "taskId": "task_worker",
                                "dispatchId": "dispatch_1",
                            },
                        }
                    ],
                }
            if operation == "reply":
                server_answer_ready.set()
                if not worker_local_check.wait(timeout=5):
                    raise AssertionError("worker did not check local answer identity")
                self.assertEqual(args[args.index("--id") + 1], question_message_id)
                self.assertEqual(args[args.index("--body") + 1], answer_body)
                return {
                    "duplicate": False,
                    "message": {
                        "id": answer_message_id,
                        "thread_id": question_message_id,
                        "run_id": "run_1",
                        "body": answer_body,
                    },
                    "question": {
                        "message_id": question_message_id,
                        "run_id": "run_1",
                        "dispatch_id": "dispatch_1",
                        "status": "answered",
                        "answer_message_id": answer_message_id,
                        "answer_body": answer_body,
                    },
                }
            if operation == "check" and "--ack" in args:
                self.assertEqual(args[args.index("--ack") + 1], delivery_id)
                return {"acknowledged": delivery_id}
            raise AssertionError(f"unexpected Main Orca operation: {args!r}")

        def observed_read_state(path: Path) -> dict[str, object]:
            current = real_question_read_state(path)
            if worker_success_ready.is_set():
                current_roles = current.get("roles")
                current_assignment = (
                    current_roles.get("worker-a")
                    if isinstance(current_roles, dict)
                    else None
                )
                question = (
                    current_assignment.get("orca_question")
                    if isinstance(current_assignment, dict)
                    else None
                )
                if (
                    isinstance(question, dict)
                    and question.get("message_id") == question_message_id
                    and question.get("answer_message_id") is None
                ):
                    worker_local_check.set()
            return current

        def save_remote_identity(
            path: Path,
            saved_identity: dict[str, object],
            *,
            message_id: str,
            thread_id: str,
            answer_message_id: str | None = None,
        ) -> None:
            real_save_remote_identity(
                path,
                saved_identity,
                message_id=message_id,
                thread_id=thread_id,
                answer_message_id=answer_message_id,
            )
            first_identity_saved.set()

        def worker() -> None:
            try:
                with orca_questions.question_context(
                    self.path,
                    socket_path,
                    identity,
                    timeout_ms=500,
                ):
                    worker_context_entered.set()
                    worker_context_done.wait(timeout=5)
            except (AssertionError, ExecutionError) as exc:
                worker_errors.append(exc)

        def read_answer() -> None:
            assert client is not None
            try:
                reader_result["frame"] = self._read_frame(client)
            except (
                AssertionError,
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as exc:
                reader_result["error"] = exc
            finally:
                answer_ready.set()

        real_question_read_state = orca_questions.read_state
        real_save_remote_identity = orca_questions._save_remote_identity
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(
            mock.patch.object(
                orca_questions, "orca_executable", return_value="orca-test"
            )
        )
        patches.enter_context(
            mock.patch.object(orca_questions._AskProcessRunner, "run", new=ask_process)
        )
        patches.enter_context(
            mock.patch.object(mcp_server, "run_orca", side_effect=fake_run_orca)
        )
        patches.enter_context(
            mock.patch.object(
                orca_questions, "read_state", side_effect=observed_read_state
            )
        )
        patches.enter_context(
            mock.patch.object(
                orca_questions,
                "_save_remote_identity",
                side_effect=save_remote_identity,
            )
        )
        session = self._runtime_session()
        worker_thread = threading.Thread(
            target=worker, name="orca-question-reply-race-worker"
        )
        worker_thread.start()
        client: socket.socket | None = None
        reader_thread: threading.Thread | None = None
        try:
            self.assertTrue(worker_context_entered.wait(timeout=3))
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(socket_path))
            client.sendall(
                json.dumps(
                    request.as_dict(), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
            )
            self.assertTrue(first_identity_saved.wait(timeout=3))
            wait_result = session.execute(
                "role_wait", {"role": "worker-a", "timeout_ms": 1_000}
            )
            self.assertEqual(wait_result["delivery_id"], delivery_id)
            self.assertTrue(resume_ask_started.wait(timeout=3))
            reader_thread = threading.Thread(
                target=read_answer, name="orca-question-reply-race-reader"
            )
            reader_thread.start()
            reply_result = session.execute(
                "message_reply",
                {"message_id": question_message_id, "body": answer_body},
            )
            self.assertEqual(reply_result, {"replied": True})
            ack_result = session.execute("delivery_ack", {"delivery_id": delivery_id})
            self.assertEqual(ack_result, {"acknowledged": True})
            self.assertTrue(answer_ready.wait(timeout=3))
            reader_error = reader_result.get("error")
            if reader_error is not None:
                failure_state = read_state(self.path)
                failure_assignment = failure_state["roles"]["worker-a"]
                failure_question = (
                    failure_assignment.get("orca_question")
                    if isinstance(failure_assignment, dict)
                    else None
                )
                worker_context_done.set()
                worker_thread.join(timeout=5)
                self.fail(
                    "worker received remote answer before local reply commit: "
                    f"reader={reader_error!r}, question={failure_question!r}, "
                    f"worker={worker_errors!r}"
                )
            answer_frame = reader_result["frame"]
            self.assertIsInstance(answer_frame, dict)
            self.assertEqual(answer_frame["kind"], "answer")
            self.assertEqual(
                answer_frame["answers"], {"question_0_custom": "remote answer"}
            )
            client.sendall(
                json.dumps(
                    {
                        "kind": "received",
                        "session_id": request.session_id,
                        "tool_call_id": request.tool_call_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            recorded_frame = self._read_frame(client)
            self.assertEqual(recorded_frame["kind"], "recorded")
            worker_context_done.set()
        except (RuntimeFailure, RuntimeError, ValueError) as exc:
            worker_context_done.set()
            self.fail(f"remote reply/local commit race failed: {exc}")
        finally:
            worker_context_done.set()
            if client is not None:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
            if reader_thread is not None:
                reader_thread.join(timeout=5)
            worker_thread.join(timeout=5)
            if socket_path.exists():
                socket_path.unlink()

        self.assertFalse(worker_thread.is_alive())
        self.assertEqual(worker_errors, [])
        final_state = read_state(self.path)
        final_assignment = final_state["roles"]["worker-a"]
        self.assertIsInstance(final_assignment, dict)
        final_question = final_assignment["orca_question"]
        self.assertIsInstance(final_question, dict)
        self.assertEqual(final_question["phase"], "recorded")
        self.assertEqual(final_question["message_id"], question_message_id)
        self.assertEqual(final_question["thread_id"], question_message_id)
        self.assertEqual(final_question["answer_message_id"], answer_message_id)
        self.assertEqual(final_assignment["acp_session_id"], request.session_id)
        self.assertEqual([call[1] for call in main_calls], ["check", "reply", "check"])
        self.assertEqual(runner_calls[0][3], "--question")
        self.assertEqual(runner_calls[1][3], "--resume")


if __name__ == "__main__":
    unittest.main()
