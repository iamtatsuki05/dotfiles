from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team import cli, codex_acp, native_backend
from agent_team.adapters import ProcessResult
from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec
from agent_team.native_acp_dependencies import CodexAcpExecutables


def graph() -> GraphSpec:
    main = NodeRef("lead", Role.MAIN)
    planner = NodeRef("plan-a", Role.PLANNER)
    worker = NodeRef("work-a", Role.WORKER)
    reviewer = NodeRef("review-a", Role.REVIEWER)
    return GraphSpec(
        nodes=(main, planner, worker, reviewer),
        edges=(
            GraphEdge("lead", "plan-a", "delegates-to"),
            GraphEdge("lead", "work-a", "delegates-to"),
            GraphEdge("plan-a", "review-a", "reviewed-by"),
            GraphEdge("work-a", "review-a", "reviewed-by"),
        ),
        coordination=Coordination("agent", ("lead",), "serial", 1),
        routes=(),
    )


def plan() -> dict[str, object]:
    specs = {
        "lead": (Role.MAIN, "claude", "orchestrator", "tui_direct"),
        "plan-a": (Role.PLANNER, "claude", "read-only", "background"),
        "work-a": (Role.WORKER, "claude", "workspace-write", "background"),
        "review-a": (Role.REVIEWER, "claude", "read-only", "background"),
    }
    roles = {
        node_id: {
            "role": node_id,
            "kind": kind.value,
            "provider": provider,
            "transport": "direct" if kind is Role.MAIN else "acp",
            "model": f"model-{node_id}",
            "effort": "high",
            "permission": permission,
            "instructions": f"instructions-{node_id}",
            "execution": execution,
            "adapter_id": "adapter" if execution == "background" else None,
        }
        for node_id, (kind, provider, permission, execution) in specs.items()
    }
    return {
        "runtime": "tmux",
        "team_id": "named-team",
        "workspace": "/tmp/workspace",
        "config_path": "/tmp/config.toml",
        "state_path": "/tmp/state.json",
        "roles": roles,
        "graph": graph(),
        "task_specs": [],
    }


class NamedNativeRunnerTest(unittest.TestCase):
    def test_start_spec_binds_each_node_id_and_kind_exactly(self) -> None:
        spec = cli._start_spec(plan(), attach=False)

        self.assertEqual(
            set(spec.role_specs),
            {
                NodeRef("lead", Role.MAIN),
                NodeRef("plan-a", Role.PLANNER),
                NodeRef("work-a", Role.WORKER),
                NodeRef("review-a", Role.REVIEWER),
            },
        )
        self.assertEqual(
            spec.role_specs[NodeRef("work-a", Role.WORKER)].model, "model-work-a"
        )
        self.assertEqual(
            spec.role_specs[NodeRef("review-a", Role.REVIEWER)].model, "model-review-a"
        )
        self.assertEqual(spec.graph, graph())

    def test_start_spec_rejects_a_mixed_named_kind_before_backend_effects(self) -> None:
        named_plan = plan()
        roles = named_plan["roles"]
        assert isinstance(roles, dict)
        roles["work-a"] = {**roles["work-a"], "kind": "reviewer"}

        with self.assertRaisesRegex(TypeError, "kind"):
            cli._start_spec(named_plan, attach=False)

    def test_management_plan_round_trips_named_state_without_loading_config(
        self,
    ) -> None:
        named_plan = plan()
        raw_roles = named_plan["roles"]
        assert isinstance(raw_roles, dict)
        state = {
            "version": 4,
            "runtime": "tmux",
            "team_id": named_plan["team_id"],
            "workspace": named_plan["workspace"],
            "config_path": named_plan["config_path"],
            "state_path": named_plan["state_path"],
            "launcher_path": "/tmp/agent-team",
            "run_id": "run-1",
            "graph": graph().as_dict(),
            "role_specs": {node_id: dict(spec) for node_id, spec in raw_roles.items()},
            "roles": {},
            "task_specs": [],
        }

        with mock.patch.object(
            cli,
            "load_config",
            side_effect=AssertionError("state must be authoritative"),
        ):
            restored = cli._management_plan_from_state(state)
            start_spec = cli._start_spec(restored, attach=False)

        self.assertEqual(start_spec.graph, graph())
        self.assertEqual(
            start_spec.role_specs[NodeRef("work-a", Role.WORKER)].model,
            "model-work-a",
        )

    def test_acp_run_rejects_unknown_named_id_before_turn_or_provider(self) -> None:
        state = {
            "version": 4,
            "runtime": "tmux",
            "graph": graph().as_dict(),
            "role_specs": {
                node.node_id: {"kind": node.kind.value} for node in graph().nodes
            },
        }
        with (
            mock.patch.object(cli, "read_state", return_value=state),
            mock.patch.object(cli, "_acp_run_turn") as turn,
        ):
            result = cli.acp_run(
                role="work-a;rm",
                state_path=Path("/tmp/state.json"),
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=Path("/tmp/prompt.md"),
                launch_nonce="nonce1234",
            )

        self.assertEqual(result, 1)
        turn.assert_not_called()

    def test_runner_role_resolution_rejects_unknown_state_version(self) -> None:
        with self.assertRaisesRegex(cli.ConfigError, "supported saved state"):
            cli._state_role_target({"version": 99}, "work-a")

    def test_named_acp_runner_binds_node_model_to_client_argv_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            private = root / "private"
            private.mkdir(mode=0o700)
            state_path = root / "state.json"
            nonce = "worknonce1234"
            target = NodeRef("work-a", Role.WORKER)
            selected = CodexAcpExecutables(
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
            spec = {
                "kind": "worker",
                "provider": "codex",
                "transport": "acp",
                "model": "worker-model",
                "effort": "medium",
                "permission": "workspace-write",
                "instructions": "worker instructions",
                "execution": "background",
                "adapter_id": "codex-acp-scoped-1.10.0",
                "acp_executables": {},
            }
            receipt = {
                "output": "worker result",
                "session_id": "session-work-a",
                "model": "worker-model",
                "effort": "medium",
                "cleanup_confirmed": True,
            }
            result_file = private / "client-result.json"
            result_file.write_text(
                json.dumps({"version": 1, "launch_nonce": nonce, "receipt": receipt}),
                encoding="utf-8",
            )
            result_file.chmod(0o600)
            assignment: dict[str, object] = {
                "role": target.node_id,
                "role_kind": target.kind.value,
                "task_id": "task-1",
                "dispatch_id": "dispatch-1",
                "terminal_handle": "terminal-1",
                "prompt_path": str(root / "prompt.md"),
                "launch_nonce": nonce,
                "launcher_owned_runner": True,
                "completion_observed": False,
                "agent_command": "codex-agent",
                "session_name": f"agent-team-{target.node_id}-{nonce}",
                "provider_private_root": str(private),
                "adapter_snapshot": {},
            }
            state = {
                "version": 4,
                "runtime": "tmux",
                "team_id": "named-team",
                "workspace": str(workspace),
                "state_path": str(state_path),
                "run_id": "run-1",
                "graph": graph().as_dict(),
                "role_specs": {
                    "lead": {"kind": "main"},
                    "plan-a": {"kind": "planner"},
                    "work-a": spec,
                    "review-a": {"kind": "reviewer"},
                },
                "roles": {target.node_id: assignment},
            }
            argv_calls: list[dict[str, object]] = []

            class FakeRunner:
                def __init__(self, **_kwargs: object) -> None:
                    pass

                def run(
                    self,
                    argv: list[str],
                    *,
                    cwd: Path,
                    env: dict[str, str],
                    input_text: str,
                    timeout_seconds: float,
                ) -> ProcessResult:
                    argv_calls.append(
                        {
                            "argv": argv,
                            "cwd": cwd,
                            "env": env,
                            "input_text": input_text,
                            "timeout_seconds": timeout_seconds,
                        }
                    )
                    return ProcessResult(0, json.dumps(receipt), "")

            with (
                mock.patch.object(
                    CodexAcpExecutables,
                    "from_dict",
                    return_value=selected,
                ),
                mock.patch.object(CodexAcpExecutables, "verify"),
                mock.patch.object(cli, "codex_adapter_snapshot", return_value={}),
                mock.patch.object(codex_acp, "validate_assignment"),
                mock.patch.object(cli, "validate_prompt_file"),
                mock.patch.object(
                    codex_acp, "agent_command", return_value="codex-agent"
                ),
                mock.patch.object(
                    codex_acp,
                    "environment",
                    return_value={"CODEX_HOME": str(root / "codex")},
                ),
                mock.patch.object(
                    cli, "client_argv", return_value=["node", "codex"]
                ) as client,
                mock.patch.object(cli, "read_prompt_file", return_value="prompt"),
                mock.patch.object(cli, "_NativeAcpClientRunner", FakeRunner),
                mock.patch.object(
                    native_backend,
                    "publish_completion",
                    return_value="succeeded",
                ) as publish,
            ):
                result = cli._acp_run_turn(
                    state=state,
                    role=target.node_id,
                    state_path=state_path,
                    task_id="task-1",
                    dispatch_id="dispatch-1",
                    terminal_handle="terminal-1",
                    prompt_path=Path(cast(str, assignment["prompt_path"])),
                    launch_nonce=nonce,
                    cancellation=threading.Event(),
                )

        self.assertEqual(result, 0)
        client.assert_called_once()
        self.assertEqual(client.call_args.kwargs["model"], "worker-model")
        self.assertEqual(argv_calls[0]["argv"], ["node", "codex"])
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["role"], target.node_id)

    def test_mcp_catalog_reads_named_nodes_without_importing_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            state = {
                "version": 4,
                "runtime": "tmux",
                "graph": graph().as_dict(),
                "role_specs": {
                    node.node_id: {"kind": node.kind.value} for node in graph().nodes
                },
            }
            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(cli, "read_state", return_value=state),
            ):
                catalog = cli._mcp_tools()

        for tool in catalog:
            schema = cast(dict[str, object], tool["inputSchema"])
            properties = cast(dict[str, object], schema["properties"])
            if "role" in properties:
                role_schema = cast(dict[str, object], properties["role"])
                self.assertEqual(role_schema["enum"], ["plan-a", "work-a", "review-a"])

    def test_mcp_catalog_does_not_fallback_when_saved_state_is_invalid(self) -> None:
        with (
            mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": "/tmp/state.json"}),
            mock.patch.object(
                cli,
                "read_state",
                side_effect=cli.ConfigError("saved state is invalid"),
            ),
            self.assertRaises(cli.ConfigError),
        ):
            cli._mcp_tools()


if __name__ == "__main__":
    unittest.main()
