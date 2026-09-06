from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import cli
from agent_team.runtime import RuntimeValidationError, read_state, write_state


class NativeConfigTest(unittest.TestCase):
    def test_mcp_declarations_do_not_load_a_backend_or_require_runtime_state(
        self,
    ) -> None:
        script = """import io,json,sys
from agent_team import cli
sys.stdin=io.StringIO(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/list"})+"\\n")
assert cli.main(["_mcp-server"])==0
assert "agent_team.orca" not in sys.modules
assert "agent_team.native_backend" not in sys.modules
"""
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.pop("AGENT_TEAM_STATE_PATH", None)
            env["HOME"] = directory
            env["XDG_CONFIG_HOME"] = str(Path(directory) / "config")
            env["XDG_STATE_HOME"] = str(Path(directory) / "state")
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=directory,
                env=env,
                check=False,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["result"]["tools"]), 7)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def config(self, root: Path, *, runtime: str = "tmux") -> Path:
        for role in ("main", "planner"):
            (root / f"{role}.md").write_text(f"{role} instructions", encoding="utf-8")
        path = root / "team.toml"
        path.write_text(
            f'''version = 3
runtime = "{runtime}"
team_prefix = "native"
max_review_rounds = 2
[main]
provider = "claude"
transport = "direct"
model = "fable"
effort = "high"
permission = "orchestrator"
prompt = "main.md"
[roles.planner]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
permission = "read-only"
prompt = "planner.md"
''',
            encoding="utf-8",
        )
        return path

    def test_native_config_selects_only_explicit_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = cli.load_config(self.config(root))
            with mock.patch.object(subprocess, "Popen", side_effect=AssertionError):
                plan = cli.build_plan(config, root)
                spec = cli._start_spec(plan, attach=False)
        self.assertEqual(plan["runtime"], "tmux")
        self.assertNotIn("orca_socket", plan)
        self.assertEqual(set(plan["roles"]), {"main", "planner"})
        self.assertEqual({role.value for role in spec.role_specs}, {"main", "planner"})

    def test_orca_still_requires_its_four_role_contract(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(cli.ConfigError),
        ):
            cli.load_config(self.config(Path(directory), runtime="orca"))

    def test_native_preflight_does_not_require_orca_or_unselected_harnesses(
        self,
    ) -> None:
        plan: dict[str, object] = {
            "runtime": "tmux",
            "roles": {
                "main": {
                    "provider": "claude",
                    "transport": "direct",
                    "execution": "tui_direct",
                }
            },
        }
        with (
            mock.patch.object(cli, "require_binary") as require,
            mock.patch.object(
                cli, "mcp_server_path", return_value=Path(sys.executable)
            ),
            mock.patch.object(
                cli.AcpExecutables, "resolve", side_effect=AssertionError
            ),
            mock.patch.object(subprocess, "Popen", side_effect=AssertionError),
        ):
            cli._start_prerequisites(plan)
        self.assertEqual(
            require.call_args_list, [mock.call("tmux"), mock.call("claude")]
        )

    def test_native_state_and_management_need_no_orca_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state" / "native-project" / "state.json"
            state: dict[str, object] = {
                "version": 3,
                "runtime": "tmux",
                "team_id": "native-project",
                "workspace": str(root),
                "config_path": str(root / "deleted.toml"),
                "state_path": str(state_path),
                "launcher_path": "/tmp/agent-team",
                "run_id": "native_run",
                "main_terminal": "native_main",
                "role_specs": {
                    "main": {
                        "provider": "claude",
                        "transport": "direct",
                        "model": "fable",
                        "effort": "high",
                        "permission": "orchestrator",
                        "instructions": "main instructions",
                        "execution": "tui_direct",
                    }
                },
                "roles": {},
                "native": {
                    "phase": "starting",
                    "run_nonce": "a" * 32,
                    "main_argv": [sys.executable, "-c", "pass"],
                },
            }
            write_state(state_path, state)
            loaded = read_state(state_path)
            plan = cli._management_plan_from_state(loaded)
            self.assertEqual(plan["runtime"], "tmux")
            self.assertNotIn("orca_socket", plan)
            self.assertEqual(set(plan["roles"]), {"main"})
            state["orca_socket"] = "/tmp/fake-orca.sock"
            with self.assertRaises(RuntimeValidationError):
                write_state(state_path, state, require_existing=True)


if __name__ == "__main__":
    unittest.main()
