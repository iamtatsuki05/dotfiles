from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_orca_program_state as state_fixture

from agent_team import orca_program
from agent_team.cleanup import startup_recovery_path
from agent_team.contracts import RuntimeFailure
from agent_team.locking import _LifecycleReservation
from agent_team.process_identity import read_process_argv
from agent_team.runtime import read_state, write_state


class OrcaProgramProcessTest(unittest.TestCase):
    def state(self, root: Path, version: int = 4) -> dict[str, object]:
        state = state_fixture._state(root, version=version)
        path = Path(str(state["state_path"]))
        state["coordinator_argv"] = orca_program.launch_argv(path, "run-1", "nonce1234")
        state["pending_coordinator_start"] = orca_program.startup_marker(
            state, "sent", "nonce1234"
        )
        write_state(path, state)
        return state

    def test_real_child_registers_waits_for_parent_and_records_exit(self):
        for version, ready in ((4, False), (5, False), (4, True), (5, True)):
            with (
                self.subTest(version=version, ready=ready),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = self.state(Path(directory), version)
                path = Path(str(state["state_path"]))
                child = subprocess.Popen(
                    state["coordinator_argv"],
                    cwd=directory,
                    env={
                        **orca_program.launch_environment(),
                        "PATH": str(Path(sys.executable).parent),
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    process = None
                    while time.monotonic() < deadline:
                        saved = read_state(path)
                        process = saved.get("coordinator_process")
                        if process is not None or child.poll() is not None:
                            break
                        time.sleep(0.02)
                    self.assertIsNotNone(process)
                    self.assertEqual(process["pid"], child.pid)
                    self.assertEqual(process["process_group_id"], child.pid)
                    self.assertEqual(
                        process["argv"], list(read_process_argv(child.pid))
                    )
                    self.assertEqual(process["phase"], "running")
                    self.assertTrue(orca_program.process_running(saved))
                    self.assertIn("pending_coordinator_start", saved)
                    lock = _LifecycleReservation(path)
                    lock.acquire_for_publication()
                    try:
                        saved = read_state(path)
                        if ready:
                            saved.pop("pending_coordinator_start")
                        else:
                            saved["orca_stop_requested"] = True
                        write_state(
                            path, saved, require_existing=True, reservation_held=True
                        )
                    finally:
                        lock.release()
                    if ready:
                        time.sleep(0.12)
                        self.assertIsNone(
                            child.poll(), "child advanced before parent signal"
                        )
                        orca_program.notify_ready(saved)
                    stdout, stderr = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 1, (stdout, stderr))
                    self.assertIn(
                        "missing ACP executable bindings"
                        if ready
                        else "startup was fenced",
                        stdout,
                    )
                    saved = read_state(path)
                    self.assertEqual(saved["roles"], {})
                    self.assertEqual(saved["coordinator_process"]["phase"], "exited")
                    self.assertEqual(saved["coordinator_process"]["exit_code"], 1)
                    self.assertTrue(
                        saved["coordinator_process"]["cli_cleanup_confirmed"]
                    )
                    self.assertFalse(orca_program.process_running(saved))
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)

    def test_registration_rejects_wrong_kernel_argv_without_publishing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(Path(directory))
            path = Path(str(state["state_path"]))
            with self.assertRaisesRegex(RuntimeFailure, "kernel argv"):
                orca_program._register(path, "run-1", "nonce1234")
            self.assertEqual(read_state(path), state)

    def test_cleared_files_do_not_replace_parent_readiness_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(Path(directory))
            path = Path(str(state["state_path"]))
            state.pop("pending_coordinator_start")
            with (
                mock.patch.object(orca_program, "read_state", return_value=state),
                mock.patch.object(orca_program, "_parent_ready", False),
                mock.patch.object(orca_program, "START_WAIT_SECONDS", 0),
                mock.patch.object(orca_program, "require_owner") as owner,
                self.assertRaisesRegex(RuntimeFailure, "readiness is unconfirmed"),
            ):
                orca_program._await_parent(path, "run-1")
            owner.assert_not_called()
            with (
                mock.patch.object(orca_program, "read_state", return_value=state),
                mock.patch.object(orca_program, "_parent_ready", True),
                mock.patch.object(orca_program, "require_owner") as owner,
            ):
                orca_program._await_parent(path, "run-1")
            owner.assert_called_once_with(state)

    def test_uncleared_sidecar_fences_owner_after_marker_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(Path(directory))
            state.pop("pending_coordinator_start")
            state["coordinator_process"] = {
                "pid": os.getpid(),
                "process_group_id": os.getpgrp(),
                "argv": list(read_process_argv(os.getpid())),
                "launch_nonce": "nonce1234",
                "phase": "running",
                "exit_code": None,
                "cli_cleanup_confirmed": None,
            }
            before = copy.deepcopy(state)
            path = Path(str(state["state_path"]))
            sidecar = startup_recovery_path(path)
            sidecar.write_text("retained startup evidence", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeFailure, "startup is pending"):
                orca_program.require_owner(state)
            self.assertEqual(state, before)
            sidecar.unlink()
            with mock.patch.object(
                orca_program,
                "read_process_argv",
                return_value=tuple(state["coordinator_process"]["argv"]),
            ):
                orca_program.require_owner(state)


if __name__ == "__main__":
    unittest.main()
