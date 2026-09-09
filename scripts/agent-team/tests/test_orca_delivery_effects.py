from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

import test_orca_tasks as fixture_support

from agent_team import orca_questions, orca_tasks
from agent_team.contracts import (
    DeliveryAck,
    DeliveryRef,
    MessageRef,
    MessageReply,
    RoleRead,
    RoleRelease,
    RuntimeFailure,
    Status,
)
from agent_team.native_question_channel import QuestionField, QuestionRequest
from agent_team.runtime import (
    RuntimeValidationError,
    read_state,
    validate_state_object,
    write_state,
)


class OrcaDeliveryEffectsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_support.OrcaTasksTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend, self.path = self.fixture.backend, self.fixture.path

    def released(self):
        self.fixture.observe()
        self.backend.request(RoleRead(self.fixture.role, 10))
        self.backend.request(RoleRelease(self.fixture.role))

    def test_ack_save_failure_retains_intent_and_prevents_replay_or_stop(self):
        self.released()
        original = orca_tasks.write_state

        def fail_final(path, state, **kwargs):
            if "pending_delivery_id" not in state:
                raise OSError("save failed after remote ACK")
            return original(path, state, **kwargs)

        with (
            mock.patch.object(orca_tasks, "write_state", side_effect=fail_final),
            self.assertRaises(OSError),
        ):
            self.backend.request(DeliveryAck(DeliveryRef("delivery_1")))
        state = read_state(self.path)
        self.assertEqual(state["pending_orca_effect"]["operation"], "ack")
        self.assertEqual(state["pending_delivery_stage"], "released")
        for key, value in (
            ("run_id", "other-run"),
            ("delivery_id", "other-delivery"),
            ("operation", "send"),
            ("message_id", "question-1"),
            ("body_sha256", "a" * 64),
        ):
            damaged = copy.deepcopy(state)
            damaged["pending_orca_effect"][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeValidationError):
                validate_state_object(self.path, damaged)
        before = self.path.read_bytes()
        calls = list(self.fixture.remote_calls)
        with self.assertRaises(RuntimeFailure):
            self.backend.request(DeliveryAck(DeliveryRef("delivery_1")))
        with self.assertRaises(RuntimeFailure):
            self.backend.stop()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.fixture.remote_calls, calls)
        self.assertEqual(self.backend.request(Status()).status, "cleanup_pending")

    def test_ack_intent_save_failure_prevents_remote_ack(self):
        self.released()
        before = list(self.fixture.remote_calls)
        with (
            mock.patch.object(
                orca_tasks, "write_state", side_effect=OSError("intent save failed")
            ),
            self.assertRaises(OSError),
        ):
            self.backend.request(DeliveryAck(DeliveryRef("delivery_1")))
        self.assertEqual(self.fixture.remote_calls, before)
        self.assertNotIn("pending_orca_effect", read_state(self.path))

    def test_reply_save_failure_retains_exact_reply_intent(self):
        state = read_state(self.path)
        assignment = state["roles"][self.fixture.role.node_id]
        request = QuestionRequest(
            "session-1", "call-1", (QuestionField("question_0_custom", "Question?"),)
        )
        orca_questions._begin_in_state(state, assignment, request)
        orca_questions.observe_question(
            state,
            assignment,
            {
                "id": "question-1",
                "type": "question",
                "from_handle": "dispatch:dispatch_1",
                "body": orca_questions.question_json(request),
                "payload": {"taskId": "task_worker", "dispatchId": "dispatch_1"},
            },
            "delivery-question",
        )
        write_state(self.path, state, require_existing=True)
        body = json.dumps({"question_0_custom": "answer"})
        response = {
            "duplicate": False,
            "message": {
                "id": "answer-1",
                "thread_id": "question-1",
                "run_id": "run_1",
                "body": body,
            },
            "question": {
                "message_id": "question-1",
                "run_id": "run_1",
                "dispatch_id": "dispatch_1",
                "status": "answered",
                "answer_message_id": "answer-1",
                "answer_body": body,
            },
        }
        original = orca_tasks.write_state

        def fail_final(path, state, **kwargs):
            if state.get("replied_question_ids"):
                raise OSError("save failed after remote reply")
            return original(path, state, **kwargs)

        with (
            mock.patch.object(
                orca_tasks.remote, "run_orca", return_value=response
            ) as remote,
            mock.patch.object(orca_tasks, "write_state", side_effect=fail_final),
            self.assertRaises(OSError),
        ):
            self.backend.request(MessageReply(MessageRef("question-1"), body))
        remote.assert_called_once()
        state = read_state(self.path)
        self.assertEqual(state["pending_orca_effect"]["operation"], "reply")
        self.assertEqual(state["pending_orca_effect"]["message_id"], "question-1")
        self.assertEqual(state["replied_question_ids"], [])
        with (
            mock.patch.object(orca_tasks.remote, "run_orca") as remote,
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.request(MessageReply(MessageRef("question-1"), body))
        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
