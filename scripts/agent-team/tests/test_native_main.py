from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team import native_main
from agent_team.locking import _LifecycleReservation


class NativeMainContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "team"
        self.root.mkdir(mode=0o700)
        self.state_path = self.root / "state.json"
        self.run_id = "run-123"
        self.environment_patcher = mock.patch.dict(
            os.environ,
            {"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            clear=False,
        )
        self.environment_patcher.start()
        self.read_patcher = mock.patch.object(
            native_main, "runtime_read_state", side_effect=self._read_state
        )
        self.write_patcher = mock.patch.object(
            native_main, "runtime_write_state", side_effect=self._write_state
        )
        self.read_patcher.start()
        self.write_patcher.start()

    def tearDown(self) -> None:
        self.write_patcher.stop()
        self.read_patcher.stop()
        self.environment_patcher.stop()
        self.directory.cleanup()

    def _read_state(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(
        self,
        path: Path,
        state: dict[str, object],
        *,
        require_existing: bool = False,
        reservation_held: bool = False,
    ) -> None:
        del require_existing, reservation_held
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    def _state(self, argv: list[str], *, phase: str = "running") -> dict[str, object]:
        return {
            "version": 3,
            "runtime": "tmux",
            "team_id": "team-123",
            "workspace": str(self.root),
            "config_path": str(self.root / "config.toml"),
            "state_path": str(self.state_path),
            "launcher_path": str(self.root / "agent-team"),
            "run_id": self.run_id,
            "main_terminal": "terminal-123",
            "role_specs": {},
            "roles": {},
            "native": {
                "phase": phase,
                "run_nonce": "nonce-123",
                "main_argv": argv,
            },
        }

    def _save_state(self, state: dict[str, object]) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.state_path.chmod(0o600)

    def _python_argv(self, source: str, *args: str) -> list[str]:
        return [sys.executable, "-c", textwrap.dedent(source), *args]

    def _wait_for_state(self, predicate: object) -> dict[str, object]:
        check = predicate
        assert callable(check)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = self._read_state(self.state_path)
            if check(state):
                return state
            time.sleep(0.02)
        self.fail("timed out waiting for native state")

    def _wait_pid_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"process remains alive: {pid}")

    def test_stopping_state_does_not_launch_the_frozen_argv(self) -> None:
        marker = self.root / "launched"
        argv = self._python_argv(
            "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('x')",
            str(marker),
        )
        self._save_state(self._state(argv, phase="stopping"))

        result = native_main.run(self.state_path, self.run_id)

        self.assertEqual(result, 1)
        self.assertFalse(marker.exists())
        self.assertNotIn("main_process", self._read_state(self.state_path)["native"])

    def test_launch_waits_for_publication_lock_without_duplicate_process(self) -> None:
        self._save_state(self._state([str(self.root / "main")]))
        holder = _LifecycleReservation(self.state_path, create_parent=True)
        holder.acquire()
        release_started = threading.Event()

        def release_holder() -> None:
            release_started.set()
            time.sleep(0.05)
            holder.release()

        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 12_345
        release_thread = threading.Thread(target=release_holder)
        release_thread.start()
        release_started.wait()
        try:
            with (
                mock.patch.object(
                    native_main.subprocess, "Popen", return_value=process
                ) as popen,
                mock.patch.object(native_main.os, "getpgid", return_value=process.pid),
            ):
                child, published = native_main._launch_if_ready(
                    self.state_path,
                    self.run_id,
                    supervisor_pid=67_890,
                    is_cancelled=lambda: False,
                )
        finally:
            release_thread.join(timeout=1.0)
        self.assertFalse(release_thread.is_alive())
        self.assertTrue(published)
        self.assertIsNotNone(child)
        popen.assert_called_once()

    def test_launch_does_not_start_after_signal_arrives_while_waiting(self) -> None:
        self._save_state(self._state([str(self.root / "main")]))
        holder = _LifecycleReservation(self.state_path, create_parent=True)
        holder.acquire()
        cancelled = threading.Event()
        release_started = threading.Event()

        def release_holder() -> None:
            release_started.set()
            cancelled.set()
            time.sleep(0.05)
            holder.release()

        release_thread = threading.Thread(target=release_holder)
        release_thread.start()
        release_started.wait()
        try:
            with mock.patch.object(native_main.subprocess, "Popen") as popen:
                child, published = native_main._launch_if_ready(
                    self.state_path,
                    self.run_id,
                    supervisor_pid=67_890,
                    is_cancelled=cancelled.is_set,
                )
        finally:
            release_thread.join(timeout=1.0)
        self.assertFalse(release_thread.is_alive())
        self.assertIsNone(child)
        self.assertFalse(published)
        popen.assert_not_called()

    def test_publish_exited_waits_for_publication_lock(self) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 12_345
        child = native_main._ChildProcess(
            process=process,
            agent_pid=process.pid,
            process_group_id=process.pid,
            launch_nonce="a" * 32,
        )
        state = self._state([str(self.root / "main")])
        native = cast(dict[str, object], state["native"])
        native["main_process"] = native_main._running_record(67_890, child)
        self._save_state(state)
        holder = _LifecycleReservation(self.state_path, create_parent=True)
        holder.acquire()
        release_started = threading.Event()

        def release_holder() -> None:
            release_started.set()
            time.sleep(0.05)
            holder.release()

        release_thread = threading.Thread(target=release_holder)
        release_thread.start()
        release_started.wait()
        try:
            published = native_main._publish_exited(
                self.state_path,
                self.run_id,
                67_890,
                child,
                returncode=0,
                group_stopped=True,
            )
        finally:
            release_thread.join(timeout=1.0)
        self.assertFalse(release_thread.is_alive())
        self.assertTrue(published)
        saved_native = cast(
            dict[str, object], self._read_state(self.state_path)["native"]
        )
        saved = cast(dict[str, object], saved_native["main_process"])
        self.assertEqual(saved["phase"], "exited")

    def test_normal_exit_reaps_a_descendant_and_publishes_exit_receipt(self) -> None:
        child_pid = self.root / "child.pid"
        argv = self._python_argv(
            """
            import subprocess, sys, time
            from pathlib import Path
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            Path(sys.argv[1]).write_text(str(child.pid))
            time.sleep(0.1)
            raise SystemExit(7)
            """,
            str(child_pid),
        )
        self._save_state(self._state(argv))

        result = native_main.run(self.state_path, self.run_id)

        self.assertEqual(result, 7)
        state = self._read_state(self.state_path)
        process = state["native"]["main_process"]
        self.assertEqual(process["phase"], "exited")
        self.assertEqual(process["returncode"], 7)
        self.assertTrue(process["group_stopped"])
        self._wait_pid_gone(int(child_pid.read_text(encoding="utf-8")))

    def test_unknown_process_group_reaps_child_and_publishes_failed_receipt(
        self,
    ) -> None:
        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 12_345
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="native-main", timeout=2.0),
            -9,
        ]

        def kill() -> None:
            process.returncode = -9

        process.kill.side_effect = kill
        argv = self._python_argv("raise SystemExit(7)")
        self._save_state(self._state(argv))

        with (
            mock.patch.object(native_main.subprocess, "Popen", return_value=process),
            mock.patch.object(
                native_main.os,
                "getpgid",
                side_effect=OSError(errno.EPERM, "process group is unavailable"),
            ),
            mock.patch.object(native_main.os, "killpg") as killpg,
        ):
            result = native_main.run(self.state_path, self.run_id)

        self.assertEqual(result, 1)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        killpg.assert_not_called()
        state = self._read_state(self.state_path)
        native = state["native"]
        assert isinstance(native, dict)
        receipt = native["main_process"]
        assert isinstance(receipt, dict)
        self.assertEqual(receipt["phase"], "exited")
        self.assertEqual(receipt["agent_pid"], process.pid)
        self.assertIsNone(receipt["process_group_id"])
        self.assertEqual(receipt["returncode"], -9)
        self.assertFalse(receipt["group_stopped"])

    def test_term_to_supervisor_reaps_only_its_owned_group(self) -> None:
        child_pid = self.root / "child.pid"
        argv = self._python_argv(
            """
            import subprocess, sys, time
            from pathlib import Path
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            Path(sys.argv[1]).write_text(str(child.pid))
            while True:
                time.sleep(1)
            """,
            str(child_pid),
        )
        self._save_state(self._state(argv))
        helper = textwrap.dedent(
            """
            import json, os, pathlib, sys
            from agent_team import native_main

            def read_state(path):
                return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))

            def write_state(path, state, **kwargs):
                del kwargs
                target = pathlib.Path(path)
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps(state), encoding='utf-8')
                temporary.chmod(0o600)
                os.replace(temporary, target)

            native_main.runtime_read_state = read_state
            native_main.runtime_write_state = write_state
            raise SystemExit(native_main.run(pathlib.Path(sys.argv[1]), sys.argv[2]))
            """
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        supervisor = subprocess.Popen(
            [sys.executable, "-c", helper, str(self.state_path), self.run_id],
            env=environment,
        )
        try:
            self._wait_for_state(
                lambda item: (
                    isinstance(item.get("native"), dict)
                    and isinstance(item["native"].get("main_process"), dict)
                    and item["native"]["main_process"].get("phase") == "running"
                    and child_pid.is_file()
                    and child_pid.read_text(encoding="utf-8").strip().isdigit()
                )
            )
            os.kill(supervisor.pid, signal.SIGTERM)
            supervisor.wait(timeout=5.0)
            final = self._wait_for_state(
                lambda item: (
                    isinstance(item.get("native"), dict)
                    and isinstance(item["native"].get("main_process"), dict)
                    and item["native"]["main_process"].get("phase") == "exited"
                )
            )
            process = final["native"]["main_process"]
            self.assertTrue(process["group_stopped"])
            self.assertEqual(process["supervisor_pid"], supervisor.pid)
            self.assertEqual(process["process_group_id"], process["agent_pid"])
            self._wait_pid_gone(int(child_pid.read_text(encoding="utf-8")))
        finally:
            if supervisor.poll() is None:
                supervisor.terminate()
                supervisor.wait(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
