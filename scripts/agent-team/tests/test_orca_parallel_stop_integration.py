from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest import mock

import test_agent_batch as batch_fixture
import test_orca_parallel_fifo as fifo_fixture

from agent_team import orca_acp, orca_parallel_stop
from agent_team.cleanup import load_cleanup_journal
from agent_team.contracts import RunRef, RuntimeFailure, StopResult
from agent_team.orca import WorkerStopContextOnlyVerdict
from agent_team.runtime import read_state, write_state


class OrcaParallelStopIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.f = fifo_fixture.OrcaParallelFifoTest(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.worker_stops = []
        self.f.backend._client.worker_stop.side_effect = self.worker_stop
        inspection = mock.patch.object(self.f.backend, "_verify_assignment")
        inspection.start()
        self.addCleanup(inspection.stop)
        final = mock.patch.object(
            self.f.backend, "_stop_locked", side_effect=self.final_cleanup
        )
        self.final = final.start()
        self.addCleanup(final.stop)

    def worker_stop(self, *, dispatch_id, cwd):
        state = read_state(self.f.path)
        owner = next(
            a for a in state["roles"].values() if a["dispatch_id"] == dispatch_id
        )
        self.assertTrue(owner["orca_result"]["cleanup_confirmed"])
        self.assertEqual(cwd, Path(state["workspace"]))
        self.worker_stops.append(dispatch_id)
        return WorkerStopContextOnlyVerdict(dispatch_id, "stopped", "none", True)

    def final_cleanup(self):
        state = read_state(self.f.path)
        self.assertNotIn("orca_delivery_batch", state)
        self.assertTrue(state["orca_stop_requested"])
        journal = load_cleanup_journal(state, self.f.path)
        self.assertTrue(
            all(
                row["remote"] == "done" and row["local"] == "done"
                for row in journal["assignments"]
            )
        )
        return StopResult(state["team_id"], RunRef(state["run_id"]))

    def suppress(self, node_id):
        state = read_state(self.f.path)
        state["orca_stop_requested"] = True
        assignment = state["roles"][node_id]
        question = assignment.get("orca_question")
        if question is not None:
            question["phase"] = "cancelling"
            question["error"] = "stop requested"
        write_state(self.f.path, state, require_existing=True)
        with mock.patch.object(orca_acp, "_send_worker_done") as send:
            orca_acp.publish_completion(
                self.f.path,
                run_id=state["run_id"],
                outcome="failed",
                body="cancelled",
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
        send.assert_not_called()

    def assert_paths(self, node_id, *, exist):
        for field in ("provider_private_root", "snapshot_root", "prompt_path"):
            self.assertEqual(
                Path(self.f.state["roles"][node_id][field]).exists(), exist
            )

    def stop(self, seconds=0):
        with mock.patch.object(orca_parallel_stop, "STOP_WAIT_SECONDS", seconds):
            return self.f.tasks.stop()

    def test_suppressed_clean_completions_reach_scoped_cleanup_and_final_stop(self):
        for role in ("worker-a", "worker-b"):
            self.suppress(role)
        result = self.stop()
        self.assertEqual(result.run_id._value, self.f.state["run_id"])
        self.assertEqual(set(self.worker_stops), {"dispatch-a", "dispatch-b"})
        self.assertEqual(len(self.f.closed), 2)
        self.final.assert_called_once()
        for role in ("worker-a", "worker-b"):
            self.assert_paths(role, exist=False)

    def test_no_result_deadline_retains_all_provider_ownership(self):
        with self.assertRaises(RuntimeFailure):
            self.stop()
        self.assertEqual(self.worker_stops, [])
        self.assertEqual(self.f.closed, [])
        self.final.assert_not_called()
        for role in ("worker-a", "worker-b"):
            self.assert_paths(role, exist=True)
        self.assertEqual(len(read_state(self.f.path)["roles"]), 2)

    def test_unanswered_batch_releases_completion_and_cancelled_question_without_ack(
        self,
    ):
        f = self.f
        f.messages = [f.completion("worker-a"), f.question("worker-b")]
        f.wait()
        self.suppress("worker-b")
        with self.assertRaises(RuntimeFailure):
            self.stop()
        state = read_state(f.path)
        self.assertEqual(state["orca_delivery_batch"]["phase"], "observed")
        self.assertEqual(
            state["roles"]["worker-b"]["orca_question"]["phase"], "cancelling"
        )
        self.assertEqual(f.ack_calls(), [])
        self.assertEqual(self.worker_stops, ["dispatch-b"])
        self.assertEqual(len(f.closed), 2)
        for role in ("worker-a", "worker-b"):
            self.assert_paths(role, exist=False)
        self.final.assert_not_called()

    def test_suppressed_peer_cleanup_continues_around_unanswered_question(self):
        f = self.f
        f.messages = [f.question("worker-b")]
        f.wait()
        self.suppress("worker-a")
        with self.assertRaises(RuntimeFailure):
            self.stop()
        self.assertEqual(self.worker_stops, ["dispatch-a"])
        self.assertEqual(len(f.closed), 1)
        self.assert_paths("worker-a", exist=False)
        self.assert_paths("worker-b", exist=True)
        self.assertEqual(f.ack_calls(), [])
        self.final.assert_not_called()

    def test_whole_batch_ack_occurs_once_after_every_read_and_release(self):
        f = self.f
        f.messages = [f.completion(role) for role in ("worker-a", "worker-b")]
        f.wait()
        self.stop()
        self.assertEqual(len(f.closed), 2)
        self.assertEqual(len(f.ack_calls()), 1)
        self.assertEqual(read_state(f.path)["roles"], {})
        self.final.assert_called_once()

    def test_stop_observes_later_suppressed_publication_without_waiting_for_a_message(
        self,
    ):
        original_sleep = orca_parallel_stop.time.sleep
        published = False

        def publish_after_fence(seconds):
            nonlocal published
            if not published:
                self.assertTrue(read_state(self.f.path)["orca_stop_requested"])
                for role in ("worker-a", "worker-b"):
                    self.suppress(role)
                published = True
            original_sleep(min(seconds, 0.001))

        with mock.patch.object(
            orca_parallel_stop.time, "sleep", side_effect=publish_after_fence
        ):
            self.stop(0.5)
        self.assertTrue(published)
        self.assertEqual(self.f.remote_calls, [])
        self.final.assert_called_once()

    def test_incomplete_verification_blocks_stop_before_any_state_or_remote_effect(
        self,
    ):
        state = copy.deepcopy(self.f.state)
        state["roles"] = {}
        for task_id in ("task-a", "task-b"):
            task = batch_fixture._task_by_id(state, task_id)
            record = batch_fixture._review_record(
                state, task, status="implementation_approved"
            )
            record.pop("verification")
            state["tasks"][task_id] = record
        state["tasks"]["task-a"]["status"] = "verifying"
        state["agent_batch"].update(
            phase="verification", revision=batch_fixture.REVISION
        )
        write_state(self.f.path, state, require_existing=True)
        before = self.f.path.read_bytes()
        with self.assertRaisesRegex(RuntimeFailure, "verification cleanup"):
            self.stop()
        self.assertEqual(self.f.path.read_bytes(), before)
        self.assertEqual(self.worker_stops, [])
        self.assertEqual(self.f.closed, [])
        self.final.assert_not_called()
