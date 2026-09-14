from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import cli
from agent_team.contracts import (
    BackendPort,
    BackendRequest,
    BackendResult,
    NodeRef,
    Role,
    RunRef,
    StartResult,
    StartSpec,
    StopResult,
    TerminalRef,
)
from agent_team.workflow import WorkflowEngine


def _task_toml(team_id: str, task_id: str, objective: str) -> str:
    return f'''\
[[teams.{team_id}.tasks]]
task_id = "{task_id}"
objective = "{objective}"
acceptance_criteria = ["the implementation is complete"]
allowed_paths = ["src"]
forbidden_paths = [".secrets"]
dependencies = []
evidence_requirements = ["the fixed verification command output"]
consultation_conditions = ["when the implementation contract is ambiguous"]

[[teams.{team_id}.tasks.verification]]
name = "unit"
argv = ["python", "-V"]
timeout_seconds = 10
'''


def _node_toml(
    team_id: str,
    node_id: str,
    label: str,
    kind: str,
    model: str,
    effort: str,
    prompt: str,
    permission: str,
) -> str:
    transport = "direct" if kind == "main" else "acp"
    return f'''\
[[teams.{team_id}.nodes]]
id = "{node_id}"
label = "{label}"
kind = "{kind}"

[teams.{team_id}.nodes.role_spec]
provider = "claude"
transport = "{transport}"
model = "{model}"
effort = "{effort}"
prompt = "prompts/{prompt}"
permission = "{permission}"
'''


def _edge_toml(team_id: str, source: str, target: str, kind: str) -> str:
    return f'''\
[[teams.{team_id}.edges]]
source = "{source}"
target = "{target}"
kind = "{kind}"
'''


def _route_toml(team_id: str, task_id: str, worker: str, reviewer: str) -> str:
    return f'''\
[[teams.{team_id}.routes]]
task_id = "{task_id}"
implementation_writer = "{worker}"
implementation_reviewer = "{reviewer}"
'''


def _team_toml(team_id: str, *, mode: str, dispatch_mode: str) -> str:
    is_agent = mode == "agent"
    nodes = (
        (
            (
                "implementation-a",
                "Implementation A",
                "worker",
                "claude-worker-a",
                "medium",
                "implementation-a.md",
                "workspace-write",
            ),
            (
                "lead",
                "Main lead",
                "main",
                "claude-main",
                "high",
                "main.md",
                "orchestrator",
            ),
            (
                "implementation-b",
                "Implementation B",
                "worker",
                "claude-worker-b",
                "xhigh",
                "implementation-b.md",
                "workspace-write",
            ),
            (
                "review-a",
                "Review A",
                "reviewer",
                "claude-review-a",
                "low",
                "review-a.md",
                "read-only",
            ),
            (
                "review-b",
                "Review B",
                "reviewer",
                "claude-review-b",
                "high",
                "review-b.md",
                "read-only",
            ),
        )
        if is_agent
        else (
            (
                "implementation-a",
                "Implementation A",
                "worker",
                "claude-worker-a",
                "medium",
                "implementation-a.md",
                "workspace-write",
            ),
            (
                "implementation-b",
                "Implementation B",
                "worker",
                "claude-worker-b",
                "xhigh",
                "implementation-b.md",
                "workspace-write",
            ),
            (
                "review-a",
                "Review A",
                "reviewer",
                "claude-review-a",
                "low",
                "review-a.md",
                "read-only",
            ),
            (
                "review-b",
                "Review B",
                "reviewer",
                "claude-review-b",
                "high",
                "review-b.md",
                "read-only",
            ),
        )
    )
    lines = [
        f"[teams.{team_id}]",
        f'name = "{team_id.title()} Team"',
        "max_review_rounds = 2",
        "",
    ]
    for node in nodes:
        lines.append(
            _node_toml(
                team_id,
                node[0],
                node[1],
                node[2],
                node[3],
                node[4],
                node[5],
                node[6],
            )
        )
    lines.extend(
        (
            _edge_toml(team_id, "lead", "implementation-a", "delegates-to")
            if is_agent
            else "",
            _edge_toml(team_id, "lead", "implementation-b", "delegates-to")
            if is_agent
            else "",
            _edge_toml(team_id, "implementation-a", "review-a", "reviewed-by"),
            _edge_toml(team_id, "implementation-b", "review-b", "reviewed-by"),
            _edge_toml(team_id, "implementation-a", "review-b", "consults-to"),
            f"[teams.{team_id}.coordination]",
            f'mode = "{mode}"',
            (
                'entry_nodes = ["lead"]'
                if is_agent
                else 'entry_nodes = ["implementation-a", "implementation-b"]'
            ),
            f'dispatch_mode = "{dispatch_mode}"',
            f"max_active = {1 if dispatch_mode == 'serial' else 2}",
            "",
            _task_toml(team_id, "implement-a", "implement the first change"),
            _task_toml(team_id, "implement-b", "implement the second change"),
            _route_toml(team_id, "implement-a", "implementation-a", "review-a"),
            _route_toml(team_id, "implement-b", "implementation-b", "review-b"),
        )
    )
    return "\n".join(lines)


def _v5_config_text(*, runtime: str = "tmux") -> str:
    return "\n".join(
        (
            "version = 5",
            f'runtime = "{runtime}"',
            "",
            _team_toml("build", mode="agent", dispatch_mode="serial"),
            _team_toml("program", mode="program", dispatch_mode="parallel"),
        )
    )


class _RecordingBackend(BackendPort):
    def __init__(self) -> None:
        self.start_spec: StartSpec | None = None
        self.last_start_response: dict[str, object] | None = None

    def start(self, spec: StartSpec) -> StartResult:
        self.start_spec = spec
        self.last_start_response = {"status": "started", "team_id": spec.team_id}
        program = spec.graph is not None and spec.graph.coordination.mode == "program"
        return StartResult(
            team_id=spec.team_id,
            run_id=RunRef("run-v5"),
            main_terminal_id=None if program else TerminalRef("main-terminal-v5"),
            coordinator_terminal_id=TerminalRef("coordinator-terminal-v5")
            if program
            else None,
            state_path=spec.state_path,
        )

    def request(self, _request: BackendRequest) -> BackendResult:
        raise AssertionError("v5 CLI integration test must not dispatch a task")

    def stop(self) -> StopResult:
        raise AssertionError("v5 CLI integration test must not stop a team")


class ConfigV5CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="agent-team-v5-cli-")
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        prompts = self.root / "prompts"
        prompts.mkdir()
        for name in (
            "main.md",
            "implementation-a.md",
            "implementation-b.md",
            "review-a.md",
            "review-b.md",
        ):
            (prompts / name).write_text(f"prompt for {name}\n", encoding="utf-8")
        self.config_path = self.root / "config-v5.toml"
        self.config_path.write_text(_v5_config_text(), encoding="utf-8")
        self.state_home = self.root / "state"
        self.old_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.state_home)

    def tearDown(self) -> None:
        if self.old_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.old_state_home
        self.temp_dir.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cli.main(
                [
                    *arguments,
                    "--config",
                    str(self.config_path),
                    "--cwd",
                    str(self.workspace),
                ]
            )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_start_dry_run_binds_exact_named_nodes_and_specs_to_start_spec(
        self,
    ) -> None:
        result, stdout, stderr = self.run_cli("start", "--team", "build", "--dry-run")

        self.assertEqual(result, 0, stderr)
        self.assertEqual(stderr, "")
        plan = json.loads(stdout)
        self.assertEqual(plan["runtime"], "tmux")
        self.assertTrue(plan["team_id"].startswith("build-"))
        spec = cli._start_spec(plan, attach=False)
        assert spec.graph is not None
        self.assertEqual(
            {node.node_id: node.kind.value for node in spec.graph.nodes},
            {
                "lead": "main",
                "implementation-a": "worker",
                "implementation-b": "worker",
                "review-a": "reviewer",
                "review-b": "reviewer",
            },
        )
        self.assertEqual(
            set(spec.role_specs),
            {
                NodeRef("lead", Role.MAIN),
                NodeRef("implementation-a", Role.WORKER),
                NodeRef("implementation-b", Role.WORKER),
                NodeRef("review-a", Role.REVIEWER),
                NodeRef("review-b", Role.REVIEWER),
            },
        )
        self.assertEqual(
            spec.role_specs[NodeRef("lead", Role.MAIN)].model,
            "claude-main",
        )
        self.assertEqual(
            spec.role_specs[NodeRef("implementation-a", Role.WORKER)].model,
            "claude-worker-a",
        )
        self.assertEqual(
            spec.role_specs[NodeRef("implementation-b", Role.WORKER)].model,
            "claude-worker-b",
        )
        self.assertEqual(
            spec.role_specs[NodeRef("implementation-a", Role.WORKER)].effort,
            "medium",
        )
        self.assertEqual(
            spec.role_specs[NodeRef("implementation-b", Role.WORKER)].effort,
            "xhigh",
        )
        self.assertIn(
            "prompt for implementation-a.md",
            spec.role_specs[NodeRef("implementation-a", Role.WORKER)].instructions,
        )
        self.assertIn(
            "prompt for implementation-b.md",
            spec.role_specs[NodeRef("implementation-b", Role.WORKER)].instructions,
        )
        self.assertEqual(
            tuple(task.task_id for task in spec.task_specs),
            ("implement-a", "implement-b"),
        )

    def test_start_requires_one_exact_team_before_prerequisites_or_runtime(
        self,
    ) -> None:
        for selection in ((), ("missing",), ("build", "program")):
            with self.subTest(selection=selection):
                with (
                    mock.patch.object(cli, "_start_prerequisites") as prerequisites,
                    mock.patch.object(cli, "_runtime_engine") as runtime,
                    mock.patch.object(cli, "start_team") as start_team,
                ):
                    arguments = ["start"]
                    for team in selection:
                        arguments.extend(("--team", team))
                    result, stdout, stderr = self.run_cli(*arguments)

                self.assertEqual(result, 2)
                self.assertEqual(stdout, "")
                self.assertIn("team", stderr.lower())
                prerequisites.assert_not_called()
                runtime.assert_not_called()
                start_team.assert_not_called()

    def test_inspection_and_validate_do_not_probe_path_subprocess_or_dependencies(
        self,
    ) -> None:
        def unavailable(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("inspection must not resolve a runtime dependency")

        commands = [
            ("teams",),
            ("graph", "--team", "build", "--format", "json"),
            ("graph", "--team", "build", "--format", "ascii"),
            ("graph", "--team", "build", "--format", "mermaid"),
            ("validate",),
            ("validate", "--team", "build"),
        ]
        with (
            mock.patch.object(cli.shutil, "which", return_value=None),
            mock.patch.object(cli.subprocess, "run", side_effect=unavailable),
            mock.patch.object(cli.subprocess, "Popen", side_effect=unavailable),
            mock.patch.object(cli, "require_binary", side_effect=unavailable),
            mock.patch.object(cli.AcpExecutables, "resolve", side_effect=unavailable),
            mock.patch.object(
                cli.NativeAcpExecutables, "resolve", side_effect=unavailable
            ),
            mock.patch.object(
                cli.CodexAcpExecutables, "resolve", side_effect=unavailable
            ),
        ):
            outputs: list[tuple[tuple[str, ...], int, str, str]] = []
            for command in commands:
                result, stdout, stderr = self.run_cli(*command)
                outputs.append((command, result, stdout, stderr))

        for command, result, stdout, stderr in outputs:
            with self.subTest(command=command):
                self.assertEqual(result, 0, stderr)
                self.assertNotEqual(stdout, "")
                self.assertEqual(stderr, "")
        teams = json.loads(outputs[0][2])
        self.assertEqual([team["id"] for team in teams["teams"]], ["build", "program"])
        graph_json = json.loads(outputs[1][2])
        self.assertEqual(graph_json["coordination"]["mode"], "agent")
        self.assertIn(
            {"source": "implementation-a", "target": "review-b", "kind": "consults-to"},
            graph_json["edges"],
        )
        self.assertIn("consults-to", outputs[2][2])
        self.assertIn("consults-to", outputs[3][2])
        all_validation = json.loads(outputs[4][2])
        selected_validation = json.loads(outputs[5][2])
        self.assertEqual(
            all_validation,
            {"version": 5, "valid": True, "teams": ["build", "program"]},
        )
        self.assertEqual(
            selected_validation,
            {"version": 5, "valid": True, "teams": ["build"]},
        )

    def test_program_parallel_plan_reaches_the_selected_runtime(
        self,
    ) -> None:
        result, stdout, stderr = self.run_cli("start", "--team", "program", "--dry-run")
        self.assertEqual(result, 0, stderr)
        plan = json.loads(stdout)
        self.assertEqual(plan["graph"]["coordination"]["mode"], "program")
        self.assertEqual(plan["graph"]["coordination"]["dispatch_mode"], "parallel")
        self.assertEqual(
            plan["graph"]["coordination"]["entry_nodes"],
            ["implementation-a", "implementation-b"],
        )
        self.assertNotIn("lead", plan["roles"])

        backend = _RecordingBackend()
        with (
            mock.patch.object(cli, "_start_prerequisites") as prerequisites,
            mock.patch.object(
                cli, "_runtime_engine", return_value=(WorkflowEngine(backend), backend)
            ) as runtime,
        ):
            result, stdout, stderr = self.run_cli("start", "--team", "program")
        self.assertEqual(result, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "started")
        prerequisites.assert_called_once()
        runtime.assert_called_once()
        assert backend.start_spec is not None
        assert backend.start_spec.graph is not None
        self.assertEqual(
            backend.start_spec.graph.coordination.dispatch_mode, "parallel"
        )
        self.assertIsNone(backend.start_spec.graph.main_node)

    def test_orca_named_program_start_passes_selected_graph_without_main(
        self,
    ) -> None:
        self.config_path.write_text(_v5_config_text(runtime="orca"), encoding="utf-8")
        backend = _RecordingBackend()
        with (
            mock.patch.object(cli, "_start_prerequisites") as prerequisites,
            mock.patch.object(
                cli, "_runtime_engine", return_value=(WorkflowEngine(backend), backend)
            ) as runtime,
        ):
            result, stdout, stderr = self.run_cli("start", "--team", "program")

        self.assertEqual(result, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "started")
        prerequisites.assert_called_once()
        runtime.assert_called_once()
        self.assertEqual(runtime.call_args.args[0]["runtime"], "orca")
        assert backend.start_spec is not None
        assert backend.start_spec.graph is not None
        self.assertEqual(backend.start_spec.graph.coordination.mode, "program")
        self.assertEqual(
            backend.start_spec.graph.coordination.dispatch_mode, "parallel"
        )
        self.assertIsNone(backend.start_spec.graph.main_node)

    def test_normal_start_passes_typed_named_start_spec_through_workflow_engine(
        self,
    ) -> None:
        backend = _RecordingBackend()
        engine = WorkflowEngine(backend)
        with (
            mock.patch.object(cli, "_start_prerequisites") as prerequisites,
            mock.patch.object(cli, "_runtime_engine", return_value=(engine, backend)),
        ):
            result, stdout, stderr = self.run_cli(
                "start", "--team", "build", "--no-attach"
            )

        self.assertEqual(result, 0, stderr)
        assert backend.start_spec is not None
        self.assertEqual(
            json.loads(stdout),
            {"status": "started", "team_id": backend.start_spec.team_id},
        )
        prerequisites.assert_called_once()
        spec = backend.start_spec
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.team_id.split("-", 1)[0], "build")
        self.assertEqual(
            {(ref.node_id, ref.kind.value) for ref in spec.role_specs},
            {
                ("lead", "main"),
                ("implementation-a", "worker"),
                ("implementation-b", "worker"),
                ("review-a", "reviewer"),
                ("review-b", "reviewer"),
            },
        )
        self.assertEqual(
            spec.role_specs[NodeRef("implementation-a", Role.WORKER)].model,
            "claude-worker-a",
        )
        self.assertEqual(
            spec.role_specs[NodeRef("implementation-b", Role.WORKER)].model,
            "claude-worker-b",
        )
        self.assertEqual(
            tuple(task.task_id for task in spec.task_specs),
            ("implement-a", "implement-b"),
        )

    def test_validate_dispatches_v3_v4_and_v5_without_start_fallback(self) -> None:
        v3 = self.root / "legacy-v3.toml"
        v3.write_text(
            """\
version = 3
runtime = "tmux"
team_prefix = "legacy"
max_review_rounds = 1

[main]
provider = "claude"
transport = "direct"
model = "claude-main"
effort = "high"
prompt = "prompts/main.md"
permission = "orchestrator"

[roles]
""",
            encoding="utf-8",
        )
        v4 = self.root / "legacy-v4.toml"
        v4.write_text(
            """\
version = 4
runtime = "orca"

[teams.legacy]
name = "Legacy Team"

[[teams.legacy.nodes]]
id = "main"
label = "Main"
main = true
[teams.legacy.nodes.profile]
provider = "claude"
transport = "direct"
permission = "orchestrator"

[[teams.legacy.nodes]]
id = "reviewer"
label = "Reviewer"
main = false
[teams.legacy.nodes.profile]
provider = "claude"
transport = "acp"
permission = "read-only"

[[teams.legacy.edges]]
source = "main"
target = "reviewer"
kind = "delegates-to"
""",
            encoding="utf-8",
        )
        for version, path in ((3, v3), (4, v4), (5, self.config_path)):
            with self.subTest(version=version):
                with (
                    mock.patch.object(cli, "_start_prerequisites") as prerequisites,
                    mock.patch.object(cli, "_runtime_engine") as runtime,
                    mock.patch.object(cli, "start_team") as start_team,
                ):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        result = cli.main(
                            [
                                "validate",
                                "--config",
                                str(path),
                                "--cwd",
                                str(self.workspace),
                            ]
                        )
                self.assertEqual(result, 0, stderr.getvalue())
                self.assertEqual(stderr.getvalue(), "")
                self.assertNotEqual(stdout.getvalue(), "")
                prerequisites.assert_not_called()
                runtime.assert_not_called()
                start_team.assert_not_called()

    def test_cli_validation_rejects_team_and_node_identifier_limits_without_effects(
        self,
    ) -> None:
        invalid_team_id = "t" * 25
        invalid_team = _v5_config_text().replace(
            "[teams.program]", f"[teams.{invalid_team_id}]", 1
        )
        invalid_node_id = "n" * 65
        invalid_node = _v5_config_text().replace(
            'id = "implementation-a"', f'id = "{invalid_node_id}"', 1
        )
        for name, content, needle in (
            ("team", invalid_team, "24"),
            ("node", invalid_node, "64"),
        ):
            with self.subTest(identifier=name):
                path = self.root / f"invalid-{name}.toml"
                path.write_text(content, encoding="utf-8")
                with (
                    mock.patch.object(cli, "_start_prerequisites") as prerequisites,
                    mock.patch.object(cli, "_runtime_engine") as runtime,
                    mock.patch.object(cli, "start_team") as start_team,
                ):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        result = cli.main(
                            [
                                "validate",
                                "--config",
                                str(path),
                                "--cwd",
                                str(self.workspace),
                            ]
                        )
                self.assertEqual(result, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(needle, stderr.getvalue())
                prerequisites.assert_not_called()
                runtime.assert_not_called()
                start_team.assert_not_called()


if __name__ == "__main__":
    unittest.main()
