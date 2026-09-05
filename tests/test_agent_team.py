from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Self
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "agent-team"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
agent_team_runtime = importlib.import_module("agent_team.runtime")

LAUNCHER = REPO_ROOT / "scripts" / "agent-team" / "agent-team"
agent_team = importlib.import_module("agent_team.cli")


def _acp_operation(argv: list[str]) -> list[str]:
    for index, token in enumerate(argv):
        if token in {"sessions", "set", "prompt"}:
            return argv[index:]
    raise AssertionError("ACP argv does not contain an operation")


class AgentTeamTestCase(unittest.TestCase):
    def make_config(self, root: Path, *, worker_provider: str = "codex") -> Path:
        prompts = root / "prompts"
        prompts.mkdir()
        for role in ("main", "planner", "worker", "reviewer"):
            (prompts / f"{role}.md").write_text(
                f"# {role}\n\n日本語の{role}指示。\n", encoding="utf-8"
            )
        config = root / "config.toml"
        config.write_text(
            textwrap.dedent(
                f"""\
                version = 3
                runtime = "orca"
                team_prefix = "agent-team"
                max_review_rounds = 2

                [main]
                provider = "claude"
                transport = "direct"
                model = "fable"
                effort = "high"
                prompt = "prompts/main.md"
                permission = "orchestrator"

                [roles.planner]
                provider = "claude"
                transport = "acp"
                model = "fable"
                effort = "high"
                prompt = "prompts/planner.md"
                permission = "read-only"

                [roles.worker]
                provider = "{worker_provider}"
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
            ),
            encoding="utf-8",
        )
        return config

    def run_launcher(
        self,
        config: Path,
        workspace: Path,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        return subprocess.run(
            [
                str(LAUNCHER),
                *args,
                "--config",
                str(config),
                "--cwd",
                str(workspace),
            ],
            check=False,
            capture_output=True,
            env=command_env,
            text=True,
        )


class AgentTeamDryRunTest(AgentTeamTestCase):
    def test_default_config_builds_orca_plan_with_role_isolation(self) -> None:
        plan = agent_team.build_plan(
            agent_team.load_config(agent_team.default_config_path()), REPO_ROOT
        )

        self.assertEqual(plan["runtime"], "orca")
        self.assertNotIn("socket_path", plan)
        self.assertNotIn("herdr", json.dumps(plan))
        roles = plan["roles"]
        self.assertEqual(roles["main"]["model"], "fable")
        self.assertEqual(roles["planner"]["model"], "fable")
        self.assertEqual(roles["worker"]["permission"], "workspace-write")
        self.assertEqual(roles["reviewer"]["permission"], "read-only")
        self.assertIn(":workspace", " ".join(roles["worker"]["argv"]))
        self.assertIn(":read-only", " ".join(roles["reviewer"]["argv"]))
        main_argv = roles["main"]["argv"]
        self.assertNotIn("Bash", main_argv)
        self.assertTrue(any("agent_team" in arg for arg in main_argv))

    def test_canonical_plan_selects_acp_only_for_read_only_planner(self) -> None:
        plan = agent_team.build_plan(
            agent_team.load_config(agent_team.default_config_path()), REPO_ROOT
        )
        roles = plan["roles"]

        self.assertEqual(roles["main"]["transport"], "direct")
        self.assertEqual(roles["planner"]["transport"], "acp")
        self.assertEqual(roles["planner"]["permission"], "read-only")
        self.assertEqual(roles["worker"]["transport"], "direct")
        self.assertEqual(roles["reviewer"]["transport"], "direct")

    def test_transport_is_required_and_unsupported_matrix_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)

            missing = config.read_text(encoding="utf-8").replace(
                'transport = "acp"\n', "", 1
            )
            config.write_text(missing, encoding="utf-8")
            with self.assertRaisesRegex(agent_team.ConfigError, "transport"):
                agent_team.load_config(config)

            # Main is direct and Planner is read-only ACP in the fixture. Moving
            # ACP to a workspace-write role must be rejected instead of weakening
            # the permission boundary.
            valid_config = missing.replace(
                '[roles.planner]\nprovider = "claude"\n',
                '[roles.planner]\nprovider = "claude"\ntransport = "acp"\n',
            )
            invalid_worker = valid_config.replace(
                '[roles.worker]\nprovider = "codex"\ntransport = "direct"',
                '[roles.worker]\nprovider = "codex"\ntransport = "acp"',
            )
            config.write_text(invalid_worker, encoding="utf-8")
            with self.assertRaisesRegex(agent_team.ConfigError, "workspace-write"):
                agent_team.load_config(config)

    def test_acp_argv_has_exact_adapter_pin_and_read_only_controls(self) -> None:
        argv = agent_team.acp_argv(
            workspace=REPO_ROOT,
            agent_command="env AGENT_TEAM_ACP_MARKER=team-test npx -y @agentclientprotocol/claude-agent-acp@0.70.0",
            model="fable",
            instructions="日本語のPlanner指示。",
            operation=("prompt", "--session", "team-test", "--file", "/tmp/prompt.md"),
        )

        self.assertEqual(argv[:3], ["npx", "-y", "acpx@0.13.2"])
        self.assertIn("@agentclientprotocol/claude-agent-acp@0.70.0", " ".join(argv))
        self.assertIn("--auth-policy", argv)
        self.assertIn("fail", argv)
        self.assertIn("--approve-reads", argv)
        self.assertIn("--non-interactive-permissions", argv)
        self.assertIn("fail", argv)
        self.assertIn("--no-fs", argv)
        self.assertIn("--no-terminal", argv)
        self.assertIn("--model", argv)
        self.assertIn("fable", argv)
        self.assertIn("--append-system-prompt", argv)
        self.assertIn("日本語のPlanner指示。", argv)
        self.assertEqual(argv[argv.index("--format") + 1], "quiet")
        self.assertEqual(
            _acp_operation(argv),
            ["prompt", "--session", "team-test", "--file", "/tmp/prompt.md"],
        )

    def test_acp_argv_uses_quiet_output_for_each_lifecycle_operation(self) -> None:
        operations = (
            ("sessions", "new", "--name", "team-test"),
            ("set", "effort", "high", "--session", "team-test"),
            ("prompt", "--session", "team-test", "--file", "-"),
            ("sessions", "close", "team-test"),
            ("sessions", "prune", "--include-history"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                argv = agent_team.acp_argv(
                    workspace=REPO_ROOT,
                    agent_command="env AGENT_TEAM_ACP_MARKER=team-test npx -y @agentclientprotocol/claude-agent-acp@0.70.0",
                    model="fable",
                    instructions="planner",
                    operation=operation,
                )
                self.assertEqual(argv[argv.index("--format") + 1], "quiet")
                self.assertNotIn("--json-strict", argv)
                self.assertEqual(_acp_operation(argv), list(operation))

    def test_acp_environment_keeps_only_subscription_runtime_inputs(self) -> None:
        supplied = {
            "PATH": "/bin",
            "HOME": "/tmp/home",
            "TMPDIR": "/tmp",
            "SHELL": "/bin/zsh",
            "USER": "tester",
            "LOGNAME": "tester",
            "LANG": "ja_JP.UTF-8",
            "LC_ALL": "ja_JP.UTF-8",
            "ANTHROPIC_API_KEY": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "OPENAI_API_KEY": "secret",
            "NPM_TOKEN": "secret",
            "NODE_OPTIONS": "--require=evil.js",
            "ORCA_SOCKET": "/tmp/orca.sock",
            "AGENT_TEAM_STATE_PATH": "/tmp/state.json",
        }
        with mock.patch.dict(os.environ, supplied, clear=True):
            environment = agent_team.acp_env()

        self.assertEqual(
            environment,
            {
                "PATH": "/bin",
                "HOME": "/tmp/home",
                "TMPDIR": "/tmp",
                "SHELL": "/bin/zsh",
                "USER": "tester",
                "LOGNAME": "tester",
                "LANG": "ja_JP.UTF-8",
                "LC_ALL": "ja_JP.UTF-8",
            },
        )

    def test_state_requires_private_regular_file_and_complete_runtime_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "state.json"
            state = {
                "version": 3,
                "runtime": "orca",
                "team_id": "agent-team-project-1",
                "workspace": str(root),
                "config_path": str(root / "config.toml"),
                "state_path": str(path),
                "launcher_path": str(LAUNCHER),
                "worktree_id": "repo::/project",
                "orca_socket": str(root / "orca.sock"),
                "run_id": "run_1",
                "main_terminal": "term_main",
                "role_specs": {},
                "roles": {},
            }
            path.write_text(json.dumps(state), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(agent_team.ConfigError, "0600"):
                agent_team.read_state(path)

            path.unlink()
            target = root / "real-state.json"
            target.write_text(json.dumps(state), encoding="utf-8")
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaisesRegex(agent_team.ConfigError, "symlink"):
                agent_team.read_state(path)

    def test_acp_runner_command_requires_explicit_state_path(self) -> None:
        state = {
            "team_id": "agent-team-project-1",
            "launcher_path": str(LAUNCHER),
            "state_path": "/tmp/state.json",
            "config_path": "/tmp/config.toml",
            "workspace": "/tmp/project",
        }
        command = agent_team.acp_runner_command(
            state,
            "planner",
            task_id="task_1",
            dispatch_id="dispatch_1",
            terminal_handle="term_worker",
            prompt_path=Path("/tmp/prompt.md"),
            launch_nonce="nonce1234",
        )
        self.assertIn("--state /tmp/state.json", command)
        self.assertNotIn("--config", command)
        self.assertNotIn("--cwd", command)

        state.pop("state_path")
        with self.assertRaisesRegex(agent_team.ConfigError, "state_path"):
            agent_team.acp_runner_command(
                state,
                "planner",
                task_id="task_1",
                dispatch_id="dispatch_1",
                terminal_handle="term_worker",
                prompt_path=Path("/tmp/prompt.md"),
                launch_nonce="nonce1234",
            )

    def test_run_acpx_kills_process_group_after_bounded_timeout(self) -> None:
        process = mock.Mock()
        process.pid = 4242
        timeout = subprocess.TimeoutExpired(["npx"], 1)
        process.communicate.side_effect = [timeout, timeout, ("", "partial stderr")]
        with (
            mock.patch.object(subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(agent_team.os, "killpg") as killpg,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            agent_team.run_acpx(["npx", "acpx"], cwd=Path("/tmp"), timeout_seconds=1)

        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(4242, signal.SIGTERM),
                mock.call(4242, signal.SIGKILL),
            ],
        )

    def test_worker_done_send_uses_bounded_orca_timeout(self) -> None:
        state = {
            "workspace": "/tmp/project",
            "run_id": "run_1",
        }
        assignment = {
            "task_id": "task_1",
            "dispatch_id": "dispatch_1",
            "terminal_handle": "term_worker",
        }
        with (
            mock.patch.object(
                agent_team,
                "run_orca",
                side_effect=subprocess.TimeoutExpired(["orca"], 30),
            ) as run,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            agent_team._send_worker_done(
                state, assignment, outcome="failed", body="timeout"
            )

        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 30)

    def test_acp_runner_cleans_session_after_creation_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config_path = self.make_config(root)
            state_path = root / "state" / "state.json"
            prompt_path = agent_team.create_prompt_file(
                state_path.parent, "planner", "nonce1234", "read the repository"
            )
            (root / "provider-private").mkdir()
            (root / "snapshot").mkdir()
            assignment = {
                "task_id": "task_1",
                "dispatch_id": "dispatch_1",
                "terminal_handle": "term_worker",
                "completion_observed": False,
                "launcher_owned_terminal": True,
                "prompt_path": str(prompt_path),
                "launch_nonce": "nonce1234",
                "agent_command": agent_team.acp_agent_command(
                    "agent-team-project-1", "planner", "nonce1234"
                ),
                "session_name": agent_team.acp_session_name("planner", "nonce1234"),
                "execution": "background",
                "adapter_id": "claude-acp-0.70.0",
                "provider_private_root": str(root / "provider-private"),
                "snapshot_root": str(root / "snapshot"),
                "adapter_snapshot": {
                    "adapter_id": "claude-acp-0.70.0",
                    "revision": "acpx@0.13.2",
                    "executable": "npx",
                    "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
                    "identity": {
                        "device": 0,
                        "inode": 0,
                        "size": 0,
                        "mtime_ns": 0,
                        "sha256": "acpx-managed",
                    },
                },
            }
            state = {
                "version": 3,
                "runtime": "orca",
                "team_id": "agent-team-project-1",
                "workspace": str(workspace),
                "config_path": str(config_path),
                "state_path": str(state_path),
                "launcher_path": str(LAUNCHER),
                "worktree_id": "repo::/project",
                "orca_socket": str(root / "orca.sock"),
                "run_id": "run_1",
                "main_terminal": "term_main",
                "role_specs": {
                    "planner": {
                        "provider": "claude",
                        "transport": "acp",
                        "model": "fable",
                        "effort": "high",
                        "permission": "read-only",
                        "execution": "background",
                        "adapter_id": "claude-acp-0.70.0",
                        "instructions": "snapshot planner instructions",
                    }
                },
                "roles": {"planner": assignment},
            }
            agent_team.write_state(state_path, state)
            failed_create = subprocess.CompletedProcess(
                ["npx"], 1, "", "session creation failed"
            )
            successful_cleanup = subprocess.CompletedProcess(["npx"], 0, "", "")
            orca_calls: list[list[str]] = []
            with (
                mock.patch.object(
                    agent_team,
                    "run_acpx",
                    side_effect=[failed_create, successful_cleanup, successful_cleanup],
                ) as acpx,
                mock.patch.object(
                    agent_team,
                    "run_orca",
                    side_effect=lambda args, *, cwd, check=True, timeout_seconds=None: (
                        orca_calls.append(args)
                        or subprocess.CompletedProcess(
                            ["orca", *args],
                            0,
                            '{"ok":true,"result":{}}',
                            "",
                        )
                    ),
                ),
            ):
                result = agent_team.acp_run(
                    role="planner",
                    state_path=state_path,
                    task_id="task_1",
                    dispatch_id="dispatch_1",
                    terminal_handle="term_worker",
                    prompt_path=prompt_path,
                    launch_nonce="nonce1234",
                )

        self.assertEqual(result, 1)
        self.assertEqual(acpx.call_count, 3)
        operations = []
        for call in acpx.call_args_list[1:]:
            argv = call.args[0]
            operations.append(_acp_operation(argv))
        self.assertEqual(
            operations,
            [
                ["sessions", "close", "agent-team-planner-nonce1234"],
                ["sessions", "prune", "--include-history"],
            ],
        )
        self.assertEqual([args[:2] for args in orca_calls], [["orchestration", "send"]])

    def test_prompt_file_is_private_regular_and_state_directory_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompt = agent_team.create_prompt_file(root, "planner", "nonce123", "内容")

            self.assertEqual(prompt.parent, root.resolve())
            self.assertFalse(prompt.is_symlink())
            self.assertTrue(stat.S_ISREG(prompt.stat().st_mode))
            self.assertEqual(stat.S_IMODE(prompt.stat().st_mode), 0o600)
            self.assertEqual(prompt.read_text(encoding="utf-8"), "内容")
            agent_team.validate_prompt_file(
                prompt, root, role="planner", launch_nonce="nonce123"
            )

            link = root / "prompt-planner-link.md"
            link.symlink_to(prompt)
            with self.assertRaisesRegex(agent_team.ConfigError, "symlink"):
                agent_team.validate_prompt_file(link, root)

    def test_state_version_one_is_rejected_without_implicit_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(
                json.dumps({"version": 1, "runtime": "orca"}), encoding="utf-8"
            )
            path.chmod(0o600)

            with self.assertRaisesRegex(agent_team.ConfigError, "version 3"):
                agent_team.read_state(path)

    def test_state_version_two_is_rejected_without_implicit_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(
                json.dumps({"version": 2, "runtime": "orca"}), encoding="utf-8"
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(agent_team.ConfigError, "version 3"):
                agent_team.read_state(path)

    def test_acp_runner_sets_effort_before_prompt_and_sends_one_worker_done(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config_path = self.make_config(root)
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
                config = agent_team.load_config(config_path)
                plan = agent_team.build_plan(config, workspace)
                state_path = Path(plan["state_path"])
                nonce = "nonce1234"
                prompt_path = agent_team.create_prompt_file(
                    state_path.parent, "planner", nonce, "read the repository"
                )
                (root / "provider-private").mkdir()
                (root / "snapshot").mkdir()
                team_id = str(plan["team_id"])
                assignment = {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                    "prompt_path": str(prompt_path),
                    "launch_nonce": nonce,
                    "agent_command": agent_team.acp_agent_command(
                        team_id, "planner", nonce
                    ),
                    "session_name": agent_team.acp_session_name("planner", nonce),
                    "execution": "background",
                    "adapter_id": "claude-acp-0.70.0",
                    "provider_private_root": str(root / "provider-private"),
                    "snapshot_root": str(root / "snapshot"),
                    "adapter_snapshot": {
                        "adapter_id": "claude-acp-0.70.0",
                        "revision": "acpx@0.13.2",
                        "executable": "npx",
                        "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
                        "identity": {
                            "device": 0,
                            "inode": 0,
                            "size": 0,
                            "mtime_ns": 0,
                            "sha256": "acpx-managed",
                        },
                    },
                }
                state = {
                    "version": 3,
                    "runtime": "orca",
                    "team_id": team_id,
                    "workspace": str(workspace),
                    "config_path": str(config_path),
                    "state_path": str(state_path),
                    "launcher_path": str(LAUNCHER),
                    "worktree_id": "repo::/project",
                    "orca_socket": str(root / "orca.sock"),
                    "run_id": "run_1",
                    "main_terminal": "term_main",
                    "role_specs": {
                        "planner": {
                            "provider": "claude",
                            "transport": "acp",
                            "model": "fable",
                            "effort": "high",
                            "permission": "read-only",
                            "execution": "background",
                            "adapter_id": "claude-acp-0.70.0",
                            "instructions": "snapshot planner instructions",
                        },
                    },
                    "roles": {"planner": assignment},
                }
                agent_team.write_state(state_path, state)
                stream = "plan"
                acpx_results = [
                    subprocess.CompletedProcess([], 0, "{}\n", ""),
                    subprocess.CompletedProcess([], 0, "{}\n", ""),
                    subprocess.CompletedProcess([], 0, stream, ""),
                    subprocess.CompletedProcess([], 0, "{}\n", ""),
                    subprocess.CompletedProcess([], 0, "{}\n", ""),
                ]
                orca_calls: list[list[str]] = []
                with (
                    mock.patch.object(
                        agent_team, "run_acpx", side_effect=acpx_results
                    ) as acpx,
                    mock.patch.object(
                        agent_team,
                        "run_orca",
                        side_effect=lambda args, *, cwd, check=True, timeout_seconds=None: (
                            orca_calls.append(args)
                            or subprocess.CompletedProcess(
                                ["orca", *args],
                                0,
                                '{"ok":true,"result":{}}',
                                "",
                            )
                        ),
                    ),
                    mock.patch.object(
                        agent_team,
                        "load_config",
                        side_effect=AssertionError(
                            "ACP runner must use state snapshot"
                        ),
                    ),
                ):
                    result = agent_team.acp_run(
                        role="planner",
                        state_path=state_path,
                        task_id="task_1",
                        dispatch_id="dispatch_1",
                        terminal_handle="term_worker",
                        prompt_path=prompt_path,
                        launch_nonce=nonce,
                    )

            self.assertEqual(result, 0)
            self.assertEqual(acpx.call_count, 5)
            operations = []
            for call in acpx.call_args_list:
                argv = call.args[0]
                operations.append(_acp_operation(argv))
            self.assertEqual(
                operations,
                [
                    ["sessions", "new", "--name", assignment["session_name"]],
                    ["set", "effort", "high", "--session", assignment["session_name"]],
                    ["prompt", "--session", assignment["session_name"], "--file", "-"],
                    ["sessions", "close", assignment["session_name"]],
                    ["sessions", "prune", "--include-history"],
                ],
            )
            self.assertEqual(
                acpx.call_args_list[2].kwargs.get("input_text"),
                "read the repository",
            )
            self.assertEqual(
                acpx.call_args_list[2].args[0][-2:],
                ["--file", "-"],
            )
            self.assertEqual(
                [args[:2] for args in orca_calls], [["orchestration", "send"]]
            )
            self.assertEqual(
                orca_calls[0][orca_calls[0].index("--task-id") + 1], "task_1"
            )
            self.assertEqual(
                orca_calls[0][orca_calls[0].index("--dispatch-id") + 1], "dispatch_1"
            )
            self.assertEqual(
                orca_calls[0][orca_calls[0].index("--from") + 1], "term_worker"
            )

    def test_acp_runner_requires_nonempty_bounded_prompt_stdout(self) -> None:
        state = {"workspace": "/tmp/project", "run_id": "run_1"}
        assignment = {
            "task_id": "task_1",
            "dispatch_id": "dispatch_1",
            "terminal_handle": "term_worker",
            "agent_command": "env AGENT_TEAM_ACP_MARKER=team-test npx -y claude-acp",
        }
        spec = {
            "transport": "acp",
            "provider": "claude",
            "permission": "read-only",
            "model": "fable",
            "effort": "high",
            "instructions": "planner",
        }
        for prompt_stdout in ("", "x" * (agent_team.MAX_ACP_OUTPUT_CHARS + 1)):
            with self.subTest(output_length=len(prompt_stdout)):
                acpx_results = [
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, prompt_stdout, ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ]
                with (
                    mock.patch.object(agent_team, "read_state", return_value=state),
                    mock.patch.object(
                        agent_team, "_acp_assignment", return_value=(assignment, spec)
                    ),
                    mock.patch.object(
                        agent_team, "read_prompt_file", return_value="prompt"
                    ),
                    mock.patch.object(agent_team, "run_acpx", side_effect=acpx_results),
                    mock.patch.object(agent_team, "_send_worker_done") as send,
                ):
                    result = agent_team.acp_run(
                        role="planner",
                        state_path=Path("/tmp/state.json"),
                        task_id="task_1",
                        dispatch_id="dispatch_1",
                        terminal_handle="term_worker",
                        prompt_path=Path("/tmp/prompt.md"),
                        launch_nonce="nonce1234",
                    )

                self.assertEqual(result, 1)
                send.assert_called_once()
                self.assertEqual(send.call_args.kwargs["outcome"], "failed")

    def test_version_two_is_rejected_instead_of_silently_using_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "version = 3", "version = 2"
                ),
                encoding="utf-8",
            )
            result = self.run_launcher(config, workspace, "start", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("version must be integer 3", result.stderr)

    def test_config_version_requires_integer_three(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "version = 3", "version = 3.0"
                ),
                encoding="utf-8",
            )
            result = self.run_launcher(config, workspace, "start", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("version must be integer 3", result.stderr)

    def test_config_rejects_non_orca_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'runtime = "orca"', 'runtime = "herdr"'
                ),
                encoding="utf-8",
            )
            result = self.run_launcher(config, workspace, "start", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("runtime must be 'orca'", result.stderr)

    def test_config_rejects_write_enabled_claude_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root, worker_provider="claude")
            result = self.run_launcher(config, workspace, "start", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("not runnable", result.stderr)

    def test_prompts_are_japanese_and_main_uses_orca_contract(self) -> None:
        config = agent_team.load_config(agent_team.default_config_path())
        instructions = agent_team.role_instructions(
            "main", config, Path("/tmp/state.json")
        )
        self.assertIn("ユーザーと対話する唯一のエージェント", instructions)
        self.assertIn("Orca", instructions)
        self.assertIn("Task", instructions)
        self.assertIn("Dispatch", instructions)
        self.assertIn("role_release", instructions)
        self.assertIn("delivery_ack", instructions)
        self.assertIn("同時にactiveにできるroleは1つだけ", instructions)
        self.assertIn("`worker_done`だけが終端通知", instructions)
        self.assertIn("終端通知より前に`role_read`や`role_release`", instructions)
        self.assertIn("Reviewerの`worker_done`後", instructions)
        self.assertIn("`ASK_USER`ならWorkerを起動せず", instructions)
        self.assertIn("変更品質の判定はReviewer", instructions)
        self.assertIn("`ASK_USER`も判定1回", instructions)
        self.assertIn("元roleとDelivery ID", instructions)
        self.assertIn("互換経路の削除、廃止、非互換化", instructions)
        self.assertIn("`outcome=failed`は終端でも成功ではありません", instructions)
        self.assertIn("`outcome=succeeded`を確認した場合だけ起動", instructions)
        self.assertIn("`CHANGES_REQUESTED`後の修正と再試行", instructions)
        self.assertNotIn("Herdr", instructions)

    def test_codex_runtime_home_links_only_auth_instructions_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            normal_home = root / "normal-codex"
            normal_home.mkdir()
            (normal_home / "auth.json").write_text("{}\n", encoding="utf-8")
            (normal_home / "AGENTS.md").write_text("# global rules\n", encoding="utf-8")
            (normal_home / "skills").mkdir()
            (normal_home / "config.toml").write_text(
                "web_search = 'live'\n", encoding="utf-8"
            )
            config = self.make_config(root)
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(normal_home),
                    "XDG_STATE_HOME": str(root / "state"),
                },
            ):
                plan = agent_team.build_plan(agent_team.load_config(config), workspace)
                agent_team.prepare_codex_homes(plan)

            runtime_homes = [
                Path(role["env"]["CODEX_HOME"])
                for role in plan["roles"].values()
                if role["provider"] == "codex"
            ]
            for runtime_home in runtime_homes:
                self.assertTrue((runtime_home / "auth.json").is_symlink())
                self.assertTrue((runtime_home / "AGENTS.md").is_symlink())
                self.assertTrue((runtime_home / "skills").is_symlink())
                self.assertFalse((runtime_home / "config.toml").exists())

    def test_runtime_socket_profile_keeps_base_permission_and_only_allows_orca_socket(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            socket_path = root / "orca.sock"
            plan = agent_team.build_plan(
                agent_team.load_config(self.make_config(root)),
                workspace,
                socket_path,
            )

        worker_args = " ".join(plan["roles"]["worker"]["argv"])
        reviewer_args = " ".join(plan["roles"]["reviewer"]["argv"])
        self.assertIn('extends=":workspace"', worker_args)
        self.assertIn('extends=":read-only"', reviewer_args)
        self.assertIn("features.network_proxy=true", worker_args)
        self.assertIn(str(socket_path), worker_args)
        self.assertIn('default_permissions="agent_team_workspace"', worker_args)


class AgentTeamStartTest(AgentTeamTestCase):
    def make_fake_bin(self, root: Path) -> tuple[Path, Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        log_path = root / "orca-log.jsonl"
        fake_orca = fake_bin / "orca"
        fake_orca_linux = fake_bin / "orca-ide"
        fake_orca.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import hashlib, json, os, sys
                from pathlib import Path
                args = sys.argv[1:]
                with Path(os.environ["FAKE_ORCA_LOG"]).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(args) + "\\n")
                fail = os.environ.get("FAKE_ORCA_FAIL")
                if fail and " ".join(args).startswith(fail):
                    print(json.dumps({{"ok": False, "error": {{"message": "forced failure"}}}}))
                    raise SystemExit(1)
                if args == ["status", "--json"]:
                    print(json.dumps({{"ok": True, "result": {{"runtime": {{"state": "ready"}}, "graph": {{"state": "ready"}}}}}}))
                elif args == ["worktree", "current", "--json"]:
                    if os.environ.get("FAKE_ORCA_MANAGED") == "0":
                        print(json.dumps({{"ok": False, "error": {{"message": "not managed"}}}})); raise SystemExit(1)
                    print(json.dumps({{"ok": True, "result": {{"worktree": {{"id": "repo::/project", "path": str(Path.cwd().parent)}}}}}}))
                elif args[:2] == ["worktree", "show"]:
                    if os.environ.get("FAKE_ORCA_MANAGED") == "0":
                        print(json.dumps({{"ok": False, "error": {{"message": "not managed"}}}})); raise SystemExit(1)
                    requested = args[args.index("--worktree") + 1]
                    expected = "path:" + str(Path.cwd().resolve())
                    path = str(Path.cwd()) if requested == expected else str(Path.cwd().parent)
                    print(json.dumps({{"ok": True, "result": {{"worktree": {{"id": "repo::/project", "path": path}}}}}}))
                elif args[:2] == ["terminal", "create"]:
                    title = args[args.index("--title") + 1]
                    print(json.dumps({{"ok": True, "result": {{"terminal": {{"handle": "term_main", "worktreeId": "repo::/project", "title": title}}}}}}))
                elif args[:2] == ["terminal", "wait"]:
                    terminal = args[args.index("--terminal") + 1]
                    condition = args[args.index("--for") + 1]
                    print(json.dumps({{"ok": True, "result": {{"wait": {{"handle": terminal, "condition": condition, "satisfied": True}}}}}}))
                elif args[:2] == ["orchestration", "run-create"]:
                    objective = args[args.index("--objective") + 1]
                    coordinator = args[args.index("--from") + 1]
                    print(json.dumps({{"ok": True, "result": {{"run": {{"id": "run_1", "objective": objective, "coordinator_handle": coordinator}}}}}}))
                elif args[:2] in (["terminal", "switch"], ["terminal", "close"]):
                    if args[:2] == ["terminal", "close"]:
                        terminal = args[args.index("--terminal") + 1]
                        print(json.dumps({{"ok": True, "result": {{"close": {{"handle": terminal, "ptyKilled": True}}}}}}))
                    else:
                        terminal = args[args.index("--terminal") + 1]
                        print(json.dumps({{"ok": True, "result": {{"focus": {{"handle": terminal, "navigated": True}}}}}}))
                elif args[:2] == ["orchestration", "run-show"]:
                    digest = hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:8]
                    objective = f"agent-team-project-{{digest}}: Planner / Worker / Reviewer coordination for {{Path.cwd()}}"
                    print(json.dumps({{"ok": True, "result": {{"run": {{"id": "run_1", "objective": objective, "coordinator_handle": "term_main"}}}}}}))
                elif args[:2] == ["terminal", "show"]:
                    terminal = args[args.index("--terminal") + 1]
                    digest = hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:8]
                    team = "agent-team-" + Path.cwd().name + "-" + digest
                    title = team + "-main" if terminal == "term_main" else team + "-worker"
                    print(json.dumps({{"ok": True, "result": {{"terminal": {{"handle": terminal, "worktreeId": "repo::/project", "title": title, "worktreePath": str(Path.cwd())}}}}}}))
                elif args[:2] == ["orchestration", "worker-list"]:
                    print(json.dumps({{"ok": True, "result": {{"workers": []}}}}))
                elif args[:2] == ["orchestration", "worker-show"]:
                    dispatch = args[args.index("--dispatch") + 1]
                    print(json.dumps({{"ok": True, "result": {{"dispatch": {{"id": dispatch, "task_id": "task_worker", "run_id": "run_1", "assignee_handle": "term_worker"}}, "worker": {{"dispatch_id": dispatch, "worktree_id": "repo::/project", "agent_terminal_handle": "term_worker", "state": "ready"}}}}}}))
                elif args[:2] == ["orchestration", "worker-stop"]:
                    dispatch = args[args.index("--dispatch") + 1]
                    print(json.dumps({{"ok": True, "result": {{"dispatchId": dispatch, "state": "stopped", "processAction": "closed_agent_terminal", "alreadySettled": False, "close": {{"ptyKilled": True}}}}}}))
                else:
                    print(json.dumps({{"ok": False, "error": {{"message": "unsupported"}}}})); raise SystemExit(2)
                """
            ),
            encoding="utf-8",
        )
        fake_orca.chmod(0o755)
        fake_orca_linux.write_text(
            fake_orca.read_text(encoding="utf-8"), encoding="utf-8"
        )
        fake_orca_linux.chmod(0o755)
        for binary in ("claude", "codex", "npx"):
            path = fake_bin / binary
            path.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            path.chmod(0o755)
        return fake_bin, log_path

    def live_env(
        self,
        root: Path,
        fake_bin: Path,
        log_path: Path,
        *,
        managed: bool = True,
        fail: str = "",
    ) -> dict[str, str]:
        normal_codex = root / "codex-home"
        normal_codex.mkdir()
        (normal_codex / "auth.json").write_text("{}\n", encoding="utf-8")
        orca_data = root / "orca-data"
        orca_data.mkdir()
        (orca_data / "orca-runtime.json").write_text(
            json.dumps(
                {"transports": [{"kind": "unix", "endpoint": str(root / "orca.sock")}]}
            ),
            encoding="utf-8",
        )
        return {
            "PATH": str(fake_bin) + os.pathsep + str(Path(sys.executable).parent),
            "FAKE_ORCA_LOG": str(log_path),
            "FAKE_ORCA_MANAGED": "1" if managed else "0",
            "FAKE_ORCA_FAIL": fail,
            "CODEX_HOME": str(normal_codex),
            "XDG_STATE_HOME": str(root / "state"),
            "ORCA_USER_DATA_PATH": str(orca_data),
        }

    def test_stop_tree_deletes_current_owner_runtime_files_and_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            env = self.live_env(root, fake_bin, log_path)
            started = self.run_launcher(
                config, workspace, "start", "--no-attach", env=env
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            state_path = Path(json.loads(started.stdout)["state_path"])
            state_root = state_path.parent
            herdr_log = state_root / "herdr-server.log"
            herdr_log.write_text("runtime output", encoding="utf-8")
            cache_dir = state_root / "codex" / "worker" / "cache"
            cache_dir.mkdir()
            (cache_dir / "logs_2.sqlite-wal").write_text("wal", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            link = state_root / "runtime-link"
            link.symlink_to(outside)

            stopped = self.run_launcher(config, workspace, "stop", env=env)
            state_root_removed = not state_root.exists()
            outside_remains = outside.exists()
            commands = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertTrue(state_root_removed)
        self.assertTrue(outside_remains)
        self.assertIn(
            ["terminal", "close", "--terminal", "term_main", "--json"],
            commands,
        )

    def test_stop_tree_rejects_special_files_before_remote_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            env = self.live_env(root, fake_bin, log_path)
            started = self.run_launcher(
                config, workspace, "start", "--no-attach", env=env
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            state_path = Path(json.loads(started.stdout)["state_path"])
            special = state_path.parent / "runtime.sock"
            os.mkfifo(special)

            stopped = self.run_launcher(config, workspace, "stop", env=env)
            state_root_exists = state_path.parent.exists()
            special_exists = special.exists()
            commands = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("special", stopped.stderr)
        self.assertTrue(state_root_exists)
        self.assertTrue(special_exists)
        self.assertFalse(any(row[:2] == ["terminal", "close"] for row in commands[6:]))

    def test_state_tree_race_does_not_follow_swapped_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "agent-team-race"
            state_root.mkdir(mode=0o700)
            state_path = state_root / "state.json"
            state_path.write_text(
                json.dumps({"state_path": str(state_path), "team_id": state_root.name}),
                encoding="utf-8",
            )
            state_path.chmod(0o600)
            nested = state_root / "nested"
            nested.mkdir(mode=0o700)
            (nested / "inside.txt").write_text("inside", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            outside_sentinel = outside / "sentinel.txt"
            outside_sentinel.write_text("must survive", encoding="utf-8")

            real_scandir = agent_team_runtime.os.scandir
            scandir_calls = 0
            swapped = False

            class ScandirResult(list[object]):
                def __enter__(self) -> Self:
                    return self

                def __exit__(self, *args: object) -> None:
                    return None

            class RaceEntry:
                def __init__(self, entry: object) -> None:
                    self._entry = entry
                    self.name = entry.name
                    self.path = entry.path

                def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
                    nonlocal swapped
                    result = self._entry.stat(follow_symlinks=follow_symlinks)
                    if not swapped:
                        backup = state_root / "nested-real"
                        nested.rename(backup)
                        nested.symlink_to(outside, target_is_directory=True)
                        swapped = True
                    return result

            def raced_scandir(target: object) -> ScandirResult:
                nonlocal scandir_calls
                scandir_calls += 1
                entries = list(real_scandir(target))
                if scandir_calls == 3:
                    return ScandirResult(
                        [
                            RaceEntry(entry) if entry.name == "nested" else entry
                            for entry in entries
                        ]
                    )
                return ScandirResult(entries)

            with (
                mock.patch.object(
                    agent_team_runtime.os, "scandir", side_effect=raced_scandir
                ),
                self.assertRaises((agent_team_runtime.RuntimeValidationError, OSError)),
            ):
                agent_team_runtime.remove_state_tree(
                    state_path,
                    {"state_path": str(state_path), "team_id": state_root.name},
                )

            outside_survives = outside_sentinel.exists()

        self.assertTrue(outside_survives)

    def test_start_creates_main_terminal_then_binds_run_and_focuses_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            result = self.run_launcher(
                config,
                workspace,
                "start",
                env=self.live_env(root, fake_bin, log_path),
            )
            log = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["run_id"], "run_1")
        self.assertEqual(payload["main_terminal"], "term_main")
        self.assertEqual(
            [row[:2] for row in log],
            [
                ["status", "--json"],
                ["worktree", "show"],
                ["terminal", "create"],
                ["terminal", "show"],
                ["terminal", "wait"],
                ["orchestration", "run-create"],
                ["orchestration", "run-show"],
                ["terminal", "switch"],
            ],
        )
        create = next(row for row in log if row[:2] == ["terminal", "create"])
        command = create[create.index("--command") + 1]
        self.assertIn("_role-run main", command)
        self.assertIn("--orca-socket", command)
        self.assertNotIn("herdr", command.lower())

    def test_start_requires_binaries_only_for_direct_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    '[main]\nprovider = "claude"',
                    '[main]\nprovider = "codex"',
                ),
                encoding="utf-8",
            )
            fake_bin, log_path = self.make_fake_bin(root)
            (fake_bin / "claude").unlink()
            result = self.run_launcher(
                config,
                workspace,
                "start",
                "--no-attach",
                env=self.live_env(root, fake_bin, log_path),
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unmanaged_checkout_fails_before_terminal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            result = self.run_launcher(
                config,
                workspace,
                "start",
                env=self.live_env(root, fake_bin, log_path, managed=False),
            )
            log = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("orca repo add", result.stderr)
        self.assertFalse(any(row[:2] == ["terminal", "create"] for row in log))

    def test_run_create_failure_closes_only_created_main_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            result = self.run_launcher(
                config,
                workspace,
                "start",
                env=self.live_env(
                    root, fake_bin, log_path, fail="orchestration run-create"
                ),
            )
            log = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertNotEqual(result.returncode, 0)
        closes = [row for row in log if row[:2] == ["terminal", "close"]]
        self.assertEqual(
            closes,
            [
                [
                    "terminal",
                    "close",
                    "--terminal",
                    "term_main",
                    "--json",
                ]
            ],
        )

    def test_focus_failure_reports_warning_without_stopping_running_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            result = self.run_launcher(
                config,
                workspace,
                "start",
                env=self.live_env(root, fake_bin, log_path, fail="terminal switch"),
            )
            payload = json.loads(result.stdout)
            state_path = Path(payload["state_path"])
            state_exists = state_path.exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "running")
        self.assertIn("focus_warning", payload)
        self.assertTrue(state_exists)

    def test_status_attach_and_stop_use_saved_exact_orca_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            env = self.live_env(root, fake_bin, log_path)
            started = self.run_launcher(
                config, workspace, "start", "--no-attach", env=env
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            status = self.run_launcher(config, workspace, "status", env=env)
            attach = self.run_launcher(config, workspace, "attach", "main", env=env)
            stop = self.run_launcher(config, workspace, "stop", env=env)
            log = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(attach.returncode, 0, attach.stderr)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertIn(["terminal", "switch", "--terminal", "term_main", "--json"], log)
        self.assertIn(
            [
                "terminal",
                "close",
                "--terminal",
                "term_main",
                "--json",
            ],
            log,
        )

    def test_stop_closes_launcher_owned_worker_and_main_terminals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            config = self.make_config(root)
            fake_bin, log_path = self.make_fake_bin(root)
            env = self.live_env(root, fake_bin, log_path)
            started = self.run_launcher(
                config, workspace, "start", "--no-attach", env=env
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            state_path = Path(json.loads(started.stdout)["state_path"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("instructions", state["role_specs"]["planner"])
            self.assertEqual(
                state["role_specs"]["planner"]["instructions"],
                "# planner\n\n日本語のplanner指示。",
            )
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            state_path.chmod(0o600)

            stopped = self.run_launcher(config, workspace, "stop", env=env)
            log = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        closes = [row for row in log if row[:2] == ["terminal", "close"]]
        self.assertNotIn(
            ["terminal", "close", "--terminal", "term_worker", "--json"],
            closes,
        )
        self.assertIn(
            ["terminal", "close", "--terminal", "term_main", "--json"],
            closes,
        )
        self.assertFalse(state_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
