from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_team.config_v4 import (
    MAX_V4_CONFIG_BYTES,
    MAX_V4_EDGES_PER_TEAM,
    MAX_V4_ERROR_COUNT,
    MAX_V4_ERROR_TOTAL_CHARS,
    MAX_V4_IDENTIFIER_CHARS,
    MAX_V4_LABEL_CHARS,
    MAX_V4_NAME_CHARS,
    MAX_V4_NODES_PER_TEAM,
    MAX_V4_TEAMS,
    V4ConfigError,
    build_v4_launch_plan,
    load_v4_config,
    read_config_file,
    select_v4_team,
)
from agent_team.topology import render_topology

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "agent-team"


VALID_V4 = """\
version = 4
runtime = "orca"

[teams.build]
name = "Build Team"

[[teams.build.nodes]]
id = "main"
label = "Main"
main = true
[teams.build.nodes.profile]
provider = "claude"
transport = "direct"
permission = "orchestrator"

[[teams.build.nodes]]
id = "worker"
label = "Worker"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "workspace-write"

[[teams.build.nodes]]
id = "reviewer"
label = "Reviewer"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "read-only"

[[teams.build.edges]]
source = "main"
target = "worker"
kind = "delegates-to"

[[teams.build.edges]]
source = "worker"
target = "reviewer"
kind = "reviewed-by"

[teams.research]
name = "Research Team"
edges = []

[[teams.research.nodes]]
id = "main"
label = "Research Main"
main = true
[teams.research.nodes.profile]
provider = "codex"
transport = "direct"
permission = "orchestrator"
"""


class ConfigSelectionCliTest(unittest.TestCase):
    def run_launcher(
        self, config: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments, "--config", str(config)],
            cwd=config.parent,
            check=False,
            capture_output=True,
            text=True,
        )

    def make_config(self, root: Path, content: str = VALID_V4) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        config = root / "config.toml"
        config.write_text(content, encoding="utf-8")
        return config

    def test_teams_lists_stable_team_metadata_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root)

            result = self.run_launcher(config, "teams")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "teams": [
                    {
                        "id": "build",
                        "name": "Build Team",
                        "valid": True,
                        "errors": [],
                    },
                    {
                        "id": "research",
                        "name": "Research Team",
                        "valid": True,
                        "errors": [],
                    },
                ]
            },
        )

    def test_graph_uses_topology_golden_output_without_running_external_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root)
            sentinel = root / "external.log"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for command in ("orca", "claude", "codex", "npx"):
                fake = fake_bin / command
                fake.write_text(
                    f'#!/bin/sh\necho "$0" >> "{sentinel}"\n', encoding="utf-8"
                )
                fake.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(
                [
                    str(LAUNCHER),
                    "graph",
                    "--config",
                    str(config),
                    "--team",
                    "build",
                    "--format",
                    "json",
                ],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "{\n"
            '  "edges": [\n'
            "    {\n"
            '      "kind": "delegates-to",\n'
            '      "source": "main",\n'
            '      "target": "worker"\n'
            "    },\n"
            "    {\n"
            '      "kind": "reviewed-by",\n'
            '      "source": "worker",\n'
            '      "target": "reviewer"\n'
            "    }\n"
            "  ],\n"
            '  "nodes": [\n'
            "    {\n"
            '      "id": "main",\n'
            '      "label": "Main",\n'
            '      "main": true,\n'
            '      "profile": {\n'
            '        "permission": "orchestrator",\n'
            '        "provider": "claude",\n'
            '        "transport": "direct"\n'
            "      }\n"
            "    },\n"
            "    {\n"
            '      "id": "reviewer",\n'
            '      "label": "Reviewer",\n'
            '      "main": false,\n'
            '      "profile": {\n'
            '        "permission": "read-only",\n'
            '        "provider": "codex",\n'
            '        "transport": "direct"\n'
            "      }\n"
            "    },\n"
            "    {\n"
            '      "id": "worker",\n'
            '      "label": "Worker",\n'
            '      "main": false,\n'
            '      "profile": {\n'
            '        "permission": "workspace-write",\n'
            '        "provider": "codex",\n'
            '        "transport": "direct"\n'
            "      }\n"
            "    }\n"
            "  ],\n"
            '  "team_id": "build"\n'
            "}\n",
        )
        self.assertFalse(sentinel.exists())

    def test_start_dry_run_contains_only_typed_v4_launch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root)
            result = self.run_launcher(config, "start", "--team", "build", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"config_path", "team_id", "workspace"})
        self.assertEqual(payload["team_id"], "build")
        self.assertEqual(payload["config_path"], str(config.resolve()))
        self.assertEqual(payload["workspace"], str(root.resolve()))

    def test_v4_team_dependent_commands_require_one_exact_team_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root)

            missing = self.run_launcher(config, "start", "--dry-run")
            unknown = self.run_launcher(config, "start", "--team", "BUILD", "--dry-run")
            repeated = self.run_launcher(
                config,
                "start",
                "--team",
                "build",
                "--team",
                "research",
                "--dry-run",
            )

        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stderr, "ERROR: version must be integer 3\n")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unknown team", unknown.stderr)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("exactly one", repeated.stderr)

    def test_v4_start_without_dry_run_stops_before_any_external_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root)
            sentinel = root / "external.log"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_orca = fake_bin / "orca"
            fake_orca.write_text(
                f'#!/bin/sh\necho invoked >> "{sentinel}"\n', encoding="utf-8"
            )
            fake_orca.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(
                [
                    str(LAUNCHER),
                    "start",
                    "--config",
                    str(config),
                    "--team",
                    "build",
                ],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--dry-run only", result.stderr)
        self.assertFalse(sentinel.exists())

    def test_v4_dry_run_rejects_no_attach_instead_of_ignoring_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root)
            result = self.run_launcher(
                config,
                "start",
                "--team",
                "build",
                "--dry-run",
                "--no-attach",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--no-attach", result.stderr)

    def test_teams_has_one_fixed_json_output_and_no_unused_json_option(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root)
            result = self.run_launcher(config, "teams", "--json")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --json", result.stderr)

    def test_v3_config_with_v4_fields_is_rejected_at_version_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.toml"
            prompts = root / "prompts"
            prompts.mkdir()
            for role in ("orchestrator", "planner", "worker", "reviewer"):
                (prompts / f"{role}.md").write_text(role, encoding="utf-8")
            config.write_text(
                (PROJECT_ROOT / "agent_team" / "defaults" / "config.toml")
                .read_text(encoding="utf-8")
                .replace(
                    "max_review_rounds = 2\n", "max_review_rounds = 2\nteams = {}\n"
                ),
                encoding="utf-8",
            )
            result = self.run_launcher(config, "start", "--dry-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version 4", result.stderr)

    def test_oversized_v3_config_keeps_the_original_loader_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / "prompts"
            prompts.mkdir()
            for role in ("orchestrator", "planner", "worker", "reviewer"):
                (prompts / f"{role}.md").write_text(role, encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                (PROJECT_ROOT / "agent_team" / "defaults" / "config.toml").read_text(
                    encoding="utf-8"
                )
                + "\n# "
                + ("x" * MAX_V4_CONFIG_BYTES),
                encoding="utf-8",
            )
            result = self.run_launcher(config, "start", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["runtime"], "orca")

    def test_v3_missing_and_directory_paths_keep_the_original_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing.toml"
            directory = root / "config-dir"
            directory.mkdir()
            missing_result = self.run_launcher(missing, "start", "--dry-run")
            directory_result = self.run_launcher(directory, "start", "--dry-run")

        self.assertEqual(missing_result.returncode, 2)
        self.assertEqual(
            missing_result.stderr,
            f"ERROR: config does not exist: {missing.resolve()}\n",
        )
        self.assertEqual(directory_result.returncode, 2)
        self.assertEqual(
            directory_result.stderr,
            f"ERROR: config does not exist: {directory.resolve()}\n",
        )

    def test_v4_invalid_utf8_after_version_returns_bounded_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "invalid-utf8.toml"
            config.write_bytes(b"version = 4\n\xff\n")
            result = self.run_launcher(config, "start", "--team", "build", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("not valid UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stderr.count("\n"), 1)

    def test_oversized_v4_is_rejected_before_toml_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "oversized-v4.toml"
            config.write_bytes(b"version = 4\n" + b"#" * MAX_V4_CONFIG_BYTES)
            result = self.run_launcher(config, "start", "--team", "build", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("maximum", result.stderr)
        self.assertNotIn("version must be integer 3", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_v4_without_team_keeps_the_v3_version_error_without_bounded_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "oversized-v4.toml"
            config.write_bytes(b"version = 4\n" + b"#" * MAX_V4_CONFIG_BYTES)
            result = self.run_launcher(config, "start", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "ERROR: version must be integer 3\n")

    def test_v3_with_team_is_rejected_by_the_explicit_v4_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / "prompts"
            prompts.mkdir()
            for role in ("orchestrator", "planner", "worker", "reviewer"):
                (prompts / f"{role}.md").write_text(role, encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                (PROJECT_ROOT / "agent_team" / "defaults" / "config.toml").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            result = self.run_launcher(config, "start", "--team", "build", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "ERROR: version must be integer 4\n")

    def test_v3_controlled_path_error_keeps_plain_text_but_escapes_only_controls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unsafe = root / "missing\x1b[31m\nconfig.toml"
            result = self.run_launcher(unsafe, "start", "--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("\x1b", result.stderr)
        self.assertEqual(result.stderr.count("\n"), 1)
        self.assertIn("\\u001b", result.stderr)
        self.assertIn("\\nconfig.toml", result.stderr)


class V4ConfigParserTest(unittest.TestCase):
    def run_launcher(
        self, config: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments, "--config", str(config)],
            cwd=config.parent,
            check=False,
            capture_output=True,
            text=True,
        )

    def make_config(self, root: Path, content: str = VALID_V4) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        config = root / "config.toml"
        config.write_text(content, encoding="utf-8")
        return config

    def test_parser_returns_stable_team_ids_and_validated_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.make_config(Path(temp_dir))
            parsed = load_v4_config(config)

        self.assertEqual(
            tuple(team.team_id for team in parsed.teams), ("build", "research")
        )
        self.assertEqual(
            tuple(team.name for team in parsed.teams), ("Build Team", "Research Team")
        )
        self.assertTrue(all(team.validation.valid for team in parsed.teams))
        self.assertEqual(parsed.team("build").definition.team_id, "build")

    def test_team_and_node_order_changes_do_not_change_rendered_bytes(self) -> None:
        reordered = """\
version = 4
runtime = "orca"

[teams.research]
name = "Research Team"
edges = []

[[teams.research.nodes]]
id = "main"
label = "Research Main"
main = true
[teams.research.nodes.profile]
provider = "codex"
transport = "direct"
permission = "orchestrator"

[teams.build]
name = "Build Team"

[[teams.build.nodes]]
id = "reviewer"
label = "Reviewer"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "read-only"

[[teams.build.nodes]]
id = "main"
label = "Main"
main = true
[teams.build.nodes.profile]
provider = "claude"
transport = "direct"
permission = "orchestrator"

[[teams.build.nodes]]
id = "worker"
label = "Worker"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "workspace-write"

[[teams.build.edges]]
source = "worker"
target = "reviewer"
kind = "reviewed-by"

[[teams.build.edges]]
source = "main"
target = "worker"
kind = "delegates-to"
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = load_v4_config(self.make_config(root / "first"))
            second = load_v4_config(self.make_config(root / "second", reordered))

        for output_format in ("json", "ascii", "mermaid"):
            with self.subTest(output_format=output_format):
                self.assertEqual(
                    render_topology(
                        first.team("build").definition, output_format, first.resolver
                    ),
                    render_topology(
                        second.team("build").definition,
                        output_format,
                        second.resolver,
                    ),
                )

    def test_v4_rejects_unknown_fields_instead_of_ignoring_them(self) -> None:
        invalid = VALID_V4.replace(
            'runtime = "orca"', 'runtime = "orca"\nlegacy = true'
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(V4ConfigError, "unsupported fields"),
        ):
            load_v4_config(self.make_config(Path(temp_dir), invalid))

    def test_unknown_field_diagnostic_escapes_control_and_is_bounded(self) -> None:
        field = "\\u001b[31m" + ("x" * 500)
        invalid = VALID_V4.replace(
            'runtime = "orca"', f'runtime = "orca"\n"{field}" = true'
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaises(V4ConfigError) as caught,
        ):
            load_v4_config(self.make_config(Path(temp_dir), invalid))

        message = str(caught.exception)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)
        self.assertIn("\\u001b", message)
        self.assertLessEqual(len(message), MAX_V4_ERROR_TOTAL_CHARS)

    def test_v4_rejects_a_config_file_over_the_byte_limit_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config.toml"
            config.write_bytes(b"#" * (MAX_V4_CONFIG_BYTES + 1))
            with self.assertRaisesRegex(V4ConfigError, "maximum.*bytes"):
                load_v4_config(config)

    def test_missing_and_directory_config_errors_name_the_requested_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing.toml"
            directory = root / "config-dir"
            directory.mkdir()
            with self.assertRaisesRegex(V4ConfigError, str(missing)):
                read_config_file(missing)
            with self.assertRaisesRegex(V4ConfigError, str(directory)):
                read_config_file(directory)

            missing_result = self.run_launcher(missing, "teams")
            directory_result = self.run_launcher(directory, "teams")

        self.assertEqual(missing_result.returncode, 2)
        self.assertIn(
            f'config does not exist: "{missing.resolve()}"', missing_result.stderr
        )
        self.assertEqual(directory_result.returncode, 2)
        self.assertIn(
            f'config is not a regular file: "{directory.resolve()}"',
            directory_result.stderr,
        )

    def test_v4_rejects_team_node_and_edge_count_over_limits(self) -> None:
        teams = 'version = 4\nruntime = "orca"\n[teams]\n' + "\n".join(
            f'[{"teams"}.{team_id}]\nname = "Team"\nedges = []\n'
            for team_id in (f"team-{index}" for index in range(MAX_V4_TEAMS + 1))
        )
        nodes = "\n".join(
            f"""[[teams.build.nodes]]
id = "node-{index}"
label = "Node"
main = {"true" if index == 0 else "false"}
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = {"orchestrator" if index == 0 else "read-only"!r}
"""
            for index in range(MAX_V4_NODES_PER_TEAM + 1)
        )
        edges = "\n".join(
            f"""[[teams.build.edges]]
source = "main"
target = "node-{index}"
kind = "delegates-to"
"""
            for index in range(MAX_V4_EDGES_PER_TEAM + 1)
        )
        for content, expected in (
            (teams, "teams.*.*maximum"),
            (
                'version = 4\nruntime = "orca"\n[teams.build]\nname = "Build"\nedges = []\n'
                + nodes,
                "nodes.*maximum",
            ),
            (
                'version = 4\nruntime = "orca"\n[teams.build]\nname = "Build"\n'
                '[[teams.build.nodes]]\nid = "main"\nlabel = "Main"\nmain = true\n'
                '[teams.build.nodes.profile]\nprovider = "codex"\ntransport = "direct"\npermission = "orchestrator"\n'
                + edges,
                "edges.*maximum",
            ),
        ):
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as temp_dir,
                self.assertRaisesRegex(V4ConfigError, "maximum"),
            ):
                load_v4_config(self.make_config(Path(temp_dir), content))

    def test_v4_rejects_identifier_name_and_label_over_limits(self) -> None:
        identifier = "x" * (MAX_V4_IDENTIFIER_CHARS + 1)
        name = "n" * (MAX_V4_NAME_CHARS + 1)
        label = "l" * (MAX_V4_LABEL_CHARS + 1)
        cases = (
            (
                VALID_V4.replace("[teams.build]", f'[teams."{identifier}"]', 1),
                "team.id",
            ),
            (VALID_V4.replace('name = "Build Team"', f'name = "{name}"', 1), "name"),
            (VALID_V4.replace('label = "Main"', f'label = "{label}"', 1), "label"),
            (VALID_V4.replace('id = "main"', f'id = "{identifier}"', 1), "id"),
        )
        for content, context in cases:
            with (
                self.subTest(context=context),
                tempfile.TemporaryDirectory() as temp_dir,
                self.assertRaisesRegex(V4ConfigError, "maximum"),
            ):
                load_v4_config(self.make_config(Path(temp_dir), content))

    def test_v4_caps_aggregated_validation_diagnostics(self) -> None:
        nodes = "\n".join(
            f"""[[teams.build.nodes]]
id = "node-{index}"
label = "Node {index}"
main = {"true" if index == 0 else "false"}
[teams.build.nodes.profile]
provider = {"codex" if index == 0 else "unknown"!r}
transport = "direct"
permission = {"orchestrator" if index == 0 else "read-only"!r}
"""
            for index in range(MAX_V4_ERROR_COUNT + 1)
        )
        content = (
            'version = 4\nruntime = "orca"\n[teams.build]\nname = "Build"\nedges = []\n'
            + nodes
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(V4ConfigError, "diagnostic"),
        ):
            load_v4_config(self.make_config(Path(temp_dir), content))

    def test_v4_requires_exact_integer_version_four(self) -> None:
        versions = (
            "version = 3",
            "version = 5",
            "version = true",
            "",
        )
        for version in versions:
            with self.subTest(version=version):
                content = VALID_V4.replace("version = 4", version, 1)
                with (
                    tempfile.TemporaryDirectory() as temp_dir,
                    self.assertRaisesRegex(V4ConfigError, "version must be integer 4"),
                ):
                    load_v4_config(self.make_config(Path(temp_dir), content))

    def test_v4_rejects_unsafe_node_text_before_topology_use(self) -> None:
        invalid = VALID_V4.replace('label = "Main"', 'label = "Bad\\nLabel"', 1)
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(V4ConfigError, "unsafe"),
        ):
            load_v4_config(self.make_config(Path(temp_dir), invalid))

    def test_v4_rejects_unknown_edge_kind_before_building_definition(self) -> None:
        invalid = VALID_V4.replace('kind = "delegates-to"', 'kind = "runs"', 1)
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(V4ConfigError, "kind must be one of"),
        ):
            load_v4_config(self.make_config(Path(temp_dir), invalid))

    def test_v4_requires_an_explicit_empty_edge_array(self) -> None:
        invalid = VALID_V4.replace("edges = []\n", "", 1)
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(V4ConfigError, "missing edges"),
        ):
            load_v4_config(self.make_config(Path(temp_dir), invalid))

    def test_v4_rejects_unknown_nested_fields_and_case_ambiguous_team_ids(self) -> None:
        nested = VALID_V4.replace(
            '[teams.build.nodes.profile]\nprovider = "claude"',
            '[teams.build.nodes.profile]\nprovider = "claude"\nmodel = "fable"',
            1,
        )
        ambiguous = VALID_V4.replace(
            "[teams.research]",
            '[teams.Build]\nname = "Other Build"\nedges = []\n\n[teams.research]',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(V4ConfigError, "unsupported fields"):
                load_v4_config(self.make_config(root / "nested", nested))
            with self.assertRaisesRegex(V4ConfigError, "ambiguous"):
                load_v4_config(self.make_config(root / "ambiguous", ambiguous))

    def test_teams_reports_invalid_topology_without_starting_a_resource(self) -> None:
        invalid = VALID_V4.replace(
            'provider = "codex"\ntransport = "direct"\npermission = "workspace-write"',
            'provider = "unknown"\ntransport = "direct"\npermission = "workspace-write"',
            1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.make_config(root, invalid)
            result = self.run_launcher(config, "teams")

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        build = payload["teams"][0]
        self.assertFalse(build["valid"])
        self.assertEqual(build["errors"][0]["code"], "unknown-profile")

    def test_v3_shape_is_not_accepted_by_v4_parser(self) -> None:
        invalid = 'version = 3\nruntime = "orca"\nteams = {}\n'
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(V4ConfigError, "version must be integer 4"),
        ):
            load_v4_config(self.make_config(Path(temp_dir), invalid))

    def test_invalid_topology_is_rejected_before_launch_plan(self) -> None:
        invalid = VALID_V4.replace(
            'provider = "codex"\ntransport = "direct"\npermission = "workspace-write"',
            'provider = "not-registered"\ntransport = "direct"\npermission = "workspace-write"',
            1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed = load_v4_config(self.make_config(root, invalid))
            with self.assertRaisesRegex(V4ConfigError, "invalid"):
                build_v4_launch_plan(parsed, root, "build")

    def test_duplicate_cycle_and_unreachable_nodes_are_validation_errors(self) -> None:
        duplicate = VALID_V4.replace(
            "[[teams.build.edges]]",
            """[[teams.build.nodes]]
id = "worker"
label = "Worker Copy"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "workspace-write"

[[teams.build.edges]]""",
            1,
        )
        cycle = (
            VALID_V4
            + """
[[teams.build.edges]]
source = "worker"
target = "main"
kind = "delegates-to"
"""
        )
        unreachable = VALID_V4.replace(
            "[[teams.build.edges]]",
            """[[teams.build.nodes]]
id = "orphan"
label = "Orphan"
main = false
[teams.build.nodes.profile]
provider = "codex"
transport = "direct"
permission = "read-only"

[[teams.build.edges]]""",
            1,
        )
        for content, expected in (
            (duplicate, "duplicate-node-id"),
            (cycle, "cycle"),
            (unreachable, "unreachable-node"),
        ):
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                parsed = load_v4_config(self.make_config(root, content))
                self.assertFalse(parsed.team("build").validation.valid)
                with self.assertRaisesRegex(V4ConfigError, expected):
                    build_v4_launch_plan(parsed, root, "build")

    def test_launch_plan_has_no_state_or_backend_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed = load_v4_config(self.make_config(root))
            plan = build_v4_launch_plan(parsed, root, "build")

        self.assertEqual(plan.team_id, "build")
        self.assertEqual(plan.workspace, root.resolve())
        self.assertEqual(plan.config_path, (root / "config.toml").resolve())
        self.assertEqual(set(plan.as_dict()), {"config_path", "team_id", "workspace"})

    def test_selection_does_not_case_fold_or_select_a_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_v4_config(self.make_config(Path(temp_dir)))

        with self.assertRaisesRegex(V4ConfigError, "required"):
            select_v4_team(config, None)
        with self.assertRaisesRegex(V4ConfigError, "exactly one"):
            select_v4_team(config, ("build", "research"))
        with self.assertRaisesRegex(V4ConfigError, "unknown team"):
            select_v4_team(config, ("BUILD",))

    def test_selection_diagnostic_escapes_control_and_newline_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_v4_config(self.make_config(Path(temp_dir)))

        for selected in ("\x1b[31m", "a\nb"):
            with self.subTest(selected=repr(selected)):
                with self.assertRaises(V4ConfigError) as caught:
                    select_v4_team(config, (selected,))
                message = str(caught.exception)
                self.assertNotIn("\x1b", message)
                self.assertNotIn("\n", message)
                if "\x1b" in selected:
                    self.assertIn("\\u001b", message)
                else:
                    self.assertIn('"a\\nb"', message)

    def test_unknown_team_cli_diagnostic_has_no_raw_control_or_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.make_config(root)
            result = self.run_launcher(
                config_path,
                "graph",
                "--team",
                "\x1b[31m\na",
                "--format",
                "json",
            )

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("\x1b", result.stderr)
        self.assertEqual(result.stderr.count("\n"), 1)
        self.assertIn("\\u001b", result.stderr)

    def test_falsey_explicit_resolver_is_not_replaced_by_registry(self) -> None:
        class FalseyResolver:
            def __bool__(self) -> bool:
                return False

            def resolve(self, _profile: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            parsed = load_v4_config(
                self.make_config(Path(temp_dir)),
                resolver=FalseyResolver(),  # type: ignore[arg-type]
            )

        self.assertFalse(parsed.team("build").validation.valid)


if __name__ == "__main__":
    unittest.main()
