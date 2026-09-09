from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import ClassVar, cast
from unittest import mock

from test_native_backend import FakePopen, FakeTmuxDriver

from agent_team import cli, codex_acp, native_acp_dependencies, tmux_backend
from agent_team import native_backend as native
from agent_team.acp_dependencies import AcpExecutables
from agent_team.adapters import ProcessResult
from agent_team.contracts import (
    AckReceipt,
    DeliveryAck,
    ReadReceipt,
    Role,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleSpec,
    RoleWait,
    StartSpec,
    WaitReceipt,
)
from agent_team.native_acp_dependencies import (
    CodexAcpExecutables,
    NativeAcpDependencyError,
    NativeAcpExecutables,
)
from agent_team.runtime import RuntimeValidationError, read_state
from agent_team.scoped_acp import CODEX_SCOPED_ADAPTER_ID


def fake_codex_executables(root: Path) -> CodexAcpExecutables:
    return CodexAcpExecutables(
        node=root / "node",
        agent=root / "codex-acp.js",
        sdk=root / "sdk.js",
        codex=root / "codex",
        node_sha256="a" * 64,
        agent_sha256="b" * 64,
        sdk_sha256="c" * 64,
        codex_sha256="d" * 64,
        agent_manifest_sha256="e" * 64,
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


def codex_plan(root: Path, *, runtime: str = "tmux") -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir()
    return {
        "runtime": runtime,
        "workspace": str(workspace),
        "roles": {
            "worker": {
                "provider": "codex",
                "transport": "acp",
                "execution": "background",
            }
        },
    }


def codex_version_runner(
    selected: CodexAcpExecutables,
    version_calls: list[tuple[str, ...]],
    node_output: str,
    codex_returncode: int,
    codex_output: str,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run_version(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        version_calls.append(tuple(argv))
        if argv == [str(selected.node), "--version"]:
            return subprocess.CompletedProcess(argv, 0, node_output, "")
        if argv == [str(selected.codex), "--version"]:
            return subprocess.CompletedProcess(argv, codex_returncode, codex_output, "")
        raise AssertionError(f"unexpected version probe: {argv}")

    return run_version


class CodexCliWiringTest(unittest.TestCase):
    def test_codex_preflight_resolves_all_selected_providers_then_snapshots_codex(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = fake_codex_executables(root)
            selected_claude = fake_claude_executables(root)
            plan = codex_plan(root)
            roles = plan["roles"]
            assert isinstance(roles, dict)
            roles["reviewer"] = {
                "provider": "claude",
                "transport": "acp",
                "execution": "background",
            }
            auth_path = root / "auth.json"
            provider_snapshot = {"auth_path": str(auth_path), "revision": "snapshot"}
            version_calls: list[tuple[str, ...]] = []

            def run_version(
                argv: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                version_calls.append(tuple(argv))
                if argv == [str(selected.node), "--version"]:
                    return subprocess.CompletedProcess(argv, 0, "v22.23.2\n", "")
                if argv == [str(selected.codex), "--version"]:
                    return subprocess.CompletedProcess(
                        argv, 0, "codex-cli 0.153.4\n", ""
                    )
                raise AssertionError(f"unexpected version probe: {argv}")

            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(os, "access", return_value=True),
                mock.patch.object(
                    cli, "mcp_server_path", return_value=root / "mcp-server"
                ),
                mock.patch.object(
                    CodexAcpExecutables, "resolve", return_value=selected
                ) as resolve,
                mock.patch.object(
                    NativeAcpExecutables,
                    "resolve",
                    return_value=selected_claude,
                ) as claude_resolve,
                mock.patch.object(
                    AcpExecutables,
                    "resolve",
                    side_effect=AssertionError("unselected Orca ACP was probed"),
                ) as orca_resolve,
                mock.patch.object(cli, "acp_environment", return_value={}),
                mock.patch.object(subprocess, "run", side_effect=run_version),
                mock.patch.object(
                    cli, "_codex_auth_path", return_value=auth_path
                ) as auth,
                mock.patch.object(
                    codex_acp, "snapshot", return_value=provider_snapshot
                ) as snapshot,
            ):
                cli._start_prerequisites(plan)

            resolve.assert_called_once_with()
            claude_resolve.assert_called_once_with()
            orca_resolve.assert_not_called()
            self.assertEqual(
                version_calls,
                [
                    (str(selected.node), "--version"),
                    (str(selected.codex), "--version"),
                ],
            )
            auth.assert_called_once_with()
            snapshot.assert_called_once_with(Path(str(plan["workspace"])), auth_path)
            self.assertEqual(roles["worker"]["acp_executables"], selected.as_dict())
            self.assertEqual(roles["worker"]["provider_snapshot"], provider_snapshot)
            self.assertEqual(
                roles["reviewer"]["acp_executables"], selected_claude.as_dict()
            )

    def test_codex_preflight_fails_before_versions_or_auth_when_dependency_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = codex_plan(root)
            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(os, "access", return_value=True),
                mock.patch.object(
                    cli, "mcp_server_path", return_value=root / "mcp-server"
                ),
                mock.patch.object(
                    CodexAcpExecutables,
                    "resolve",
                    side_effect=NativeAcpDependencyError("missing node"),
                ),
                mock.patch.object(
                    subprocess,
                    "run",
                    side_effect=AssertionError(
                        "version check ran after missing dependency"
                    ),
                ) as version,
                mock.patch.object(
                    cli,
                    "_codex_auth_path",
                    side_effect=AssertionError(
                        "auth was read after missing dependency"
                    ),
                ) as auth,
                mock.patch.object(
                    codex_acp,
                    "snapshot",
                    side_effect=AssertionError("snapshot ran after missing dependency"),
                ) as snapshot,
                self.assertRaisesRegex(
                    cli.ConfigError, "selected codex ACP dependencies"
                ),
            ):
                cli._start_prerequisites(plan)

            version.assert_not_called()
            auth.assert_not_called()
            snapshot.assert_not_called()

    def test_claude_only_native_preflight_does_not_probe_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_claude = fake_claude_executables(root)
            plan = codex_plan(root)
            roles = plan["roles"]
            assert isinstance(roles, dict)
            roles["worker"]["provider"] = "claude"

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
                    CodexAcpExecutables,
                    "resolve",
                    side_effect=AssertionError("unselected Codex ACP was probed"),
                ) as codex_resolve,
                mock.patch.object(cli, "acp_environment", return_value={}),
                mock.patch.object(subprocess, "run", side_effect=run_node_version),
                mock.patch.object(
                    cli,
                    "_codex_auth_path",
                    side_effect=AssertionError("unselected Codex auth was read"),
                ) as auth,
                mock.patch.object(
                    codex_acp,
                    "snapshot",
                    side_effect=AssertionError(
                        "unselected Codex snapshot was captured"
                    ),
                ) as snapshot,
            ):
                cli._start_prerequisites(plan)

            claude_resolve.assert_called_once_with()
            codex_resolve.assert_not_called()
            auth.assert_not_called()
            snapshot.assert_not_called()

    def test_codex_preflight_rejects_invalid_versions_before_snapshot(self) -> None:
        cases = (
            ("node-too-old", "v21.9.0\n", 0, "", "selected Node must be version"),
            (
                "codex-mismatch",
                "v22.23.2\n",
                0,
                "codex-cli 0.153.3\n",
                "selected codex-cli 0.153.4",
            ),
            (
                "codex-failed",
                "v22.23.2\n",
                1,
                "",
                "selected codex-cli 0.153.4",
            ),
        )
        for name, node_output, codex_returncode, codex_output, message in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                selected = fake_codex_executables(root)
                plan = codex_plan(root)
                version_calls: list[tuple[str, ...]] = []

                run_version = codex_version_runner(
                    selected,
                    version_calls,
                    node_output,
                    codex_returncode,
                    codex_output,
                )

                with (
                    mock.patch.object(cli, "require_binary"),
                    mock.patch.object(os, "access", return_value=True),
                    mock.patch.object(
                        cli, "mcp_server_path", return_value=root / "mcp-server"
                    ),
                    mock.patch.object(
                        CodexAcpExecutables, "resolve", return_value=selected
                    ),
                    mock.patch.object(cli, "acp_environment", return_value={}),
                    mock.patch.object(subprocess, "run", side_effect=run_version),
                    mock.patch.object(
                        cli,
                        "_codex_auth_path",
                        side_effect=AssertionError(
                            "auth was read before version acceptance"
                        ),
                    ) as auth,
                    mock.patch.object(codex_acp, "snapshot") as snapshot,
                    self.assertRaisesRegex(cli.ConfigError, message),
                ):
                    cli._start_prerequisites(plan)

                snapshot.assert_not_called()
                auth.assert_not_called()
                self.assertEqual(
                    version_calls,
                    [(str(selected.node), "--version")]
                    if name == "node-too-old"
                    else [
                        (str(selected.node), "--version"),
                        (str(selected.codex), "--version"),
                    ],
                )

    def test_codex_acp_is_rejected_for_fixed_orca_before_dependency_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = codex_plan(root, runtime="orca")
            with (
                mock.patch.object(cli, "require_binary"),
                mock.patch.object(
                    CodexAcpExecutables,
                    "resolve",
                    side_effect=AssertionError("Codex ACP was resolved for Orca"),
                ) as resolve,
                self.assertRaisesRegex(
                    cli.ConfigError, "requires a native or named Orca runtime"
                ),
            ):
                cli._start_prerequisites(plan)

            resolve.assert_not_called()

    def test_start_spec_copies_codex_provider_snapshot_into_role_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = fake_codex_executables(root)
            provider_snapshot = {"auth_path": str(root / "auth.json")}
            plan: dict[str, object] = {
                "runtime": "tmux",
                "team_id": "team-test",
                "workspace": str(root / "workspace"),
                "config_path": str(root / "team.toml"),
                "state_path": str(root / "state.json"),
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
                    "worker": {
                        "provider": "codex",
                        "transport": "acp",
                        "model": "gpt-6-astra",
                        "effort": "medium",
                        "permission": "workspace-write",
                        "instructions": "worker",
                        "execution": "background",
                        "adapter_id": codex_acp.ADAPTER_ID,
                        "acp_executables": selected.as_dict(),
                        "provider_snapshot": provider_snapshot,
                    },
                },
                "task_specs": [],
            }

            spec = cli._start_spec(plan, attach=False)
            plan_roles = plan["roles"]
            assert isinstance(plan_roles, dict)
            plan_roles["worker"]["provider_snapshot"] = {"changed": True}

        saved = spec.role_specs[Role.WORKER].provider_snapshot
        self.assertEqual(saved, {"auth_path": str(root / "auth.json")})
        self.assertIsNot(saved, provider_snapshot)

    def test_acp_assignment_selects_codex_class_and_validates_codex_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            prompt_path = root / "prompt.txt"
            selected = fake_codex_executables(root)
            adapter_snapshot = {"adapter_id": codex_acp.ADAPTER_ID}
            spec: dict[str, object] = {
                "provider": "codex",
                "transport": "acp",
                "permission": "workspace-write",
                "execution": "background",
                "adapter_id": codex_acp.ADAPTER_ID,
                "model": "gpt-6-astra",
                "effort": "medium",
                "instructions": "worker instructions",
                "acp_executables": selected.as_dict(),
                "provider_snapshot": {"revision": "snapshot"},
            }
            assignment: dict[str, object] = {
                "task_id": "task-1",
                "dispatch_id": "dispatch-1",
                "terminal_handle": "terminal-1",
                "prompt_path": str(prompt_path),
                "launch_nonce": "nonce1234",
                "agent_command": "codex-agent-command",
                "session_name": cli.acp_session_name("worker", "nonce1234"),
                "adapter_snapshot": adapter_snapshot,
            }
            state: dict[str, object] = {
                "version": 3,
                "runtime": "tmux",
                "team_id": "team-test",
                "state_path": str(state_path),
                "roles": {"worker": assignment},
                "role_specs": {"worker": spec},
            }

            with (
                mock.patch.object(
                    CodexAcpExecutables, "from_dict", return_value=selected
                ) as from_dict,
                mock.patch.object(CodexAcpExecutables, "verify") as verify,
                mock.patch.object(
                    cli, "codex_adapter_snapshot", return_value=adapter_snapshot
                ) as snapshot,
                mock.patch.object(
                    codex_acp, "validate_assignment"
                ) as validate_assignment,
                mock.patch.object(
                    codex_acp, "agent_command", return_value="codex-agent-command"
                ) as agent_command,
                mock.patch.object(cli, "validate_prompt_file"),
            ):
                actual_assignment, actual_spec, actual_executables = (
                    cli._acp_assignment(
                        state,
                        "worker",
                        state_path=state_path,
                        task_id="task-1",
                        dispatch_id="dispatch-1",
                        terminal_handle="terminal-1",
                        prompt_path=prompt_path,
                        launch_nonce="nonce1234",
                    )
                )

            self.assertIs(actual_assignment, assignment)
            self.assertIs(actual_spec, spec)
            self.assertIs(actual_executables, selected)
            from_dict.assert_called_once_with(spec["acp_executables"])
            verify.assert_called_once_with()
            validate_assignment.assert_called_once_with(state, assignment, spec)
            agent_command.assert_called_once_with(selected)
            snapshot.assert_called_once_with(selected)

    def test_acp_run_turn_uses_codex_native_runner_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            state_path = root / "state.json"
            selected = fake_codex_executables(root)
            assignment: dict[str, object] = {
                "provider_private_root": str(root / "private")
            }
            (root / "private").mkdir(mode=0o700)
            spec: dict[str, object] = {
                "provider": "codex",
                "transport": "acp",
                "permission": "workspace-write",
                "execution": "background",
                "adapter_id": codex_acp.ADAPTER_ID,
                "model": "gpt-6-astra",
                "effort": "medium",
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
                "output": "codex output",
                "session_id": "session-1",
                "model": "gpt-6-astra",
                "effort": "medium",
                "cleanup_confirmed": True,
            }

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
                        {
                            "argv": argv,
                            "cwd": cwd,
                            "env": env,
                            "input_text": input_text,
                            "timeout_seconds": timeout_seconds,
                        }
                    )
                    result_file = root / "private" / "client-result.json"
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
                    cli,
                    "_acp_assignment",
                    return_value=(assignment, spec, selected),
                ),
                mock.patch.object(cli, "read_prompt_file", return_value="prompt"),
                mock.patch.object(
                    codex_acp, "agent_command", return_value="codex-agent-command"
                ) as agent_command,
                mock.patch.object(
                    codex_acp,
                    "environment",
                    return_value={"CODEX_PATH": "/private/codex-proxy"},
                ) as environment,
                mock.patch.object(
                    cli,
                    "client_argv",
                    return_value=["node", "codex-client"],
                ) as client,
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
            agent_command.assert_called_once_with(selected)
            environment.assert_called_once_with(
                Path(str(assignment["provider_private_root"])), selected
            )
            client.assert_called_once_with(
                selected,
                "codex-agent-command",
                harness="codex",
                workspace=workspace,
                permission="workspace-write",
                model="gpt-6-astra",
                effort="medium",
                instructions="worker instructions",
                timeout_seconds=cli.ACP_TIMEOUT_SECONDS,
                result_file=root / "private" / "client-result.json",
                launch_nonce="nonce1234",
            )
            run_acpx.assert_not_called()
            self.assertEqual(len(FakeRunner.instances), 1)
            self.assertEqual(
                FakeRunner.instances[0].calls[0]["argv"], ["node", "codex-client"]
            )
            self.assertEqual(
                FakeRunner.instances[0].calls[0]["env"],
                {"CODEX_PATH": "/private/codex-proxy"},
            )
            self.assertEqual(FakeRunner.instances[0].calls[0]["input_text"], "prompt")
            publish.assert_called_once()
            self.assertEqual(publish.call_args.kwargs["outcome"], "succeeded")
            self.assertEqual(stdout.getvalue(), "codex output\n")

    def test_codex_validation_failure_publishes_and_consumes_failed_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            launcher = root / "agent-team"
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o700)
            state_path = root / "state" / "state.json"
            selected = fake_codex_executables(root)
            provider_snapshot = {"fixture": "provider-snapshot"}
            adapter_snapshot = {
                "adapter_id": codex_acp.ADAPTER_ID,
                "revision": "@agentclientprotocol/sdk@1.4.0",
                "executable": str(selected.sdk),
                "version": "@agentclientprotocol/codex-acp@1.10.0",
                "identity": {
                    "device": 1,
                    "inode": 2,
                    "size": 3,
                    "mtime_ns": 4,
                    "sha256": selected.sdk_sha256,
                },
            }
            main_spec = RoleSpec(
                provider="claude",
                transport="direct",
                model="fable",
                effort="high",
                permission="orchestrator",
                instructions="main instructions",
                execution="tui_direct",
            )
            planner_spec = RoleSpec(
                provider="codex",
                transport="acp",
                model="gpt-6-astra",
                effort="medium",
                permission="read-only",
                instructions="planner instructions",
                execution="background",
                adapter_id=CODEX_SCOPED_ADAPTER_ID,
                acp_executables=selected.as_dict(),
                provider_snapshot=provider_snapshot,
            )
            start_spec = StartSpec(
                team_id="codex-validation-failure",
                workspace=workspace,
                config_path=root / "team.toml",
                state_path=state_path,
                role_specs={Role.MAIN: main_spec, Role.PLANNER: planner_spec},
            )

            real_mkdtemp = cast(Callable[..., str], tempfile.mkdtemp)

            def fixture_mkdtemp(**kwargs: object) -> str:
                if kwargs.get("dir") == "/tmp":
                    kwargs["dir"] = str(root)
                path = Path(real_mkdtemp(**kwargs))
                if kwargs.get("prefix") in {
                    "agent-team-provider-",
                    "agent-team-snapshot-",
                }:
                    self.addCleanup(native.remove_owned_tree, path)
                return str(path)

            def prepare_assignment(**kwargs: object) -> dict[str, object]:
                private_root = kwargs["private_root"]
                assert isinstance(private_root, Path)
                return {
                    "write_policy_path": str(private_root / "write-policy.json"),
                    "write_policy_sha256": "p" * 64,
                    "codex_launch_path": str(private_root / "codex-launch.json"),
                    "codex_launch_sha256": "l" * 64,
                    "codex_proxy_path": str(private_root / "codex-proxy"),
                    "codex_proxy_sha256": "x" * 64,
                    "codex_config_snapshot": "c" * 64,
                }

            with (
                mock.patch.object(tmux_backend, "TmuxDriver", FakeTmuxDriver),
                mock.patch.object(shutil, "which", return_value="/usr/bin/true"),
                mock.patch(
                    "agent_team.native_backend.acp_environment",
                    return_value={"PATH": "/bin"},
                ),
                mock.patch.object(tempfile, "mkdtemp", side_effect=fixture_mkdtemp),
                mock.patch.object(
                    CodexAcpExecutables, "from_dict", return_value=selected
                ),
                mock.patch.object(CodexAcpExecutables, "verify"),
                mock.patch.object(
                    native_acp_dependencies,
                    "codex_adapter_snapshot",
                    return_value=adapter_snapshot,
                ),
                mock.patch.object(codex_acp, "verify_snapshot"),
                mock.patch.object(
                    codex_acp, "prepare_assignment", side_effect=prepare_assignment
                ),
                mock.patch.object(subprocess, "Popen", FakePopen),
                mock.patch.object(os, "getpgid", return_value=77_001),
            ):
                backend = tmux_backend.TmuxBackend(
                    tmux_executable="tmux", launcher_path=launcher
                )
                backend.start(start_spec)
                backend.request(RolePrompt(Role.PLANNER, "inspect the workspace"))
                state = read_state(state_path)

                roles = state["roles"]
                assert isinstance(roles, dict)
                assignment = roles[Role.PLANNER.value]
                assert isinstance(assignment, dict)
                role_specs = state["role_specs"]
                assert isinstance(role_specs, dict)
                role_spec = role_specs[Role.PLANNER.value]
                assert isinstance(role_spec, dict)

                with (
                    mock.patch.object(
                        codex_acp,
                        "validate_assignment",
                        side_effect=RuntimeValidationError(
                            "Codex provider artifact drift"
                        ),
                    ) as validate_assignment,
                    mock.patch(
                        "agent_team.native_backend._assert_publisher"
                    ) as assert_publisher,
                    mock.patch.object(
                        cli,
                        "ProcessRunner",
                        side_effect=AssertionError("provider process must not start"),
                    ),
                    mock.patch.object(sys, "stderr", io.StringIO()),
                ):
                    result = cli._acp_run_turn(
                        cancellation=Event(),
                        state=state,
                        role=Role.PLANNER.value,
                        state_path=state_path,
                        task_id=str(assignment["task_id"]),
                        dispatch_id=str(assignment["dispatch_id"]),
                        terminal_handle=str(assignment["terminal_handle"]),
                        prompt_path=Path(str(assignment["prompt_path"])),
                        launch_nonce=str(assignment["launch_nonce"]),
                    )

                self.assertEqual(result, 1)
                validate_assignment.assert_called_once_with(
                    state, assignment, role_spec
                )
                assert_publisher.assert_called_once()
                published = read_state(state_path)["native_result"]
                self.assertIsInstance(published, dict)
                assert isinstance(published, dict)
                self.assertEqual(published["outcome"], "failed")
                self.assertTrue(published["cleanup_confirmed"])
                self.assertIn("ACP runner validation failed", published["body"])

                wait = cast(WaitReceipt, backend.request(RoleWait(Role.PLANNER, 1_000)))
                outcome = wait.events[0].outcome
                self.assertIsNotNone(outcome)
                assert outcome is not None
                self.assertEqual(outcome.value, "failed")
                read = cast(ReadReceipt, backend.request(RoleRead(Role.PLANNER, 2_000)))
                self.assertIn(
                    "Codex provider artifact drift",
                    read.output,
                )
                backend.request(RoleRelease(Role.PLANNER))
                self.assertIsNotNone(wait.delivery_id)
                assert wait.delivery_id is not None
                ack = cast(AckReceipt, backend.request(DeliveryAck(wait.delivery_id)))
                self.assertTrue(ack.acknowledged)
                final_state = read_state(state_path)
                self.assertEqual(final_state["roles"], {})
                self.assertNotIn("native_result", final_state)


if __name__ == "__main__":
    unittest.main()
