from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest import mock

import test_native_backend as fixture_support

from agent_team import cli, contracts, native_question_channel, tmux_backend
from agent_team import native_backend as native
from agent_team.adapters import ProcessResult
from agent_team.contracts import (
    DeliveryAck,
    MessageReply,
    Role,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    TaskDispatch,
)
from agent_team.runtime_mcp import RuntimeMcpSession


class NamedNativeBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_support.NativeBackendTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def start(self, *, program=False):
        from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute

        node = contracts.NodeRef
        self.main = node("lead", Role.MAIN)
        self.worker_a = node("implementation-a", Role.WORKER)
        self.worker_b = node("implementation-b", Role.WORKER)
        self.reviewer_a = node("review-a", Role.REVIEWER)
        self.reviewer_b = node("review-b", Role.REVIEWER)
        self.task_a = replace(
            self.fixture.task_spec(), task_id="task-a", allowed_paths=("result-a.txt",)
        )
        self.task_b = replace(
            self.fixture.task_spec(), task_id="task-b", allowed_paths=("result-b.txt",)
        )
        graph = GraphSpec(
            (self.main, self.worker_a, self.worker_b, self.reviewer_a, self.reviewer_b),
            (
                GraphEdge("lead", "implementation-a", "delegates-to"),
                GraphEdge("lead", "implementation-b", "delegates-to"),
                GraphEdge("implementation-a", "review-a", "reviewed-by"),
                GraphEdge("implementation-b", "review-b", "reviewed-by"),
            ),
            Coordination("agent", ("lead",), "serial", 1),
            (
                TaskRoute("task-a", None, None, "implementation-a", "review-a"),
                TaskRoute("task-b", None, None, "implementation-b", "review-b"),
            ),
        )
        executables = fixture_support.FakeExecutables()
        specs = {self.main: fixture_support.role_spec(Role.MAIN)}
        for selected in (
            self.worker_a,
            self.worker_b,
            self.reviewer_a,
            self.reviewer_b,
        ):
            worker = selected.kind is Role.WORKER
            specs[selected] = replace(
                fixture_support.role_spec(
                    selected.kind,
                    transport="acp",
                    permission="workspace-write" if worker else "read-only",
                    execution="background",
                    executables=executables.as_dict(),
                ),
                model="claude-" + selected.node_id,
                instructions=selected.node_id + " instructions",
                adapter_id="claude-acp-scoped-0.70.0"
                if worker
                else "claude-acp-0.70.0",
            )
        if program:
            graph = replace(
                graph,
                nodes=tuple(node for node in graph.nodes if node != self.main),
                edges=tuple(edge for edge in graph.edges if edge.source != "lead"),
                coordination=Coordination(
                    "program", ("implementation-a", "implementation-b"), "serial", 1
                ),
            )
            del specs[self.main]
        spec = replace(
            self.fixture.spec(),
            role_specs=specs,
            task_specs=(self.task_a, self.task_b),
            graph=graph,
        )
        snapshot = {
            "adapter_id": "claude-acp-0.70.0",
            "revision": "@agentclientprotocol/sdk@1.3.0",
            "executable": str(executables.sdk),
            "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "c" * 64,
            },
        }

        def popen(argv, **kwargs):
            process = fixture_support.FakePopen(argv, **kwargs)
            self.stack.callback(process.wait)
            return process

        for patch in (
            mock.patch.object(
                tmux_backend, "TmuxDriver", fixture_support.FakeTmuxDriver
            ),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(
                native, "build_acp_agent_command", return_value="fixture-agent"
            ),
            mock.patch.object(
                native.native_acp_dependencies.NativeAcpExecutables,
                "from_dict",
                return_value=executables,
            ),
            mock.patch.object(
                native.native_acp_dependencies,
                "adapter_snapshot",
                return_value=snapshot,
            ),
            mock.patch.object(native.subprocess, "Popen", side_effect=popen),
            mock.patch.object(native.os, "getpgid", return_value=77_001),
        ):
            self.stack.enter_context(patch)
        self.backend = self.fixture.backend(spec)
        self.backend.start(spec)
        return spec

    def identity(self):
        state = native.runtime_read_state(self.fixture.state_path)
        assignment = state["roles"]["implementation-b"]
        return {
            "role": "implementation-b",
            "run_id": state["run_id"],
            **{
                key: assignment[key]
                for key in ("task_id", "dispatch_id", "terminal_handle", "launch_nonce")
            },
        }

    def test_selected_node_specs_and_assignment_are_distinct(self) -> None:
        self.start()
        state = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(state["version"], 4)
        self.assertEqual(
            set(state["role_specs"]),
            {"lead", "implementation-a", "implementation-b", "review-a", "review-b"},
        )
        self.assertEqual(state["role_specs"]["implementation-b"]["kind"], "worker")
        self.assertEqual(
            state["role_specs"]["implementation-b"]["model"], "claude-implementation-b"
        )
        result = self.backend.request(TaskDispatch(self.worker_b, self.task_b, "work"))
        self.assertEqual(
            result.role, contracts.NodeRef("implementation-b", Role.WORKER)
        )
        state = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(set(state["roles"]), {"implementation-b"})
        self.assertEqual(state["roles"]["implementation-b"]["role_kind"], "worker")
        self.assertIn(
            "prompt-implementation-b-",
            state["roles"]["implementation-b"]["prompt_path"],
        )

    def test_wrong_writer_and_wrong_kind_leave_state_unchanged(self) -> None:
        self.start()
        before = self.fixture.state_path.read_bytes()
        for target in (
            self.worker_a,
            contracts.NodeRef("implementation-b", Role.REVIEWER),
            Role.WORKER,
        ):
            with self.subTest(target=target), self.assertRaises(RuntimeFailure):
                self.backend.request(TaskDispatch(target, self.task_b, "work"))
            self.assertEqual(self.fixture.state_path.read_bytes(), before)

    def test_running_graph_and_model_snapshot_cannot_change(self) -> None:
        self.start()
        original = native.runtime_read_state(self.fixture.state_path)
        for changed in ("graph", "model"):
            with self.subTest(changed=changed):
                state = json.loads(json.dumps(original))
                if changed == "graph":
                    state["graph"]["edges"].append(
                        {
                            "source": "implementation-b",
                            "target": "review-a",
                            "kind": "consults-to",
                        }
                    )
                else:
                    state["role_specs"]["implementation-b"]["model"] = "changed-model"
                native.runtime_write_state(self.fixture.state_path, state)
                before = self.fixture.state_path.read_bytes()
                with self.assertRaises(RuntimeFailure) as failure:
                    self.backend.request(
                        TaskDispatch(self.worker_b, self.task_b, "work")
                    )
                self.assertEqual(
                    failure.exception.code, contracts.ErrorCode.IDENTITY_MISMATCH
                )
                self.assertEqual(self.fixture.state_path.read_bytes(), before)
        native.runtime_write_state(self.fixture.state_path, original)

    def test_orca_rejects_program_contract_before_runtime_access(self) -> None:
        from agent_team.backend import OrcaBackend

        spec = self.start(program=True)
        backend = OrcaBackend(mock.Mock())
        with (
            mock.patch.object(
                backend,
                "_ensure_supported_platform",
                side_effect=AssertionError("runtime accessed"),
            ),
            self.assertRaises(RuntimeFailure) as failure,
        ):
            backend.start(spec)
        self.assertEqual(failure.exception.code, contracts.ErrorCode.INVALID_REQUEST)

    def test_named_completion_keeps_read_release_ack_order(self) -> None:
        self.start()
        self.backend.request(TaskDispatch(self.worker_b, self.task_b, "work"))
        state = native.runtime_read_state(self.fixture.state_path)
        assignment = state["roles"]["implementation-b"]
        identity = {
            "role": "implementation-b",
            "run_id": state["run_id"],
            **{
                key: assignment[key]
                for key in ("task_id", "dispatch_id", "terminal_handle", "launch_nonce")
            },
        }
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_completion(
                self.fixture.state_path,
                **identity,
                outcome="succeeded",
                body="implemented",
                cleanup_confirmed=True,
            )
        state = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(state["native_result"]["role_kind"], "worker")
        with self.assertRaises(RuntimeFailure):
            self.backend.request(RoleWait(self.worker_a, 1_000))
        wait = self.backend.request(
            RoleWait(contracts.NodeRef("implementation-b", Role.WORKER), 1_000)
        )
        with self.assertRaises(RuntimeFailure):
            self.backend.request(RoleRelease(self.worker_b))
        with self.assertRaises(RuntimeFailure):
            self.backend.request(DeliveryAck(wait.delivery_id))
        self.backend.request(RoleRead(self.worker_b, 400))
        self.backend.request(RoleRelease(self.worker_b))
        self.backend.request(DeliveryAck(wait.delivery_id))
        state = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(state["roles"], {})
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(state["tasks"]["task-b"]["writer_role"], "implementation-b")

    def test_question_roundtrip_keeps_exact_named_assignment(self) -> None:
        self.start()
        self.backend.request(TaskDispatch(self.worker_b, self.task_b, "work"))
        identity = {**self.identity(), "role_kind": "worker"}
        question = self.fixture.question_request(2)
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_question(
                self.fixture.state_path, **identity, request=question
            )
            state = native.runtime_read_state(self.fixture.state_path)
            self.assertEqual(state["native_question"]["role_kind"], "worker")
            wait = self.backend.request(RoleWait(self.worker_b, 1_000))
            self.backend.request(MessageReply(wait.events[0].message_id, "answer one"))
            with self.assertRaises(RuntimeFailure):
                self.backend.request(DeliveryAck(wait.delivery_id))
            self.backend.request(MessageReply(wait.events[1].message_id, "answer two"))
            self.backend.request(DeliveryAck(wait.delivery_id))
            answers = native.question_answers(
                self.fixture.state_path, **identity, request=question
            )
            self.assertEqual(
                answers,
                {"question_0_custom": "answer one", "question_1_custom": "answer two"},
            )
            native.confirm_question(
                self.fixture.state_path, **identity, request=question
            )
            native.record_question_sent(
                self.fixture.state_path, **identity, request=question
            )
        state = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(
            state["roles"]["implementation-b"]["dispatch_id"], identity["dispatch_id"]
        )
        receipt = state["roles"]["implementation-b"]["question_receipts"][0]
        self.assertEqual(
            (receipt["role"], receipt["role_kind"]), ("implementation-b", "worker")
        )

    def test_question_identity_requires_matching_named_kind_before_publication(self):
        self.start()
        self.backend.request(TaskDispatch(self.worker_b, self.task_b, "work"))
        identity = self.identity()
        before = self.fixture.state_path.read_bytes()
        for extra in (
            {},
            {"role_kind": "reviewer"},
            {"role_kind": "worker", "x": "y"},
            {"role": "unknown-node", "role_kind": "worker"},
        ):
            self.fixture.state_path.write_bytes(before)
            with (
                self.subTest(extra=extra),
                mock.patch.object(native, "_assert_publisher") as publisher,
            ):
                with self.assertRaises(RuntimeFailure):
                    native.publish_question(
                        self.fixture.state_path,
                        **{**identity, **extra},
                        request=self.fixture.question_request(),
                    )
                publisher.assert_not_called()
                self.assertEqual(self.fixture.state_path.read_bytes(), before)

    def test_runner_question_and_completion_use_actual_publishers(self):
        self.start()
        self.backend.request(TaskDispatch(self.worker_b, self.task_b, "work"))
        state = native.runtime_read_state(self.fixture.state_path)
        assignment = state["roles"]["implementation-b"]
        spec = state["role_specs"]["implementation-b"]
        raw = fixture_support.FakeExecutables()
        executable_type = cli.NativeAcpExecutables
        executables = executable_type(
            raw.node,
            raw.agent,
            raw.sdk,
            raw.library,
            raw.node_sha256,
            raw.agent_sha256,
            raw.sdk_sha256,
            raw.library_sha256,
        )
        request = native_question_channel.validate_question_request(
            self.fixture.question_request(2)
        )
        receipt = {
            "output": "implemented",
            "session_id": request.session_id,
            "model": spec["model"],
            "effort": spec["effort"],
            "cleanup_confirmed": True,
        }
        result_path = Path(assignment["provider_private_root"]) / "client-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "launch_nonce": assignment["launch_nonce"],
                    "receipt": receipt,
                }
            )
        )
        result_path.chmod(0o600)
        callbacks = {}
        backend = self.backend
        target = self.worker_b
        checks = self
        published = threading.Event()
        publish = native.publish_question

        def publish_and_signal(*args, **kwargs):
            publish(*args, **kwargs)
            published.set()

        class Channel:
            failure = None

            def __init__(self, _path, exchange, delivered, failed, *, recorded):
                callbacks.update(
                    exchange=exchange, delivered=delivered, recorded=recorded
                )

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Runner:
            def __init__(self, **kwargs):
                pass

            def run(self, *args, **kwargs):
                stopped = threading.Event()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    answer = pool.submit(callbacks["exchange"], request, stopped)
                    try:
                        checks.assertTrue(published.wait(2))
                        wait = backend.request(RoleWait(target, 1_000))
                        checks.assertEqual(len(wait.events), 2)
                        for index, event in enumerate(wait.events):
                            backend.request(
                                MessageReply(event.message_id, f"answer {index}")
                            )
                        backend.request(DeliveryAck(wait.delivery_id))
                        checks.assertEqual(
                            answer.result(timeout=2),
                            {
                                "question_0_custom": "answer 0",
                                "question_1_custom": "answer 1",
                            },
                        )
                    finally:
                        stopped.set()
                callbacks["delivered"](request)
                callbacks["recorded"](request)
                return ProcessResult(0, json.dumps(receipt), "")

        with (
            mock.patch.object(executable_type, "from_dict", return_value=executables),
            mock.patch.object(executable_type, "verify"),
            mock.patch.object(cli, "_validate_acp_assignment_snapshot"),
            mock.patch.object(
                cli, "acp_agent_command", return_value=assignment["agent_command"]
            ),
            mock.patch.object(cli, "client_argv", return_value=["node", "fixture"]),
            mock.patch.object(native_question_channel, "QuestionChannel", Channel),
            mock.patch.object(cli, "_NativeAcpClientRunner", Runner),
            mock.patch.object(native, "_assert_publisher"),
            mock.patch.object(
                native, "publish_question", side_effect=publish_and_signal
            ),
        ):
            result = cli._acp_run_turn(
                state=state,
                role=target.node_id,
                state_path=self.fixture.state_path,
                task_id=assignment["task_id"],
                dispatch_id=assignment["dispatch_id"],
                terminal_handle=assignment["terminal_handle"],
                prompt_path=Path(assignment["prompt_path"]),
                launch_nonce=assignment["launch_nonce"],
                cancellation=threading.Event(),
            )
        self.assertEqual(result, 0)
        saved = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(
            (saved["native_result"]["role"], saved["native_result"]["role_kind"]),
            (target.node_id, "worker"),
        )
        self.assertEqual(
            saved["native_result"]["question_receipts"][0]["role_kind"], "worker"
        )
        wait = self.backend.request(RoleWait(target, 1_000))
        self.backend.request(RoleRead(target, 400))
        self.backend.request(RoleRelease(target))
        self.backend.request(DeliveryAck(wait.delivery_id))
        saved = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(saved["roles"], {})
        self.assertNotIn("native_result", saved)
        self.assertEqual(
            saved["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )

    def test_runner_prelaunch_failure_publishes_named_cleanup_receipt(self):
        self.start()
        self.backend.request(TaskDispatch(self.worker_b, self.task_b, "work"))
        state = native.runtime_read_state(self.fixture.state_path)
        assignment = state["roles"]["implementation-b"]
        with (
            mock.patch.object(
                cli, "_acp_assignment", side_effect=cli.ConfigError("fixture invalid")
            ),
            mock.patch.object(cli, "_NativeAcpClientRunner") as client,
            mock.patch.object(native, "_assert_publisher"),
        ):
            result = cli._acp_run_turn(
                state=state,
                role=self.worker_b.node_id,
                state_path=self.fixture.state_path,
                task_id=assignment["task_id"],
                dispatch_id=assignment["dispatch_id"],
                terminal_handle=assignment["terminal_handle"],
                prompt_path=Path(assignment["prompt_path"]),
                launch_nonce=assignment["launch_nonce"],
                cancellation=threading.Event(),
            )
        self.assertEqual(result, 1)
        client.assert_not_called()
        saved = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(
            (saved["native_result"]["role"], saved["native_result"]["role_kind"]),
            (self.worker_b.node_id, "worker"),
        )
        self.assertEqual(saved["native_result"]["outcome"], "failed")
        self.assertTrue(saved["native_result"]["cleanup_confirmed"])
        wait = self.backend.request(RoleWait(self.worker_b, 1_000))
        self.backend.request(RoleRead(self.worker_b, 400))
        self.backend.request(RoleRelease(self.worker_b))
        self.backend.request(DeliveryAck(wait.delivery_id))
        saved = native.runtime_read_state(self.fixture.state_path)
        self.assertEqual(saved["roles"], {})
        self.assertNotIn("native_result", saved)
        self.assertEqual(saved["tasks"]["task-b"]["status"], "failed")

    def test_completion_kind_tampering_is_rejected_without_acknowledgement(
        self,
    ) -> None:
        self.start()
        self.backend.request(TaskDispatch(self.worker_b, self.task_b, "work"))
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_completion(
                self.fixture.state_path,
                **self.identity(),
                outcome="succeeded",
                body="implemented",
                cleanup_confirmed=True,
            )
        state = native.runtime_read_state(self.fixture.state_path)
        for field, value in (("role", "implementation-a"), ("role_kind", "reviewer")):
            with self.subTest(field=field):
                altered = json.loads(json.dumps(state))
                altered["native_result"][field] = value
                self.fixture.state_path.write_text(json.dumps(altered))
                before = self.fixture.state_path.read_bytes()
                with self.assertRaises(RuntimeFailure):
                    self.backend.request(RoleWait(self.worker_b, 1_000))
                self.assertEqual(self.fixture.state_path.read_bytes(), before)
        self.fixture.state_path.write_text(json.dumps(state))

    def test_mcp_dispatch_uses_selected_node_without_kind_alias(self) -> None:
        self.start()
        state = native.runtime_read_state(self.fixture.state_path)
        session = RuntimeMcpSession.__new__(RuntimeMcpSession)
        session.path = self.fixture.state_path
        session.run_id = state["run_id"]
        session.runtime = "tmux"
        session.backend = self.backend
        before = self.fixture.state_path.read_bytes()
        with self.assertRaises((RuntimeFailure, ValueError)):
            session.execute(
                "task_dispatch",
                {"role": "worker", "task": self.task_b.as_dict(), "message": "work"},
            )
        self.assertEqual(self.fixture.state_path.read_bytes(), before)
        receipt = session.execute(
            "task_dispatch",
            {
                "role": "implementation-b",
                "task": self.task_b.as_dict(),
                "message": "work",
            },
        )
        self.assertEqual(
            receipt["role"], {"node_id": "implementation-b", "kind": "worker"}
        )


if __name__ == "__main__":
    unittest.main()
