from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import cli, mcp_server
from agent_team.acp_dependencies import AcpDependencyError, AcpExecutables
from agent_team.contracts import Role
from agent_team.runtime import build_acp_agent_command, build_acp_argv


class ResolvedAcpTest(unittest.TestCase):
    def make_executables(self, root: Path) -> AcpExecutables:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        node = bin_dir / "node"
        node.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo v22.23.2; fi\n',
            encoding="utf-8",
        )
        node.chmod(0o700)
        for package_name, version, command in (
            ("acpx", "0.13.2", "acpx"),
            (
                "@agentclientprotocol/claude-agent-acp",
                "0.70.0",
                "claude-agent-acp",
            ),
        ):
            package = root / "node_modules" / package_name
            (package / "dist").mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": package_name,
                        "version": version,
                        "bin": {command: "dist/cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            entry = package / "dist" / "cli.js"
            entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            entry.chmod(0o755)
            (bin_dir / command).symlink_to(entry)
        return AcpExecutables.resolve(path=str(bin_dir))

    def make_plan(self, *, acp: bool) -> dict[str, object]:
        roles: dict[str, dict[str, object]] = {}
        for role in cli.ALL_ROLES:
            selected_acp = acp and role == "planner"
            roles[role] = {
                "role": role,
                "provider": "claude" if role == "main" or selected_acp else "codex",
                "transport": "acp" if selected_acp else "direct",
                "model": "fable" if role in {"main", "planner"} else "gpt-6-astra",
                "effort": "high" if role != "worker" else "medium",
                "permission": (
                    "orchestrator"
                    if role == "main"
                    else "workspace-write"
                    if role == "worker"
                    else "read-only"
                ),
                "instructions": f"{role} instructions",
                "execution": "background" if selected_acp else "tui_direct",
                "adapter_id": "claude-acp-0.70.0" if selected_acp else None,
                "env": {},
                "argv": [],
            }
        return {
            "runtime": "orca",
            "team_id": "agent-team-test",
            "workspace": "/tmp/project",
            "config_path": "/tmp/config.toml",
            "state_path": "/tmp/state.json",
            "orca_socket": "/tmp/orca.sock",
            "roles": roles,
        }

    def test_runtime_acp_builders_use_resolved_absolute_node_and_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executables = self.make_executables(Path(directory))
            command = build_acp_agent_command(
                "agent-team-test",
                "planner",
                "nonce1234",
                executables=executables,
            )
            command_argv = shlex.split(command)
            argv = build_acp_argv(
                workspace=Path("/tmp/project"),
                agent_command=command,
                model="fable",
                instructions="planner",
                operation=("sessions", "new", "--name", "session"),
                timeout_seconds=900,
                executables=executables,
            )

        self.assertEqual(
            command_argv[:2],
            [
                "env",
                "AGENT_TEAM_ACP_MARKER=agent-team/agent-team-test/planner/nonce1234",
            ],
        )
        self.assertEqual(
            command_argv[2:], [str(executables.node), str(executables.agent)]
        )
        self.assertEqual(argv[:2], [str(executables.node), str(executables.client)])
        self.assertNotIn("npx", argv)
        self.assertNotIn("npm", argv)
        self.assertNotIn("npx", command_argv)
        self.assertNotIn("npm", command_argv)

    def test_cli_resolves_selected_acp_once_and_copies_snapshot_to_start_spec(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = self.make_executables(root)
            plan = self.make_plan(acp=True)
            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(cli.os, "access", return_value=True),
                mock.patch.object(cli, "mcp_server_path", return_value=root / "mcp"),
                mock.patch.object(
                    cli.AcpExecutables, "resolve", return_value=selected
                ) as resolve,
            ):
                cli._start_prerequisites(plan)
            self.assertEqual(resolve.call_count, 1)
            roles = plan["roles"]
            assert isinstance(roles, dict)
            self.assertEqual(roles["planner"]["acp_executables"], selected.as_dict())
            spec = cli._start_spec(plan, attach=False)

        self.assertEqual(
            spec.role_specs[Role.PLANNER].acp_executables,
            selected.as_dict(),
        )

    def test_cli_does_not_resolve_unselected_acp_dependencies(self) -> None:
        plan = self.make_plan(acp=False)
        with (
            mock.patch.object(cli, "require_binary"),
            mock.patch.object(cli.os, "access", return_value=True),
            mock.patch.object(cli, "mcp_server_path", return_value=Path("/tmp/mcp")),
            mock.patch.object(
                cli.AcpExecutables,
                "resolve",
                side_effect=AssertionError("unselected ACP must not resolve"),
            ),
        ):
            cli._start_prerequisites(plan)

    def test_cli_does_not_probe_node_when_dependency_file_is_missing(self) -> None:
        plan = self.make_plan(acp=True)
        with (
            mock.patch.object(cli, "require_binary"),
            mock.patch.object(cli.os, "access", return_value=True),
            mock.patch.object(cli, "mcp_server_path", return_value=Path("/tmp/mcp")),
            mock.patch.object(
                cli.AcpExecutables,
                "resolve",
                side_effect=AcpDependencyError("missing node, acpx, claude-agent-acp"),
            ),
            mock.patch.object(
                cli.subprocess,
                "run",
                side_effect=AssertionError(
                    "Node must not be probed before missing check"
                ),
            ),
            self.assertRaisesRegex(cli.ConfigError, "missing"),
        ):
            cli._start_prerequisites(plan)

    def test_mcp_rejects_missing_saved_acp_binding_before_task_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project").mkdir()
            state_path = root / "state.json"
            state = {
                "version": 3,
                "runtime": "orca",
                "team_id": "agent-team-test",
                "workspace": str(root / "project"),
                "config_path": str(root / "config.toml"),
                "state_path": str(state_path),
                "launcher_path": "/tmp/agent-team",
                "worktree_id": "repo::project",
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
                        "instructions": "planner",
                        "execution": "background",
                        "adapter_id": "claude-acp-0.70.0",
                    }
                },
                "roles": {},
            }
            mcp_server.runtime_write_state(state_path, state)
            with (
                mock.patch.object(
                    mcp_server,
                    "_create_task",
                    side_effect=AssertionError("Task must follow ACP preflight"),
                ),
                self.assertRaisesRegex(mcp_server.ToolInputError, "ACP executables"),
            ):
                mcp_server.start_acp_role(state_path, state, "planner", "work")

    def test_acp_runner_rejects_replaced_saved_binding_before_running_acpx(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executables = self.make_executables(root)
            state_path = root / "state" / "state.json"
            prompt_path = cli.create_prompt_file(
                state_path.parent, "planner", "nonce1234", "read-only work"
            )
            (root / "provider-private").mkdir()
            (root / "snapshot").mkdir()
            agent_command = build_acp_agent_command(
                "agent-team-test",
                "planner",
                "nonce1234",
                executables=executables,
            )
            assignment = {
                "task_id": "task_1",
                "dispatch_id": "dispatch_1",
                "terminal_handle": "term_worker",
                "completion_observed": False,
                "launcher_owned_terminal": True,
                "prompt_path": str(prompt_path),
                "launch_nonce": "nonce1234",
                "agent_command": agent_command,
                "session_name": "agent-team-planner-nonce1234",
                "execution": "background",
                "adapter_id": "claude-acp-0.70.0",
                "provider_private_root": str(root / "provider-private"),
                "snapshot_root": str(root / "snapshot"),
                "adapter_snapshot": mcp_server._acp_adapter_snapshot(executables),
            }
            state = {
                "version": 3,
                "runtime": "orca",
                "team_id": "agent-team-test",
                "workspace": str(root),
                "config_path": str(root / "config.toml"),
                "state_path": str(state_path),
                "launcher_path": "/tmp/agent-team",
                "worktree_id": "repo::project",
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
                        "instructions": "planner",
                        "execution": "background",
                        "adapter_id": "claude-acp-0.70.0",
                        "acp_executables": executables.as_dict(),
                    }
                },
                "roles": {"planner": assignment},
            }
            cli.write_state(state_path, state)
            cli._acp_assignment(
                state,
                "planner",
                state_path=state_path,
                task_id="task_1",
                dispatch_id="dispatch_1",
                terminal_handle="term_worker",
                prompt_path=prompt_path,
                launch_nonce="nonce1234",
            )
            executables.agent.write_text("replaced", encoding="utf-8")
            with self.assertRaisesRegex(cli.ConfigError, "changed"):
                cli._acp_assignment(
                    state,
                    "planner",
                    state_path=state_path,
                    task_id="task_1",
                    dispatch_id="dispatch_1",
                    terminal_handle="term_worker",
                    prompt_path=prompt_path,
                    launch_nonce="nonce1234",
                )


if __name__ == "__main__":
    unittest.main()
