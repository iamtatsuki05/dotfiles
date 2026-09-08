from __future__ import annotations

import copy
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import test_named_native_backend as named_support

from agent_team import contracts, task_verification
from agent_team import native_backend as native
from agent_team.contracts import RuntimeFailure, TaskDispatch
from agent_team.native_mcp import NativeMcpSession


class AgentParallelNativeTest(unittest.TestCase):
    def setUp(self):
        self.named = named_support.NamedNativeBackendTest(methodName="runTest")
        self.named.setUp()
        self.addCleanup(self.named.doCleanups)
        self.spawned = set()
        native.subprocess.run(
            ("git", "init", "--quiet", str(self.named.fixture.workspace)),
            check=True,
            capture_output=True,
        )

    def start(self, *, max_active=2, empty_catalog=False):
        start = native.NativeBackend.start
        real_popen = native.subprocess.Popen

        def parallel_start(backend, spec):
            graph = replace(
                spec.graph,
                coordination=replace(
                    spec.graph.coordination,
                    dispatch_mode="parallel",
                    max_active=max_active,
                ),
            )
            if empty_catalog:
                graph = replace(graph, routes=())
            self.spec = replace(
                spec, graph=graph, task_specs=() if empty_catalog else spec.task_specs
            )
            return start(backend, self.spec)

        with mock.patch.object(native.NativeBackend, "start", parallel_start):
            self.named.start()
        self.backend = self.named.backend
        self.path = self.named.fixture.state_path
        popen = native.subprocess.Popen

        def spawn(argv, **kwargs):
            if tuple(argv[1:3]) != ("-c", native._RUNNER_GATE_SCRIPT):
                return real_popen(argv, **kwargs)
            process = popen(argv, **kwargs)
            process.pid = 77101 + len(self.spawned)
            self.spawned.add(process.pid)
            return process

        self.named.stack.enter_context(
            mock.patch.object(native.subprocess, "Popen", side_effect=spawn)
        )
        self.named.stack.enter_context(
            mock.patch.object(
                native.os,
                "getpgid",
                side_effect=lambda pid: pid if pid in self.spawned else 77001,
            )
        )

    def mcp(self):
        session = NativeMcpSession.__new__(NativeMcpSession)
        session.path = self.path
        session.run_id = native.runtime_read_state(self.path)["run_id"]
        session.runtime = self.backend.runtime
        session.backend = self.backend
        return session

    def dispatch(self, target, task):
        return self.mcp().execute(
            "task_dispatch",
            {
                "role": target.node_id,
                "task": task.as_dict(),
                "message": "担当工程を進めてください",
            },
        )

    def complete(self, target, *, decision=None):
        state = native.runtime_read_state(self.path)
        entry = state["roles"][target.node_id]
        evidence = None
        if decision is not None:
            evidence = {
                "task_id": entry["task_spec"]["task_id"],
                "stage": entry["task_stage"],
                "revision": entry["task_revision"],
                "decision": decision,
                "findings": [] if decision == "approve" else ["修正が必要"],
            }
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_completion(
                self.path,
                role=target.node_id,
                run_id=state["run_id"],
                **{
                    key: entry[key]
                    for key in (
                        "task_id",
                        "dispatch_id",
                        "terminal_handle",
                        "launch_nonce",
                    )
                },
                outcome="succeeded",
                body="担当結果",
                cleanup_confirmed=True,
                task_evidence=evidence,
            )

    def drain(self, target):
        session = self.mcp()
        wait = session.execute(
            "role_wait", {"role": target.node_id, "timeout_ms": 1000}
        )
        session.execute("role_read", {"role": target.node_id})
        session.execute("role_release", {"role": target.node_id})
        session.execute("delivery_ack", {"delivery_id": wait["delivery_id"]})

    def writers_done(self):
        self.mcp().execute("task_batch_open", {"task_ids": ["task-a", "task-b"]})
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.dispatch(self.named.worker_b, self.named.task_b)
        self.complete(self.named.worker_a)
        self.drain(self.named.worker_a)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.dispatch(self.named.reviewer_a, self.named.task_a)
        self.assertEqual(self.path.read_bytes(), before)
        self.complete(self.named.worker_b)
        self.drain(self.named.worker_b)

    def test_empty_task_catalog_is_rejected_before_main_or_runtime_effects(self):
        with self.assertRaisesRegex(RuntimeFailure, "parallel.*TaskSpecs"):
            self.start(empty_catalog=True)
        self.assertFalse(self.named.fixture.state_path.exists())
        self.assertFalse(self.spawned)

    def test_main_start_and_explicit_batch_dispatch_independent_writers(self):
        self.start()
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["version"], 5)
        self.assertIn("main_terminal", state)
        self.assertIn("main_argv", state["native"])
        main_argv = state["native"]["main_argv"]
        batch_tool = "mcp__agent_team__task_batch_open"
        self.assertIn(batch_tool, main_argv[main_argv.index("--tools") + 1].split(","))
        self.assertIn(
            batch_tool,
            main_argv[
                main_argv.index("--allowedTools") + 1 : main_argv.index(
                    "--permission-mode"
                )
            ],
        )
        self.assertNotIn("coordinator_terminal", state)
        self.assertNotIn("program_wave", state)
        self.assertNotIn("agent_batch", state)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.backend.request(
                TaskDispatch(self.named.worker_a, self.named.task_a, "work")
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.spawned)
        receipt = self.backend.request(contracts.TaskBatchOpen(("task-b", "task-a")))
        self.assertEqual(receipt.task_ids, ("task-a", "task-b"))
        for target, task in (
            (self.named.worker_a, self.named.task_a),
            (self.named.worker_b, self.named.task_b),
        ):
            self.backend.request(TaskDispatch(target, task, "work"))
        state = native.runtime_read_state(self.path)
        self.assertEqual(set(state["roles"]), {"implementation-a", "implementation-b"})
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(state["agent_batch"]["phase"], "writers")
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.backend.request(contracts.TaskBatchOpen(("task-a",)))
        self.assertEqual(self.path.read_bytes(), before)

    def test_parallel_role_prompt_rejects_without_inventing_a_task(self):
        self.start()
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.backend.request(
                contracts.RolePrompt(self.named.reviewer_a, "調査してください")
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.spawned)

    def test_rejected_reopen_preserves_peer_evidence_then_exact_batch_rereviews(self):
        self.start()
        self.writers_done()
        self.dispatch(self.named.reviewer_a, self.named.task_a)
        self.dispatch(self.named.reviewer_b, self.named.task_b)
        self.complete(self.named.reviewer_a, decision="request_changes")
        self.drain(self.named.reviewer_a)
        self.complete(self.named.reviewer_b, decision="approve")
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.dispatch(self.named.worker_a, self.named.task_a)
        self.assertEqual(self.path.read_bytes(), before)
        self.drain(self.named.reviewer_b)
        before = self.path.read_bytes()
        before_memory = copy.deepcopy(self.backend._state)
        with self.assertRaises(RuntimeFailure):
            self.dispatch(self.named.worker_b, self.named.task_a)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.backend._state, before_memory)
        with (
            mock.patch.object(
                native.native_acp_dependencies.NativeAcpExecutables,
                "from_dict",
                side_effect=ValueError("selected dependency changed"),
            ),
            self.assertRaises(RuntimeFailure),
        ):
            self.dispatch(self.named.worker_a, self.named.task_a)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.backend._state, before_memory)
        self.dispatch(self.named.worker_a, self.named.task_a)
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["agent_batch"]["phase"], "writers")
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(state["tasks"]["task-b"]["review_rounds"]["implementation"], 1)
        Path(state["workspace"], "result-a.txt").write_text(
            "revision after requested changes"
        )
        self.complete(self.named.worker_a)
        self.drain(self.named.worker_a)
        for target, task in (
            (self.named.reviewer_a, self.named.task_a),
            (self.named.reviewer_b, self.named.task_b),
        ):
            self.dispatch(target, task)
            self.complete(target, decision="approve")
            self.drain(target)
        state = native.runtime_read_state(self.path)
        self.assertEqual(
            {record["revision"] for record in state["tasks"].values()},
            {state["agent_batch"]["revision"]},
        )
        self.assertTrue(
            all(
                record["status"] == "implementation_approved"
                for record in state["tasks"].values()
            )
        )

    def test_verification_waits_for_all_reviews_and_failed_peer_reopens_completed_task(
        self,
    ):
        self.start()
        self.writers_done()
        self.dispatch(self.named.reviewer_a, self.named.task_a)
        self.complete(self.named.reviewer_a, decision="approve")
        self.drain(self.named.reviewer_a)
        before = self.path.read_bytes()
        with (
            mock.patch.object(task_verification, "run_verification") as verify,
            self.assertRaises(RuntimeFailure),
        ):
            self.mcp().execute("task_verify", {"task_id": "task-a"})
        verify.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.dispatch(self.named.reviewer_b, self.named.task_b)
        self.complete(self.named.reviewer_b, decision="approve")
        self.drain(self.named.reviewer_b)
        called = []

        def verified(task, workspace, revision):
            saved = native.runtime_read_state(self.path)
            self.assertEqual(saved["agent_batch"]["phase"], "verification")
            self.assertEqual(saved["tasks"][task.task_id]["status"], "verifying")
            self.assertEqual(revision, saved["agent_batch"]["revision"])
            self.assertEqual(Path(saved["workspace"]), workspace)
            called.append(task.task_id)
            passed = task.task_id == "task-a"
            error = None if passed else "verification reported failure"
            return {
                "revision": revision,
                "passed": passed,
                "cleanup_confirmed": True,
                "error": error,
                "commands": [
                    {
                        "name": command.name,
                        "argv": list(command.argv),
                        "timeout_seconds": command.timeout_seconds,
                        "returncode": 0 if passed else 1,
                        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                        "error": error,
                    }
                    for command in task.verification
                ],
            }

        with mock.patch.object(
            task_verification, "run_verification", side_effect=verified
        ):
            self.assertEqual(
                self.mcp().execute("task_verify", {"task_id": "task-a"})["status"],
                "completed",
            )
            self.assertEqual(
                self.mcp().execute("task_verify", {"task_id": "task-b"})["status"],
                "verification_failed",
            )
        self.assertEqual(called, ["task-a", "task-b"])
        self.dispatch(self.named.worker_b, self.named.task_b)
        saved = native.runtime_read_state(self.path)
        self.assertEqual(
            saved["tasks"]["task-a"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(saved["tasks"]["task-b"]["status"], "running")
        self.assertEqual(saved["agent_batch"]["phase"], "writers")
