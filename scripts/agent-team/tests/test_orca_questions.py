from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Self
from unittest import TestCase, mock

from agent_team import orca_questions
from agent_team.adapters import ExecutionError, ProcessResult
from agent_team.contracts import EventKind
from agent_team.native_question_channel import (
    QuestionChannelError,
    QuestionField,
    QuestionRequest,
    validate_answers,
)
from agent_team.scoped_acp import native_profile


class _Reservation:
    def __init__(self, _path: Path, *, create_parent: bool = False) -> None:
        del create_parent

    def acquire(self) -> None:
        return None

    def acquire_for_publication(self) -> None:
        return None

    def release(self) -> None:
        return None


class OrcaQuestionTest(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.id().replace(".", "-"))
        self.path = self.root / "state.json"
        self.state = {
            "version": 4,
            "runtime": "orca",
            "main_terminal": "main-terminal",
            "run_id": "run-1",
            "workspace": str(self.root),
            "state_path": str(self.path),
            "graph": {
                "nodes": [
                    {"node_id": "main", "kind": "main"},
                    {"node_id": "worker-a", "kind": "worker"},
                ],
                "edges": [
                    {"source": "main", "target": "worker-a", "kind": "delegates-to"}
                ],
                "coordination": {
                    "mode": "agent",
                    "entry_nodes": ["main"],
                    "dispatch_mode": "serial",
                    "max_active": 1,
                },
                "routes": [],
            },
            "roles": {
                "worker-a": {
                    "role": "worker-a",
                    "role_kind": "worker",
                    "task_id": "remote-task-1",
                    "dispatch_id": "dispatch-1",
                    "terminal_handle": "terminal-1",
                    "launch_nonce": "nonce1234",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            },
            "role_specs": {
                "main": {
                    "kind": "main",
                    "provider": "claude",
                    "transport": "direct",
                    "model": "model-main",
                    "effort": "high",
                    "permission": "orchestrator",
                    "instructions": "main",
                    "execution": "tui_direct",
                },
                "worker-a": {
                    "kind": "worker",
                    **native_profile("claude", "worker"),
                    "model": "model-worker",
                    "effort": "high",
                    "instructions": "worker",
                },
            },
        }
        self.saved: list[dict[str, object]] = []
        self._patches = mock.patch.multiple(
            orca_questions,
            read_state=mock.Mock(side_effect=self._read),
            write_state=mock.Mock(side_effect=self._write),
            _LifecycleReservation=_Reservation,
        )
        self._patches.start()
        self.addCleanup(self._patches.stop)

    def _read(self, _path: Path) -> dict[str, object]:
        return copy.deepcopy(self.state)

    def _write(self, _path: Path, state: dict[str, object], **_kwargs: object) -> None:
        self.state = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))

    def request(self, *, session: str = "session-1") -> QuestionRequest:
        return QuestionRequest(
            session,
            "tool-1",
            (
                QuestionField("question_0_custom", "Choose one: yes or no."),
                QuestionField("question_1_custom", "Explain the choice."),
            ),
        )

    def identity(self) -> dict[str, str]:
        return {
            "role": "worker-a",
            "role_kind": "worker",
            "run_id": "run-1",
            "task_id": "remote-task-1",
            "dispatch_id": "dispatch-1",
            "terminal_handle": "terminal-1",
            "launch_nonce": "nonce1234",
        }

    def saved_assignment(self) -> dict[str, object]:
        roles = self.state["roles"]
        assert isinstance(roles, dict)
        assignment = roles["worker-a"]
        assert isinstance(assignment, dict)
        return assignment

    def begin_and_observe(self) -> tuple[QuestionRequest, dict[str, object]]:
        request = self.request()
        orca_questions.begin_question(self.path, request, **self.identity())
        assignment = self.saved_assignment()
        message = {
            "id": "message-1",
            "type": "question",
            "from_handle": "dispatch:dispatch-1",
            "body": orca_questions.question_json(request),
            "payload": json.dumps(
                {"taskId": "remote-task-1", "dispatchId": "dispatch-1"}
            ),
        }
        event = orca_questions.observe_question(
            self.state, assignment, message, "delivery-1"
        )
        self.assertEqual(event.kind, EventKind.QUESTION)
        return request, assignment

    def test_begin_saves_placeholder_and_binds_session(self) -> None:
        request = self.request()

        result = orca_questions.begin_question(self.path, request, **self.identity())

        self.assertEqual(result["phase"], "asking")
        self.assertEqual(result["message_id"], None)
        self.assertEqual(self.saved_assignment()["acp_session_id"], "session-1")
        orca_questions.validate_outbox(self.state, self.saved_assignment())

    def test_identity_profile_and_lifecycle_guards_fail_before_state_write(
        self,
    ) -> None:
        baseline = copy.deepcopy(self.state)

        def missing_runtime(state: dict[str, object]) -> None:
            state.pop("runtime")

        def wrong_version(state: dict[str, object]) -> None:
            state["version"] = 5

        def unknown_completion(state: dict[str, object]) -> None:
            roles = state["roles"]
            assert isinstance(roles, dict)
            assignment = roles["worker-a"]
            assert isinstance(assignment, dict)
            assignment["completion_observed"] = None

        def wrong_provider(state: dict[str, object]) -> None:
            role_specs = state["role_specs"]
            assert isinstance(role_specs, dict)
            spec = role_specs["worker-a"]
            assert isinstance(spec, dict)
            spec["provider"] = "codex"

        def wrong_node_kind(state: dict[str, object]) -> None:
            graph = state["graph"]
            assert isinstance(graph, dict)
            nodes = graph["nodes"]
            assert isinstance(nodes, list)
            nodes[1] = {"node_id": "worker-a", "kind": "reviewer"}

        def invalid_role_spec(state: dict[str, object]) -> None:
            role_specs = state["role_specs"]
            assert isinstance(role_specs, dict)
            role_specs["worker-a"] = "invalid"

        cases: dict[str, Callable[[dict[str, object]], None]] = {
            "missing-runtime": missing_runtime,
            "wrong-version": wrong_version,
            "unknown-completion": unknown_completion,
            "wrong-provider": wrong_provider,
            "wrong-node-kind": wrong_node_kind,
            "invalid-role-spec": invalid_role_spec,
        }
        for name, damage in cases.items():
            with self.subTest(name=name):
                self.state = copy.deepcopy(baseline)
                self.saved.clear()
                damage(self.state)
                with self.assertRaises(ValueError):
                    orca_questions.begin_question(
                        self.path, self.request(), **self.identity()
                    )
                self.assertEqual(self.saved, [])

    def test_observe_preserves_all_fields_and_adds_json_answer_guidance(self) -> None:
        request, assignment = self.begin_and_observe()

        event = orca_questions.observe_question(
            self.state,
            assignment,
            {
                "id": "message-1",
                "type": "question",
                "from_handle": "dispatch:dispatch-1",
                "payload": {"taskId": "remote-task-1", "dispatchId": "dispatch-1"},
                "body": orca_questions.question_json(request),
            },
            "delivery-1",
        )

        self.assertEqual(event.body.count("question_0_custom"), 2)
        self.assertIn("Choose one: yes or no.", event.body)
        self.assertIn("Explain the choice.", event.body)
        self.assertIn("Reply with JSON", event.body)
        saved = self.saved_assignment()["orca_question"]
        assert isinstance(saved, dict)
        self.assertEqual(saved["request"], request.as_dict())
        self.assertEqual(saved["phase"], "observed")
        self.assertEqual(self.state["pending_question_ids"], ["message-1"])

    def test_reply_requires_exact_canonical_answer_mapping_then_ack(self) -> None:
        _request, assignment = self.begin_and_observe()
        body = '{"question_0_custom":"yes","question_1_custom":"because"}'

        parsed = orca_questions.prepare_reply(self.state, assignment, "message-1", body)
        self.assertEqual(parsed["question_0_custom"], "yes")
        self.assertEqual(parsed["question_1_custom"], "because")

        response = {
            "duplicate": False,
            "message": {
                "id": "answer-1",
                "thread_id": "message-1",
                "run_id": "run-1",
                "body": body,
            },
            "question": {
                "message_id": "message-1",
                "run_id": "run-1",
                "dispatch_id": "dispatch-1",
                "status": "answered",
                "answer_message_id": "answer-1",
                "answer_body": body,
            },
        }
        accepted = orca_questions.accept_reply(
            self.state,
            assignment,
            response,
            message_id="message-1",
            body=body,
        )
        self.assertEqual(accepted, parsed)
        question = assignment["orca_question"]
        assert isinstance(question, dict)
        self.assertEqual(question["phase"], "replied")

        orca_questions.acknowledge_question(self.state, assignment, "delivery-1")
        question = assignment["orca_question"]
        assert isinstance(question, dict)
        self.assertEqual(question["phase"], "acked")
        self.assertNotIn("pending_delivery_id", self.state)

    def test_reply_rejects_plain_extra_duplicate_and_nan_json(self) -> None:
        _request, assignment = self.begin_and_observe()

        for body in (
            "yes",
            '{"question_0_custom":"yes"}',
            '{"question_0_custom":"yes","question_1_custom":"because","extra":"x"}',
            '{"question_0_custom":"yes","question_0_custom":"no","question_1_custom":"because"}',
            '{"question_0_custom":"yes","question_1_custom":NaN}',
        ):
            with (
                self.subTest(body=body),
                self.assertRaises((ValueError, RuntimeError, TypeError)),
            ):
                orca_questions.prepare_reply(self.state, assignment, "message-1", body)

    def test_reply_accepts_pretty_json_and_receipt_uses_exact_body(self) -> None:
        _request, assignment = self.begin_and_observe()
        body = '{\n  "question_1_custom": "because",\n  "question_0_custom": "yes"\n}'
        response = {
            "duplicate": False,
            "message": {
                "id": "answer-1",
                "thread_id": "message-1",
                "run_id": "run-1",
                "body": body,
            },
            "question": {
                "message_id": "message-1",
                "run_id": "run-1",
                "dispatch_id": "dispatch-1",
                "status": "answered",
                "answer_message_id": "answer-1",
                "answer_body": body,
            },
        }
        parsed = orca_questions.prepare_reply(self.state, assignment, "message-1", body)
        self.assertEqual(
            parsed,
            {
                "question_0_custom": "yes",
                "question_1_custom": "because",
            },
        )
        self.assertEqual(
            orca_questions.accept_reply(
                self.state, assignment, response, "message-1", body
            ),
            parsed,
        )

    def test_cancelling_question_keeps_pending_delivery_without_ack(self) -> None:
        request, _assignment = self.begin_and_observe()

        orca_questions.fail_question(
            self.path,
            request,
            cancelling=True,
            error="stop requested",
            **self.identity(),
        )

        assignment = self.saved_assignment()
        question = assignment["orca_question"]
        assert isinstance(question, dict)
        self.assertEqual(question["phase"], "cancelling")
        self.assertEqual(self.state["pending_delivery_id"], "delivery-1")
        self.assertIsNone(question["answers"])

    def test_recorded_question_allows_next_form_in_same_acp_session(self) -> None:
        request, assignment = self.begin_and_observe()
        body = '{"question_0_custom":"yes","question_1_custom":"because"}'
        response = {
            "duplicate": False,
            "message": {
                "id": "answer-1",
                "thread_id": "message-1",
                "run_id": "run-1",
                "body": body,
            },
            "question": {
                "message_id": "message-1",
                "run_id": "run-1",
                "dispatch_id": "dispatch-1",
                "status": "answered",
                "answer_message_id": "answer-1",
                "answer_body": body,
            },
        }
        orca_questions.accept_reply(self.state, assignment, response, "message-1", body)
        orca_questions.acknowledge_question(self.state, assignment, "delivery-1")
        orca_questions.confirm_question(self.path, request, **self.identity())
        orca_questions.record_question_sent(self.path, request, **self.identity())

        next_request = QuestionRequest(
            "session-1",
            "tool-2",
            (QuestionField("question_0_custom", "A second form."),),
        )
        next_question = orca_questions.begin_question(
            self.path, next_request, **self.identity()
        )
        self.assertEqual(next_question["phase"], "asking")

    def test_exchange_resumes_same_message_and_waits_for_local_ack(self) -> None:
        request, assignment = self.begin_and_observe()
        started = threading.Event()
        allow_ack = threading.Event()
        calls: list[tuple[str | None, str | None]] = []
        result: dict[str, object] = {}

        answer = '{"question_0_custom":"yes","question_1_custom":"because"}'

        def ask(
            question: str | None,
            resume_message_id: str | None,
            timeout_ms: int,
            stopped: threading.Event,
        ) -> dict[str, object]:
            del timeout_ms
            self.assertFalse(stopped.is_set())
            calls.append((question, resume_message_id))
            if len(calls) == 1:
                started.set()
                return {
                    "answer": None,
                    "messageId": "message-1",
                    "threadId": "message-1",
                    "timedOut": True,
                    "cancelled": False,
                    "connectionLost": False,
                    "timeoutMs": 500,
                }
            self.assertIsNone(question)
            self.assertEqual(resume_message_id, "message-1")
            started.set()
            allow_ack.wait(timeout=2)
            return {
                "answer": answer,
                "messageId": "message-1",
                "answerMessageId": "answer-1",
                "threadId": "message-1",
                "timedOut": False,
                "cancelled": False,
                "connectionLost": False,
                "timeoutMs": 500,
            }

        def run() -> None:
            identity = self.identity()
            result["answers"] = orca_questions.exchange(
                self.path,
                request,
                threading.Event(),
                ask=ask,
                role=identity["role"],
                role_kind=identity["role_kind"],
                run_id=identity["run_id"],
                task_id=identity["task_id"],
                dispatch_id=identity["dispatch_id"],
                terminal_handle=identity["terminal_handle"],
                launch_nonce=identity["launch_nonce"],
            )

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(timeout=2))
        time.sleep(0.05)
        self.assertTrue(worker.is_alive())

        body = answer
        assignment = self.saved_assignment()
        response = {
            "duplicate": False,
            "message": {
                "id": "answer-1",
                "thread_id": "message-1",
                "run_id": "run-1",
                "body": body,
            },
            "question": {
                "message_id": "message-1",
                "run_id": "run-1",
                "dispatch_id": "dispatch-1",
                "status": "answered",
                "answer_message_id": "answer-1",
                "answer_body": body,
            },
        }
        orca_questions.accept_reply(
            self.state,
            assignment,
            response,
            message_id="message-1",
            body=body,
        )
        orca_questions.acknowledge_question(self.state, assignment, "delivery-1")
        allow_ack.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            result["answers"], validate_answers(request, json.loads(answer))
        )
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(question is None for question, _resume in calls))
        self.assertTrue(all(resume == "message-1" for _question, resume in calls))

    def test_large_answers_are_checked_per_field_and_frame_size(self) -> None:
        request = QuestionRequest(
            "session-large",
            "tool-large",
            tuple(
                QuestionField(f"question_{index}_custom", "prompt")
                for index in range(4)
            ),
        )
        answers = {field.field: "あ" * 20_000 for field in request.questions}
        parsed = orca_questions.validate_answer_mapping(request, answers)
        self.assertEqual(parsed, answers)
        with self.assertRaises(ValueError):
            orca_questions.validate_answer_mapping(
                request,
                {**answers, "question_0_custom": "x" * 20_001},
            )

    def test_bare_ask_parser_allows_pending_nonzero_only_for_pending_flags(
        self,
    ) -> None:
        pending = {
            "answer": None,
            "messageId": "message-1",
            "threadId": "message-1",
            "timedOut": True,
            "cancelled": False,
            "connectionLost": False,
            "timeoutMs": 500,
        }
        self.assertEqual(orca_questions._parse_bare_ask_result(pending, 1), pending)
        normal = {
            **pending,
            "answer": "yes",
            "answerMessageId": "answer-1",
            "timedOut": False,
        }
        self.assertEqual(orca_questions._parse_bare_ask_result(normal, 0), normal)
        for bad_payload, returncode in (
            ({"ok": True, "result": normal}, 0),
            ({**pending, "messageId": 1}, 1),
            ({**pending, "timedOut": False}, 1),
        ):
            with (
                self.subTest(bad_payload=bad_payload, returncode=returncode),
                self.assertRaises(ValueError),
            ):
                orca_questions._parse_bare_ask_result(bad_payload, returncode)

    def test_actual_ask_adapter_uses_selected_orca_and_bare_json(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = ProcessResult(
            1,
            json.dumps(
                {
                    "answer": None,
                    "messageId": "message-1",
                    "threadId": "message-1",
                    "timedOut": True,
                    "cancelled": False,
                    "connectionLost": False,
                    "timeoutMs": 500,
                }
            ),
            "",
        )
        stopped = threading.Event()
        with (
            mock.patch.object(
                orca_questions, "orca_executable", return_value="orca-test"
            ),
            mock.patch.object(
                orca_questions, "_AskProcessRunner", return_value=runner
            ) as runner_class,
        ):
            result = orca_questions._ask_orca(
                self.path,
                '{"kind":"question"}',
                None,
                500,
                stopped,
                **self.identity(),
            )
        self.assertEqual(result["messageId"], "message-1")
        self.assertIs(runner_class.call_args.args[0], stopped)
        argv = runner.run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "orca-test",
                "orchestration",
                "ask",
                "--question",
                '{"kind":"question"}',
                "--run",
                "run-1",
                "--from",
                "terminal-1",
                "--timeout-ms",
                "500",
                "--json",
            ],
        )

    def test_question_context_exposes_typed_cleanup_failure(self) -> None:
        class BadChannel:
            failure: Exception | None = None

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> Self:
                raise QuestionChannelError("socket failed", cleanup_confirmed=False)

            def __exit__(self, *_args: object) -> None:
                return None

        with (
            mock.patch.object(orca_questions, "QuestionChannel", BadChannel),
            self.assertRaises(ExecutionError) as raised,
            orca_questions.question_context(
                self.path,
                self.root / "q.sock",
                self.identity(),
                ask=lambda _question, _resume, _timeout, _stopped: {},
            ),
        ):
            pass
        self.assertFalse(raised.exception.cleanup_confirmed)

    def test_accept_reply_reuses_strict_wire_receipt(self) -> None:
        _request, assignment = self.begin_and_observe()
        body = '{"question_0_custom":"yes","question_1_custom":"because"}'
        bad = {
            "duplicate": False,
            "message": {
                "id": "answer-1",
                "thread_id": "message-other",
                "run_id": "run-1",
                "body": body,
            },
            "question": {
                "message_id": "message-1",
                "run_id": "run-1",
                "dispatch_id": "dispatch-1",
                "status": "answered",
                "answer_message_id": "answer-1",
                "answer_body": body,
            },
        }
        with self.assertRaises((ValueError, RuntimeError, TypeError)):
            orca_questions.accept_reply(
                self.state,
                assignment,
                bad,
                message_id="message-1",
                body=body,
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
