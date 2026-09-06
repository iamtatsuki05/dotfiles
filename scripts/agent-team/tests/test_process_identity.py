from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_team.process_identity import read_process_argv


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

                self.assertEqual(observed, tuple(argv))
                self.assertNotIn("must-not-be-returned", observed or ())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5.0)

    def test_absent_or_non_positive_pid_returns_none(self) -> None:
        self.assertIsNone(read_process_argv(0))
        self.assertIsNone(read_process_argv(-1))
        self.assertIsNone(read_process_argv(2**31 - 1))


if __name__ == "__main__":
    unittest.main()
