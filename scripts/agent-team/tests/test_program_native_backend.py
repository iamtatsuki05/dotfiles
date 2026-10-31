from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import test_named_native_backend as named_support

from agent_team import cli, process_identity
from agent_team import native_backend as native
from agent_team.contracts import (
    AttachCoordinator,
    CoordinatorAttachReceipt,
    DeliveryAck,
    DeliveryRef,
    ErrorCode,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    Status,
    TaskDispatch,
    TaskVerify,
)


class ProgramNativeBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.named = named_support.NamedNativeBackendTest(methodName="runTest")
        self.named.setUp()
        self.addCleanup(self.named.doCleanups)
        self.named.fixture.state_path = (
            self.named.fixture.root / "agent-team-native-test" / "state.json"
        )

    def start(self):
        try:
            spec = self.named.start(program=True)
        except RuntimeFailure as exc:
            self.fail(f"valid native program start was rejected: {exc}")
        self.backend = self.named.backend
        self.path = self.named.fixture.state_path
        return spec

    def publish_owner(self, **changes):
        state = native.runtime_read_state(self.path)
        process = {
            "supervisor_pid": 70001,
            "coordinator_pid": 61002,
            "process_group_id": 61002,
            "launch_nonce": "a" * 32,
            "phase": "running",
            **changes,
        }
        state["native"]["coordinator_process"] = process
        native.runtime_write_state(self.path, state, require_existing=True)
        return process

    def owner(self):
        stack = ExitStack()
        saved = native.runtime_read_state(self.path)
        read_argv = native.read_process_argv
        stack.enter_context(
            mock.patch.object(
                native,
                "read_process_argv",
                side_effect=lambda pid: (
                    tuple(saved["native"]["coordinator_argv"])
                    if pid == 61002
                    else tuple(saved["native"]["supervisor_argv"])
                    if pid == 70001
                    else read_argv(pid)
                ),
            )
        )
        owner_os = SimpleNamespace(**vars(native.os))
        owner_os.getpid = lambda: 61002
        owner_os.getppid = lambda: 70001
        owner_os.getpgid = lambda pid: 61002 if pid == 61002 else 77001
        stack.enter_context(mock.patch.object(native, "os", owner_os))
        return stack

    def test_program_start_has_only_coordinator_identity_and_no_main_launch(self):
        with mock.patch.object(native, "build_claude_argv") as main_launch:
            self.start()
        main_launch.assert_not_called()
        state = native.runtime_read_state(self.path)
        self.assertNotIn("main_terminal", state)
        self.assertNotIn("main_argv", state["native"])
        self.assertNotIn("main_process", state["native"])
        self.assertNotIn("lead", state["role_specs"])
        self.assertIsInstance(state["coordinator_terminal"], str)
        argv = state["native"]["coordinator_argv"]
        self.assertEqual(argv[1:4], ["-m", "agent_team", "_program-run"])
        self.assertEqual(argv[-1], state["run_id"])
        self.assertEqual(
            self.backend.last_start_response["coordinator_terminal"],
            state["coordinator_terminal"],
        )
        self.backend.request(Status())
        self.assertNotIn("main_terminal", self.backend.last_status_response)

    def test_owner_fixture_preserves_current_python_process_identity(self):
        self.start()
        self.publish_owner()
        current_pid = process_identity.os.getpid()
        launch = [process_identity.sys.executable, "-m", "agent_team", "argument"]
        expected_argv = process_identity.python_process_argv(launch)
        with self.owner():
            self.assertEqual(native.os.getpid(), 61002)
            self.assertEqual(process_identity.os.getpid(), current_pid)
            self.assertEqual(
                process_identity.python_process_argv(launch), expected_argv
            )

    def test_external_progress_operations_fail_before_effects(self):
        self.start()
        self.publish_owner()
        original = self.path.read_bytes()
        for request in (
            TaskDispatch(self.named.worker_a, self.named.task_a, "work"),
            TaskVerify("task-a"),
            RoleWait(self.named.worker_a, 0),
            RoleRead(self.named.worker_a, 100),
            RoleRelease(self.named.worker_a),
            DeliveryAck(DeliveryRef("unknown")),
        ):
            with self.subTest(request=type(request).__name__):
                with self.assertRaises(RuntimeFailure) as caught:
                    self.backend.request(request)
                self.assertEqual(caught.exception.code, ErrorCode.IDENTITY_MISMATCH)
                self.assertEqual(self.path.read_bytes(), original)

    def test_missing_receipt_wrong_parent_or_process_group_cannot_dispatch(self):
        self.start()
        for process, parent, group in (
            (None, 70001, 61002),
            ({}, 62001, 61002),
            ({}, 70001, 62002),
        ):
            with self.subTest(process=process, parent=parent, group=group):
                if process is not None:
                    self.publish_owner()
                original = self.path.read_bytes()
                with (
                    mock.patch.object(native.os, "getpid", return_value=61002),
                    mock.patch.object(native.os, "getppid", return_value=parent),
                    mock.patch.object(native.os, "getpgid", return_value=group),
                    self.assertRaises(RuntimeFailure) as caught,
                ):
                    self.backend.request(
                        TaskDispatch(self.named.worker_a, self.named.task_a, "work")
                    )
                self.assertEqual(caught.exception.code, ErrorCode.IDENTITY_MISMATCH)
                self.assertEqual(self.path.read_bytes(), original)

    def test_same_pid_under_own_supervisor_can_dispatch_but_changed_nonce_cannot(self):
        self.start()
        self.publish_owner()
        with self.owner():
            assignment = self.backend.request(
                TaskDispatch(self.named.worker_a, self.named.task_a, "work")
            )
            self.assertEqual(assignment.role, self.named.worker_a)
            self.publish_owner(launch_nonce="b" * 32)
            original = self.path.read_bytes()
            with self.assertRaises(RuntimeFailure) as caught:
                self.backend.request(RoleWait(self.named.worker_a, 0))
            self.assertEqual(caught.exception.code, ErrorCode.IDENTITY_MISMATCH)
            self.assertEqual(self.path.read_bytes(), original)

    def test_program_attach_addresses_controller_without_main_node(self):
        self.start()
        with mock.patch.object(
            native.subprocess, "run", return_value=mock.Mock(returncode=0)
        ):
            receipt = self.backend.request(AttachCoordinator())
        self.assertIsInstance(receipt, CoordinatorAttachReceipt)
        self.assertNotIn("role", self.backend.last_attach_response)

    def test_user_cli_answers_all_questions_before_coordinator_ack(self):
        self.start()
        self.publish_owner()
        with self.owner():
            self.backend.request(
                TaskDispatch(self.named.worker_b, self.named.task_b, "work")
            )
        initial = native.runtime_read_state(self.path)
        request = self.named.fixture.question_request(count=2)
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_question(
                self.path, **self.named.identity(), role_kind="worker", request=request
            )
        with self.owner():
            wait = self.backend.request(RoleWait(self.named.worker_b, 1000))
        self.backend.request(Status())
        self.assertEqual(
            len(self.backend.last_status_response["question"]["messages"]), 2
        )
        for index, event in enumerate(wait.events):
            response = cli.manage_team(
                "answer",
                cli._management_plan_from_state(native.runtime_read_state(self.path)),
                None,
                message_id=event.message_id._value,
                body=f"回答 {index + 1}",
            )
            self.assertEqual(response["status"], "answered")
            if index == 0:
                with self.owner(), self.assertRaises(RuntimeFailure):
                    self.backend.request(DeliveryAck(wait.delivery_id))
        with self.assertRaises(RuntimeFailure) as caught:
            self.backend.request(DeliveryAck(wait.delivery_id))
        self.assertEqual(caught.exception.code, ErrorCode.IDENTITY_MISMATCH)
        with self.owner():
            self.backend.request(DeliveryAck(wait.delivery_id))
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["roles"], initial["roles"])
        self.assertEqual(state["tasks"], initial["tasks"])
        self.assertNotIn("pending_delivery_id", state)
        with mock.patch.object(native, "_assert_publisher"):
            answers = native.question_answers(
                self.path, **self.named.identity(), role_kind="worker", request=request
            )
        self.assertEqual(set(answers.values()), {"回答 1", "回答 2"})

    def test_stop_and_changed_owner_between_selection_and_locked_dispatch_reject_effects(
        self,
    ):
        self.start()
        self.publish_owner()
        dispatch = self.backend._prompt
        saved_after_race = []

        def race(request):
            state = native.runtime_read_state(self.path)
            state["native"]["phase"] = "stopping"
            native.runtime_write_state(self.path, state, require_existing=True)
            saved_after_race.append(self.path.read_bytes())
            return dispatch(request)

        with (
            self.owner(),
            mock.patch.object(self.backend, "_prompt", side_effect=race),
            self.assertRaises(RuntimeFailure) as caught,
        ):
            self.backend.request(
                TaskDispatch(self.named.worker_a, self.named.task_a, "work")
            )
        self.assertEqual(caught.exception.code, ErrorCode.BUSY)
        self.assertEqual(self.path.read_bytes(), saved_after_race[0])
        state = native.runtime_read_state(self.path)
        self.assertEqual(state["roles"], {})
        self.assertEqual(state["tasks"], {})

    def test_external_stop_reclaims_program_after_supervisor_confirms_cleanup(self):
        self.start()
        self.publish_owner(phase="exited", returncode=0, group_stopped=True)
        with (
            mock.patch.object(native, "_remove_socket_root") as socket_cleanup,
            mock.patch.object(native.os, "kill") as kill,
        ):
            result = self.backend.stop()
        self.assertEqual(result.team_id, "agent-team-native-test")
        self.assertFalse(self.path.exists())
        self.assertTrue(self.backend.last_stop_response["status"], "stopped")
        socket_cleanup.assert_called_once()
        kill.assert_not_called()

    def test_exec_replacement_or_reused_parent_with_different_argv_cannot_advance(self):
        self.start()
        self.publish_owner()
        for replaced_pid in (61002, 70001):
            with self.subTest(pid=replaced_pid), self.owner():
                original_argv = native.read_process_argv
                before = self.path.read_bytes()
                with (
                    mock.patch.object(
                        native,
                        "read_process_argv",
                        side_effect=lambda pid, replaced_pid=replaced_pid, original_argv=original_argv: (
                            ("/bin/other-process",)
                            if pid == replaced_pid
                            else original_argv(pid)
                        ),
                    ),
                    self.assertRaises(RuntimeFailure) as caught,
                ):
                    self.backend.request(
                        TaskDispatch(self.named.worker_a, self.named.task_a, "work")
                    )
                self.assertEqual(caught.exception.code, ErrorCode.IDENTITY_MISMATCH)
                self.assertEqual(self.path.read_bytes(), before)

    def test_program_child_waits_for_its_supervisor_receipt_before_progress(self):
        from agent_team import native_program

        self.start()
        run_id = native.runtime_read_state(self.path)["run_id"]
        with (
            mock.patch.object(native_program.os, "getpid", return_value=61002),
            mock.patch.object(
                native_program.time,
                "sleep",
                side_effect=lambda _seconds: self.publish_owner(),
            ) as wait,
        ):
            ready = native_program._ready_state(self.path, run_id)
        wait.assert_called_once()
        self.assertEqual(
            ready["native"]["coordinator_process"]["coordinator_pid"], 61002
        )
        self.assertEqual(ready["tasks"], {})
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure) as caught:
            native_program._ready_state(self.path, "different-run")
        self.assertEqual(caught.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(self.path.read_bytes(), before)

    def test_human_consultation_reply_is_allowed_without_progress_ownership(self):
        import test_named_task_routing as routing

        from agent_team import program_driver, task_execution
        from agent_team.contracts import (
            TaskConsultationReply,
            TaskGet,
            TaskStatusReceipt,
        )

        self.start()
        self.publish_owner()
        state = native.runtime_read_state(self.path)
        for writer, task in (
            (self.named.worker_a, self.named.task_a),
            (self.named.worker_b, self.named.task_b),
        ):
            record, _ = task_execution.prepare_dispatch(
                state, TaskDispatch(writer, task, "作成")
            )
            dispatch = "write-" + task.task_id
            record["dispatch_id"] = dispatch
            task_execution.acknowledge_task(
                state, routing._result(dispatch_id=dispatch, target=writer)
            )
        task_execution.transition_program_wave(state, "seal_wave", revision="a" * 64)
        task = self.named.task_a
        reviewer = self.named.reviewer_a
        record, _ = task_execution.prepare_dispatch(
            state, TaskDispatch(reviewer, task, "レビュー"), revision="a" * 64
        )
        record["dispatch_id"] = "consult-review"
        evidence = routing._review(
            task,
            stage="implementation",
            revision="a" * 64,
            decision="consult",
            findings=["対象を確認してください"],
        )
        task_execution.acknowledge_task(
            state,
            routing._result(
                dispatch_id="consult-review", target=reviewer, evidence=evidence
            ),
        )
        native.runtime_write_state(self.path, state, require_existing=True)
        self.backend.request(Status())
        question = self.backend.last_status_response["consultations"][0]
        task_receipt = self.backend.request(TaskGet(task.task_id))
        self.assertEqual(
            task_receipt.record["consultation"]["consultation_id"],
            question["consultation_id"],
        )
        notice = program_driver._notice(
            state, reason="reviewer_consultation_required", task_id=task.task_id
        )
        self.assertEqual(
            notice["consultation"]["consultation_id"], question["consultation_id"]
        )
        self.assertIn("--consultation-id", notice["answer_command"])
        before = self.path.read_bytes()
        with mock.patch.object(self.backend, "_require_program_owner") as owner_gate:
            reply = self.backend.request(
                TaskConsultationReply(
                    question["consultation_id"], "現在の許可範囲で進めてください"
                )
            )
        owner_gate.assert_not_called()
        self.assertIsInstance(reply, TaskStatusReceipt)
        self.assertEqual(reply.status, "consultation_required")
        self.assertNotEqual(self.path.read_bytes(), before)
        after = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.backend.request(
                TaskConsultationReply(question["consultation_id"], "回答の置換")
            )
        self.assertEqual(self.path.read_bytes(), after)
        stopping = native.runtime_read_state(self.path)
        stopping["native"]["phase"] = "stopping"
        native.runtime_write_state(self.path, stopping, require_existing=True)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.backend.request(
                TaskConsultationReply(
                    question["consultation_id"], "現在の許可範囲で進めてください"
                )
            )
        self.assertEqual(self.path.read_bytes(), before)
