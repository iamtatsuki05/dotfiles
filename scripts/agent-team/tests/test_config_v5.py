from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team.config_roles import parse_role
from agent_team.config_v5 import (
    V5ConfigError,
    V5Node,
    V5Team,
    load_v5_config_data,
    render_v5_team,
    select_v5_team,
    v5_team_rows,
)
from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import GraphSpec


def _task(task_id: str = "work-task") -> dict[str, object]:
    return {
        "task_id": task_id,
        "objective": "complete the work",
        "acceptance_criteria": ["the result is complete"],
        "allowed_paths": ["src"],
        "forbidden_paths": [],
        "dependencies": [],
        "verification": [{"name": "check", "argv": ["true"], "timeout_seconds": 1}],
        "evidence_requirements": ["test output"],
        "consultation_conditions": [],
    }


def _node(
    node_id: str,
    kind: str,
    prompt: str,
    model: str,
    effort: str = "medium",
    label: str | None = None,
    permission: str | None = None,
) -> dict[str, object]:
    permissions = {
        "main": "orchestrator",
        "planner": "read-only",
        "worker": "workspace-write",
        "reviewer": "read-only",
    }
    return {
        "id": node_id,
        "label": label or node_id.title(),
        "kind": kind,
        "role_spec": {
            "provider": "codex",
            "transport": "direct",
            "model": model,
            "effort": effort,
            "prompt": prompt,
            "permission": permission or permissions[kind],
        },
    }


def _team(prompt: str = "prompts/main.md") -> dict[str, object]:
    nodes = [
        _node("main", "main", prompt, "gpt-main"),
        _node("planner", "planner", "prompts/planner.md", "gpt-planner"),
        _node("worker-a", "worker", "prompts/worker-a.md", "gpt-worker-a"),
        _node("worker-b", "worker", "prompts/worker-b.md", "gpt-worker-b", "high"),
        _node("reviewer", "reviewer", "prompts/reviewer.md", "gpt-reviewer"),
    ]
    return {
        "name": "Build Team",
        "max_review_rounds": 2,
        "nodes": nodes,
        "edges": [
            {"source": "main", "target": "planner", "kind": "delegates-to"},
            {"source": "main", "target": "worker-a", "kind": "delegates-to"},
            {"source": "main", "target": "worker-b", "kind": "delegates-to"},
            {"source": "planner", "target": "reviewer", "kind": "reviewed-by"},
            {"source": "worker-a", "target": "reviewer", "kind": "reviewed-by"},
            {"source": "worker-b", "target": "reviewer", "kind": "reviewed-by"},
        ],
        "coordination": {
            "mode": "agent",
            "entry_nodes": ["main"],
            "dispatch_mode": "serial",
            "max_active": 1,
        },
        "tasks": [_task()],
        "routes": [
            {
                "task_id": "work-task",
                "implementation_writer": "worker-a",
                "implementation_reviewer": "reviewer",
            }
        ],
    }


def _config_data() -> dict[str, object]:
    return {"version": 5, "runtime": "tmux", "teams": {"build": _team()}}


class ConfigV5Test(unittest.TestCase):
    def _load(self, data: dict[str, object] | None = None) -> object:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "prompts").mkdir()
        for prompt in (
            "main.md",
            "planner.md",
            "worker-a.md",
            "worker-b.md",
            "reviewer.md",
        ):
            (root / "prompts" / prompt).write_text(
                "秘密の prompt 本文", encoding="utf-8"
            )
        return load_v5_config_data(root / "config.toml", data or _config_data())

    def test_load_builds_named_nodes_and_normalizes_optional_routes(self) -> None:
        config = self._load()

        self.assertEqual(config.config_path.name, "config.toml")
        self.assertEqual(config.runtime, "tmux")
        self.assertEqual(tuple(team.team_id for team in config.teams), ("build",))
        team = config.teams[0]
        self.assertIsInstance(team, V5Team)
        self.assertIsInstance(team.graph, GraphSpec)
        self.assertIsInstance(team.nodes[0], V5Node)
        self.assertEqual(team.graph.route("work-task").plan_writer, None)
        self.assertEqual(
            team.graph.route("work-task").implementation_writer, "worker-a"
        )
        self.assertEqual(team.nodes[0].ref, NodeRef("main", Role.MAIN))
        self.assertEqual(team.nodes[3].role_spec.model, "gpt-worker-b")
        self.assertEqual(team.nodes[3].role_spec.effort, "high")

    def test_same_kind_nodes_keep_distinct_models_and_prompts(self) -> None:
        config = self._load()
        workers = [
            node for node in config.teams[0].nodes if node.ref.kind is Role.WORKER
        ]

        self.assertEqual(
            tuple(node.role_spec.model for node in workers),
            ("gpt-worker-a", "gpt-worker-b"),
        )
        self.assertNotEqual(
            workers[0].role_spec.prompt_path, workers[1].role_spec.prompt_path
        )

    def test_parse_role_requires_explicit_kind(self) -> None:
        with self.assertRaises(TypeError):
            parse_role(
                _node("worker", "worker", "prompts/worker.md", "gpt-worker")[
                    "role_spec"
                ],
                context="roles.worker",
                config_dir=Path("/catalog"),
            )

    def test_unknown_fields_are_rejected_at_each_schema_boundary(self) -> None:
        cases: list[tuple[str, dict[str, object], str]] = []
        top = _config_data()
        top["legacy"] = True
        cases.append(("top", top, "config has unsupported fields"))
        team = _config_data()
        cast_team = team["teams"]
        assert isinstance(cast_team, dict)
        cast_team["build"]["legacy"] = True  # type: ignore[index]
        cases.append(("team", team, "teams.build has unsupported fields"))
        node = _config_data()
        node_team = node["teams"]
        assert isinstance(node_team, dict)
        node_team["build"]["nodes"][0]["legacy"] = True  # type: ignore[index]
        cases.append(("node", node, "unsupported fields"))
        role = _config_data()
        role_team = role["teams"]
        assert isinstance(role_team, dict)
        role_team["build"]["nodes"][0]["role_spec"]["legacy"] = True  # type: ignore[index]
        cases.append(("role", role, "role_spec has unsupported fields"))

        for _name, invalid, expected in cases:
            with (
                self.subTest(schema=_name),
                self.assertRaisesRegex(V5ConfigError, expected),
            ):
                self._load(invalid)

    def test_missing_exact_fields_are_rejected(self) -> None:
        cases = [
            ("version", lambda data: data.pop("version")),
            ("runtime", lambda data: data.pop("runtime")),
            ("teams", lambda data: data.pop("teams")),
        ]
        for field, mutate in cases:
            with self.subTest(field=field):
                invalid = _config_data()
                mutate(invalid)
                with self.assertRaisesRegex(V5ConfigError, "missing"):
                    self._load(invalid)

        cases = [
            ("node.kind", lambda team: team["nodes"][0].pop("kind")),
            (
                "role_spec.prompt",
                lambda team: team["nodes"][0]["role_spec"].pop("prompt"),
            ),
            ("route.task_id", lambda team: team["routes"][0].pop("task_id")),
        ]
        for field, mutate in cases:
            with self.subTest(nested_field=field):
                invalid = _config_data()
                invalid_team = invalid["teams"]
                assert isinstance(invalid_team, dict)
                mutate(invalid_team["build"])
                with self.assertRaisesRegex(V5ConfigError, "missing"):
                    self._load(invalid)

    def test_review_round_limit_rejects_boolean_and_zero(self) -> None:
        for value in (False, True, 0):
            with self.subTest(value=value):
                invalid = _config_data()
                invalid_team = invalid["teams"]
                assert isinstance(invalid_team, dict)
                invalid_team["build"]["max_review_rounds"] = value
                with self.assertRaisesRegex(V5ConfigError, "positive integer"):
                    self._load(invalid)

        for field in (
            "name",
            "max_review_rounds",
            "nodes",
            "edges",
            "coordination",
            "tasks",
            "routes",
        ):
            with self.subTest(team_field=field):
                invalid = _config_data()
                invalid_team = invalid["teams"]
                assert isinstance(invalid_team, dict)
                invalid_team["build"].pop(field)  # type: ignore[index]
                with self.assertRaisesRegex(V5ConfigError, "missing"):
                    self._load(invalid)

    def test_invalid_version_and_runtime_are_rejected(self) -> None:
        for version in (4, True, "5"):
            with self.subTest(version=version):
                invalid = _config_data()
                invalid["version"] = version
                with self.assertRaisesRegex(V5ConfigError, "version"):
                    self._load(invalid)
        for runtime in ("orcaish", "native", ""):
            with self.subTest(runtime=runtime):
                invalid = _config_data()
                invalid["runtime"] = runtime
                with self.assertRaisesRegex(V5ConfigError, "runtime"):
                    self._load(invalid)

    def test_team_id_keeps_runtime_prefix_limit_while_node_id_keeps_graph_limit(
        self,
    ) -> None:
        valid = _config_data()
        teams = valid["teams"]
        assert isinstance(teams, dict)
        team = teams.pop("build")
        teams["a" * 24] = team
        config = self._load(valid)
        self.assertEqual(config.teams[0].team_id, "a" * 24)

        invalid = _config_data()
        invalid_teams = invalid["teams"]
        assert isinstance(invalid_teams, dict)
        team = invalid_teams.pop("build")
        invalid_teams["a" * 25] = team
        with self.assertRaisesRegex(V5ConfigError, "at most 24"):
            self._load(invalid)

    def test_program_and_parallel_shapes_remain_explicit_for_pure_validation(
        self,
    ) -> None:
        invalid = _config_data()
        team = invalid["teams"]
        assert isinstance(team, dict)
        team["build"]["coordination"] = {  # type: ignore[index]
            "mode": "program",
            "entry_nodes": ["planner", "worker-a"],
            "dispatch_mode": "parallel",
            "max_active": 2,
        }
        team["build"]["nodes"] = (  # type: ignore[index]
            team["build"]["nodes"][1:3] + team["build"]["nodes"][4:]
        )
        team["build"]["edges"] = [  # type: ignore[index]
            {"source": "planner", "target": "reviewer", "kind": "reviewed-by"},
            {"source": "worker-a", "target": "reviewer", "kind": "reviewed-by"},
        ]

        config = self._load(invalid)
        coordination = config.teams[0].graph.coordination
        self.assertEqual(coordination.mode, "program")
        self.assertEqual(coordination.dispatch_mode, "parallel")
        self.assertEqual(coordination.max_active, 2)
        self.assertIsNone(config.teams[0].graph.main_node)

    def test_agent_coordination_without_main_is_rejected(self) -> None:
        invalid = _config_data()
        team = invalid["teams"]
        assert isinstance(team, dict)
        team["build"]["nodes"] = team["build"]["nodes"][1:]  # type: ignore[index]
        team["build"]["edges"] = []  # type: ignore[index]
        team["build"]["coordination"]["entry_nodes"] = ["planner"]  # type: ignore[index]
        with self.assertRaisesRegex(V5ConfigError, "main"):
            self._load(invalid)

    def test_graph_reference_invalid_is_rejected(self) -> None:
        invalid = _config_data()
        team = invalid["teams"]
        assert isinstance(team, dict)
        team["build"]["edges"][0]["target"] = "unknown"  # type: ignore[index]
        with self.assertRaisesRegex(V5ConfigError, "undeclared node"):
            self._load(invalid)

    def test_permission_must_match_explicit_node_kind(self) -> None:
        invalid = _config_data()
        team = invalid["teams"]
        assert isinstance(team, dict)
        team["build"]["nodes"][1]["role_spec"]["permission"] = "workspace-write"  # type: ignore[index]
        with self.assertRaisesRegex(V5ConfigError, "permission must be 'read-only'"):
            self._load(invalid)

    def test_prompt_must_be_an_internal_regular_file(self) -> None:
        invalid = _config_data()
        team = invalid["teams"]
        assert isinstance(team, dict)
        team["build"]["nodes"][0]["role_spec"]["prompt"] = "../outside.md"  # type: ignore[index]
        with self.assertRaisesRegex(V5ConfigError, "stay within"):
            self._load(invalid)

    def test_exact_team_selection_rejects_missing_multiple_casefold_and_alias(
        self,
    ) -> None:
        data = _config_data()
        teams = data["teams"]
        assert isinstance(teams, dict)
        teams["other"] = copy.deepcopy(teams["build"])
        config = self._load(data)
        self.assertEqual(select_v5_team(config, "build").team_id, "build")
        for selected in (None, (), ("build", "other"), "BUILD", "default"):
            with self.subTest(selected=selected), self.assertRaises(V5ConfigError):
                select_v5_team(config, selected)

    def test_rows_are_deterministic_and_renderer_does_not_expose_prompt_body(
        self,
    ) -> None:
        data = _config_data()
        team = data["teams"]
        assert isinstance(team, dict)
        team["build"]["nodes"][0]["label"] = "<img src=x> & Main"  # type: ignore[index]
        config = self._load(data)
        rows = v5_team_rows(config)
        self.assertEqual(rows[0]["id"], "build")
        self.assertEqual(rows[0]["name"], "Build Team")
        self.assertEqual(rows[0]["max_review_rounds"], 2)

        for output_format in ("json", "ascii", "mermaid"):
            with self.subTest(output_format=output_format):
                rendered = render_v5_team(config, "build", output_format)
                self.assertIn("worker-a", rendered)
                self.assertIn("gpt-worker-a", rendered)
                self.assertIn("reviewed-by", rendered)
                self.assertNotIn("<img", rendered)
                self.assertNotIn("本文", rendered)
                if output_format == "ascii":
                    self.assertIn('"main" --["delegates-to"]--> "planner"', rendered)
        decoded = json.loads(render_v5_team(config, "build", "json"))
        self.assertEqual(decoded["nodes"][0]["kind"], "main")
        self.assertEqual(decoded["coordination"]["dispatch_mode"], "serial")

    def test_parser_does_not_probe_provider_or_runtime(self) -> None:
        with (
            mock.patch("agent_team.registry.shutil.which", side_effect=AssertionError),
            mock.patch("subprocess.run", side_effect=AssertionError),
        ):
            self._load()


if __name__ == "__main__":
    unittest.main()
