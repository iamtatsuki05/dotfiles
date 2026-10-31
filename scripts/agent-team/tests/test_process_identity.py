from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_team import process_identity
from agent_team.process_identity import python_process_argv, read_process_argv


class ProcessIdentityTest(unittest.TestCase):
    def test_reads_exact_boundaries_for_an_owned_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            source = (
                "import pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8'); "
                "time.sleep(30)"
            )
            argv = [
                sys.executable,
                "-c",
                source,
                str(marker),
                "argument with spaces",
                "",
                "日本語の引数 🚀",
                "",
            ]
            environment = dict(os.environ)
            environment["AGENT_TEAM_PROCESS_IDENTITY_ENV"] = "must-not-be-returned"
            process = subprocess.Popen(argv, env=environment)
            try:
                deadline = time.monotonic() + 5.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())

                observed = read_process_argv(process.pid)

                self.assertEqual(observed, python_process_argv(argv))
                self.assertNotIn("must-not-be-returned", observed or ())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5.0)

    def test_absent_or_non_positive_pid_returns_none(self) -> None:
        self.assertIsNone(read_process_argv(0))
        self.assertIsNone(read_process_argv(-1))
        self.assertIsNone(read_process_argv(2**31 - 1))

    def test_framework_kernel_argv_preserves_arguments_and_launch_command(self) -> None:
        launch = [sys.executable, "-m", "agent_team", "", "日本語 with spaces"]
        application = (
            "/Library/Frameworks/Python.framework/Python.app/Contents/MacOS/Python"
        )
        with (
            mock.patch.object(process_identity.sys, "platform", "darwin"),
            mock.patch.object(
                process_identity.sysconfig, "get_config_var", return_value="Python"
            ),
            mock.patch.object(
                process_identity,
                "read_process_argv",
                return_value=(application, "parent"),
            ),
        ):
            self.assertEqual(python_process_argv(launch), (application, *launch[1:]))
        self.assertEqual(launch[0], sys.executable)

    def test_framework_argv_does_not_guess_an_unavailable_identity(self) -> None:
        with (
            mock.patch.object(process_identity.sys, "platform", "darwin"),
            mock.patch.object(
                process_identity.sysconfig, "get_config_var", return_value="Python"
            ),
            mock.patch.object(process_identity, "read_process_argv", return_value=None),
            self.assertRaisesRegex(RuntimeError, "identity is unavailable"),
        ):
            python_process_argv([sys.executable, "-m", "agent_team"])


if __name__ == "__main__":
    unittest.main()
