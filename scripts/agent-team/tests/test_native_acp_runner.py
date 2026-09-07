from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import cli, native_backend
from agent_team.adapters import ExecutionError, ProcessResult
from agent_team.native_acp_dependencies import NativeAcpExecutables, adapter_snapshot
from agent_team.runtime import read_state
from agent_team.scoped_acp import (
    SCOPED_AGENT,
    SCOPED_CLIENT,
    SCOPED_POLICY,
    checked_digest,
    create_write_policy,
)


class NativeAcpRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        self.state_dir.chmod(0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.workspace.chmod(0o700)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _executables(self) -> NativeAcpExecutables:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(mode=0o700)
        bin_dir.chmod(0o700)
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\n", encoding="utf-8")
        node.chmod(0o700)
        for package_name, version, command in (
            ("@agentclientprotocol/sdk", "1.3.0", None),
            (
                "@agentclientprotocol/claude-agent-acp",
                "0.70.0",
                "claude-agent-acp",
            ),
        ):
            package = self.root / "node_modules" / package_name
            (package / "dist").mkdir(parents=True, mode=0o700)
            entry_name = "acp.js" if command is None else "index.js"
            metadata = {"name": package_name, "version": version}
            if command is None:
                metadata.update(
                    main="dist/acp.js", exports={".": {"import": "./dist/acp.js"}}
                )
            else:
                metadata.update(
                    bin={command: "dist/index.js"},
                    dependencies={"@agentclientprotocol/sdk": "1.3.0"},
                )
            (package / "package.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )
            entry = package / "dist" / entry_name
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            entry.chmod(0o700)
            if command is not None:
                (package / "dist" / "lib.js").write_text(
                    "export {};\n", encoding="utf-8"
                )
                (bin_dir / command).symlink_to(entry)
        return NativeAcpExecutables.resolve(path=str(bin_dir))

    def _state(self) -> tuple[dict[str, object], NativeAcpExecutables, Path]:
        executables = self._executables()
        state_path = self.state_dir / "state.json"
        prompt_path = cli.create_prompt_file(
            self.state_dir, "planner", "planner1234", "inspect the workspace"
        )
        provider_root = self.root / "provider"
        provider_root.mkdir(mode=0o700)
        policy, policy_digest = create_write_policy(
            provider_root,
            self.workspace,
            state_path,
            None,
            executables.agent,
            permission="read-only",
        )
        snapshot_root = self.root / "snapshot"
        snapshot_root.mkdir(mode=0o700)
        assignment = {
            "task_id": "task-1",
            "dispatch_id": "dispatch-1",
            "terminal_handle": "terminal-1",
            "completion_observed": False,
            "launcher_owned_runner": True,
            "launch_nonce": "planner1234",
            "prompt_path": str(prompt_path),
            "execution": "background",
            "adapter_id": "claude-acp-0.70.0",
            "agent_command": cli.acp_agent_command(
                "agent-team-test",
                "planner",
                "planner1234",
                executables=executables,
                write_policy=policy,
            ),
            "session_name": "agent-team-planner-planner1234",
            "provider_private_root": str(provider_root),
            "snapshot_root": str(snapshot_root),
            "adapter_snapshot": adapter_snapshot(executables),
            "write_policy_path": str(policy),
            "write_policy_sha256": policy_digest,
        }
        state = {
            "version": 3,
            "runtime": "tmux",
            "team_id": "agent-team-test",
            "workspace": str(self.workspace),
            "config_path": str(self.root / "config.toml"),
            "state_path": str(state_path),
            "launcher_path": str(self.root / "agent-team"),
            "run_id": "run-1",
            "main_terminal": "main-terminal",
            "role_specs": {
                "main": {
                    "provider": "claude",
                    "transport": "direct",
                    "model": "fable",
                    "effort": "high",
                    "permission": "orchestrator",
                    "instructions": "main instructions",
                    "execution": "tui_direct",
                },
                "planner": {
                    "provider": "claude",
                    "transport": "acp",
                    "model": "fable",
                    "effort": "high",
                    "permission": "read-only",
                    "instructions": "planner instructions",
                    "execution": "background",
                    "adapter_id": "claude-acp-0.70.0",
                    "acp_executables": executables.as_dict(),
                    "scoped_wrapper_sha256": checked_digest(SCOPED_AGENT),
                    "scoped_client_sha256": checked_digest(SCOPED_CLIENT),
                    "scoped_policy_sha256": checked_digest(SCOPED_POLICY),
                },
            },
            "roles": {"planner": assignment},
            "native": {
                "phase": "running",
                "run_nonce": "mainnonce1234",
                "main_argv": ["/usr/bin/true"],
            },
        }
        cli.write_state(state_path, state)
        return state, executables, prompt_path

    def _run(
        self, state: dict[str, object], state_path: Path, prompt_path: Path
    ) -> int:
        return cli._acp_run_turn(
            state=state,
            role="planner",
            state_path=state_path,
            task_id="task-1",
            dispatch_id="dispatch-1",
            terminal_handle="terminal-1",
            prompt_path=prompt_path,
            launch_nonce="planner1234",
        )

    def test_native_validation_drift_publishes_failed_completion_before_provider(
        self,
    ) -> None:
        state, executables, prompt_path = self._state()
        executables.agent.write_text("changed\n", encoding="utf-8")
        state_path = self.state_dir / "state.json"

        with (
            mock.patch.object(
                cli,
                "run_acpx",
                side_effect=AssertionError(
                    "provider must not start after validation failure"
                ),
            ),
            mock.patch.object(native_backend, "_assert_publisher"),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        published = read_state(state_path)["native_result"]
        self.assertIsInstance(published, dict)
        assert isinstance(published, dict)
        self.assertEqual(published["outcome"], "failed")
        self.assertTrue(published["cleanup_confirmed"])
        self.assertIn("ACP runner validation failed", published["body"])

    def test_native_prompt_validation_publishes_failed_completion_before_provider(
        self,
    ) -> None:
        state, _executables, prompt_path = self._state()
        prompt_path.unlink()
        state_path = self.state_dir / "state.json"

        with (
            mock.patch.object(
                cli,
                "run_acpx",
                side_effect=AssertionError(
                    "provider must not start after validation failure"
                ),
            ),
            mock.patch.object(native_backend, "_assert_publisher"),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        published = read_state(state_path)["native_result"]
        self.assertIsInstance(published, dict)
        assert isinstance(published, dict)
        self.assertEqual(published["outcome"], "failed")
        self.assertTrue(published["cleanup_confirmed"])
        self.assertIn("ACP runner validation failed", published["body"])

    def test_native_result_body_accepts_maximum_provider_output(self) -> None:
        state, _executables, prompt_path = self._state()
        state_path = self.state_dir / "state.json"
        output = "x" * cli.MAX_ACP_OUTPUT_CHARS
        prompt_completed = ProcessResult(
            0,
            json.dumps(
                {
                    "output": output,
                    "session_id": "test-session",
                    "model": "fable",
                    "effort": "high",
                    "cleanup_confirmed": True,
                }
            ),
            "",
        )

        with (
            mock.patch.object(
                cli.ProcessRunner,
                "run",
                return_value=prompt_completed,
            ),
            mock.patch.object(native_backend, "publish_completion") as publish,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 0)
        publish.assert_called_once()
        body = publish.call_args.kwargs["body"]
        self.assertEqual(len(body), cli.MAX_ACP_OUTPUT_CHARS)
        self.assertTrue(body.endswith(output[-100:]))

    def test_native_result_body_prioritizes_a_long_failure_reason(self) -> None:
        state, _executables, prompt_path = self._state()
        state_path = self.state_dir / "state.json"
        failure = "provider failure: " + ("reason " * 40_000)

        with (
            mock.patch.object(
                cli.ProcessRunner,
                "run",
                side_effect=ExecutionError(failure, cleanup_confirmed=True),
            ),
            mock.patch.object(native_backend, "publish_completion") as publish,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        publish.assert_called_once()
        body = publish.call_args.kwargs["body"]
        self.assertLessEqual(len(body), cli.MAX_ACP_OUTPUT_CHARS)
        self.assertIn("ACP runner failure: provider failure:", body)
        self.assertIn("reason reason", body)

    def test_native_validation_keeps_assignment_when_completion_identity_is_unknown(
        self,
    ) -> None:
        state, _executables, prompt_path = self._state()
        roles = state["roles"]
        assert isinstance(roles, dict)
        assignment = roles["planner"]
        assert isinstance(assignment, dict)
        assignment["task_id"] = "foreign-task"
        state_path = self.state_dir / "state.json"
        cli.write_state(state_path, state)

        with (
            mock.patch.object(
                cli,
                "run_acpx",
                side_effect=AssertionError(
                    "provider must not start after validation failure"
                ),
            ),
            mock.patch.object(native_backend, "publish_completion") as publish,
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        publish.assert_not_called()
        self.assertNotIn("native_result", read_state(state_path))


if __name__ == "__main__":
    unittest.main()
