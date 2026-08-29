from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import __version__
from agent_team.cli import ConfigError, default_config_path, load_config
from agent_team.registry import (
    CANONICAL_HARNESSES,
    adapter_id_for_profile,
    profile_execution,
    status_rows,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTFILES_CONFIG = (
    PROJECT_ROOT.parents[1] / "dotfiles" / ".agent" / "apps" / "agent-team"
)


class ProjectSmokeTest(unittest.TestCase):
    def test_bundled_resources_and_dotfiles_snapshot_match(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(PROJECT_ROOT / ".test-no-user-config")},
            clear=False,
        ):
            self.assertEqual(
                default_config_path().resolve(),
                (PROJECT_ROOT / "agent_team" / "defaults" / "config.toml").resolve(),
            )
        for relative in (
            "config.toml",
            "prompts/orchestrator.md",
            "prompts/planner.md",
            "prompts/worker.md",
            "prompts/reviewer.md",
        ):
            bundled = PROJECT_ROOT / "agent_team" / "defaults" / relative
            snapshot = DOTFILES_CONFIG / relative
            self.assertTrue(bundled.is_file(), bundled)
            self.assertTrue(snapshot.is_file(), snapshot)
            self.assertEqual(
                hashlib.sha256(bundled.read_bytes()).digest(),
                hashlib.sha256(snapshot.read_bytes()).digest(),
                relative,
            )

    def test_registry_has_all_managed_harnesses_and_only_verified_profiles(
        self,
    ) -> None:
        rows = status_rows()
        self.assertEqual(tuple(row["harness_id"] for row in rows), CANONICAL_HARNESSES)
        self.assertTrue(all(row["recognized"] for row in rows))
        self.assertTrue(
            all(
                row["implemented"]
                == (row["harness_id"] in {"claude", "codex", "copilot"})
                for row in rows
            )
        )
        self.assertTrue(
            all(not bool(row["runnable"]) or bool(row["implemented"]) for row in rows)
        )
        self.assertEqual(rows[0]["acp_status"], "verified")
        self.assertEqual(rows[1]["acp_status"], "known-but-rejected")
        self.assertEqual(rows[2]["acp_status"], "verified")
        self.assertEqual(rows[7]["acp_status"], "known-unverified")
        self.assertEqual(rows[4]["acp_adapter"], "devin acp")
        self.assertEqual(rows[6]["acp_adapter"], "hermes acp")

    def test_copilot_path_collision_status_explains_runtime_preflight(self) -> None:
        with (
            mock.patch("agent_team.registry.shutil.which", return_value="/bin/copilot"),
            mock.patch(
                "agent_team.registry._github_copilot_identity", return_value=False
            ),
        ):
            row = next(
                item for item in status_rows() if item["harness_id"] == "copilot"
            )
        self.assertFalse(row["available"])
        self.assertEqual(
            row["command_resolution_status"],
            "path-collision; runtime-preflight-checks-pinned-mise",
        )

    def test_copilot_effort_contract_fails_during_config_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / "prompts"
            prompts.mkdir()
            for role in ("orchestrator", "planner", "worker", "reviewer"):
                (prompts / f"{role}.md").write_text(role, encoding="utf-8")
            base = (PROJECT_ROOT / "agent_team" / "defaults" / "config.toml").read_text(
                encoding="utf-8"
            )
            copilot = base.replace(
                'provider = "claude"\ntransport = "acp"\nmodel = "fable"\neffort = "high"',
                'provider = "copilot"\ntransport = "direct"\nmodel = "auto"\neffort = "minimal"',
                1,
            )
            config = root / "config.toml"
            config.write_text(copilot, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "auto.*none"):
                load_config(config)
            config.write_text(
                copilot.replace(
                    'model = "auto"\neffort = "minimal"',
                    'model = "gpt-test"\neffort = "none"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "explicit.*none"):
                load_config(config)

    def test_rejected_recognized_provider_fails_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompts = root / "prompts"
            prompts.mkdir()
            for role in ("orchestrator", "planner", "worker", "reviewer"):
                (prompts / f"{role}.md").write_text(role, encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                (PROJECT_ROOT / "agent_team" / "defaults" / "config.toml")
                .read_text(encoding="utf-8")
                .replace(
                    'provider = "codex"\ntransport = "direct"\nmodel = "gpt-5.6-sol"',
                    'provider = "cursor"\ntransport = "direct"\nmodel = "fable"',
                    1,
                )
                .replace('prompt = "prompts/', 'prompt = "prompts/', 4),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "not runnable"):
                load_config(config)

    def test_background_registry_is_closed_and_has_no_worker_or_provider_fallback(
        self,
    ) -> None:
        self.assertEqual(
            profile_execution("copilot", "planner", "direct", "read-only"),
            "background",
        )
        with self.assertRaises(ValueError):
            profile_execution("copilot", "worker", "direct", "workspace-write")
        with self.assertRaises(ValueError):
            adapter_id_for_profile("opencode", "reviewer", "direct", "read-only")
        with self.assertRaises(ValueError):
            profile_execution("cursor", "planner", "direct", "read-only")

    def test_console_and_module_entrypoints_report_same_versioned_registry(
        self,
    ) -> None:
        env = os.environ.copy()
        module = subprocess.run(
            [sys.executable, "-m", "agent_team", "harnesses", "--json"],
            cwd=PROJECT_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        launcher = subprocess.run(
            [str(PROJECT_ROOT / "agent-team"), "harnesses", "--json"],
            cwd=PROJECT_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(module.stdout), json.loads(launcher.stdout))
        self.assertEqual(__version__, "0.1.0")

    def test_source_launcher_preserves_the_callers_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            caller = Path(temp_dir).resolve()
            result = subprocess.run(
                [
                    str(PROJECT_ROOT / "agent-team"),
                    "start",
                    "--dry-run",
                    "--config",
                    str(PROJECT_ROOT / "agent_team" / "defaults" / "config.toml"),
                ],
                cwd=caller,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(json.loads(result.stdout)["workspace"], str(caller))


if __name__ == "__main__":
    unittest.main()
