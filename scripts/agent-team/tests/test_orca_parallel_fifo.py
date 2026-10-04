from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_orca_parallel_state import _state

from agent_team import orca_acp, orca_questions, orca_tasks
from agent_team.backend import OrcaBackend
from agent_team.contracts import (
    DeliveryAck,
    DeliveryRef,
    MessageRef,
    MessageReply,
    NodeRef,
    Role,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
)
from agent_team.native_question_channel import QuestionField, QuestionRequest
from agent_team.orca import TerminalCloseVerdict
from agent_team.runtime import create_prompt_file, read_state, write_state


class OrcaParallelFifoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = _state(self.root)
        self.path = Path(self.state["state_path"])
        for node_id, assignment in self.state["roles"].items():
            for field, prefix in (
                ("provider_private_root", "agent-team-provider-"),
                ("snapshot_root", "agent-team-snapshot-"),
            ):
                directory = tempfile.TemporaryDirectory(prefix=prefix)
                self.addCleanup(directory.cleanup)
                assignment[field] = str(Path(directory.name).resolve())
            assignment["prompt_path"] = str(
                create_prompt_file(
                    self.root,
                    NodeRef(node_id, Role.WORKER),
                    assignment["launch_nonce"],
                    "Declared task",
                )
            )
        write_state(self.path, self.state)
        client = mock.Mock()
        self.closed = []
        client.terminal_close.side_effect = self.close_terminal
        self.backend = OrcaBackend(client)
        self.backend._state = copy.deepcopy(self.state)
        self.tasks = orca_tasks.OrcaTasks(self.backend)
        self.messages = []
        self.remote_calls = []
        self.delivery_id = "delivery-shared"
        self.ack_error = None
        self.acknowledged = False
        self.before_check_returns = None
        verification = mock.patch.object(orca_tasks.OrcaTasks, "verify_remote")
        verification.start()
        self.addCleanup(verification.stop)
        remote = mock.patch.object(
            orca_tasks.remote, "run_orca", side_effect=self.remote
        )
        remote.start()
        self.addCleanup(remote.stop)

    def close_terminal(self, *, terminal_id, cwd):
        self.assertEqual(cwd, Path(self.state["workspace"]))
        self.closed.append(terminal_id)
        return TerminalCloseVerdict(terminal_id, "tab", True, "stopped")

    def remote(self, state, argv, **_kwargs):
        self.remote_calls.append(tuple(argv))
        operation = argv[1]
        if operation == "check":
            if "--ack" in argv:
                self.assertEqual(
                    read_state(self.path)["orca_delivery_batch"]["phase"],
                    "acknowledging",
                )
                if self.ack_error:
                    raise self.ack_error
                self.acknowledged = True
                return {"acknowledged": argv[argv.index("--ack") + 1]}
            if self.acknowledged or not self.messages:
                return {"deliveryId": None, "messages": []}
            if self.before_check_returns:
                self.before_check_returns()
            return {
                "deliveryId": self.delivery_id,
                "messages": copy.deepcopy(self.messages),
            }
        if operation in {"worker-read", "worker-release"}:
            dispatch = argv[argv.index("--dispatch") + 1]
            owner = next(
                a for a in self.state["roles"].values() if a["dispatch_id"] == dispatch
            )
            if operation == "worker-read":
                return {
                    "dispatchId": dispatch,
                    "source": "terminal",
                    "sourceIdentity": owner["terminal_handle"],
                    "terminal": {
                        "handle": owner["terminal_handle"],
                        "tail": ["observed output"],
                    },
                }
            return {
                "dispatchId": dispatch,
                "state": "retained",
                "reason": "no_owned_resource",
                "processAction": "none",
                "archive": None,
            }
        if operation == "reply":
            message_id = argv[argv.index("--id") + 1]
            body = argv[argv.index("--body") + 1]
            owner = next(
                a
                for a in state["roles"].values()
                if a.get("orca_question", {}).get("message_id") == message_id
            )
            return {
                "duplicate": False,
                "message": {
                    "id": "answer-1",
                    "thread_id": message_id,
                    "run_id": state["run_id"],
                    "body": body,
                },
                "question": {
                    "message_id": message_id,
                    "run_id": state["run_id"],
                    "dispatch_id": owner["dispatch_id"],
                    "status": "answered",
                    "answer_message_id": "answer-1",
                    "answer_body": body,
                },
            }
        raise AssertionError(argv)

    def completion(self, node_id):
        assignment = self.state["roles"][node_id]
        with mock.patch.object(orca_acp, "_send_worker_done"):
            orca_acp.publish_completion(
                self.path,
                run_id=self.state["run_id"],
                outcome="succeeded",
                body=node_id,
                cleanup_confirmed=True,
                **{
                    field: assignment[field]
                    for field in (
                        "role",
                        "role_kind",
                        "task_id",
                        "dispatch_id",
                        "terminal_handle",
                        "launch_nonce",
                    )
                },
            )
        return {
            "id": "msg-" + node_id,
            "run_id": self.state["run_id"],
            "type": "worker_done",
            "from_handle": assignment["terminal_handle"],
            "body": node_id,
            "payload": json.dumps(
                {
                    "taskId": assignment["task_id"],
                    "dispatchId": assignment["dispatch_id"],
                    "outcome": "succeeded",
                }
            ),
        }

    def question(self, node_id):
        assignment = self.state["roles"][node_id]
        request = QuestionRequest(
            "session-" + node_id,
            "tool-1",
            (QuestionField("question_0_custom", "Choose a value"),),
        )
        orca_questions.begin_question(
            self.path,
            request,
            run_id=self.state["run_id"],
            **{
                field: assignment[field]
                for field in (
                    "role",
                    "role_kind",
                    "task_id",
                    "dispatch_id",
                    "terminal_handle",
                    "launch_nonce",
                )
            },
        )
        return {
            "id": "msg-" + node_id,
            "run_id": self.state["run_id"],
            "type": "question",
            "from_handle": "dispatch:" + assignment["dispatch_id"],
            "body": orca_questions.question_json(request),
            "payload": json.dumps(
                {
                    "taskId": assignment["task_id"],
                    "dispatchId": assignment["dispatch_id"],
                }
            ),
        }

    def wait(self, node_id="worker-a"):
        return self.tasks.wait(RoleWait(NodeRef(node_id, Role.WORKER), 1_000))

    def consume(self, node_id):
        role = NodeRef(node_id, Role.WORKER)
        self.assertEqual(self.tasks.read(RoleRead(role, 10)).output, node_id)
        self.tasks.release(RoleRelease(role))

    def ack(self):
        return self.tasks.ack(DeliveryAck(DeliveryRef(self.delivery_id)))

    def ack_calls(self):
        return [call for call in self.remote_calls if "--ack" in call]

    def test_wait_for_b_returns_a_batch_without_a_second_fifo_check(self):
        self.messages = [self.completion("worker-a")]
        receipt = self.wait("worker-b")
        self.assertEqual(len(receipt.events), 1)
        self.assertEqual(
            receipt.events[0].identity.dispatch_id._value,
            self.state["roles"]["worker-a"]["dispatch_id"],
        )
        self.assertEqual(len(self.remote_calls), 1)
        with self.assertRaisesRegex(
            RuntimeFailure, "pending Run Delivery delivery-shared"
        ):
            self.wait("worker-a")
        self.assertEqual(len(self.remote_calls), 1)

    def test_two_completions_require_both_reads_and_releases_before_one_ack(self):
        self.messages = [self.completion(role) for role in ("worker-a", "worker-b")]
        receipt = self.wait()
        self.assertEqual(len(receipt.events), 2)
        self.assertEqual(
            len(read_state(self.path)["orca_delivery_batch"]["members"]), 2
        )
        self.consume("worker-a")
        with self.assertRaisesRegex(RuntimeFailure, "read and release worker-b"):
            self.ack()
        self.assertEqual(self.ack_calls(), [])
        self.assertEqual(len(read_state(self.path)["roles"]), 2)
        self.consume("worker-b")
        self.ack()
        state = read_state(self.path)
        self.assertEqual(state["roles"], {})
        self.assertNotIn("orca_delivery_batch", state)
        self.assertEqual(
            {r["status"] for r in state["tasks"].values()},
            {"awaiting_implementation_review"},
        )
        self.assertEqual(len(self.ack_calls()), 1)
        self.assertEqual(len(self.closed), 2)

    def test_mixed_completion_and_question_needs_answer_before_shared_ack(self):
        self.messages = [self.completion("worker-a"), self.question("worker-b")]
        receipt = self.wait()
        self.assertEqual(
            [event.kind.value for event in receipt.events], ["worker_done", "question"]
        )
        self.consume("worker-a")
        with self.assertRaises(RuntimeFailure):
            self.ack()
        self.assertEqual(self.ack_calls(), [])
        self.tasks.reply(
            MessageReply(
                MessageRef("msg-worker-b"), json.dumps({"question_0_custom": "chosen"})
            )
        )
        self.ack()
        state = read_state(self.path)
        self.assertEqual(set(state["roles"]), {"worker-b"})
        self.assertEqual(state["roles"]["worker-b"]["orca_question"]["phase"], "acked")
        self.assertNotIn("pending_delivery_id", state["roles"]["worker-b"])
        self.assertEqual(len(self.ack_calls()), 1)

    def test_lost_whole_ack_retains_all_owners_and_never_resends(self):
        self.messages = [self.completion(role) for role in ("worker-a", "worker-b")]
        self.wait()
        for role in ("worker-a", "worker-b"):
            self.consume(role)
        self.ack_error = RuntimeError("response lost")
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.ack()
        retained = read_state(self.path)
        self.assertEqual(retained["orca_delivery_batch"]["phase"], "acknowledging")
        self.assertEqual(len(retained["roles"]), 2)
        with self.assertRaises(RuntimeFailure):
            self.ack()
        self.assertEqual(len(self.ack_calls()), 1)
        self.assertEqual(read_state(self.path), retained)

    def test_unknown_peer_message_keeps_invalid_batch_without_partial_observation(self):
        self.messages = [
            self.completion("worker-a"),
            {"id": "unknown", "type": "unknown"},
        ]
        before = read_state(self.path)
        with self.assertRaises(RuntimeFailure):
            self.wait()
        retained = read_state(self.path)
        batch = retained.pop("orca_delivery_batch")
        self.assertEqual(retained, before)
        self.assertEqual(batch["phase"], "invalid")
        self.assertEqual(batch["members"], [])
        self.assertEqual(batch["message_count"], 2)
        with self.assertRaises(RuntimeFailure):
            self.ack()
        self.assertEqual(self.ack_calls(), [])

    def test_stale_wait_does_not_put_old_delivery_on_reassigned_role(self):
        self.messages = [self.completion("worker-a")]
        updated = read_state(self.path)
        assignment = updated["roles"]["worker-a"]
        assignment.pop("orca_result")
        assignment["dispatch_id"] = "dispatch-a-next"
        updated["tasks"]["task-a"]["dispatch_id"] = "dispatch-a-next"
        self.before_check_returns = lambda: write_state(
            self.path, updated, require_existing=True
        )
        with self.assertRaisesRegex(RuntimeFailure, "assignment changed"):
            self.wait()
        self.assertEqual(read_state(self.path), updated)

    def _assert_stale_unknown_batch_is_rejected(self, *, known_owner):
        self.messages = [self.completion("worker-a")] if known_owner else []
        self.messages.append({"id": "unknown", "type": "unknown"})
        updated = read_state(self.path)
        updated["roles"]["worker-b"]["dispatch_id"] = "dispatch-b-next"
        updated["tasks"]["task-b"]["dispatch_id"] = "dispatch-b-next"
        self.before_check_returns = lambda: write_state(
            self.path, updated, require_existing=True
        )
        with self.assertRaisesRegex(RuntimeFailure, "assignment changed"):
            self.wait()
        self.assertEqual(read_state(self.path), updated)
        self.assertEqual(self.ack_calls(), [])

    def test_unknown_only_batch_cannot_be_saved_after_peer_reassignment(self):
        self._assert_stale_unknown_batch_is_rejected(known_owner=False)

    def test_mixed_unknown_batch_cannot_be_saved_after_peer_reassignment(self):
        self._assert_stale_unknown_batch_is_rejected(known_owner=True)

    def test_known_question_reply_is_not_sent_twice(self):
        self.messages = [self.question("worker-b")]
        self.wait()
        body = json.dumps({"question_0_custom": "chosen"})
        request = MessageReply(MessageRef("msg-worker-b"), body)
        self.tasks.reply(request)
        replied = read_state(self.path)
        with self.assertRaisesRegex(RuntimeFailure, "reply is already confirmed"):
            self.tasks.reply(request)
        self.assertEqual(read_state(self.path), replied)
        self.assertEqual(
            len([call for call in self.remote_calls if call[1] == "reply"]), 1
        )
        with self.assertRaisesRegex(ValueError, "reply body changed"):
            self.tasks.reply(
                MessageReply(
                    MessageRef("msg-worker-b"),
                    json.dumps({"question_0_custom": "different"}),
                )
            )
        self.assertEqual(read_state(self.path), replied)

    def test_question_reply_receipt_identity_cannot_change(self):
        self.messages = [self.question("worker-b")]
        self.wait()
        body = json.dumps({"question_0_custom": "chosen"})
        self.tasks.reply(MessageReply(MessageRef("msg-worker-b"), body))
        state = read_state(self.path)
        assignment = state["roles"]["worker-b"]
        response = self.remote(
            state,
            ["orchestration", "reply", "--id", "msg-worker-b", "--body", body],
        )
        before = copy.deepcopy(state)
        orca_questions.accept_reply(state, assignment, response, "msg-worker-b", body)
        self.assertEqual(state, before)
        response["message"]["id"] = "answer-2"
        response["question"]["answer_message_id"] = "answer-2"
        with self.assertRaisesRegex(ValueError, "reply receipt identity changed"):
            orca_questions.accept_reply(
                state, assignment, response, "msg-worker-b", body
            )
        self.assertEqual(state, before)
