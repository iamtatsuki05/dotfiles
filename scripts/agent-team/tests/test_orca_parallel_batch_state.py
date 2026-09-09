from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from test_orca_parallel_state import (
    _result,
    _role_map,
    _state,
    _state_path,
)

from agent_team import orca_acp
from agent_team.native_question_channel import QuestionField, QuestionRequest
from agent_team.runtime import (
    RuntimeValidationError,
    read_state,
    resolve_state_role,
    validate_state_object,
    write_state,
)


def _messages_sha256(messages: object) -> str:
    encoded = json.dumps(
        messages,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _member(
    assignment: dict[str, object], *, message_id: str, kind: str
) -> dict[str, object]:
    return {
        "role": assignment["role"],
        "role_kind": assignment["role_kind"],
        "message_id": message_id,
        "kind": kind,
        "task_id": assignment["task_id"],
        "dispatch_id": assignment["dispatch_id"],
        "terminal_handle": assignment["terminal_handle"],
        "launch_nonce": assignment["launch_nonce"],
    }


def _batch(
    delivery_id: str, members: list[dict[str, object]], *, phase: str = "observed"
) -> dict[str, object]:
    messages = [{"id": member["message_id"]} for member in members]
    return {
        "delivery_id": delivery_id,
        "phase": phase,
        "members": members,
        "message_count": len(members),
        "messages_sha256": _messages_sha256(messages),
        "error": None,
    }


def _batch_mapping(state: dict[str, object]) -> dict[str, object]:
    value = state.get("orca_delivery_batch")
    if not isinstance(value, dict):
        raise TypeError("batch fixture is invalid")
    return value


def _batch_members(state: dict[str, object]) -> list[dict[str, object]]:
    value = _batch_mapping(state).get("members")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise TypeError("batch fixture is invalid")
    return cast(list[dict[str, object]], value)


def _attach_done(
    assignment: dict[str, object], *, delivery_id: str, message_id: str
) -> dict[str, object]:
    result = _result(assignment, delivery_id=delivery_id)
    assignment.update(
        {
            "completion_observed": True,
            "orca_result": result,
            "pending_delivery_id": delivery_id,
            "pending_delivery_kind": "worker_done",
            "pending_delivery_stage": "observed",
            "pending_question_ids": [],
            "replied_question_ids": [],
        }
    )
    return _member(assignment, message_id=message_id, kind="worker_done")


def _attach_question(
    assignment: dict[str, object],
    *,
    delivery_id: str,
    message_id: str,
    replied: bool = False,
) -> dict[str, object]:
    request = QuestionRequest(
        "session-b",
        "tool-b",
        (QuestionField("question_0_custom", "Continue?"),),
    )
    answers = {"question_0_custom": "yes"} if replied else None
    question = {
        "session_id": request.session_id,
        "tool_call_id": request.tool_call_id,
        "request": request.as_dict(),
        "message_id": message_id,
        "thread_id": message_id,
        "answer_message_id": "answer-b" if replied else None,
        "phase": "replied" if replied else "observed",
        "answers": answers,
        "answer_sha256": _messages_sha256(answers) if answers is not None else None,
        "error": None,
    }
    assignment.update(
        {
            "orca_question": question,
            "pending_delivery_id": delivery_id,
            "pending_delivery_kind": "question",
            "pending_delivery_stage": "observed",
            "pending_question_ids": [message_id],
            "replied_question_ids": [message_id] if replied else [],
        }
    )
    return _member(assignment, message_id=message_id, kind="question")


def _release_done(assignment: dict[str, object]) -> None:
    result = assignment["orca_result"]
    if not isinstance(result, dict):
        raise TypeError("completion fixture is invalid")
    assignment["pending_delivery_stage"] = "released"
    assignment["orca_release"] = {
        "phase": "released",
        "identity": {
            field: result[field]
            for field in (
                "role",
                "role_kind",
                "run_id",
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
                "delivery_id",
            )
        },
        "terminal_close": {
            "handle": assignment["terminal_handle"],
            "close_mode": "tab",
            "pty_killed": True,
            "pty_stop_verdict": "exited",
        },
    }


class OrcaParallelBatchStateTest(unittest.TestCase):
    def test_active_question_rejects_notification_result_on_same_assignment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _role_map(state)["worker-b"]
            _attach_question(
                assignment,
                delivery_id="delivery-question",
                message_id="message-question",
            )
            question = assignment["orca_question"]
            if not isinstance(question, dict):
                raise TypeError("question fixture is invalid")
            question.update(
                {
                    "phase": "asking",
                    "message_id": None,
                    "thread_id": None,
                    "answer_message_id": None,
                    "answers": None,
                    "answer_sha256": None,
                }
            )
            for field in (
                "pending_delivery_id",
                "pending_delivery_kind",
                "pending_delivery_stage",
                "pending_question_ids",
                "replied_question_ids",
            ):
                assignment.pop(field, None)
            result = _result(assignment, delivery_id="unused-delivery")
            result.pop("delivery_id")
            assignment["orca_result"] = result

            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(state), state)

    def test_question_terminal_outbox_phases_remain_valid_without_pending_delivery(
        self,
    ) -> None:
        for phase in ("acked", "received", "recorded"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = _state(root)
                assignment = _role_map(state)["worker-b"]
                _attach_question(
                    assignment,
                    delivery_id="delivery-question",
                    message_id="message-question",
                    replied=True,
                )
                question = assignment["orca_question"]
                if not isinstance(question, dict):
                    raise TypeError("question fixture is invalid")
                question["phase"] = phase
                for field in (
                    "pending_delivery_id",
                    "pending_delivery_kind",
                    "pending_delivery_stage",
                    "pending_question_ids",
                    "replied_question_ids",
                ):
                    assignment.pop(field, None)
                validate_state_object(_state_path(state), state)

    def test_one_fifo_delivery_can_own_two_completion_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = _role_map(state)
            members = [
                _attach_done(
                    roles["worker-a"],
                    delivery_id="delivery-shared",
                    message_id="message-a",
                ),
                _attach_done(
                    roles["worker-b"],
                    delivery_id="delivery-shared",
                    message_id="message-b",
                ),
            ]
            state["orca_delivery_batch"] = _batch("delivery-shared", members)

            validate_state_object(_state_path(state), state)

    def test_one_fifo_delivery_can_mix_completion_and_question_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = _role_map(state)
            members = [
                _attach_done(
                    roles["worker-a"],
                    delivery_id="delivery-mixed",
                    message_id="message-a",
                ),
                _attach_question(
                    roles["worker-b"],
                    delivery_id="delivery-mixed",
                    message_id="message-b",
                ),
            ]
            state["orca_delivery_batch"] = _batch("delivery-mixed", members)

            validate_state_object(_state_path(state), state)

    def test_acknowledging_batch_requires_every_member_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = _role_map(state)
            members = [
                _attach_done(
                    roles["worker-a"],
                    delivery_id="delivery-ack",
                    message_id="message-a",
                ),
                _attach_done(
                    roles["worker-b"],
                    delivery_id="delivery-ack",
                    message_id="message-b",
                ),
            ]
            state["orca_delivery_batch"] = _batch(
                "delivery-ack", members, phase="acknowledging"
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(state), state)

            roles["worker-a"]["pending_delivery_stage"] = "read"
            _release_done(roles["worker-b"])
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(state), state)

            _release_done(roles["worker-a"])
            validate_state_object(_state_path(state), state)

            effect_state = copy.deepcopy(state)
            effect_assignment = _role_map(effect_state)["worker-a"]
            effect_assignment["pending_orca_effect"] = {
                "operation": "ack",
                "run_id": effect_state["run_id"],
                "main_terminal": effect_state["main_terminal"],
                "delivery_id": "delivery-ack",
                "message_id": None,
                "body_sha256": None,
            }
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(effect_state), effect_state)

    def test_question_cancellation_keeps_suppressed_failed_result_with_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _role_map(state)["worker-b"]
            member = _attach_question(
                assignment,
                delivery_id="delivery-cancelled",
                message_id="message-b",
            )
            question = assignment["orca_question"]
            if not isinstance(question, dict):
                raise TypeError("question fixture is invalid")
            question["phase"] = "cancelling"
            question["error"] = "stop requested"
            state["orca_delivery_batch"] = _batch("delivery-cancelled", [member])
            path = _state_path(state)
            write_state(path, state)

            with mock.patch.object(orca_acp, "_send_worker_done") as send:
                outcome = orca_acp.publish_completion(
                    path,
                    role="worker-b",
                    role_kind="worker",
                    run_id="run-orca-parallel",
                    task_id=str(assignment["task_id"]),
                    dispatch_id=str(assignment["dispatch_id"]),
                    terminal_handle=str(assignment["terminal_handle"]),
                    launch_nonce=str(assignment["launch_nonce"]),
                    outcome="succeeded",
                    body="cancelled",
                    cleanup_confirmed=True,
                )

            self.assertEqual(outcome, "failed")
            send.assert_not_called()
            saved = read_state(path)
            saved_assignment = _role_map(saved)["worker-b"]
            result = saved_assignment.get("orca_result")
            if not isinstance(result, dict):
                raise TypeError("saved completion fixture is invalid")
            self.assertEqual(result["outcome"], "failed")
            self.assertFalse(result["notification_expected"])
            self.assertNotIn("delivery_id", result)
            self.assertEqual(
                saved_assignment["pending_delivery_id"], "delivery-cancelled"
            )
            self.assertEqual(saved_assignment["pending_delivery_kind"], "question")

    def test_suppressed_stop_result_can_coexist_with_escalation_but_not_ack(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _role_map(state)["worker-a"]
            assignment.update(
                {
                    "pending_delivery_id": "delivery-escalation",
                    "pending_delivery_kind": "escalation",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                    "orca_result": {
                        **_result(assignment, delivery_id="unused-delivery"),
                        "outcome": "failed",
                        "notification_expected": False,
                    },
                }
            )
            result = assignment["orca_result"]
            if not isinstance(result, dict):
                raise TypeError("result fixture is invalid")
            result.pop("delivery_id")
            state["orca_stop_requested"] = True
            member = _member(
                assignment, message_id="message-escalation", kind="escalation"
            )
            state["orca_delivery_batch"] = _batch("delivery-escalation", [member])
            validate_state_object(_state_path(state), state)

            acknowledging = copy.deepcopy(state)
            _batch_mapping(acknowledging)["phase"] = "acknowledging"
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(acknowledging), acknowledging)

    def test_suppressed_result_does_not_mask_an_active_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _role_map(state)["worker-b"]
            member = _attach_question(
                assignment,
                delivery_id="delivery-question",
                message_id="message-question",
            )
            assignment["orca_result"] = {
                **_result(assignment, delivery_id="unused-delivery"),
                "outcome": "failed",
                "notification_expected": False,
            }
            result = assignment["orca_result"]
            if not isinstance(result, dict):
                raise TypeError("result fixture is invalid")
            result.pop("delivery_id")
            state["orca_delivery_batch"] = _batch("delivery-question", [member])
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(state), state)

    def test_missing_batch_cannot_publish_assignment_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory))
            roles = _role_map(state)
            _attach_done(
                roles["worker-a"],
                delivery_id="delivery-missing",
                message_id="message-a",
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(state), state)

    def test_batch_rejects_stale_generation_and_reused_message_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = _role_map(state)
            members = [
                _attach_done(
                    roles["worker-a"],
                    delivery_id="delivery-reuse",
                    message_id="message-a",
                ),
                _attach_done(
                    roles["worker-b"],
                    delivery_id="delivery-reuse",
                    message_id="message-b",
                ),
            ]
            state["orca_delivery_batch"] = _batch("delivery-reuse", members)
            validate_state_object(_state_path(state), state)

            stale = copy.deepcopy(state)
            stale_members = _batch_members(stale)
            stale_members[1]["dispatch_id"] = "dispatch-stale"
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(stale), stale)

            reused = copy.deepcopy(state)
            reused_members = _batch_members(reused)
            reused_members[1]["message_id"] = "message-a"
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(reused), reused)

    def test_batch_rejects_invalid_journal_and_wrong_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = _role_map(state)
            members = [
                _attach_done(
                    roles["worker-a"],
                    delivery_id="delivery-invalid",
                    message_id="message-a",
                )
            ]
            state["orca_delivery_batch"] = _batch("delivery-invalid", members)

            invalid_effect = copy.deepcopy(state)
            _batch_members(invalid_effect)[0]["kind"] = "question"
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(invalid_effect), invalid_effect)

            extra_key = copy.deepcopy(state)
            _batch_mapping(extra_key)["extra"] = True
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(extra_key), extra_key)

            wrong_phase = copy.deepcopy(state)
            _batch_mapping(wrong_phase)["phase"] = []
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(wrong_phase), wrong_phase)

            wrong_kind = copy.deepcopy(state)
            _batch_members(wrong_kind)[0]["kind"] = []
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(wrong_kind), wrong_kind)

    def test_batch_is_rejected_on_v3_v4_and_native_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            base["orca_delivery_batch"] = _batch("delivery-generation", [])
            for version, runtime in ((3, "orca"), (4, "orca"), (5, "claude")):
                damaged = copy.deepcopy(base)
                damaged["version"] = version
                damaged["runtime"] = runtime
                with (
                    self.subTest(version=version, runtime=runtime),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(_state_path(damaged), damaged)
                with (
                    self.subTest(operation="resolve", version=version, runtime=runtime),
                    self.assertRaises(RuntimeValidationError),
                ):
                    resolve_state_role(damaged, "worker-a")


if __name__ == "__main__":
    unittest.main()
