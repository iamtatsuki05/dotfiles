from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import test_orca_tasks as task_fixture

from agent_team import orca_questions, orca_tasks
from agent_team.cleanup import (
    cleanup_journal_path,
    load_cleanup_journal,
    write_cleanup_journal,
)
from agent_team.contracts import RoleWait, RuntimeFailure
from agent_team.locking import _LifecycleReservation
from agent_team.native_question_channel import QuestionField, QuestionRequest
from agent_team.orca import (
    TerminalCloseVerdict,
    WorkerStopContextOnlyVerdict,
    WorkerStopOwnedVerdict,
)
from agent_team.runtime import read_state, write_state


class NamedOrcaStopTest(unittest.TestCase):
    def setUp(self):
        self.fixture = task_fixture.OrcaTasksTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = self.fixture.backend
        self.path = self.fixture.path
        self.client = self.fixture.fixture.client
        self.effects = []

        def terminal_show(*, terminal_id, cwd):
            role = "main" if terminal_id == "term_main" else "worker-a"
            return {
                "terminal": {
                    "handle": terminal_id,
                    "worktreeId": "repo::project",
                    "title": f"team-project-{role}",
                    "worktreePath": str(cwd),
                }
            }

        def terminal_close(*, terminal_id, cwd):
            del cwd
            self.effects.append(("terminal-close", terminal_id))
            return TerminalCloseVerdict(terminal_id, "tab", True, "exited")

        def worker_stop(*, dispatch_id, cwd):
            del cwd
            result = read_state(self.path)["orca_result"]
            self.assertTrue(result["cleanup_confirmed"])
            self.assertTrue(read_state(self.path)["orca_stop_requested"])
            self.effects.append(("worker-stop", dispatch_id))
            return WorkerStopContextOnlyVerdict(
                dispatch_id=dispatch_id,
                state="stopped",
                process_action="none",
                already_settled=False,
            )

        self.client.terminal_show = terminal_show
        self.client.terminal_close = terminal_close
        self.client.worker_stop = worker_stop

    def test_stop_drains_existing_completion_before_main_close(self):
        self.fixture.publish()
        self.assertTrue(read_state(self.path)["orca_result"]["notification_expected"])
        self.backend.stop()
        self.assertEqual(
            self.fixture.remote_calls,
            ["check", "worker-read", "worker-release", "check"],
        )
        self.assertEqual(
            self.effects,
            [("terminal-close", "term_planner"), ("terminal-close", "term_main")],
        )
        self.assertFalse(self.path.parent.exists())

    def test_running_stop_releases_lock_for_failed_publication(self):
        def publish_after_stop(_seconds):
            lock = _LifecycleReservation(self.path)
            lock.acquire()
            lock.release()
            self.assertTrue(read_state(self.path)["orca_stop_requested"])
            with self.assertRaises(RuntimeFailure):
                self.backend.request(RoleWait(self.fixture.role, 1000))
            self.fixture.publish(outcome="failed")
            self.assertFalse(
                read_state(self.path)["orca_result"]["notification_expected"]
            )

        with mock.patch.object(
            orca_tasks.time, "sleep", side_effect=publish_after_stop
        ):
            self.backend.stop()
        self.assertEqual(self.fixture.remote_calls, [])
        self.assertEqual(
            self.effects,
            [
                ("worker-stop", "dispatch_1"),
                ("terminal-close", "term_planner"),
                ("terminal-close", "term_main"),
            ],
        )
        self.assertFalse(self.path.parent.exists())
        for field in ("prompt_path", "provider_private_root", "snapshot_root"):
            self.assertFalse(Path(self.fixture.assignment[field]).exists())

    def test_unknown_provider_cleanup_retains_state_without_remote_stop(self):
        self.fixture.publish(cleanup=False)
        with self.assertRaisesRegex(RuntimeFailure, "cleanup.*unconfirmed"):
            self.backend.stop()
        self.assertEqual(self.effects, [])
        self.assertTrue(self.path.exists())

    def test_missing_completion_times_out_without_closing_any_terminal(self):
        with (
            mock.patch.object(orca_tasks, "STOP_WAIT_SECONDS", 0),
            self.assertRaisesRegex(RuntimeFailure, "cleanup.*unconfirmed"),
        ):
            self.backend.stop()
        self.assertTrue(read_state(self.path)["orca_stop_requested"])
        self.assertEqual(self.effects, [])

    def test_retry_after_local_release_failure_uses_saved_close_receipt(self):
        self.fixture.publish()
        with (
            mock.patch.object(
                orca_tasks, "remove_owned_tree", side_effect=OSError("local cleanup")
            ),
            self.assertRaises(OSError),
        ):
            self.backend.stop()
        self.assertEqual(read_state(self.path)["orca_release"]["phase"], "closed")
        self.backend.stop()
        self.assertEqual(
            self.effects,
            [
                ("terminal-close", "term_planner"),
                ("terminal-close", "term_main"),
            ],
        )
        self.assertFalse(self.path.exists())

    def test_retry_after_missing_remote_notification_still_requires_real_delivery(self):
        self.fixture.publish()
        with (
            mock.patch.object(orca_tasks, "STOP_WAIT_SECONDS", 0),
            mock.patch.object(
                orca_tasks.remote, "run_orca", return_value={"messages": []}
            ),
            self.assertRaisesRegex(RuntimeFailure, "Delivery.*unconfirmed"),
        ):
            self.backend.stop()
        self.assertTrue(read_state(self.path)["orca_result"]["notification_expected"])
        self.assertEqual(self.effects, [])
        self.backend.stop()
        self.assertFalse(self.path.exists())

    def test_retry_after_prompt_unlink_and_state_publish_failure(self):
        self.fixture.publish()
        original = orca_tasks.write_state

        def fail_released(path, state, **kwargs):
            if state.get("pending_delivery_stage") == "released":
                raise OSError("state publication failed")
            return original(path, state, **kwargs)

        with (
            mock.patch.object(orca_tasks, "write_state", side_effect=fail_released),
            self.assertRaises(OSError),
        ):
            self.backend.stop()
        self.assertEqual(read_state(self.path)["orca_release"]["phase"], "closed")
        self.assertFalse(Path(self.fixture.assignment["prompt_path"]).exists())
        self.backend.stop()
        self.assertEqual(
            self.effects,
            [
                ("terminal-close", "term_planner"),
                ("terminal-close", "term_main"),
            ],
        )
        self.assertFalse(self.path.parent.exists())

    def test_delayed_completion_is_drained_within_stop_budget(self):
        self.fixture.publish()
        original = self.fixture.remote
        checks = 0

        def delayed(state, args, **kwargs):
            nonlocal checks
            if "--wait" in args:
                checks += 1
                if checks == 1:
                    return {"messages": []}
            return original(state, args, **kwargs)

        with mock.patch.object(orca_tasks.remote, "run_orca", side_effect=delayed):
            self.backend.stop()
        self.assertEqual(checks, 2)
        self.assertFalse(self.path.parent.exists())

    def test_unknown_journal_fails_before_stop_flag_or_remote_effect(self):
        journal = load_cleanup_journal(read_state(self.path), self.path)
        journal["assignments"][0]["remote"] = "unknown"
        write_cleanup_journal(cleanup_journal_path(self.path), journal)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeFailure, "cleanup.*unconfirmed"):
            self.backend.stop()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.effects, [])

    def _stop_failed_question(self, phase):
        state = read_state(self.path)
        assignment = state["roles"][self.fixture.role.node_id]
        request = QuestionRequest(
            "acp-session", "call-1", (QuestionField("question_0_custom", "Question?"),)
        )
        question = orca_questions._begin_in_state(state, assignment, request)
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
            "question-delivery",
        )
        question.update(phase=phase, error="interrupted question")
        write_state(self.path, state, require_existing=True)

        def publish(_seconds):
            self.fixture.publish(outcome="failed")
            current = read_state(self.path)
            self.assertEqual(current["pending_delivery_id"], "question-delivery")
            self.assertFalse(current["orca_result"]["notification_expected"])

        with mock.patch.object(orca_tasks.time, "sleep", side_effect=publish):
            self.backend.stop()
        self.assertEqual(self.fixture.remote_calls, [])
        self.assertEqual(self.effects[0], ("worker-stop", "dispatch_1"))
        self.assertFalse(self.path.parent.exists())

    def test_failed_question_stops_without_synthetic_reply_or_ack(self):
        self._stop_failed_question("failed")

    def test_cancelling_question_stops_without_synthetic_reply_or_ack(self):
        self._stop_failed_question("cancelling")

    def test_unexpected_supervised_stop_receipt_retains_unknown_journal(self):
        state = read_state(self.path)
        state["orca_stop_requested"] = True
        write_state(self.path, state, require_existing=True)
        self.fixture.publish(outcome="failed")
        self.client.worker_stop = lambda **kwargs: WorkerStopOwnedVerdict(
            dispatch_id=kwargs["dispatch_id"],
            state="stopped",
            process_action="closed_agent_terminal",
            already_settled=False,
            pty_killed=True,
        )
        with self.assertRaisesRegex(RuntimeFailure, "effect is unknown"):
            self.backend.stop()
        journal = load_cleanup_journal(read_state(self.path), self.path)
        self.assertEqual(journal["assignments"][0]["remote"], "unknown")
        self.assertEqual(self.effects, [])

    def test_closed_release_retry_keeps_symlink_target_untouched(self):
        self.fixture.publish()
        with (
            mock.patch.object(
                orca_tasks, "remove_owned_tree", side_effect=OSError("local cleanup")
            ),
            self.assertRaises(OSError),
        ):
            self.backend.stop()
        prompt = Path(self.fixture.assignment["prompt_path"])
        target = self.fixture.fixture.root / "preserve.txt"
        target.write_text("preserve", encoding="utf-8")
        prompt.unlink()
        prompt.symlink_to(target)
        with self.assertRaises(RuntimeFailure):
            self.backend.stop()
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve")
        self.assertTrue(prompt.is_symlink())
        self.assertTrue(self.path.exists())
        self.assertEqual(self.effects, [("terminal-close", "term_planner")])


if __name__ == "__main__":
    unittest.main()
