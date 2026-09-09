from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_orca_backend import FakeOrcaClient

from agent_team import cli
from agent_team.backend import OrcaBackend
from agent_team.contracts import RuntimeFailure
from agent_team.harness_launch import build_claude_argv, build_codex_argv


def _fake_executable(path: Path, identity: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' {identity!r}\n"
        "printf 'ANTHROPIC_API_KEY=%s\\n' \"${ANTHROPIC_API_KEY-<unset>}\"\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _fixed_plan(
    root: Path, argv: list[str], *, provider: str = "claude"
) -> dict[str, object]:
    return {
        "runtime": "orca",
        "team_id": "team-test",
        "workspace": str(root),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "orca_socket": str(root / "orca.sock"),
        "roles": {
            "main": {
                "role": "main",
                "provider": provider,
                "transport": "direct",
                "model": "fable" if provider == "claude" else "gpt-6-astra",
                "effort": "high",
                "permission": "orchestrator",
                "instructions": "fixed main instructions",
                "execution": "tui_direct",
                "adapter_id": None,
                "env": {"CODEX_HOME": str(root / "codex-home")}
                if provider == "codex"
                else {},
                "argv": argv,
            }
        },
    }


def _named_plan(root: Path, argv: list[str]) -> dict[str, object]:
    return {
        "runtime": "orca",
        "team_id": "team-named",
        "workspace": str(root),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "roles": {
            "lead": {
                "role": "lead",
                "kind": "main",
                "provider": "claude",
                "transport": "direct",
                "model": "fable",
                "effort": "high",
                "permission": "orchestrator",
                "instructions": "node ID is lead",
                "execution": "tui_direct",
                "adapter_id": None,
                "env": {},
                "argv": argv,
            }
        },
        "graph": {
            "nodes": [{"node_id": "lead", "kind": "main"}],
            "edges": [],
            "coordination": {
                "mode": "agent",
                "entry_nodes": ["lead"],
                "dispatch_mode": "serial",
                "max_active": 1,
            },
            "routes": [],
        },
    }


class OrcaMainLaunchIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-orca-main-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.caller_bin = self.root / "caller-bin"
        self.orca_bin = self.root / "orca-bin"
        self.caller_bin.mkdir()
        self.orca_bin.mkdir()
        _fake_executable(self.caller_bin / "claude", "CALLER-CLAUDE")
        _fake_executable(self.orca_bin / "claude", "ORCA-CLAUDE")
        _fake_executable(self.caller_bin / "codex", "CALLER-CODEX")
        self.env = {
            "PATH": str(self.caller_bin),
            "HOME": str(self.root / "home"),
            "ANTHROPIC_API_KEY": "caller-sentinel",
            "CLAUDE_CONFIG_DIR": str(self.root / "profile"),
        }

    def _claude_argv(self, role: str = "main") -> list[str]:
        return list(
            build_claude_argv(
                role=role,
                model="fable",
                effort="high",
                permission="orchestrator" if role == "main" else "read-only",
                instructions="node ID is lead" if role == "main" else "role",
                state_path=self.root / "state.json",
                mcp_server_path=self.root / "agent-team-mcp",
            )
        )

    def _factory(self, plan: dict[str, object]):
        with (
            mock.patch.object(
                cli, "launcher_path", return_value=self.root / "agent-team"
            ),
            mock.patch("agent_team.backend.OrcaBackend") as backend_class,
            mock.patch.dict(os.environ, self.env, clear=False),
        ):
            cli._runtime_engine(plan, resume_existing=False)
        return backend_class.call_args.kwargs["main_command_factory"]

    def _run_hostile_orca_shell(self, command: str) -> str:
        result = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": str(self.orca_bin),
                "ANTHROPIC_API_KEY": "orca-sentinel",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_fixed_v3_factory_pins_executable_and_closes_environment(self) -> None:
        plan = _fixed_plan(self.root, self._claude_argv())
        with mock.patch.dict(os.environ, self.env, clear=False):
            factory = self._factory(plan)
            command = factory(self.root / "orca.sock")

        parsed = shlex.split(command)
        self.assertEqual(parsed[:2], ["/usr/bin/env", "-i"])
        self.assertIn(f"PATH={self.caller_bin}", parsed)
        self.assertIn(f"HOME={self.root / 'home'}", parsed)
        self.assertNotIn("ANTHROPIC_API_KEY=caller-sentinel", parsed)
        executable_index = parsed.index(str((self.caller_bin / "claude").resolve()))
        self.assertEqual(
            parsed[executable_index + 1 :], plan["roles"]["main"]["argv"][1:]
        )

        output = self._run_hostile_orca_shell(command)
        self.assertIn("CALLER-CLAUDE", output)
        self.assertIn("ANTHROPIC_API_KEY=<unset>", output)
        self.assertNotIn("ORCA-CLAUDE", output)

    def test_named_v4_factory_keeps_named_main_and_frozen_argv(self) -> None:
        plan = _named_plan(self.root, self._claude_argv())
        with mock.patch.dict(os.environ, self.env, clear=False):
            factory = self._factory(plan)
            command = factory(self.root / "orca.sock")

        parsed = shlex.split(command)
        executable_index = parsed.index(str((self.caller_bin / "claude").resolve()))
        self.assertEqual(
            parsed[executable_index + 1 :], plan["roles"]["lead"]["argv"][1:]
        )
        self.assertIn("node ID is lead", parsed)
        output = self._run_hostile_orca_shell(command)
        self.assertIn("CALLER-CLAUDE", output)
        self.assertIn("ANTHROPIC_API_KEY=<unset>", output)
        self.assertNotIn("ORCA-CLAUDE", output)

    def test_codex_main_keeps_socket_rebuild_and_saved_role_environment(self) -> None:
        raw_argv = ["codex", "-m", "gpt-6-astra", "-a", "never"]
        plan = _fixed_plan(self.root, raw_argv, provider="codex")
        with (
            mock.patch.object(cli, "mcp_server_path", return_value=self.root / "mcp"),
            mock.patch.dict(os.environ, self.env, clear=False),
        ):
            command = cli.role_command(
                plan,
                "main",
                executable=(self.caller_bin / "codex").resolve(),
                environment=cli.acp_environment(),
            )

        parsed = shlex.split(command)
        self.assertEqual(parsed[:2], ["/usr/bin/env", "-i"])
        self.assertIn(f"CODEX_HOME={self.root / 'codex-home'}", parsed)
        executable_index = parsed.index(str((self.caller_bin / "codex").resolve()))
        expected = list(
            build_codex_argv(
                role="main",
                model="gpt-6-astra",
                effort="high",
                permission="orchestrator",
                instructions="fixed main instructions",
                state_path=self.root / "state.json",
                workspace=self.root,
                control_socket=self.root / "orca.sock",
                mcp_server_path=self.root / "mcp",
            )
        )
        self.assertEqual(parsed[executable_index + 1 :], expected[1:])
        self.assertIn(str(self.root / "orca.sock"), " ".join(parsed))

    def test_public_role_command_remains_bare_and_dependency_free(self) -> None:
        plan = _fixed_plan(self.root, self._claude_argv())
        with (
            mock.patch.object(
                cli.shutil, "which", side_effect=AssertionError("resolve")
            ),
            mock.patch.object(
                cli, "acp_environment", side_effect=AssertionError("env")
            ),
        ):
            command = cli.role_command(plan, "main")
        self.assertEqual(shlex.split(command)[0], "claude")

    def test_missing_main_executable_has_no_prepare_or_backend_resource_effect(
        self,
    ) -> None:
        plan = _named_plan(self.root, self._claude_argv())
        state_path = self.root / "private-state" / "state.json"
        plan["state_path"] = str(state_path)
        client = FakeOrcaClient()
        with (
            mock.patch.object(
                cli, "launcher_path", return_value=self.root / "agent-team"
            ),
            mock.patch("agent_team.backend.OrcaClient", return_value=client),
            mock.patch.object(
                OrcaBackend,
                "_ensure_orca_ready",
                return_value=("repo::project", self.root / "orca.sock"),
            ),
            mock.patch.object(cli, "prepare_codex_homes_with_rollback") as prepare,
            mock.patch.object(cli.shutil, "which", return_value=None) as selected,
            mock.patch.dict(os.environ, self.env, clear=False),
        ):
            engine, _backend = cli._runtime_engine(plan, resume_existing=False)
            with self.assertRaises(RuntimeFailure):
                engine.start(cli._start_spec(plan, attach=False))

        prepare.assert_not_called()
        selected.assert_called_once_with("claude")
        self.assertEqual(client.calls, [])
        self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
