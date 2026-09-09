from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from agent_team import adapters
from agent_team.contracts import RuntimeFailure
from agent_team.task_spec import TaskSpec, VerificationSpec
from agent_team.task_verification import run_verification
from agent_team.workspace_revision import snapshot_revision


class TaskVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name) / "repo"
        self.workspace.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Task Verification Test")
        (self.workspace / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
        )

    def _commands(self, result: dict[str, object]) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], result["commands"])

    def _task(self, *specs: VerificationSpec) -> TaskSpec:
        return TaskSpec(
            task_id="verify-task",
            objective="Run the declared verification commands.",
            acceptance_criteria=("All declared commands are checked.",),
            allowed_paths=("tracked.txt",),
            forbidden_paths=(),
            dependencies=(),
            verification=specs,
            evidence_requirements=("Report command outcomes.",),
            consultation_conditions=(),
        )

    def test_success_records_only_bounded_hash_evidence(self) -> None:
        task = self._task(
            VerificationSpec(
                "positive",
                (sys.executable, "-c", "print('verified')"),
                5,
            )
        )
        revision = snapshot_revision(self.workspace)

        result = run_verification(task, self.workspace, revision)

        self.assertEqual(
            set(result),
            {"revision", "passed", "commands", "error", "cleanup_confirmed"},
        )
        self.assertIs(result["cleanup_confirmed"], True)
        self.assertTrue(result["passed"])
        self.assertIsNone(result["error"])
        command = self._commands(result)[0]
        self.assertEqual(
            set(command),
            {
                "name",
                "argv",
                "timeout_seconds",
                "returncode",
                "stdout_sha256",
                "stderr_sha256",
                "error",
            },
        )
        self.assertEqual(command["returncode"], 0)
        self.assertEqual(
            command["stdout_sha256"], hashlib.sha256(b"verified\n").hexdigest()
        )

    def test_nonzero_command_collects_remaining_commands(self) -> None:
        marker = self.workspace / "second-ran.txt"
        task = self._task(
            VerificationSpec("fails", (sys.executable, "-c", "raise SystemExit(3)"), 5),
            VerificationSpec(
                "continues",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('second-ran.txt').write_text('ran')",
                ),
                5,
            ),
        )

        result = run_verification(
            task, self.workspace, snapshot_revision(self.workspace)
        )

        self.assertFalse(result["passed"])
        commands = self._commands(result)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0]["returncode"], 3)
        self.assertEqual(commands[1]["returncode"], 0)
        self.assertTrue(marker.is_file())

    def test_stale_revision_fails_before_starting_a_process(self) -> None:
        marker = self.workspace / "must-not-run.txt"
        task = self._task(
            VerificationSpec(
                "must-not-run",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('must-not-run.txt').write_text('x')",
                ),
                5,
            )
        )
        revision = snapshot_revision(self.workspace)
        (self.workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")

        with self.assertRaises(RuntimeFailure):
            run_verification(task, self.workspace, revision)

        self.assertFalse(marker.exists())

    def test_workspace_change_after_command_stops_later_commands(self) -> None:
        marker = self.workspace / "must-not-run.txt"
        task = self._task(
            VerificationSpec(
                "changes-workspace",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('changed.txt').write_text('x')",
                ),
                5,
            ),
            VerificationSpec(
                "later",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('must-not-run.txt').write_text('x')",
                ),
                5,
            ),
        )

        result = run_verification(
            task, self.workspace, snapshot_revision(self.workspace)
        )

        self.assertFalse(result["passed"])
        self.assertEqual(len(self._commands(result)), 1)
        self.assertIsNotNone(result["error"])
        self.assertFalse(marker.exists())

    def test_timeout_is_failed_and_returns_after_runner_cleanup(self) -> None:
        task = self._task(
            VerificationSpec(
                "timeout", (sys.executable, "-c", "import time; time.sleep(5)"), 1
            )
        )

        revision = snapshot_revision(self.workspace)
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def capture_popen(
            args: Sequence[str],
            *,
            cwd: Path,
            env: Mapping[str, str],
            stdin: int | None,
            stdout: int | None,
            stderr: int | None,
            shell: bool,
            start_new_session: bool,
        ) -> subprocess.Popen[bytes]:
            self.assertIs(start_new_session, True)
            process = real_popen(
                args,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                shell=shell,
                start_new_session=start_new_session,
            )
            processes.append(process)
            return process

        runner_subprocess = SimpleNamespace(
            **{**vars(subprocess), "Popen": capture_popen}
        )
        try:
            with mock.patch.object(adapters, "subprocess", runner_subprocess):
                result = run_verification(task, self.workspace, revision)

            self.assertFalse(result["passed"])
            self.assertIs(result["cleanup_confirmed"], True)
            command = self._commands(result)[0]
            self.assertIsNone(command["returncode"])
            self.assertIn("timed out after 1s", cast(str, command["error"]))
            self.assertEqual(len(processes), 1)
            process = processes[0]
            self.assertIn(process.poll(), {-signal.SIGTERM, -signal.SIGKILL})
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
            for stream in (process.stdin, process.stdout, process.stderr):
                self.assertTrue(stream is not None and stream.closed)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
