from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import test_program_native_backend as program_support

from agent_team import native_backend as native
from agent_team.contracts import (
    DeliveryAck,
    ErrorCode,
    MessageReply,
    RoleGet,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    Status,
    TaskDispatch,
)


class ParallelNativeBackendTest(unittest.TestCase):
    def setUp(self):
        self.program = program_support.ProgramNativeBackendTest(methodName="runTest")
        self.program.setUp()
        self.addCleanup(self.program.doCleanups)
        self.named = self.program.named
        self.spawned = set()

    def start(self, *, max_active=2):
        start = native.NativeBackend.start

        def parallel_start(backend, spec):
            graph = replace(
                spec.graph,
                coordination=replace(
                    spec.graph.coordination,
                    dispatch_mode="parallel",
                    max_active=max_active,
                ),
            )
            self.spec = replace(spec, graph=graph)
            return start(backend, self.spec)

        with mock.patch.object(native.NativeBackend, "start", parallel_start):
            self.program.start()
        self.backend = self.program.backend
        self.path = self.program.path
        self.program.publish_owner()
        popen = native.subprocess.Popen

        def spawn(argv, **kwargs):
            process = popen(argv, **kwargs)
            process.pid = 77101 + len(self.spawned)
            self.spawned.add(process.pid)
            return process

        self.named.stack.enter_context(
            mock.patch.object(native.subprocess, "Popen", side_effect=spawn)
        )

    def owner(self):
        stack = self.program.owner()
        getpgid = native.os.getpgid
        stack.enter_context(
            mock.patch.object(
                native.os,
                "getpgid",
                side_effect=lambda pid: pid if pid in self.spawned else getpgid(pid),
            )
        )
        return stack

    def dispatch(self, target, task):
        with self.owner():
            return self.backend.request(TaskDispatch(target, task, "作成してください"))

    def complete(self, target, *, outcome="succeeded", cleanup_confirmed=True):
        state = native.runtime_read_state(self.path)
        entry = state["roles"][target.node_id]
        identity = {
            name: entry[name]
            for name in ("task_id", "dispatch_id", "terminal_handle", "launch_nonce")
        }
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_completion(
                self.path,
                role=target.node_id,
                run_id=state["run_id"],
                **identity,
                outcome=outcome,
                body="実装結果",
                cleanup_confirmed=cleanup_confirmed,
            )

    def test_disjoint_workers_keep_separate_completions_until_exact_ack(self):
        self.start()
        first = self.dispatch(self.named.worker_a, self.named.task_a)
        second = self.dispatch(self.named.worker_b, self.named.task_b)
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["version"], 5)
        self.assertEqual(set(state["roles"]), {"implementation-a", "implementation-b"})
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(len(self.spawned), 2)
        self.complete(self.named.worker_b)
        before_first = copy.deepcopy(
            native.runtime_read_state(self.path)["roles"]["implementation-a"]
        )
        with self.owner():
            wait = self.backend.request(RoleWait(self.named.worker_b, 1000))
            self.assertEqual(len(wait.events), 1)
            with self.assertRaises(RuntimeFailure):
                self.backend.request(DeliveryAck(wait.delivery_id))
            self.backend.request(RoleRead(self.named.worker_b, 100))
            self.backend.request(RoleRelease(self.named.worker_b))
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["roles"]["implementation-a"], before_first)
        self.assertIn("implementation-b", state["roles"])
        self.assertEqual(
            state["roles"]["implementation-b"]["pending_delivery_stage"], "released"
        )
        self.assertNotIn("native_result", state)
        with self.owner():
            self.backend.request(DeliveryAck(wait.delivery_id))
        state = native.runtime_read_state(self.path)
        self.assertEqual(set(state["roles"]), {"implementation-a"})
        self.assertEqual(state["roles"]["implementation-a"], before_first)
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        with self.owner(), self.assertRaises(RuntimeFailure) as failure:
            self.backend.request(DeliveryAck(wait.delivery_id))
        self.assertEqual(failure.exception.code, ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN)

    def test_completed_node_operations_keep_typed_missing_assignment_error(self):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.complete(self.named.worker_a)
        with self.owner():
            wait = self.backend.request(RoleWait(self.named.worker_a, 1000))
            self.backend.request(RoleRead(self.named.worker_a, 100))
            self.backend.request(RoleRelease(self.named.worker_a))
            self.backend.request(DeliveryAck(wait.delivery_id))
            before = self.path.read_bytes()
            for request in (
                RoleGet(self.named.worker_a),
                RoleWait(self.named.worker_a, 1000),
                RoleRead(self.named.worker_a, 100),
                RoleRelease(self.named.worker_a),
            ):
                with self.subTest(request=type(request).__name__):
                    with self.assertRaises(RuntimeFailure) as caught:
                        self.backend.request(request)
                    self.assertEqual(caught.exception.code, ErrorCode.TEAM_NOT_RUNNING)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_parallel_cap_one_waits_without_creating_second_assignment(self):
        self.start(max_active=1)
        self.dispatch(self.named.worker_a, self.named.task_a)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure) as failure:
            self.dispatch(self.named.worker_b, self.named.task_b)
        self.assertEqual(failure.exception.code, ErrorCode.BUSY)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.spawned), 1)

    def question(self, target):
        state = native.runtime_read_state(self.path)
        assignment = state["roles"][target.node_id]
        identity = {
            "role": target.node_id,
            "role_kind": target.kind.value,
            "run_id": state["run_id"],
            **{
                field: assignment[field]
                for field in (
                    "task_id",
                    "dispatch_id",
                    "terminal_handle",
                    "launch_nonce",
                )
            },
        }
        request = self.named.fixture.question_request(count=2)
        request["session_id"] = "session-" + target.node_id
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_question(self.path, **identity, request=request)
        return request, identity

    def test_questions_and_completions_are_consumed_by_their_exact_node(self):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        request_a, identity_a = self.question(self.named.worker_a)
        with self.owner():
            question_a = self.backend.request(RoleWait(self.named.worker_a, 1000))
        self.dispatch(self.named.worker_b, self.named.task_b)
        self.question(self.named.worker_b)
        with self.owner():
            question_b = self.backend.request(RoleWait(self.named.worker_b, 1000))
        before_b = copy.deepcopy(
            native.runtime_read_state(self.path)["roles"]["implementation-b"]
        )
        self.backend.request(Status())
        self.assertNotIn("question", self.backend.last_status_response)
        self.assertEqual(
            {q["role"] for q in self.backend.last_status_response["questions"]},
            {"implementation-a", "implementation-b"},
        )
        for event in question_a.events:
            self.backend.request(MessageReply(event.message_id, "A回答"))
        with self.owner():
            with self.assertRaises(RuntimeFailure):
                self.backend.request(DeliveryAck(question_b.delivery_id))
            self.backend.request(DeliveryAck(question_a.delivery_id))
        self.assertEqual(
            native.runtime_read_state(self.path)["roles"]["implementation-b"], before_b
        )
        with mock.patch.object(native, "_assert_publisher"):
            self.assertEqual(
                set(
                    native.question_answers(
                        self.path, **identity_a, request=request_a
                    ).values()
                ),
                {"A回答"},
            )
            native.confirm_question(self.path, **identity_a, request=request_a)
            native.record_question_sent(self.path, **identity_a, request=request_a)
        self.complete(self.named.worker_a)
        with self.owner():
            done = self.backend.request(RoleWait(self.named.worker_a, 1000))
            self.backend.request(RoleRead(self.named.worker_a, 100))
            self.backend.request(RoleRelease(self.named.worker_a))
            self.backend.request(DeliveryAck(done.delivery_id))
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["roles"]["implementation-b"], before_b)
        self.assertEqual(
            state["tasks"]["task-a"]["status"], "awaiting_implementation_review"
        )
        self.assertNotIn("implementation-a", state["roles"])

    def _stop_with_completion(self, stage):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.dispatch(self.named.worker_b, self.named.task_b)
        self.complete(self.named.worker_a)
        with self.owner():
            if stage in {"observed", "read", "released"}:
                self.backend.request(RoleWait(self.named.worker_a, 1000))
            if stage in {"read", "released"}:
                self.backend.request(RoleRead(self.named.worker_a, 100))
            if stage == "released":
                self.backend.request(RoleRelease(self.named.worker_a))
        self.program.publish_owner(phase="exited", returncode=0, group_stopped=True)
        steps = []
        operations = {
            name: getattr(self.backend, name)
            for name in ("_wait", "_read", "_release", "_ack")
        }

        def observe(name):
            def call(request, **kwargs):
                result = operations[name](request, **kwargs)
                steps.append((name, getattr(request, "role", None), result))
                return result

            return call

        cancel = self.backend._cancel_runner
        cancelled = []

        def cancel_b(state, role, **kwargs):
            cancelled.append(role)
            self.assertEqual(role, self.named.worker_b)
            self.complete(role, outcome="failed")
            return cancel(state, role, **kwargs)

        with (
            mock.patch.object(self.backend, "_cancel_runner", side_effect=cancel_b),
            mock.patch.object(native, "_process_group_alive", return_value=False),
            mock.patch.object(native, "_remove_socket_root"),
            mock.patch.object(self.backend, "_wait", side_effect=observe("_wait")),
            mock.patch.object(self.backend, "_read", side_effect=observe("_read")),
            mock.patch.object(
                self.backend, "_release", side_effect=observe("_release")
            ),
            mock.patch.object(self.backend, "_ack", side_effect=observe("_ack")),
            mock.patch.object(self.backend, "_task_verify") as verify,
        ):
            result = self.backend.stop()
        self.assertEqual(result.team_id, "agent-team-native-test")
        self.assertFalse(self.path.exists())
        self.assertEqual(cancelled, [self.named.worker_b])
        verify.assert_not_called()
        first_steps = [
            name for name, role, _receipt in steps if role == self.named.worker_a
        ]
        self.assertEqual(
            first_steps,
            {
                "unobserved": ["_wait", "_read", "_release"],
                "observed": ["_read", "_release"],
                "read": ["_release"],
                "released": [],
            }[stage],
        )
        self.assertEqual(sum(name == "_ack" for name, _role, _receipt in steps), 2)
        reads = [receipt.output for name, _role, receipt in steps if name == "_read"]
        self.assertTrue(all(body == "実装結果" for body in reads))

    def test_dead_coordinator_stop_drains_unobserved_result_and_cancels_peer(self):
        self._stop_with_completion("unobserved")

    def test_dead_coordinator_stop_drains_observed_result_and_cancels_peer(self):
        self._stop_with_completion("observed")

    def test_dead_coordinator_stop_releases_read_result_and_cancels_peer(self):
        self._stop_with_completion("read")

    def test_dead_coordinator_stop_only_acks_released_result_and_cancels_peer(self):
        self._stop_with_completion("released")

    def test_stop_retains_unknown_cleanup_but_drains_safe_peer_then_retries(self):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.dispatch(self.named.worker_b, self.named.task_b)
        self.complete(self.named.worker_a, cleanup_confirmed=False)
        self.complete(self.named.worker_b)
        self.program.publish_owner(phase="exited", returncode=0, group_stopped=True)
        before = native.runtime_read_state(self.path)
        with (
            mock.patch.object(native, "_process_group_alive", return_value=False),
            mock.patch.object(native, "_remove_socket_root"),
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.stop()
        state = native.runtime_read_state(self.path)
        self.assertEqual(set(state["roles"]), {"implementation-a"})
        self.assertEqual(
            state["roles"]["implementation-a"]["native_result"],
            before["roles"]["implementation-a"]["native_result"],
        )
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(state["tasks"]["task-a"]["status"], "running")
        retained = self.path.read_bytes()
        with (
            mock.patch.object(self.backend, "_cancel_runner") as cancel,
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.stop()
        cancel.assert_not_called()
        self.assertEqual(self.path.read_bytes(), retained)

    def test_stop_cancels_unanswered_question_without_fabricating_answer_or_question_ack(
        self,
    ):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.question(self.named.worker_a)
        with self.owner():
            question = self.backend.request(RoleWait(self.named.worker_a, 1000))
        self.program.publish_owner(phase="exited", returncode=0, group_stopped=True)
        cancelled_question = []
        cancel = self.backend._cancel_runner

        def publish_cancel(state, role, **kwargs):
            self.complete(role, outcome="failed")
            cancelled_question.append(
                copy.deepcopy(
                    native.runtime_read_state(self.path)["roles"][role.node_id][
                        "native_question"
                    ]
                )
            )
            return cancel(state, role, **kwargs)

        acknowledgements = []
        ack = self.backend._ack

        def record_ack(request, **kwargs):
            acknowledgements.append(request.delivery_id)
            return ack(request, **kwargs)

        with (
            mock.patch.object(
                self.backend, "_cancel_runner", side_effect=publish_cancel
            ),
            mock.patch.object(self.backend, "_ack", side_effect=record_ack),
            mock.patch.object(native, "_process_group_alive", return_value=False),
            mock.patch.object(native, "_remove_socket_root"),
        ):
            self.backend.stop()
        self.assertEqual(cancelled_question[0]["answers"], {})
        self.assertEqual(cancelled_question[0]["phase"], "cancelling")
        self.assertNotIn(question.delivery_id, acknowledgements)
        self.assertEqual(len(acknowledgements), 1)
        self.assertFalse(self.path.exists())

    def test_stop_rejects_changed_runner_argv_but_drains_peer_and_can_retry(self):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.dispatch(self.named.worker_b, self.named.task_b)
        self.complete(self.named.worker_b)
        self.program.publish_owner(phase="exited", returncode=0, group_stopped=True)
        before = native.runtime_read_state(self.path)
        first = before["roles"]["implementation-a"]
        pid = first["runner_pid"]
        self.backend._runners["implementation-a"].returncode = None
        with (
            mock.patch.object(
                native, "_process_group_alive", side_effect=lambda group: group == pid
            ),
            mock.patch.object(native.os, "getpgid", return_value=pid),
            mock.patch.object(native, "read_process_argv", return_value=("unrelated",)),
            mock.patch.object(native, "_terminate_process_group") as terminate,
            mock.patch.object(native, "_remove_socket_root"),
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.stop()
        terminate.assert_not_called()
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["roles"], {"implementation-a": first})
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        self.assertIn("argv", self.backend.last_stop_response["retained"][0]["error"])
        self.complete(self.named.worker_a, outcome="failed")
        self.backend._runners["implementation-a"].returncode = 1
        with (
            mock.patch.object(native, "_process_group_alive", return_value=False),
            mock.patch.object(native, "_remove_socket_root"),
        ):
            self.backend.stop()
        self.assertFalse(self.path.exists())
        self.assertEqual(
            [item["role"] for item in self.backend.last_stop_response["drained"]],
            ["implementation-a"],
        )

    def test_stop_preserves_local_cleanup_failure_and_retries_remaining_node_only(self):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.dispatch(self.named.worker_b, self.named.task_b)
        self.complete(self.named.worker_a)
        self.complete(self.named.worker_b)
        self.program.publish_owner(phase="exited", returncode=0, group_stopped=True)
        before = native.runtime_read_state(self.path)
        private_a = Path(before["roles"]["implementation-a"]["provider_private_root"])
        remove = native.remove_owned_tree

        def fail_one(path):
            if path == private_a:
                raise OSError("owned fixture cleanup denied")
            return remove(path)

        with (
            mock.patch.object(native, "remove_owned_tree", side_effect=fail_one),
            mock.patch.object(native, "_remove_socket_root"),
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.stop()
        state = native.runtime_read_state(self.path)
        self.assertEqual(set(state["roles"]), {"implementation-a"})
        self.assertEqual(
            state["roles"]["implementation-a"]["pending_delivery_stage"], "read"
        )
        self.assertTrue(private_a.exists())
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )
        self.assertEqual(state["tasks"]["task-a"]["status"], "running")
        with mock.patch.object(native, "_remove_socket_root"):
            self.backend.stop()
        self.assertFalse(private_a.exists())
        self.assertFalse(self.path.exists())
        self.assertEqual(
            [item["role"] for item in self.backend.last_stop_response["drained"]],
            ["implementation-a"],
        )

    def _assert_stop_retains_changed_argv(self, *, released):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        self.dispatch(self.named.worker_b, self.named.task_b)
        self.complete(self.named.worker_a)
        self.complete(self.named.worker_b)
        if released:
            with self.owner():
                self.backend.request(RoleWait(self.named.worker_a, 1000))
                self.backend.request(RoleRead(self.named.worker_a, 100))
                self.backend.request(RoleRelease(self.named.worker_a))
        self.program.publish_owner(phase="exited", returncode=0, group_stopped=True)
        state = native.runtime_read_state(self.path)
        state["roles"]["implementation-a"]["runner_argv"] = ["/abs/unrelated"]
        native.runtime_write_state(self.path, state, require_existing=True)
        retained = copy.deepcopy(state["roles"]["implementation-a"])
        with (
            mock.patch.object(native, "_remove_socket_root"),
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.stop()
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["roles"], {"implementation-a": retained})
        self.assertEqual(state["tasks"]["task-a"]["status"], "running")
        self.assertEqual(
            state["tasks"]["task-b"]["status"], "awaiting_implementation_review"
        )

    def test_stop_retains_released_assignment_with_changed_saved_argv(self):
        self._assert_stop_retains_changed_argv(released=True)

    def test_stop_does_not_observe_result_when_saved_runner_argv_changed(self):
        self._assert_stop_retains_changed_argv(released=False)

    def test_stop_and_late_publication_preserve_failed_question_diagnostics(self):
        self.start()
        self.dispatch(self.named.worker_a, self.named.task_a)
        request, identity = self.question(self.named.worker_a)
        with mock.patch.object(native, "_assert_publisher"):
            native.fail_question(self.path, **identity, request=request)
        question = native.runtime_read_state(self.path)["roles"]["implementation-a"][
            "native_question"
        ]
        self.assertEqual(question["phase"], "failed")
        self.assertIsInstance(question["error"], str)
        self.program.publish_owner(phase="exited", returncode=0, group_stopped=True)
        with (
            mock.patch.object(
                self.backend,
                "_cancel_runner",
                side_effect=RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE, "unknown cleanup"
                ),
            ),
            mock.patch.object(native, "_remove_socket_root"),
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.stop()
        self.assertEqual(
            native.runtime_read_state(self.path)["roles"]["implementation-a"][
                "native_question"
            ],
            question,
        )
        self.complete(self.named.worker_a, outcome="failed", cleanup_confirmed=False)
        self.assertEqual(
            native.runtime_read_state(self.path)["roles"]["implementation-a"][
                "native_question"
            ],
            question,
        )
        with mock.patch.object(native, "_assert_publisher"):
            native.fail_question(self.path, **identity, request=request)
        self.assertEqual(
            native.runtime_read_state(self.path)["roles"]["implementation-a"][
                "native_question"
            ],
            question,
        )
