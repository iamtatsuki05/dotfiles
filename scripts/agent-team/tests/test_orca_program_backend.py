from __future__ import annotations

import os
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

import test_named_orca_backend as named_fixture

from agent_team import backend as backend_module
from agent_team import orca_program
from agent_team.backend import OrcaBackend
from agent_team.cleanup import (
    load_cleanup_journal,
    remove_startup_recovery,
    startup_recovery_path,
)
from agent_team.contracts import (
    AttachCoordinator,
    ErrorCode,
    NodeRef,
    Role,
    RuntimeFailure,
    Status,
    TaskDispatch,
)
from agent_team.locking import _LifecycleReservation
from agent_team.orca import OrcaTransportError
from agent_team.runtime import read_state, write_state


class OrcaProgramBackendTest(unittest.TestCase):
    def setUp(self):
        self.fixture = named_fixture.NamedOrcaBackendTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        graph = self.fixture.spec.graph
        self.spec = replace(
            self.fixture.spec,
            graph=replace(
                graph,
                nodes=tuple(node for node in graph.nodes if node.kind is not Role.MAIN),
                edges=tuple(edge for edge in graph.edges if edge.source != "lead"),
                coordination=replace(
                    graph.coordination, mode="program", entry_nodes=("worker-a",)
                ),
            ),
            role_specs={
                node: spec
                for node, spec in self.fixture.spec.role_specs.items()
                if node.kind is not Role.MAIN
            },
        )
        self.backend = OrcaBackend(self.fixture.client)
        patch = mock.patch.object(
            backend_module,
            "preflight_scoped_role",
            side_effect=self.fixture.scoped_preflight,
        )
        patch.start()
        self.addCleanup(patch.stop)
        ready = mock.patch.object(orca_program, "notify_ready")
        self.ready = ready.start()
        self.addCleanup(ready.stop)

    def test_parent_releases_startup_lock_before_child_registration(self):
        original_send = self.fixture.client.terminal_send
        errors = []
        registered = threading.Event()

        def register():
            lock = _LifecycleReservation(self.spec.state_path)
            try:
                lock.acquire_for_publication()
                state = read_state(self.spec.state_path)
                self.assertEqual(state["pending_coordinator_start"]["phase"], "sent")
                self.assertTrue(startup_recovery_path(self.spec.state_path).exists())
                state["coordinator_process"] = {
                    "pid": os.getpid(),
                    "process_group_id": os.getpgrp(),
                    "launch_nonce": state["coordinator_argv"][-1],
                    "argv": state["coordinator_argv"],
                    "phase": "running",
                    "exit_code": None,
                    "cli_cleanup_confirmed": None,
                }
                write_state(
                    self.spec.state_path,
                    state,
                    require_existing=True,
                    reservation_held=True,
                )
                registered.set()
            except (
                AssertionError,
                RuntimeFailure,
                OSError,
                TypeError,
                ValueError,
                KeyError,
            ) as exc:
                errors.append(exc)
            finally:
                lock.release()

        threads = []

        def send(**kwargs):
            thread = threading.Thread(target=register)
            threads.append(thread)
            thread.start()
            return original_send(**kwargs)

        self.fixture.client.terminal_send = send
        with mock.patch.object(orca_program, "process_running", return_value=True):
            result = self.backend.start(self.spec)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(registered.is_set())
        self.assertIsNone(result.main_terminal_id)
        self.assertIsNotNone(result.coordinator_terminal_id)
        state = read_state(self.spec.state_path)
        self.assertNotIn("main_terminal", state)
        self.assertNotIn("pending_coordinator_start", state)
        self.assertFalse(startup_recovery_path(self.spec.state_path).exists())
        before = list(self.fixture.client.calls)
        with self.assertRaisesRegex(
            RuntimeFailure, "recorded Orca program coordinator"
        ):
            self.backend.request(
                TaskDispatch(
                    NodeRef("worker-a", Role.WORKER), self.spec.task_specs[0], "work"
                )
            )
        self.assertEqual(self.fixture.client.calls, before)

    def test_unknown_send_is_retained_without_a_second_launch(self):
        def send(**_kwargs):
            raise OrcaTransportError("send response lost")

        self.fixture.client.terminal_send = send
        with self.assertRaises(RuntimeFailure):
            self.backend.start(self.spec)
        state = read_state(self.spec.state_path)
        self.assertEqual(state["pending_coordinator_start"]["phase"], "send_started")
        self.assertIsNone(state["coordinator_process"])
        self.assertTrue(startup_recovery_path(self.spec.state_path).exists())
        before = list(self.fixture.client.calls)
        with self.assertRaises(RuntimeFailure):
            self.backend.start(self.spec)
        self.assertEqual(self.fixture.client.calls, before)

    def registered_state(self):
        with mock.patch.object(self.backend, "_await_program_start"):
            self.backend.start(self.spec)
        state = read_state(self.spec.state_path)
        state.pop("pending_coordinator_start")
        state["coordinator_process"] = {
            "pid": 999999999,
            "process_group_id": 999999999,
            "launch_nonce": state["coordinator_argv"][-1],
            "argv": state["coordinator_argv"],
            "phase": "exited",
            "exit_code": 0,
            "cli_cleanup_confirmed": True,
        }
        write_state(self.spec.state_path, state, require_existing=True)
        remove_startup_recovery(startup_recovery_path(self.spec.state_path))
        self.backend._state = state
        return state

    def test_public_stop_aborts_unknown_startup_without_resending(self):
        with (
            mock.patch.object(
                self.fixture.client,
                "terminal_send",
                side_effect=OrcaTransportError("send response lost"),
            ) as send,
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.start(self.spec)
        self.assertEqual(send.call_count, 1)
        self.backend = OrcaBackend(self.fixture.client, resume_existing=True)
        self.backend.start(self.spec)
        self.backend.stop()
        self.assertFalse(self.spec.state_path.parent.exists())

    def test_missing_running_coordinator_is_reported_unavailable(self):
        state = self.registered_state()
        state["coordinator_process"].update(
            phase="running", exit_code=None, cli_cleanup_confirmed=None
        )
        write_state(self.spec.state_path, state, require_existing=True)
        with mock.patch.object(orca_program, "process_running", return_value=False):
            result = self.backend.request(Status())
        self.assertEqual(result.status, "coordinator_unavailable")
        self.assertTrue(self.spec.state_path.exists())

    def test_disappeared_coordinator_without_exit_receipt_is_not_closed(self):
        state = self.registered_state()
        state["coordinator_process"].update(
            phase="running", exit_code=None, cli_cleanup_confirmed=None
        )
        write_state(self.spec.state_path, state, require_existing=True)
        before = list(self.fixture.client.calls)
        with self.assertRaisesRegex(
            RuntimeFailure, "disappeared without a cleanup receipt"
        ):
            self.backend.stop()
        self.assertTrue(read_state(self.spec.state_path)["orca_stop_requested"])
        self.assertEqual(self.fixture.client.calls, before)

    def test_stop_waits_outside_lock_for_coordinator_exit_publication(self):
        state = self.registered_state()
        state["coordinator_process"].update(
            phase="running", exit_code=None, cli_cleanup_confirmed=None
        )
        write_state(self.spec.state_path, state, require_existing=True)
        errors = []

        def publish_exit():
            try:
                deadline = time.monotonic() + 3
                while not read_state(self.spec.state_path).get("orca_stop_requested"):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                lock = _LifecycleReservation(self.spec.state_path)
                lock.acquire_for_publication()
                try:
                    current = read_state(self.spec.state_path)
                    current["coordinator_process"].update(
                        phase="exited", exit_code=1, cli_cleanup_confirmed=True
                    )
                    write_state(
                        self.spec.state_path,
                        current,
                        require_existing=True,
                        reservation_held=True,
                    )
                finally:
                    lock.release()
            except (
                AssertionError,
                RuntimeFailure,
                OSError,
                TypeError,
                ValueError,
                KeyError,
            ) as exc:
                errors.append(exc)

        thread = threading.Thread(target=publish_exit)
        thread.start()
        try:
            with mock.patch.object(
                orca_program,
                "process_running",
                side_effect=lambda saved: (
                    saved["coordinator_process"]["phase"] == "running"
                ),
            ):
                self.backend.stop()
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.spec.state_path.parent.exists())

    def test_startup_stop_waits_for_registration_lock(self):
        with mock.patch.object(self.backend, "_await_program_start"):
            self.backend.start(self.spec)
        acquire = _LifecycleReservation.acquire_for_publication
        calls = 0

        def contend(lock):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeFailure(ErrorCode.TEAM_ALREADY_RUNNING, "contention")
            return acquire(lock)

        with mock.patch.object(
            _LifecycleReservation, "acquire_for_publication", contend
        ):
            self.backend.stop()
        self.assertGreaterEqual(calls, 2)
        self.assertFalse(self.spec.state_path.parent.exists())

    def test_parent_retries_contention_before_sending_readiness(self):
        state = self.registered_state()
        state["coordinator_process"].update(
            phase="running", exit_code=None, cli_cleanup_confirmed=None
        )
        state["pending_coordinator_start"] = orca_program.startup_marker(
            state, "sent", state["coordinator_argv"][-1]
        )
        write_state(self.spec.state_path, state, require_existing=True)
        acquire = _LifecycleReservation.acquire_for_publication
        calls = 0

        def contend(lock):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeFailure(ErrorCode.TEAM_ALREADY_RUNNING, "contention")
            return acquire(lock)

        with (
            mock.patch.object(
                _LifecycleReservation, "acquire_for_publication", contend
            ),
            mock.patch.object(orca_program, "process_running", return_value=True),
        ):
            self.backend._await_program_start(self.spec)
        self.assertGreaterEqual(calls, 2)
        self.ready.assert_called_once()

    def test_unlink_then_fsync_failure_does_not_send_readiness(self):
        state = self.registered_state()
        state["coordinator_process"].update(
            phase="running", exit_code=None, cli_cleanup_confirmed=None
        )
        state["pending_coordinator_start"] = orca_program.startup_marker(
            state, "sent", state["coordinator_argv"][-1]
        )
        write_state(self.spec.state_path, state, require_existing=True)
        sidecar = startup_recovery_path(self.spec.state_path)
        sidecar.touch(mode=0o600)

        def failed_remove(path):
            path.unlink()
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "directory fsync failed"
            )

        with (
            mock.patch.object(
                backend_module, "remove_startup_recovery", side_effect=failed_remove
            ),
            mock.patch.object(orca_program, "process_running", return_value=True),
            self.assertRaisesRegex(RuntimeFailure, "directory fsync failed"),
        ):
            self.backend._await_program_start(self.spec)
        self.ready.assert_not_called()
        self.assertTrue(self.spec.state_path.exists())

    def test_management_reports_attaches_and_stops_exact_coordinator(self):
        state = self.registered_state()
        status = self.backend.request(Status())
        self.assertEqual(status.status, "coordinator_exited")
        self.assertIn("coordinator", self.backend.last_status_response)
        self.assertNotIn("main", self.backend.last_status_response)
        attached = self.backend.request(AttachCoordinator())
        self.assertEqual(attached.terminal_id._value, state["coordinator_terminal"])
        with mock.patch.object(orca_program, "process_running", return_value=False):
            self.backend.stop()
        self.assertFalse(self.spec.state_path.parent.exists())

    def test_stop_retains_state_before_closing_when_process_is_unconfirmed(self):
        self.registered_state()
        with (
            mock.patch.object(
                orca_program,
                "exit_cleanup_confirmed",
                side_effect=RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH, "program process identity changed"
                ),
            ),
            self.assertRaisesRegex(RuntimeFailure, "program process identity changed"),
        ):
            self.backend.stop()
        state = read_state(self.spec.state_path)
        self.assertTrue(state["orca_stop_requested"])
        journal = load_cleanup_journal(state, self.spec.state_path)
        self.assertEqual(journal["coordinator"], "pending")
        self.assertNotIn("main", journal)


if __name__ == "__main__":
    unittest.main()
