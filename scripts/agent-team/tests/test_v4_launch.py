from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import cli
from agent_team.config_v4 import V4ConfigError, load_v4_config
from agent_team.contracts import ErrorCode, RuntimeFailure

V3_CONFIG = """\
version = 3
runtime = "orca"
team_prefix = "build"
max_review_rounds = 2

[main]
provider = "claude"
transport = "direct"
model = "fable"
effort = "high"
prompt = "prompts/orchestrator.md"
permission = "orchestrator"

[roles.planner]
provider = "claude"
transport = "acp"
model = "fable"
effort = "high"
prompt = "prompts/planner.md"
permission = "read-only"

[roles.worker]
provider = "codex"
transport = "direct"
model = "gpt-6-astra"
effort = "medium"
prompt = "prompts/worker.md"
permission = "workspace-write"

[roles.reviewer]
provider = "codex"
transport = "direct"
model = "gpt-6-astra"
effort = "high"
prompt = "prompts/reviewer.md"
permission = "read-only"
"""


V4_CONFIG = """\
version = 4
runtime = "orca"

[teams.build]
name = "Build Team"
launch_config = "launch/team-v3.toml"

[[teams.build.nodes]]
id = "main"
label = "Main"
main = true
[teams.build.nodes.profile]
provider = "claude"
transport = "direct"
permission = "orchestrator"

[[teams.build.nodes]]
id = "planner"
label = "Planner"
main = false
[teams.build.nodes.profile]
provider = "claude"
transport = "acp"
permission = "read-only"

[[teams.build.nodes]]
id = "worker"
label = "Worker"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "workspace-write"

[[teams.build.nodes]]
id = "reviewer"
label = "Reviewer"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "read-only"

[[teams.build.edges]]
source = "main"
target = "planner"
kind = "delegates-to"

[[teams.build.edges]]
source = "main"
target = "worker"
kind = "delegates-to"

[[teams.build.edges]]
source = "main"
target = "reviewer"
kind = "delegates-to"

[[teams.build.edges]]
source = "planner"
target = "reviewer"
kind = "reviewed-by"

[[teams.build.edges]]
source = "worker"
target = "reviewer"
kind = "reviewed-by"

[[teams.build.edges]]
source = "planner"
target = "main"
kind = "escalates-to"

[[teams.build.edges]]
source = "worker"
target = "main"
kind = "escalates-to"

[[teams.build.edges]]
source = "reviewer"
target = "main"
kind = "escalates-to"
"""


class V4LaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="agent-team-v4-launch-")
        self.root = Path(self.temp_dir.name)
        (self.root / "launch" / "prompts").mkdir(parents=True)
        for name in ("orchestrator", "planner", "worker", "reviewer"):
            (self.root / "launch" / "prompts" / f"{name}.md").write_text(
                f"{name} instructions\n", encoding="utf-8"
            )
        self.launch_config = self.root / "launch" / "team-v3.toml"
        self.launch_config.write_text(V3_CONFIG, encoding="utf-8")
        self.v4_config = self.root / "config-v4.toml"
        self.v4_config.write_text(V4_CONFIG, encoding="utf-8")
        self.old_xdg_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")

    def tearDown(self) -> None:
        if self.old_xdg_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.old_xdg_state_home
        self.temp_dir.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cli.main([*arguments, "--config", str(self.v4_config)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_launch_config_is_resolved_inside_catalog_as_regular_file(self) -> None:
        parsed = load_v4_config(self.v4_config)

        self.assertEqual(
            parsed.team("build").launch_config, self.launch_config.resolve()
        )

        outside = self.root / "outside.toml"
        outside.write_text(V3_CONFIG, encoding="utf-8")
        escaped = V4_CONFIG.replace(
            'launch_config = "launch/team-v3.toml"',
            'launch_config = "../outside.toml"',
        )
        escaped_path = self.root / "escaped.toml"
        escaped_path.write_text(escaped, encoding="utf-8")
        with self.assertRaisesRegex(V4ConfigError, "catalog"):
            load_v4_config(escaped_path)

        symlink = self.root / "launch" / "link.toml"
        symlink.symlink_to(self.launch_config)
        linked = V4_CONFIG.replace(
            'launch_config = "launch/team-v3.toml"',
            'launch_config = "launch/link.toml"',
        )
        linked_path = self.root / "linked.toml"
        linked_path.write_text(linked, encoding="utf-8")
        with self.assertRaisesRegex(V4ConfigError, "regular file|symlink"):
            load_v4_config(linked_path)

    def test_v4_start_dry_run_emits_the_referenced_v3_launch_plan(self) -> None:
        result, stdout, stderr = self.run_cli(
            "start", "--team", "build", "--dry-run", "--cwd", str(self.root)
        )

        self.assertEqual(result, 0, stderr)
        plan = json.loads(stdout)
        self.assertEqual(plan["config_path"], str(self.launch_config.resolve()))
        self.assertEqual(plan["team_id"].split("-", 1)[0], "build")
        self.assertEqual(set(plan["roles"]), {"main", "planner", "worker", "reviewer"})
        self.assertEqual(stderr, "")

    def test_v4_runtime_delegates_to_existing_v3_lifecycle(self) -> None:
        with (
            mock.patch.object(
                cli, "start_team", return_value={"status": "running"}
            ) as start,
            mock.patch.object(
                cli, "manage_team", return_value={"status": "ok"}
            ) as manage,
        ):
            result, stdout, stderr = self.run_cli(
                "start", "--team", "build", "--no-attach", "--cwd", str(self.root)
            )
            self.assertEqual(result, 0, stderr)
            plan = start.call_args.args[0]
            self.assertEqual(plan["config_path"], str(self.launch_config.resolve()))
            self.assertEqual(plan["team_id"].split("-", 1)[0], "build")
            self.assertEqual(json.loads(stdout), {"status": "running"})

            for command, extra in (
                ("status", ()),
                ("attach", ("main",)),
                ("stop", ()),
            ):
                result, _stdout, stderr = self.run_cli(
                    command, *extra, "--team", "build", "--cwd", str(self.root)
                )
                self.assertEqual(result, 0, stderr)
                self.assertEqual(manage.call_args.args[0], command)
                self.assertEqual(manage.call_args.args[2], "main" if extra else None)

            self.assertEqual(start.call_count, 1)
            self.assertEqual(manage.call_count, 3)

    def test_prefix_mismatch_fails_before_runtime(self) -> None:
        mismatched = self.launch_config.with_name("mismatch.toml")
        mismatched.write_text(
            V3_CONFIG.replace('team_prefix = "build"', 'team_prefix = "other"'),
            encoding="utf-8",
        )
        config = self.v4_config.read_text(encoding="utf-8").replace(
            'launch_config = "launch/team-v3.toml"',
            'launch_config = "launch/mismatch.toml"',
        )
        self.v4_config.write_text(config, encoding="utf-8")

        with mock.patch.object(cli, "start_team") as start:
            result, _stdout, stderr = self.run_cli(
                "start", "--team", "build", "--cwd", str(self.root)
            )

        self.assertEqual(result, 2)
        self.assertIn("team_prefix", stderr)
        start.assert_not_called()

    def test_management_requires_launch_config_before_runtime(self) -> None:
        no_launch_config = self.root / "inspection-only.toml"
        no_launch_config.write_text(
            V4_CONFIG.replace(
                'launch_config = "launch/team-v3.toml"\n',
                "",
            ),
            encoding="utf-8",
        )
        original = self.v4_config
        self.v4_config = no_launch_config
        try:
            with mock.patch.object(cli, "manage_team") as manage:
                result, _stdout, stderr = self.run_cli(
                    "status", "--team", "build", "--cwd", str(self.root)
                )
        finally:
            self.v4_config = original

        self.assertEqual(result, 2)
        self.assertIn("launch_config", stderr)
        manage.assert_not_called()

    def test_profile_or_graph_mismatch_fails_before_runtime(self) -> None:
        cases = (
            (
                "profile",
                V4_CONFIG.replace(
                    'permission = "workspace-write"',
                    'permission = "read-only"',
                    1,
                ),
                "profile",
            ),
            (
                "graph",
                V4_CONFIG.replace(
                    'target = "planner"\nkind = "delegates-to"',
                    'target = "worker"\nkind = "delegates-to"',
                    1,
                ),
                "edge",
            ),
        )
        for name, config_text, expected in cases:
            with self.subTest(name=name):
                config_path = self.root / f"{name}.toml"
                config_path.write_text(config_text, encoding="utf-8")
                original = self.v4_config
                self.v4_config = config_path
                try:
                    with mock.patch.object(cli, "start_team") as start:
                        result, _stdout, stderr = self.run_cli(
                            "start", "--team", "build", "--cwd", str(self.root)
                        )
                finally:
                    self.v4_config = original
                self.assertEqual(result, 2)
                self.assertIn(expected, stderr)
                start.assert_not_called()

    def test_runtime_failure_is_exit_one(self) -> None:
        with mock.patch.object(
            cli,
            "start_team",
            side_effect=RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "runtime failed"
            ),
        ):
            result, _stdout, stderr = self.run_cli(
                "start", "--team", "build", "--cwd", str(self.root)
            )

        self.assertEqual(result, 1)
        self.assertEqual(stderr, "ERROR: runtime failed\n")

    def test_runtime_error_is_exit_one_without_traceback(self) -> None:
        with mock.patch.object(
            cli, "start_team", side_effect=RuntimeError("runtime failed")
        ):
            result, _stdout, stderr = self.run_cli(
                "start", "--team", "build", "--cwd", str(self.root)
            )

        self.assertEqual(result, 1)
        self.assertEqual(stderr, "ERROR: runtime failed\n")
        self.assertNotIn("Traceback", stderr)

    def test_launcher_resolves_relative_config_and_cwd_from_caller_directory(
        self,
    ) -> None:
        project_root = Path(__file__).resolve().parents[3]
        launcher = project_root / "scripts" / "agent-team" / "agent-team"
        result = subprocess.run(
            [
                str(launcher),
                "start",
                "--config",
                "scripts/agent-team/agent_team/defaults/teams.toml",
                "--team",
                "agent-team",
                "--dry-run",
                "--cwd",
                ".",
            ],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(
            Path(plan["config_path"]),
            (
                project_root / "scripts/agent-team/agent_team/defaults/config.toml"
            ).resolve(),
        )
        self.assertEqual(Path(plan["workspace"]), project_root.resolve())


if __name__ == "__main__":
    unittest.main()
