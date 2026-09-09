from __future__ import annotations

import os
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_team import orca_program
from agent_team.contracts import RuntimeFailure


class OrcaProgramLaunchTest(unittest.TestCase):
    def test_program_command_preserves_the_selected_interpreter_and_quotes_identity(
        self,
    ):
        path = Path("/tmp/work with spaces; not a command/state.json")
        argv = orca_program.launch_argv(path, "run-1", "a" * 32)
        environment = orca_program.launch_environment()
        command = orca_program.launch_command(argv, environment)
        words = shlex.split(command)
        self.assertEqual(words[:3], ["exec", "/usr/bin/env", "-i"])
        self.assertEqual(words[-len(argv) :], argv)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:5], ["-P", "-m", "agent_team", "_orca-program-run"])
        self.assertNotIn("claude", argv)
        self.assertIn(str(path), argv)

    def test_program_environment_pins_its_package_and_omits_provider_credentials(self):
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp/home",
                "PYTHONPATH": "/unrelated",
                "ANTHROPIC_API_KEY": "not-used",
                "OPENAI_API_KEY": "not-used",
            },
            clear=True,
        ):
            environment = orca_program.launch_environment()
        self.assertEqual(
            environment["PYTHONPATH"],
            str(Path(orca_program.__file__).resolve().parent.parent),
        )
        self.assertTrue(
            environment["PATH"].startswith(
                str(Path(sys.executable).parent) + os.pathsep
            )
        )
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_missing_launch_identity_is_rejected(self):
        for path, run, nonce in [
            (Path("relative.json"), "run", "nonce"),
            (Path("/state.json"), "", "nonce"),
            (Path("/state.json"), "run", ""),
        ]:
            with (
                self.subTest(path=path, run=run, nonce=nonce),
                self.assertRaises(RuntimeFailure),
            ):
                orca_program.launch_argv(path, run, nonce)


if __name__ == "__main__":
    unittest.main()
