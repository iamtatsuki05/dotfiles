from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team import mcp_server
from agent_team.adapters import (
    AdapterContext,
    AdapterSnapshot,
    ExecutionResult,
    FileIdentity,
    ReadSnapshot,
)
from agent_team.registry import (
    adapter_id_for_profile,
    profile_execution,
    require_profile,
)
from agent_team.runtime import (
    RuntimeValidationError,
    build_background_runner_command,
    validate_state_object,
    write_state,
)


def state_fixture(root: Path, *, version: int = 3) -> dict[str, object]:
    state_path = root / "state.json"
    roles = {
        "main": {
            "provider": "claude",
            "transport": "direct",
            "model": "fable",
            "effort": "high",
            "permission": "orchestrator",
            "instructions": "main",
            "execution": "tui_direct",
        },
        "planner": {
            "provider": "copilot",
            "transport": "direct",
            "model": "gpt-4.1",
            "effort": "none",
            "permission": "read-only",
            "instructions": "planner",
            "execution": "background",
            "adapter_id": "github-copilot-direct-readonly-1.0.81",
        },
        "worker": {
            "provider": "codex",
            "transport": "direct",
            "model": "gpt-6-astra",
            "effort": "medium",
            "permission": "workspace-write",
            "instructions": "worker",
            "execution": "tui_direct",
        },
        "reviewer": {
            "provider": "codex",
            "transport": "direct",
            "model": "gpt-6-astra",
            "effort": "high",
            "permission": "read-only",
            "instructions": "reviewer",
            "execution": "tui_direct",
        },
    }
    return {
        "version": version,
        "runtime": "orca",
        "team_id": root.name,
        "workspace": str(root / "workspace"),
        "config_path": str(root / "config.toml"),
        "state_path": str(state_path),
        "launcher_path": "/tmp/agent-team",
        "worktree_id": "repo::workspace",
        "orca_socket": str(root / "orca.sock"),
        "run_id": "run_1",
        "main_terminal": "term_main",
        "role_specs": roles,
        "roles": {},
    }


class BackgroundContractTest(unittest.TestCase):
    def test_copilot_read_only_is_the_only_new_runnable_profile(self) -> None:
        self.assertEqual(
            require_profile("copilot", "planner", "direct", "read-only").harness_id,
            "copilot",
        )
        self.assertEqual(
            profile_execution("copilot", "reviewer", "direct", "read-only"),
            "background",
        )
        self.assertEqual(
            adapter_id_for_profile("copilot", "planner", "direct", "read-only"),
            "github-copilot-direct-readonly-1.0.81",
        )
        with self.assertRaises(ValueError):
            require_profile("copilot", "worker", "direct", "workspace-write")
        with self.assertRaises(ValueError):
            require_profile("opencode", "planner", "direct", "read-only")

    def test_state_v2_is_rejected_and_v3_requires_execution_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "team"
            root.mkdir(mode=0o700)
            state = state_fixture(root)
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(root / "state.json", {**state, "version": 2})
            role_specs = cast(dict[str, dict[str, object]], state["role_specs"])
            role_specs["planner"] = {**role_specs["planner"], "adapter_id": None}
            with self.assertRaisesRegex(RuntimeValidationError, "adapter_id"):
                validate_state_object(root / "state.json", state)

    def test_background_start_preflights_before_task_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "team"
            root.mkdir(mode=0o700)
            workspace = root / "workspace"
            workspace.mkdir()
            state = state_fixture(root)
            state["workspace"] = str(workspace)
            events: list[str] = []
            snapshot_root = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
            snapshot = ReadSnapshot(snapshot_root, ())
            adapter_snapshot = AdapterSnapshot(
                "github-copilot-direct-readonly-1.0.81",
                "agent-team-test",
                Path("/bin/copilot"),
                "GitHub Copilot CLI 1.0.81",
                FileIdentity(1, 2, 3, 4, "hash"),
            )

            class FakeAdapter:
                adapter_id = "github-copilot-direct-readonly-1.0.81"

                def preflight(self, context: AdapterContext) -> AdapterSnapshot:
                    events.append("preflight")
                    return adapter_snapshot

            def create_task(*args: object, **kwargs: object) -> str:
                events.append("task")
                return "task_1"

            with (
                mock.patch.object(
                    mcp_server, "background_adapter", return_value=FakeAdapter()
                ),
                mock.patch.object(
                    mcp_server,
                    "create_read_snapshot",
                    return_value=snapshot,
                ) as snapshot_create,
                mock.patch.object(mcp_server, "_create_task", side_effect=create_task),
                mock.patch.object(
                    mcp_server, "create_prompt_file", return_value=root / "prompt"
                ),
                mock.patch.object(mcp_server, "save_state"),
                mock.patch.object(
                    mcp_server,
                    "run_orca",
                    side_effect=[
                        {"terminal": {"handle": "term_worker"}},
                        {
                            "injected": False,
                            "dispatch": {
                                "id": "dispatch_1",
                                "task_id": "task_1",
                                "assignee_handle": "term_worker",
                                "run_id": "run_1",
                            },
                        },
                        {},
                    ],
                ),
            ):
                assignment = mcp_server.start_background_role(
                    root / "state.json", state, "planner", "plan"
                )

            self.assertEqual(events, ["preflight", "task"])
            snapshot_create.assert_called_once_with(workspace, state_root=root)
            self.assertEqual(assignment["execution"], "background")
            self.assertEqual(assignment["adapter_id"], adapter_snapshot.adapter_id)
            mcp_server.cleanup_background_resources(
                assignment, root / "state.json", role="planner"
            )

    def test_background_runner_command_does_not_include_prompt_text(self) -> None:
        command = build_background_runner_command(
            {
                "launcher_path": "/tmp/agent-team",
                "state_path": "/tmp/team/state.json",
            },
            "planner",
            task_id="task_1",
            dispatch_id="dispatch_1",
            terminal_handle="term_worker",
            prompt_path=Path("/tmp/team/prompt-planner-nonce1234.md"),
            launch_nonce="nonce1234",
        )
        self.assertIn("_background-run planner", command)
        self.assertNotIn("user prompt", command)

    def test_background_runner_executes_on_snapshot_and_sends_matching_done(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "team"
            root.mkdir(mode=0o700)
            workspace = root / "workspace"
            workspace.mkdir()
            private = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
            snapshot = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
            state = state_fixture(root)
            state["workspace"] = str(workspace)
            prompt = mcp_server.create_prompt_file(
                root, "planner", "nonce1234", "planner instructions\n\nreview"
            )
            state["roles"] = {
                "planner": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                    "execution": "background",
                    "adapter_id": "github-copilot-direct-readonly-1.0.81",
                    "launch_nonce": "nonce1234",
                    "prompt_path": str(prompt),
                    "provider_private_root": str(private),
                    "snapshot_root": str(snapshot),
                    "adapter_snapshot": {
                        "adapter_id": "github-copilot-direct-readonly-1.0.81",
                        "revision": "test",
                        "executable": "/bin/copilot",
                        "version": "GitHub Copilot CLI 1.0.81",
                        "identity": {
                            "device": 1,
                            "inode": 2,
                            "size": 3,
                            "mtime_ns": 4,
                            "sha256": "hash",
                        },
                    },
                }
            }
            write_state(root / "state.json", state)
            observed: dict[str, object] = {}
            done: list[tuple[str, dict[str, object], str]] = []

            class FakeAdapter:
                adapter_id = "github-copilot-direct-readonly-1.0.81"

                def execute(
                    self,
                    context: AdapterContext,
                    adapter_snapshot: AdapterSnapshot,
                    prompt_text: str,
                    runner: object,
                ) -> ExecutionResult:
                    observed["workspace"] = context.workspace
                    observed["private_root"] = context.private_root
                    observed["prompt"] = prompt_text
                    return ExecutionResult("provider output; worker_done", "", 0)

            from agent_team import cli

            with (
                mock.patch.object(
                    cli, "background_adapter", return_value=FakeAdapter()
                ),
                mock.patch.object(
                    cli,
                    "_send_worker_done",
                    side_effect=lambda state, assignment, *, outcome, body: done.append(
                        (outcome, assignment, body)
                    ),
                ),
            ):
                exit_code = cli.background_run(
                    role="planner",
                    state_path=root / "state.json",
                    task_id="task_1",
                    dispatch_id="dispatch_1",
                    terminal_handle="term_worker",
                    prompt_path=prompt,
                    launch_nonce="nonce1234",
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(observed["workspace"], snapshot)
            self.assertEqual(observed["private_root"], private)
            self.assertIn("worker_done", str(done[0][2]))
            self.assertEqual(done[0][0], "succeeded")
            self.assertTrue(prompt.exists())
            self.assertFalse(private.exists())
            self.assertFalse(snapshot.exists())
            mcp_server.cleanup_background_resources(
                done[0][1],
                root / "state.json",
                role="planner",
            )


if __name__ == "__main__":
    unittest.main()
