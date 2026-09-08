from __future__ import annotations

import copy
import unittest

import test_named_task_routing as routing
import test_program_wave as wave

from agent_team import task_execution as tasks
from agent_team.contracts import NodeRef, Role, RuntimeFailure, TaskDispatch
from agent_team.program_policy import select_action


def consultation_fixture(*, plan=False, max_rounds=3):
    state = routing._state(implementation_only=not plan, max_review_rounds=max_rounds)
    state["run_id"] = "test-run"
    task = routing._task()
    writer = NodeRef("plan-a", Role.PLANNER) if plan else NodeRef("work-a", Role.WORKER)
    reviewer = NodeRef("review-plan-a" if plan else "review-a", Role.REVIEWER)
    record, _ = tasks.prepare_dispatch(state, TaskDispatch(writer, task, "作成"))
    record["dispatch_id"] = "write-1"
    tasks.acknowledge_task(state, routing._result(dispatch_id="write-1", target=writer))
    record, _ = tasks.prepare_dispatch(
        state,
        TaskDispatch(reviewer, task, "レビュー"),
        revision=None if plan else "a" * 64,
    )
    record["dispatch_id"] = "review-1"
    verdict = routing._review(
        task,
        stage=record["stage"],
        revision=record["revision"],
        decision="consult",
        findings=["対象範囲を確認してください"],
    )
    tasks.acknowledge_task(
        state,
        routing._result(dispatch_id="review-1", target=reviewer, evidence=verdict),
    )
    tasks.validate_saved_tasks(state)
    return state, task, writer, reviewer


class TaskConsultationTest(unittest.TestCase):
    def test_answer_binds_exact_review_and_requires_writer_then_new_review(self):
        for plan in (False, True):
            with self.subTest(plan=plan):
                state, task, writer, reviewer = consultation_fixture(plan=plan)
                record = state["tasks"][task.task_id]
                question = tasks.task_consultation(state, record)
                self.assertEqual(question["findings"], ["対象範囲を確認してください"])
                self.assertFalse(question["answered"])
                before = copy.deepcopy(state)
                with self.assertRaises(RuntimeFailure):
                    tasks.prepare_dispatch(state, TaskDispatch(writer, task, "再作成"))
                self.assertEqual(state, before)
                saved = tasks.answer_task_consultation(
                    state, question["consultation_id"], "現在の許可範囲で進めてください"
                )
                self.assertEqual(saved["status"], "consultation_required")
                self.assertEqual(saved["spec"], before["tasks"][task.task_id]["spec"])
                with self.assertRaises(RuntimeFailure):
                    tasks.prepare_dispatch(
                        state, TaskDispatch(reviewer, task, "直接承認")
                    )
                wrong = NodeRef("work-b", Role.WORKER)
                with self.assertRaises(RuntimeFailure):
                    tasks.prepare_dispatch(
                        state, TaskDispatch(wrong, task, "誤った担当")
                    )
                record, prompt = tasks.prepare_dispatch(
                    state, TaskDispatch(writer, task, "再作成")
                )
                self.assertIn("現在の許可範囲で進めてください", prompt)
                self.assertEqual(
                    record["review_rounds"]["plan" if plan else "implementation"], 1
                )
                record["dispatch_id"] = "write-2"
                tasks.acknowledge_task(
                    state, routing._result(dispatch_id="write-2", target=writer)
                )
                tasks.validate_saved_tasks(state)
                record, _ = tasks.prepare_dispatch(
                    state,
                    TaskDispatch(reviewer, task, "再レビュー"),
                    revision=None if plan else "b" * 64,
                )
                self.assertNotIn("consultation_answer", record)
                record["dispatch_id"] = "review-2"
                verdict = routing._review(
                    task,
                    stage=record["stage"],
                    revision=record["revision"],
                    decision="consult",
                    findings=["追加確認"],
                )
                tasks.acknowledge_task(
                    state,
                    routing._result(
                        dispatch_id="review-2", target=reviewer, evidence=verdict
                    ),
                )
                later = tasks.task_consultation(state, state["tasks"][task.task_id])
                self.assertNotEqual(
                    later["consultation_id"], question["consultation_id"]
                )
                self.assertFalse(later["answered"])
                with self.assertRaises(RuntimeFailure):
                    tasks.answer_task_consultation(
                        state, question["consultation_id"], "古い回答"
                    )

    def test_duplicate_answer_is_idempotent_but_replacement_and_invalid_body_fail(self):
        state, task, _, _ = consultation_fixture()
        question = tasks.task_consultation(state, state["tasks"][task.task_id])
        for body in ("", "   ", "a" * 16001, "bad\x00body"):
            before = copy.deepcopy(state)
            with self.assertRaises(RuntimeFailure):
                tasks.answer_task_consultation(state, question["consultation_id"], body)
            self.assertEqual(state, before)
        tasks.answer_task_consultation(state, question["consultation_id"], "範囲を維持")
        before = copy.deepcopy(state)
        tasks.answer_task_consultation(state, question["consultation_id"], "範囲を維持")
        self.assertEqual(state, before)
        with self.assertRaises(RuntimeFailure):
            tasks.answer_task_consultation(
                state, question["consultation_id"], "回答を置換"
            )
        self.assertEqual(state, before)

    def test_run_binding_and_answer_integrity(self):
        state, task, _, _ = consultation_fixture()
        record = state["tasks"][task.task_id]
        question = tasks.task_consultation(state, record)
        other = copy.deepcopy(state)
        other["run_id"] = "another-run"
        with self.assertRaises(RuntimeFailure):
            tasks.answer_task_consultation(
                other, question["consultation_id"], "越境回答"
            )
        tasks.answer_task_consultation(state, question["consultation_id"], "範囲を維持")
        for field, value in (
            ("body", "改変"),
            ("body_sha256", "0" * 64),
            ("consultation_id", "consult-" + "0" * 64),
        ):
            bad = copy.deepcopy(state)
            bad["tasks"][task.task_id]["consultation_answer"][field] = value
            with self.assertRaises(RuntimeFailure):
                tasks.validate_saved_tasks(bad)

    def test_answer_does_not_reset_review_limit(self):
        state, task, writer, _ = consultation_fixture(max_rounds=1)
        question = tasks.task_consultation(state, state["tasks"][task.task_id])
        tasks.answer_task_consultation(state, question["consultation_id"], "範囲を維持")
        before = copy.deepcopy(state)
        with self.assertRaisesRegex(RuntimeFailure, "maximum review rounds"):
            tasks.prepare_dispatch(state, TaskDispatch(writer, task, "再作成"))
        self.assertEqual(state, before)

    def test_program_answer_reopens_wave_and_invalidates_peer_approval(self):
        state, catalog = wave.state_fixture()
        state["run_id"] = "program-run"
        for task in catalog[:2]:
            wave.write_result(state, task, "write-" + task.task_id)
        tasks.transition_program_wave(state, "seal_wave", revision="a" * 64)
        wave.review_result(state, catalog[0], "a" * 64, "review-a", "consult")
        wave.review_result(state, catalog[1], "a" * 64, "review-b")
        self.assertEqual(select_action(state).kind, "wait_user")
        question = tasks.task_consultation(state, state["tasks"]["task-a"])
        tasks.answer_task_consultation(state, question["consultation_id"], "範囲を維持")
        self.assertEqual(select_action(state).kind, "reopen_wave")
        tasks.transition_program_wave(state, "reopen_wave")
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(select_action(state).role, NodeRef("work-a", Role.WORKER))
        wave.write_result(state, catalog[0], "write-a-2")
        tasks.transition_program_wave(state, "seal_wave", revision="b" * 64)
        for task in catalog[:2]:
            wave.review_result(state, task, "b" * 64, "second-" + task.task_id)
        tasks.transition_program_wave(state, "verify_wave")
        self.assertEqual(state["program_wave"]["revision"], "b" * 64)

    def test_unanswered_peer_prevents_program_reopen(self):
        state, catalog = wave.state_fixture()
        state["run_id"] = "program-run"
        for task in catalog[:2]:
            wave.write_result(state, task, "write-" + task.task_id)
        tasks.transition_program_wave(state, "seal_wave", revision="a" * 64)
        for task in catalog[:2]:
            wave.review_result(
                state, task, "a" * 64, "review-" + task.task_id, "consult"
            )
        question = tasks.task_consultation(state, state["tasks"]["task-a"])
        tasks.answer_task_consultation(state, question["consultation_id"], "範囲を維持")
        before = copy.deepcopy(state)
        self.assertEqual(select_action(state).kind, "wait_user")
        with self.assertRaises(RuntimeFailure):
            tasks.transition_program_wave(state, "reopen_wave")
        self.assertEqual(state, before)
