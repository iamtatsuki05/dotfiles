from __future__ import annotations

import copy
import json
import unittest
from typing import cast

import test_orca_questions as serial_fixture

from agent_team import orca_questions
from agent_team.native_question_channel import QuestionField, QuestionRequest


class OrcaParallelQuestionTest(unittest.TestCase):
    """Question Delivery journals stay local to overlapping Orca assignments."""

    def setUp(self) -> None:
        self.fixture = serial_fixture.OrcaQuestionTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path
        self._make_parallel_state()

    @property
    def state(self) -> dict[str, object]:
        state = self.fixture.state
        self.assertIsInstance(state, dict)
        return state

    def _make_parallel_state(self) -> None:
        state = self.state
        state["version"] = 5
        graph = cast(dict[str, object], state["graph"])
        nodes = cast(list[dict[str, object]], graph["nodes"])
        nodes.append({"node_id": "worker-b", "kind": "worker"})
        edges = cast(list[dict[str, object]], graph["edges"])
        edges.append({"source": "main", "target": "worker-b", "kind": "delegates-to"})
        coordination = cast(dict[str, object], graph["coordination"])
        coordination["dispatch_mode"] = "parallel"
        coordination["max_active"] = 2

        roles = cast(dict[str, object], state["roles"])
        worker_a = cast(dict[str, object], roles["worker-a"])
        worker_b = copy.deepcopy(worker_a)
        worker_b.update(
            {
                "role": "worker-b",
                "task_id": "remote-task-2",
                "dispatch_id": "dispatch-2",
                "terminal_handle": "terminal-2",
                "launch_nonce": "nonce5678",
            }
        )
        roles["worker-b"] = worker_b

        role_specs = cast(dict[str, object], state["role_specs"])
        role_specs["worker-b"] = copy.deepcopy(role_specs["worker-a"])

    @staticmethod
    def _request(role: str) -> QuestionRequest:
        return QuestionRequest(
            f"session-{role}",
            f"tool-{role}",
            (QuestionField("question_0_custom", f"Question for {role}."),),
        )

    def _identity(self, role: str) -> dict[str, str]:
        roles = cast(dict[str, object], self.state["roles"])
        assignment = cast(dict[str, object], roles[role])
        return {
            field: cast(str, assignment[field])
            for field in (
                "role",
                "role_kind",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
            )
        } | {"run_id": cast(str, self.state["run_id"])}

    def _assignment(self, role: str) -> dict[str, object]:
        roles = cast(dict[str, object], self.state["roles"])
        return cast(dict[str, object], roles[role])

    def _begin_and_observe(self, role: str, delivery_id: str) -> QuestionRequest:
        request = self._request(role)
        orca_questions.begin_question(self.path, request, **self._identity(role))
        assignment = self._assignment(role)
        message = {
            "id": f"message-{role}",
            "type": "question",
            "from_handle": f"dispatch:{assignment['dispatch_id']}",
            "body": orca_questions.question_json(request),
            "payload": {
                "taskId": assignment["task_id"],
                "dispatchId": assignment["dispatch_id"],
            },
        }
        orca_questions.observe_question(self.state, assignment, message, delivery_id)
        return request

    def _reply_response(
        self, role: str, body: str, answer_message_id: str
    ) -> dict[str, object]:
        assignment = self._assignment(role)
        message_id = f"message-{role}"
        return {
            "duplicate": False,
            "message": {
                "id": answer_message_id,
                "thread_id": message_id,
                "run_id": self.state["run_id"],
                "body": body,
            },
            "question": {
                "message_id": message_id,
                "run_id": self.state["run_id"],
                "dispatch_id": assignment["dispatch_id"],
                "status": "answered",
                "answer_message_id": answer_message_id,
                "answer_body": body,
            },
        }

    def test_exact_reply_and_ack_change_only_the_owner_container(self) -> None:
        self._begin_and_observe("worker-a", "delivery-batch")
        self._begin_and_observe("worker-b", "delivery-batch")
        peer_before = copy.deepcopy(self._assignment("worker-b"))

        body = json.dumps({"question_0_custom": "answer-a"})
        orca_questions.accept_reply(
            self.state,
            self._assignment("worker-a"),
            self._reply_response("worker-a", body, "answer-a"),
            "message-worker-a",
            body,
        )
        owner = self._assignment("worker-a")
        owner_question = cast(dict[str, object], owner["orca_question"])
        self.assertEqual(owner_question["phase"], "replied")
        self.assertEqual(owner_question["answers"], {"question_0_custom": "answer-a"})
        self.assertEqual(self._assignment("worker-b"), peer_before)

        orca_questions.acknowledge_question(self.state, owner, "delivery-batch")
        owner_question = cast(dict[str, object], owner["orca_question"])
        self.assertEqual(owner_question["phase"], "acked")
        self.assertNotIn("pending_delivery_id", owner)
        self.assertEqual(self._assignment("worker-b"), peer_before)

    def test_peer_result_does_not_block_a_new_owner_question(self) -> None:
        peer = self._assignment("worker-b")
        peer.update(
            {
                "completion_observed": True,
                "orca_result": {"role": "worker-b", "body": "done"},
                "pending_delivery_id": "delivery-result-b",
                "pending_delivery_kind": "worker_done",
                "pending_delivery_stage": "observed",
                "pending_question_ids": [],
                "replied_question_ids": [],
            }
        )
        peer_before = copy.deepcopy(peer)

        request = self._begin_and_observe("worker-a", "delivery-a")
        owner = self._assignment("worker-a")
        self.assertEqual(
            cast(dict[str, object], owner["orca_question"])["request"],
            request.as_dict(),
        )
        self.assertEqual(self._assignment("worker-b"), peer_before)

    def test_peer_question_does_not_block_or_corrupt_owner_question(self) -> None:
        self._begin_and_observe("worker-b", "delivery-b")
        peer_before = copy.deepcopy(self._assignment("worker-b"))

        self._begin_and_observe("worker-a", "delivery-a")

        self.assertEqual(self._assignment("worker-b"), peer_before)
        owner = self._assignment("worker-a")
        self.assertEqual(
            cast(dict[str, object], owner["orca_question"])["message_id"],
            "message-worker-a",
        )
        self.assertEqual(owner["pending_delivery_id"], "delivery-a")
        self.assertEqual(
            self._assignment("worker-b")["pending_delivery_id"], "delivery-b"
        )

    def test_mismatched_delivery_is_rejected_and_unknown_peer_effect_is_retained(
        self,
    ) -> None:
        self._begin_and_observe("worker-a", "delivery-a")
        peer = self._assignment("worker-b")
        unknown_effect = {
            "operation": "future_effect",
            "delivery_id": "delivery-b",
        }
        peer["pending_orca_effect"] = unknown_effect

        with self.assertRaises(ValueError):
            orca_questions.acknowledge_question(
                self.state, self._assignment("worker-a"), "delivery-b"
            )

        owner = self._assignment("worker-a")
        self.assertEqual(owner["pending_delivery_id"], "delivery-a")
        self.assertEqual(peer["pending_orca_effect"], unknown_effect)

    def test_only_agent_serial_v4_and_agent_parallel_v5_graphs_are_question_scoped(
        self,
    ) -> None:
        state = self.state
        graph = cast(dict[str, object], state["graph"])
        coordination = cast(dict[str, object], graph["coordination"])
        coordination["mode"] = "program"
        with self.assertRaises(ValueError):
            orca_questions.begin_question(
                self.path,
                self._request("worker-a"),
                **self._identity("worker-a"),
            )

        coordination["mode"] = "agent"
        coordination["dispatch_mode"] = "parallel"
        state["pending_delivery_id"] = "legacy-root-delivery"
        with self.assertRaises(ValueError):
            orca_questions.begin_question(
                self.path,
                self._request("worker-a"),
                **self._identity("worker-a"),
            )
        state.pop("pending_delivery_id")

        coordination["dispatch_mode"] = "serial"
        with self.assertRaises(ValueError):
            orca_questions.begin_question(
                self.path,
                self._request("worker-a"),
                **self._identity("worker-a"),
            )


if __name__ == "__main__":
    unittest.main()
