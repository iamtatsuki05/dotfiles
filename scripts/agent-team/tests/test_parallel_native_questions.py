from __future__ import annotations

import copy
import unittest

from agent_team import native_questions

RUN_ID = "run-parallel-1"


def _spec(kind: str) -> dict[str, str]:
    permission = "workspace-write" if kind == "worker" else "read-only"
    adapter = "claude-acp-scoped-0.70.0" if kind == "worker" else "claude-acp-0.70.0"
    return {
        "kind": kind,
        "provider": "claude",
        "transport": "acp",
        "permission": permission,
        "execution": "background",
        "adapter_id": adapter,
    }


def _assignment(
    node_id: str,
    *,
    task_id: str | None = None,
    dispatch_id: str | None = None,
    kind: str = "worker",
    question: dict[str, object] | None = None,
    result: dict[str, object] | None = None,
    receipts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    assignment: dict[str, object] = {
        "role": node_id,
        "role_kind": kind,
        "task_id": task_id or f"provider-{node_id}",
        "dispatch_id": dispatch_id or f"dispatch-{node_id}",
        "terminal_handle": f"terminal-{node_id}",
        "launch_nonce": f"nonce-{node_id}",
        "completion_observed": False,
    }
    if question is not None:
        assignment["native_question"] = question
        if question["phase"] not in {"received", "recorded"}:
            assignment.update(
                {
                    "pending_delivery_id": question["delivery_id"],
                    "pending_delivery_kind": "question",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": list(question["message_ids"]),
                    "replied_question_ids": [
                        message_id
                        for message_id in question["message_ids"]
                        if message_id in question["answers"]
                    ],
                }
            )
    if result is not None:
        assignment["native_result"] = result
    if receipts is not None:
        assignment["question_receipts"] = receipts
    return assignment


def _question(
    node_id: str,
    *,
    task_id: str | None = None,
    dispatch_id: str | None = None,
    delivery_id: str | None = None,
    message_id: str | None = None,
    phase: str = "observed",
    answer: str | None = None,
    kind: str = "worker",
    error: str | None = None,
) -> dict[str, object]:
    task = task_id or f"provider-{node_id}"
    dispatch = dispatch_id or f"dispatch-{node_id}"
    delivery = delivery_id or f"delivery-{node_id}"
    message = message_id or f"message-{node_id}"
    answers = {} if answer is None else {message: answer}
    return {
        "role": node_id,
        "role_kind": kind,
        "run_id": RUN_ID,
        "task_id": task,
        "dispatch_id": dispatch,
        "terminal_handle": f"terminal-{node_id}",
        "launch_nonce": f"nonce-{node_id}",
        "phase": phase,
        "request": {
            "kind": "question",
            "session_id": f"session-{node_id}",
            "tool_call_id": f"tool-{node_id}",
            "questions": [
                {"field": "question_0_custom", "body": f"Approve {node_id}?"}
            ],
        },
        "delivery_id": delivery,
        "message_ids": [message],
        "answers": answers,
        "error": error,
    }


def _result(
    node_id: str,
    *,
    outcome: str = "succeeded",
    task_id: str | None = None,
    dispatch_id: str | None = None,
    delivery_id: str | None = None,
    kind: str = "worker",
) -> dict[str, object]:
    return {
        "role": node_id,
        "role_kind": kind,
        "run_id": RUN_ID,
        "task_id": task_id or f"provider-{node_id}",
        "dispatch_id": dispatch_id or f"dispatch-{node_id}",
        "terminal_handle": f"terminal-{node_id}",
        "launch_nonce": f"nonce-{node_id}",
        "delivery_id": delivery_id or f"result-{node_id}",
        "outcome": outcome,
        "cleanup_confirmed": True,
        "body": "completed",
    }


def _state(*assignments: dict[str, object]) -> dict[str, object]:
    return {
        "version": 5,
        "run_id": RUN_ID,
        "native": {"phase": "running"},
        "role_specs": {
            assignment["role"]: _spec(assignment["role_kind"])
            for assignment in assignments
        },
        "roles": {assignment["role"]: assignment for assignment in assignments},
    }


def _historical_question(identity: dict[str, str]) -> dict[str, object]:
    return {
        **identity,
        "request": {
            "kind": "question",
            "session_id": "history-session",
            "tool_call_id": "history-tool",
            "questions": [{"field": "question_0_custom", "body": "Historical answer?"}],
        },
        "message_ids": ["history-message"],
        "answers": {"history-message": "yes"},
        "delivery_id": "history-delivery",
    }


def _historical_state(
    version: int, field: str
) -> tuple[dict[str, object], dict[str, str]]:
    named = version in {4, 5}
    node = "worker-a" if named else "worker"
    identity = {
        "role": node,
        "run_id": RUN_ID,
        "task_id": "old-task",
        "dispatch_id": "dispatch-old",
        "terminal_handle": "terminal-old",
        "launch_nonce": "nonce-old",
    }
    if named:
        identity["role_kind"] = "worker"
    question = _historical_question(identity)
    historical_receipt = native_questions.receipt(question, expected_identity=identity)
    historical_result: dict[str, object] = {
        **identity,
        "question_receipts": [historical_receipt],
    }
    if version == 3:
        assignment = {
            "task_id": "new-task",
            "dispatch_id": "dispatch-next",
            "terminal_handle": "terminal-next",
            "launch_nonce": "nonce-next",
            "completion_observed": False,
        }
        state: dict[str, object] = {
            "version": version,
            "run_id": RUN_ID,
            "role_specs": {node: {}},
            "roles": {node: assignment},
        }
    elif version == 4:
        assignment = {
            "role": node,
            "role_kind": "worker",
            "task_id": "new-task",
            "dispatch_id": "dispatch-next",
            "terminal_handle": "terminal-next",
            "launch_nonce": "nonce-next",
            "completion_observed": False,
        }
        state = {
            "version": version,
            "run_id": RUN_ID,
            "role_specs": {node: {"kind": "worker"}},
            "roles": {node: assignment},
        }
    else:
        assignment = _assignment(node)
        state = _state(assignment)
    state["tasks"] = {"old-task": {field: historical_result}}
    return state, identity


class ParallelNativeQuestionTest(unittest.TestCase):
    def test_two_independent_question_outboxes_are_valid(self) -> None:
        state = _state(
            _assignment("worker-a", question=_question("worker-a")),
            _assignment("worker-b", question=_question("worker-b")),
        )

        native_questions.validate_state(state)
        self.assertIs(
            native_questions.outbox(state, "worker-a"),
            state["roles"]["worker-a"]["native_question"],
        )
        self.assertIs(
            native_questions.outbox(state, "worker-b"),
            state["roles"]["worker-b"]["native_question"],
        )

    def test_answering_one_question_does_not_touch_peer_outbox(self) -> None:
        state = _state(
            _assignment("worker-a", question=_question("worker-a")),
            _assignment("worker-b", question=_question("worker-b")),
        )
        peer_before = copy.deepcopy(state["roles"]["worker-b"])
        question = native_questions.outbox(state, "worker-a")
        assert question is not None
        question["answers"] = {"message-worker-a": "yes"}
        state["roles"]["worker-a"]["replied_question_ids"] = ["message-worker-a"]

        native_questions.validate_state(state)
        self.assertEqual(state["roles"]["worker-b"], peer_before)

    def test_question_target_identity_must_match_selected_node(self) -> None:
        state = _state(
            _assignment("worker-a", question=_question("worker-b")),
            _assignment("worker-b", question=_question("worker-b")),
        )

        with self.assertRaisesRegex(ValueError, "target node identity"):
            native_questions.outbox(state, "worker-a")
        with self.assertRaises(ValueError):
            native_questions.validate_state(state)

    def test_question_identity_fields_must_match_assignment(self) -> None:
        for field in (
            "task_id",
            "dispatch_id",
            "terminal_handle",
            "launch_nonce",
        ):
            with self.subTest(field=field):
                question = _question("worker-a")
                question[field] = f"forged-{field}"
                state = _state(_assignment("worker-a", question=question))
                with self.assertRaises(ValueError):
                    native_questions.validate_state(state)

    def test_result_target_identity_must_match_selected_node(self) -> None:
        native_questions.validate_state(
            _state(_assignment("worker-a", result=_result("worker-a")))
        )
        state = _state(
            _assignment("worker-a", result=_result("worker-b")),
            _assignment("worker-b"),
        )

        with self.assertRaises(ValueError):
            native_questions.validate_state(state)

    def test_root_and_nested_mixed_delivery_versions_are_rejected(self) -> None:
        root_mixed = _state(_assignment("worker-a"))
        root_mixed["native_question"] = None
        with self.assertRaises(ValueError):
            native_questions.validate_state(root_mixed)

        nested_mixed = _state(_assignment("worker-a"))
        nested_mixed["roles"]["worker-a"]["pending_delivery"] = {
            "delivery_id": "legacy-delivery"
        }
        with self.assertRaises(ValueError):
            native_questions.validate_state(nested_mixed)

        legacy_nested = {
            "version": 4,
            "run_id": RUN_ID,
            "native": {"phase": "running"},
            "role_specs": {"worker-a": _spec("worker")},
            "roles": {
                "worker-a": {
                    **_assignment("worker-a"),
                    "native_question": {},
                }
            },
        }
        with self.assertRaises(ValueError):
            native_questions.validate_state(legacy_nested)

    def test_duplicate_current_question_delivery_and_message_ids_are_rejected(
        self,
    ) -> None:
        duplicate_delivery = _state(
            _assignment(
                "worker-a",
                question=_question("worker-a", delivery_id="same-delivery"),
            ),
            _assignment(
                "worker-b",
                question=_question("worker-b", delivery_id="same-delivery"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "delivery identity"):
            native_questions.validate_state(duplicate_delivery)

        duplicate_message = _state(
            _assignment(
                "worker-a",
                question=_question("worker-a", message_id="same-message"),
            ),
            _assignment(
                "worker-b",
                question=_question("worker-b", message_id="same-message"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "message identity"):
            native_questions.validate_state(duplicate_message)

    def test_independent_received_question_receipts_stay_bound_to_each_assignment(
        self,
    ) -> None:
        question_a = _question("worker-a", answer="yes", phase="received")
        question_b = _question("worker-b", answer="no", phase="received")
        identity_a = {
            key: question_a[key] for key in native_questions.NAMED_IDENTITY_FIELDS
        }
        identity_b = {
            key: question_b[key] for key in native_questions.NAMED_IDENTITY_FIELDS
        }
        state = _state(
            _assignment(
                "worker-a",
                question=question_a,
                receipts=[
                    native_questions.receipt(question_a, expected_identity=identity_a)
                ],
            ),
            _assignment(
                "worker-b",
                question=question_b,
                receipts=[
                    native_questions.receipt(question_b, expected_identity=identity_b)
                ],
            ),
        )

        native_questions.validate_state(state)
        self.assertEqual(
            state["roles"]["worker-a"]["question_receipts"][0]["role"],
            "worker-a",
        )
        self.assertEqual(
            state["roles"]["worker-b"]["question_receipts"][0]["role"],
            "worker-b",
        )

    def test_failed_cancellation_may_coexist_with_question_but_success_may_not(
        self,
    ) -> None:
        cancelling = _question("worker-a", phase="cancelling")
        accepted = _state(
            _assignment(
                "worker-a",
                question=cancelling,
                result=_result("worker-a", outcome="failed"),
            )
        )
        accepted["native"] = {"phase": "stopping"}
        native_questions.validate_state(accepted)

        rejected = _state(
            _assignment(
                "worker-a",
                question=_question("worker-a"),
                result=_result("worker-a", outcome="succeeded"),
            )
        )
        with self.assertRaisesRegex(ValueError, "coexist"):
            native_questions.validate_state(rejected)

    def test_historical_question_receipts_bind_to_their_stored_result_identity(
        self,
    ) -> None:
        for version in (3, 4, 5):
            for field in ("result", "writer_result", "review_result"):
                with self.subTest(version=version, field=field):
                    state, _identity = _historical_state(version, field)
                    native_questions.validate_state(state)

    def test_historical_question_receipts_reject_cross_run_and_identity_mismatch(
        self,
    ) -> None:
        for version in (3, 4, 5):
            with self.subTest(version=version, failure="cross-run"):
                state, _identity = _historical_state(version, "result")
                result = state["tasks"]["old-task"]["result"]
                result["run_id"] = "different-run"
                with self.assertRaises(ValueError):
                    native_questions.validate_state(state)

            with self.subTest(version=version, failure="receipt-identity"):
                state, _identity = _historical_state(version, "result")
                receipt = state["tasks"]["old-task"]["result"]["question_receipts"][0]
                receipt["dispatch_id"] = "wrong-history-dispatch"
                with self.assertRaises(ValueError):
                    native_questions.validate_state(state)


if __name__ == "__main__":
    unittest.main()
