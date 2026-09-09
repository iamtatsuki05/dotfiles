from __future__ import annotations

import os
import shlex
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_config_v5_cli import _RecordingBackend, _v5_config_text

from agent_team import cli, orca, runtime_mcp
from agent_team.contracts import NodeRef, Role, TaskGet, TaskStatusReceipt
from agent_team.named_graph import GraphSpec
from agent_team.native_acp_dependencies import NativeAcpDependencyError
from agent_team.workflow import WorkflowEngine


class NamedOrcaCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="agent-team-named-orca-")
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config_path = self.root / "config.toml"
        text = _v5_config_text(runtime="orca")
        self.config_path.write_text(text, encoding="utf-8")
        prompts = self.root / "prompts"
        prompts.mkdir()
        for name in (
            "main.md",
            "implementation-a.md",
            "implementation-b.md",
            "review-a.md",
            "review-b.md",
        ):
            (prompts / name).write_text(f"saved prompt for {name}\n", encoding="utf-8")
        config = cli.load_v5_config_data(self.config_path, tomllib.loads(text))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            self.plan = cli._v5_runtime_plan(config, self.workspace, "build")

    def test_serial_start_passes_exact_named_graph_to_orca_backend(self) -> None:
        backend = _RecordingBackend()
        engine = WorkflowEngine(backend)
        with (
            mock.patch.object(cli, "_start_prerequisites") as prerequisites,
            mock.patch.object(cli, "_runtime_engine", return_value=(engine, backend)),
        ):
            result = cli.start_team(self.plan, attach=False)
        prerequisites.assert_called_once_with(self.plan)
        self.assertEqual(result["status"], "started")
        spec = backend.start_spec
        self.assertIsNotNone(spec)
        assert spec is not None and spec.graph is not None
        self.assertEqual(spec.graph.main_node, NodeRef("lead", Role.MAIN))
        self.assertIn(NodeRef("implementation-a", Role.WORKER), spec.role_specs)
        self.assertEqual(
            [task.task_id for task in spec.task_specs], ["implement-a", "implement-b"]
        )
        self.assertFalse((self.root / "state").exists())

    def test_orca_main_command_uses_named_main_and_saved_plan_argv(self) -> None:
        selected = self.root / "caller-bin" / "claude"
        selected.parent.mkdir()
        selected.write_text("#!/bin/sh\n", encoding="utf-8")
        selected.chmod(0o700)
        with (
            mock.patch("agent_team.backend.OrcaBackend") as backend_class,
            mock.patch.object(cli.shutil, "which", return_value=str(selected)),
            mock.patch.object(
                cli,
                "acp_environment",
                return_value={"PATH": str(selected.parent), "HOME": str(self.root)},
            ),
        ):
            cli._runtime_engine(self.plan, resume_existing=False)
            factory = backend_class.call_args.kwargs["main_command_factory"]
            command = factory(self.root / "orca.sock")
        argv = shlex.split(command)
        roles = self.plan["roles"]
        assert isinstance(roles, dict)
        self.assertEqual(argv[:2], ["/usr/bin/env", "-i"])
        executable_index = argv.index(str(selected.resolve()))
        self.assertEqual(argv[executable_index + 1 :], roles["lead"]["argv"][1:])
        self.assertIn("mcp__agent_team__task_dispatch", " ".join(argv))
        self.assertIn("あなたのnode IDはleadです", " ".join(argv))
        self.assertNotIn("mcp__agent_team__task_batch_open", " ".join(argv))

    def _program_plan(self, dispatch_mode: str) -> dict[str, object]:
        raw = GraphSpec.from_dict(self.plan["graph"]).as_dict()
        raw["nodes"] = [node for node in raw["nodes"] if node["kind"] != "main"]
        raw["edges"] = [edge for edge in raw["edges"] if edge["source"] != "lead"]
        raw["coordination"] = {
            "mode": "program",
            "entry_nodes": ["implementation-a", "implementation-b"],
            "dispatch_mode": dispatch_mode,
            "max_active": 1 if dispatch_mode == "serial" else 2,
        }
        return {
            **self.plan,
            "graph": raw,
            "roles": {
                key: value for key, value in self.plan["roles"].items() if key != "lead"
            },
        }

    def test_program_without_tasks_fails_before_dependencies_or_runtime(self) -> None:
        for mode in ("serial", "parallel"):
            plan = {**self._program_plan(mode), "task_specs": []}
            with (
                mock.patch.object(cli, "_start_prerequisites") as prerequisites,
                mock.patch.object(cli, "_runtime_engine") as runtime,
                self.assertRaisesRegex(cli.ConfigError, "declared TaskSpecs"),
            ):
                cli.start_team(plan, attach=False)
            prerequisites.assert_not_called()
            runtime.assert_not_called()

    def test_program_engine_never_builds_a_main_command(self) -> None:
        for mode in ("serial", "parallel"):
            plan = self._program_plan(mode)
            cli._require_named_runtime_plan(plan)
            with (
                mock.patch("agent_team.backend.OrcaBackend") as backend_class,
                mock.patch.object(
                    cli, "_resolve_orca_main_executable"
                ) as main_executable,
            ):
                cli._runtime_engine(plan, resume_existing=False)
            self.assertIsNone(backend_class.call_args.kwargs["main_command_factory"])
            main_executable.assert_not_called()

    def test_parallel_orca_start_exposes_batch_tools_and_declared_tasks(self) -> None:
        text = (
            _v5_config_text(runtime="orca")
            .replace('dispatch_mode = "serial"', 'dispatch_mode = "parallel"')
            .replace("max_active = 1", "max_active = 2")
        )
        config = cli.load_v5_config_data(self.config_path, tomllib.loads(text))
        plan = cli._v5_runtime_plan(config, self.workspace, "build")
        self.assertIn(
            "mcp__agent_team__task_batch_open", " ".join(plan["roles"]["lead"]["argv"])
        )
        backend = _RecordingBackend()
        with (
            mock.patch.object(cli, "_start_prerequisites"),
            mock.patch.object(
                cli, "_runtime_engine", return_value=(WorkflowEngine(backend), backend)
            ),
        ):
            cli.start_team(plan, attach=False)
        self.assertEqual(
            backend.start_spec.graph.coordination.dispatch_mode, "parallel"
        )
        with (
            mock.patch.object(cli, "_start_prerequisites") as prerequisites,
            self.assertRaisesRegex(cli.ConfigError, "declared TaskSpecs"),
        ):
            cli.start_team({**plan, "task_specs": []}, attach=False)
        prerequisites.assert_not_called()

    def test_management_uses_the_saved_named_snapshot(self) -> None:
        self.config_path.write_text("invalid replacement config", encoding="utf-8")
        state = {
            **self.plan,
            "version": 4,
            "orca_socket": str(self.root / "orca.sock"),
            "role_specs": self.plan["roles"],
        }
        plan = cli._management_plan_from_state(state)
        spec = cli._start_spec(plan, attach=False)
        assert spec.graph is not None
        self.assertEqual(spec.graph.main_node, NodeRef("lead", Role.MAIN))
        self.assertEqual(spec.task_specs[0].task_id, "implement-a")
        self.assertIn(
            "saved prompt for implementation-a.md",
            spec.role_specs[NodeRef("implementation-a", Role.WORKER)].instructions,
        )

    def test_named_orca_mcp_routes_through_the_selected_typed_backend(self) -> None:
        state = {
            **self.plan,
            "version": 4,
            "orca_socket": str(self.root / "orca.sock"),
            "run_id": "orca-run-1",
            "main_terminal": "orca-main-terminal",
            "worktree_id": f"orca-repo::{self.workspace}",
            "role_specs": self.plan["roles"],
            "roles": {},
        }
        backend = mock.Mock()
        backend.request.return_value = TaskStatusReceipt("implement-a", "pending", {})
        with mock.patch.object(
            cli, "_runtime_engine", return_value=(None, backend)
        ) as runtime:
            session = runtime_mcp.RuntimeMcpSession(
                Path(str(state["state_path"])), state
            )
        runtime.assert_called_once()
        self.assertEqual(runtime.call_args.args[0]["runtime"], "orca")
        self.assertTrue(runtime.call_args.kwargs["resume_existing"])
        with mock.patch.object(runtime_mcp, "read_state", return_value=state):
            result = session.execute("task_get", {"task_id": "implement-a"})
        backend.request.assert_called_once_with(TaskGet("implement-a"))
        self.assertEqual(
            result, {"task_id": "implement-a", "status": "pending", "record": {}}
        )

    def test_named_claude_dependency_failure_does_not_use_acpx_or_another_runtime(
        self,
    ) -> None:
        for platform, command in (("darwin", "orca"), ("linux", "orca-ide")):
            with self.subTest(platform=platform):
                with (
                    mock.patch.object(orca, "sys", SimpleNamespace(platform=platform)),
                    mock.patch.object(cli, "require_binary") as require,
                    mock.patch.object(
                        cli, "mcp_server_path", return_value=Path(__file__)
                    ),
                    mock.patch.object(cli.os, "access", return_value=True),
                    mock.patch.object(
                        cli.NativeAcpExecutables,
                        "resolve",
                        side_effect=NativeAcpDependencyError("selected SDK is missing"),
                    ) as scoped,
                    mock.patch.object(
                        cli.AcpExecutables,
                        "resolve",
                        side_effect=AssertionError("acpx fallback"),
                    ) as acpx,
                    mock.patch.object(
                        cli.CodexAcpExecutables,
                        "resolve",
                        side_effect=AssertionError("unselected Codex"),
                    ) as codex,
                    self.assertRaisesRegex(
                        cli.ConfigError,
                        "selected claude ACP dependencies.*selected SDK",
                    ),
                ):
                    cli._start_prerequisites(self.plan)
                scoped.assert_called_once_with()
                acpx.assert_not_called()
                codex.assert_not_called()
                self.assertEqual(
                    {call.args[0] for call in require.call_args_list},
                    {command, "claude"},
                )
                self.assertFalse((self.root / "state").exists())


if __name__ == "__main__":
    unittest.main()
