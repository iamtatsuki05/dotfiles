from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team import cli
from agent_team.contracts import Role
from agent_team.harness_launch import (
    LaunchValidationError,
    build_claude_argv,
    build_snapshot_role_command,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = PROJECT_ROOT / "agent_team" / "defaults"
INSPECT_WITHOUT_ORCA = """
import importlib.abc
import subprocess
import sys

class MissingOrca(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"agent_team.orca", "agent_team.backend"}:
            raise ModuleNotFoundError("Orca implementation is unavailable")

def refuse_process(*args, **kwargs):
    raise AssertionError("inspection must not start an external process")

sys.meta_path.insert(0, MissingOrca())
subprocess.Popen = refuse_process
from agent_team.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


class DependencyIsolationTest(unittest.TestCase):
    def test_main_command_uses_frozen_argv_after_config_and_prompt_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            shutil.copytree(DEFAULTS, config_root)
            prompt_path = config_root / "prompts" / "orchestrator.md"
            prompt = "Main $(touch SHOULD_NOT_EXIST); 'quoted' \"double\""
            prompt_path.write_text(prompt, encoding="utf-8")
            config_path = config_root / "config.toml"
            workspace = root / "workspace"
            workspace.mkdir()
            plan = cli.build_plan(cli.load_config(config_path), workspace)

            shutil.rmtree(config_root)
            with mock.patch.object(
                cli,
                "load_config",
                side_effect=AssertionError("child launch must not reread config"),
            ):
                command = cli.role_command(plan, "main")

            argv = shlex.split(command)

        self.assertEqual(argv[0], "claude")
        self.assertNotIn("_role-run", argv)
        self.assertNotIn("--config", argv)
        self.assertNotIn("--append-system-prompt-file", argv)
        frozen_instructions = argv[argv.index("--append-system-prompt") + 1]
        self.assertTrue(frozen_instructions.startswith(prompt))
        self.assertEqual(argv[argv.index("--model") + 1], "fable")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_saved_direct_role_specs_build_worker_and_reviewer_without_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            shutil.copytree(DEFAULTS, config_root)
            config_path = config_root / "config.toml"
            workspace = root / "workspace"
            workspace.mkdir()
            state_root = root / "state" / "agent-team" / "team"
            with mock.patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                plan = cli.build_plan(cli.load_config(config_path), workspace)

            roles = cast(dict[str, dict[str, object]], plan["roles"])
            state = {
                "state_path": str(state_root / "state.json"),
                "workspace": str(workspace),
                "orca_socket": str(root / "socket with space"),
                "role_specs": {
                    role: {
                        key: roles[role][key]
                        for key in (
                            "provider",
                            "transport",
                            "model",
                            "effort",
                            "permission",
                            "instructions",
                            "execution",
                        )
                    }
                    for role in ("worker", "reviewer")
                },
            }
            shutil.rmtree(config_root)

            commands = {
                role: build_snapshot_role_command(state, role)
                for role in ("worker", "reviewer")
            }
            parsed = {role: shlex.split(command) for role, command in commands.items()}

        for role, argv in parsed.items():
            self.assertEqual(argv[0], "env")
            self.assertIn(
                f"CODEX_HOME={state_root / 'codex' / role}",
                argv,
            )
            self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")
            self.assertEqual(
                argv[argv.index("-c") + 1],
                'model_reasoning_effort="medium"'
                if role == "worker"
                else 'model_reasoning_effort="high"',
            )
            self.assertNotIn("--config", argv)
            self.assertNotIn("--append-system-prompt-file", argv)
            self.assertIn("features.network_proxy=true", argv)
            self.assertIn(str(root / "socket with space"), " ".join(argv))

    def test_main_builder_requires_communication_command(self) -> None:
        with self.assertRaisesRegex(
            LaunchValidationError, "MCP server path is required"
        ):
            build_claude_argv(
                role="main",
                model="fable",
                effort="high",
                permission="orchestrator",
                instructions="main",
                state_path=Path("/tmp/state.json"),
            )

    def test_snapshot_builder_rejects_unverified_saved_profile(self) -> None:
        state = {
            "state_path": "/tmp/team/state.json",
            "workspace": "/tmp/project",
            "role_specs": {
                "worker": {
                    "provider": "cursor",
                    "transport": "direct",
                    "model": "fable",
                    "effort": "high",
                    "permission": "workspace-write",
                    "instructions": "worker",
                    "execution": "tui_direct",
                }
            },
        }
        with self.assertRaisesRegex(LaunchValidationError, "not runnable"):
            build_snapshot_role_command(state, "worker")

    def test_start_spec_keeps_instructions_from_the_validated_launch_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            shutil.copytree(DEFAULTS, config_root)
            prompt_path = config_root / "prompts" / "worker.md"
            prompt_path.write_text("Original worker instructions", encoding="utf-8")
            config_path = config_root / "config.toml"
            plan = cli.build_plan(cli.load_config(config_path), root)
            prompt_path.write_text("Changed instructions", encoding="utf-8")
            config_path.unlink()

            with mock.patch.object(
                cli,
                "load_config",
                side_effect=AssertionError("a validated plan must not reread config"),
            ):
                spec = cli._start_spec(plan, attach=False)

            self.assertEqual(
                spec.role_specs[Role.WORKER].instructions,
                "Original worker instructions",
            )
            self.assertEqual(spec.role_specs[Role.WORKER].model, "gpt-6-astra")

    def test_inspection_does_not_load_orca_or_require_agent_commands(self) -> None:
        commands = (
            ("--help",),
            (
                "start",
                "--dry-run",
                "--config",
                str(DEFAULTS / "config.toml"),
            ),
            ("teams", "--config", str(DEFAULTS / "teams.toml")),
            (
                "graph",
                "--config",
                str(DEFAULTS / "teams.toml"),
                "--team",
                "agent-team",
                "--format",
                "json",
            ),
            ("harnesses", "--json"),
        )
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **os.environ,
                "PATH": "",
                "XDG_CONFIG_HOME": directory,
                "XDG_STATE_HOME": directory,
            }
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-c", INSPECT_WITHOUT_ORCA, *command],
                        cwd=directory,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.strip())
                    self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
