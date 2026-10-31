from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from typing import ClassVar
from unittest import mock

from agent_team import cli, copilot_acp
from agent_team.acp_dependencies import AcpExecutables
from agent_team.adapters import ProcessResult
from agent_team.native_acp_dependencies import (
    CodexAcpExecutables,
    CopilotAcpExecutables,
    NativeAcpExecutables,
)

E1_MISSING = (
    "selected copilot ACP dependencies are unavailable: native Copilot ACP profile "
    "requires installed commands: node, copilot; install @github/copilot@1.0.91 and "
    "@agentclientprotocol/sdk@1.4.0 into one npm prefix (npm install --prefix DIR "
    "@github/copilot@1.0.91 @agentclientprotocol/sdk@1.4.0), then put "
    "DIR/node_modules/.bin and Node.js 22 or newer first on PATH"
)


def fake_copilot_executables(root: Path) -> CopilotAcpExecutables:
    return CopilotAcpExecutables(
        node=root / "node",
        loader=root / "node_modules" / "@github" / "copilot" / "npm-loader.js",
        copilot=root / "copilot",
        sdk=root / "sdk.js",
        node_sha256="a" * 64,
        copilot_sha256="b" * 64,
        sdk_sha256="c" * 64,
        package_manifest_sha256="d" * 64,
        platform_manifest_sha256="e" * 64,
        sdk_manifest_sha256="f" * 64,
    )


def fake_claude_executables(root: Path) -> NativeAcpExecutables:
    return NativeAcpExecutables(
        node=root / "node",
        agent=root / "claude-agent-acp.js",
        sdk=root / "claude-sdk.js",
        library=root / "claude-library.js",
        node_sha256="1" * 64,
        agent_sha256="2" * 64,
        sdk_sha256="3" * 64,
        library_sha256="4" * 64,
    )


def copilot_plan(root: Path, *, runtime: str = "tmux") -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir()
    return {
        "runtime": runtime,
        "team_id": "copilot-team",
        "workspace": str(workspace),
        "config_path": str(root / "team.toml"),
        "state_path": str(root / "state" / "state.json"),
        "roles": {
            "main": {
                "provider": "claude",
                "transport": "direct",
                "model": "fable",
                "effort": "high",
                "permission": "orchestrator",
                "instructions": "main",
                "execution": "tui_direct",
            },
            "reviewer": {
                "provider": "copilot",
                "transport": "acp",
                "model": "gpt-5.2",
                "effort": "high",
                "permission": "read-only",
                "instructions": "reviewer",
                "execution": "background",
                "adapter_id": copilot_acp.ADAPTER_ID,
            },
        },
        "task_specs": [],
    }


class CopilotCliWiringTest(unittest.TestCase):
    def test_missing_dependencies_fail_before_engine_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = copilot_plan(root)
            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(os, "access", return_value=True),
                mock.patch.object(
                    cli, "mcp_server_path", return_value=root / "mcp-server"
                ),
                mock.patch(
                    "agent_team.native_acp_dependencies.shutil.which",
                    return_value=None,
                ) as which,
                mock.patch.object(
                    subprocess,
                    "run",
                    side_effect=AssertionError("a version probe ran"),
                ) as version,
                mock.patch.object(
                    cli,
                    "_runtime_engine",
                    side_effect=AssertionError("the engine was created"),
                ) as engine,
                self.assertRaises(cli.ConfigError) as raised,
            ):
                cli.start_team(plan, attach=False)

            self.assertEqual(str(raised.exception), E1_MISSING)
            self.assertEqual(
                [call.args[0] for call in which.call_args_list], ["node", "copilot"]
            )
            version.assert_not_called()
            engine.assert_not_called()
            self.assertFalse((root / "state").exists())

    def test_claude_only_team_never_probes_copilot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_claude = fake_claude_executables(root)
            plan = copilot_plan(root)
            roles = plan["roles"]
            assert isinstance(roles, dict)
            roles["reviewer"].update(
                provider="claude", model="fable", adapter_id="claude-acp-0.70.0"
            )

            def run_node_version(
                argv: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                self.assertEqual(argv, [str(selected_claude.node), "--version"])
                return subprocess.CompletedProcess(argv, 0, "v22.23.2\n", "")

            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(os, "access", return_value=True),
                mock.patch.object(
                    cli, "mcp_server_path", return_value=root / "mcp-server"
                ),
                mock.patch.object(
                    NativeAcpExecutables, "resolve", return_value=selected_claude
                ) as claude_resolve,
                mock.patch.object(
                    CopilotAcpExecutables,
                    "resolve",
                    side_effect=AssertionError("unselected Copilot ACP was probed"),
                ) as copilot_resolve,
                mock.patch(
                    "agent_team.native_acp_dependencies.shutil.which",
                    wraps=shutil.which,
                ) as which,
                mock.patch.object(cli, "acp_environment", return_value={}),
                mock.patch.object(subprocess, "run", side_effect=run_node_version),
            ):
                cli._start_prerequisites(plan)

            claude_resolve.assert_called_once_with()
            copilot_resolve.assert_not_called()
            self.assertNotIn("copilot", [call.args[0] for call in which.call_args_list])
            self.assertEqual(
                roles["reviewer"]["acp_executables"], selected_claude.as_dict()
            )

    def test_fixed_v3_orca_rejects_copilot_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = copilot_plan(root, runtime="orca")
            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(
                    CopilotAcpExecutables,
                    "resolve",
                    side_effect=AssertionError("Copilot ACP was resolved for Orca"),
                ) as resolve,
                self.assertRaises(cli.ConfigError) as raised,
            ):
                cli._start_prerequisites(plan)

            self.assertEqual(
                str(raised.exception),
                "scoped Copilot ACP requires a native or named Orca runtime",
            )
            resolve.assert_not_called()

    def test_selected_copilot_is_resolved_without_running_copilot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = fake_copilot_executables(root)
            plan = copilot_plan(root)
            version_calls: list[tuple[str, ...]] = []

            def run_version(
                argv: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                version_calls.append(tuple(argv))
                return subprocess.CompletedProcess(argv, 0, "v22.23.2\n", "")

            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(os, "access", return_value=True),
                mock.patch.object(
                    cli, "mcp_server_path", return_value=root / "mcp-server"
                ),
                mock.patch.object(
                    CopilotAcpExecutables, "resolve", return_value=selected
                ) as resolve,
                mock.patch.object(
                    NativeAcpExecutables,
                    "resolve",
                    side_effect=AssertionError("unselected Claude ACP was probed"),
                ),
                mock.patch.object(
                    CodexAcpExecutables,
                    "resolve",
                    side_effect=AssertionError("unselected Codex ACP was probed"),
                ),
                mock.patch.object(
                    AcpExecutables,
                    "resolve",
                    side_effect=AssertionError("unselected Orca ACP was probed"),
                ),
                mock.patch.object(cli, "acp_environment", return_value={}),
                mock.patch.object(subprocess, "run", side_effect=run_version),
            ):
                cli._start_prerequisites(plan)

            resolve.assert_called_once_with()
            self.assertEqual(version_calls, [(str(selected.node), "--version")])
            roles = plan["roles"]
            assert isinstance(roles, dict)
            self.assertEqual(roles["reviewer"]["acp_executables"], selected.as_dict())
            self.assertNotIn("provider_snapshot", roles["reviewer"])

    def test_acp_assignment_validates_the_copilot_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            prompt_path = root / "prompt.txt"
            selected = fake_copilot_executables(root)
            adapter_snapshot = {"adapter_id": "copilot-acp-1.0.91"}
            spec: dict[str, object] = {
                "provider": "copilot",
                "transport": "acp",
                "permission": "read-only",
                "execution": "background",
                "adapter_id": copilot_acp.ADAPTER_ID,
                "model": "gpt-5.2",
                "effort": "high",
                "instructions": "reviewer instructions",
                "acp_executables": selected.as_dict(),
            }
            assignment: dict[str, object] = {
                "task_id": "task-1",
                "dispatch_id": "dispatch-1",
                "terminal_handle": "terminal-1",
                "prompt_path": str(prompt_path),
                "launch_nonce": "nonce1234",
                "agent_command": "copilot-agent-command",
                "session_name": cli.acp_session_name("reviewer", "nonce1234"),
                "adapter_snapshot": adapter_snapshot,
            }
            state: dict[str, object] = {
                "version": 3,
                "runtime": "tmux",
                "team_id": "copilot-team",
                "state_path": str(state_path),
                "roles": {"reviewer": assignment},
                "role_specs": {"reviewer": spec},
            }

            with (
                mock.patch.object(
                    CopilotAcpExecutables, "from_dict", return_value=selected
                ) as from_dict,
                mock.patch.object(CopilotAcpExecutables, "verify") as verify,
                mock.patch.object(
                    cli, "copilot_adapter_snapshot", return_value=adapter_snapshot
                ) as snapshot,
                mock.patch.object(
                    copilot_acp, "validate_assignment"
                ) as validate_assignment,
                mock.patch.object(
                    copilot_acp, "agent_command", return_value="copilot-agent-command"
                ) as agent_command,
                mock.patch.object(
                    cli,
                    "validate_write_policy",
                    side_effect=AssertionError("Claude policy validation ran"),
                ),
                mock.patch.object(cli, "validate_prompt_file"),
            ):
                actual = cli._acp_assignment(
                    state,
                    "reviewer",
                    state_path=state_path,
                    task_id="task-1",
                    dispatch_id="dispatch-1",
                    terminal_handle="terminal-1",
                    prompt_path=prompt_path,
                    launch_nonce="nonce1234",
                )

            self.assertEqual(actual, (assignment, spec, selected))
            from_dict.assert_called_once_with(spec["acp_executables"])
            verify.assert_called_once_with()
            validate_assignment.assert_called_once_with(state, assignment, spec)
            agent_command.assert_called_once_with(
                selected, permission="read-only", model="gpt-5.2", effort="high"
            )
            snapshot.assert_called_once_with(selected)

            assignment["adapter_snapshot"] = {"adapter_id": "copilot-acp-1.0.90"}
            with (
                mock.patch.object(
                    CopilotAcpExecutables, "from_dict", return_value=selected
                ),
                mock.patch.object(CopilotAcpExecutables, "verify"),
                mock.patch.object(
                    cli, "copilot_adapter_snapshot", return_value=adapter_snapshot
                ),
                mock.patch.object(copilot_acp, "validate_assignment"),
                mock.patch.object(
                    copilot_acp, "agent_command", return_value="copilot-agent-command"
                ),
                self.assertRaisesRegex(cli.ConfigError, "invalid executable snapshot"),
            ):
                cli._acp_assignment(
                    state,
                    "reviewer",
                    state_path=state_path,
                    task_id="task-1",
                    dispatch_id="dispatch-1",
                    terminal_handle="terminal-1",
                    prompt_path=prompt_path,
                    launch_nonce="nonce1234",
                )

    def test_acp_run_turn_uses_the_closed_environment_policy_and_no_questions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            state_path = root / "state.json"
            private_root = root / "private"
            private_root.mkdir(mode=0o700)
            selected = fake_copilot_executables(root)
            assignment: dict[str, object] = {
                "provider_private_root": str(private_root),
                "write_policy_path": str(private_root / "write-policy.json"),
            }
            spec: dict[str, object] = {
                "provider": "copilot",
                "transport": "acp",
                "permission": "workspace-write",
                "execution": "background",
                "adapter_id": copilot_acp.ADAPTER_ID,
                "model": "gpt-5.2",
                "effort": "high",
                "instructions": "worker instructions",
            }
            state: dict[str, object] = {
                "version": 3,
                "runtime": "tmux",
                "workspace": str(workspace),
                "state_path": str(state_path),
                "run_id": "run-1",
                "role_specs": {"worker": spec},
            }
            receipt = {
                "output": "copilot output",
                "session_id": "session-1",
                "model": "gpt-5.2",
                "effort": "high",
                "cleanup_confirmed": True,
            }
            environment = {"COPILOT_HOME": str(private_root / "copilot-home")}

            class FakeRunner:
                instances: ClassVar[list[FakeRunner]] = []

                def __init__(self, **kwargs: object) -> None:
                    self.kwargs = kwargs
                    self.calls: list[dict[str, object]] = []
                    self.__class__.instances.append(self)

                def run(
                    self,
                    argv: list[str],
                    *,
                    cwd: Path,
                    env: dict[str, str],
                    input_text: str,
                    timeout_seconds: float,
                ) -> ProcessResult:
                    self.calls.append(
                        {"argv": argv, "cwd": cwd, "env": env, "input": input_text}
                    )
                    result_file = private_root / "client-result.json"
                    result_file.write_text(
                        json.dumps(
                            {
                                "version": 1,
                                "launch_nonce": "nonce1234",
                                "receipt": receipt,
                            }
                        ),
                        encoding="utf-8",
                    )
                    result_file.chmod(0o600)
                    return ProcessResult(0, json.dumps(receipt), "")

            with (
                mock.patch.object(
                    cli, "_acp_assignment", return_value=(assignment, spec, selected)
                ),
                mock.patch.object(cli, "read_prompt_file", return_value="prompt"),
                mock.patch.object(
                    copilot_acp, "agent_command", return_value="copilot-agent-command"
                ) as agent_command,
                mock.patch.object(
                    copilot_acp, "environment", return_value=environment
                ) as copilot_environment,
                mock.patch.object(
                    cli, "client_argv", return_value=["node", "copilot-client"]
                ) as client,
                mock.patch.object(
                    cli,
                    "validate_write_policy",
                    side_effect=AssertionError("Claude policy validation ran"),
                ),
                mock.patch.object(
                    cli,
                    "_native_question_context",
                    side_effect=AssertionError("a question channel was opened"),
                ),
                mock.patch.object(cli, "_NativeAcpClientRunner", FakeRunner),
                mock.patch.object(cli, "run_acpx") as run_acpx,
                mock.patch.object(sys, "stdout", io.StringIO()) as stdout,
                mock.patch(
                    "agent_team.native_backend.publish_completion",
                    return_value="succeeded",
                ) as publish,
            ):
                result = cli._acp_run_turn(
                    cancellation=Event(),
                    state=state,
                    role="worker",
                    state_path=state_path,
                    task_id="task-1",
                    dispatch_id="dispatch-1",
                    terminal_handle="terminal-1",
                    prompt_path=root / "prompt.txt",
                    launch_nonce="nonce1234",
                )

            self.assertEqual(result, 0)
            agent_command.assert_called_once_with(
                selected, permission="workspace-write", model="gpt-5.2", effort="high"
            )
            copilot_environment.assert_called_once_with(private_root)
            client.assert_called_once_with(
                selected,
                "copilot-agent-command",
                harness="copilot",
                workspace=workspace,
                permission="workspace-write",
                model="gpt-5.2",
                effort="high",
                instructions="worker instructions",
                timeout_seconds=cli.ACP_TIMEOUT_SECONDS,
                result_file=private_root / "client-result.json",
                launch_nonce="nonce1234",
                policy=private_root / "write-policy.json",
            )
            run_acpx.assert_not_called()
            self.assertEqual(len(FakeRunner.instances), 1)
            self.assertEqual(FakeRunner.instances[0].calls[0]["env"], environment)
            self.assertEqual(FakeRunner.instances[0].calls[0]["input"], "prompt")
            publish.assert_called_once()
            self.assertEqual(publish.call_args.kwargs["outcome"], "succeeded")
            self.assertEqual(stdout.getvalue(), "copilot output\n")


if __name__ == "__main__":
    unittest.main()
