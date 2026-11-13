#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_agent_skills.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-agent-skills.yml"
PUBLISH_CONFIG = REPO_ROOT / "config" / "agent-skills-publish.json"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class AgentSkillExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.fixture = Path(self.tempdir.name) / "source"
        self.output = Path(self.tempdir.name) / "output"
        skills_root = self.fixture / "dotfiles" / ".agent" / "skills"

        write(
            skills_root / "published-skill" / "SKILL.md",
            """---
name: published-skill
description: A published fixture skill.
---

# Published skill
""",
        )
        write(
            skills_root / "published-skill" / "references" / "guide.md",
            "# Guide\n",
        )
        write(
            skills_root / "private-skill" / "SKILL.md",
            """---
name: private-skill
description: A fixture that must not be exported.
---

# Private skill
""",
        )
        write(
            self.fixture / "config" / "agent-skills-publish.json",
            json.dumps(
                {
                    "version": 1,
                    "repository": "iamtatsuki05/skills",
                    "plugin": {
                        "name": "example-skills",
                        "marketplace_name": "example",
                        "description": "Example skills.",
                        "author": {"name": "Example Author"},
                    },
                    "skills": ["published-skill"],
                }
            )
            + "\n",
        )
        subprocess.run(["git", "init", "-q", self.fixture], check=True)
        subprocess.run(["git", "-C", self.fixture, "add", "."], check=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_export(self, output: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(EXPORT_SCRIPT),
                "--repo-root",
                str(self.fixture),
                "--output",
                str(output or self.output),
            ],
            text=True,
            capture_output=True,
        )

    def test_exports_only_allowlisted_tracked_skills_without_mutating_source(self) -> None:
        before = tree_digest(self.fixture / "dotfiles" / ".agent" / "skills")

        result = self.run_export()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            before,
            tree_digest(self.fixture / "dotfiles" / ".agent" / "skills"),
        )
        self.assertTrue((self.output / "skills" / "published-skill" / "SKILL.md").is_file())
        self.assertTrue(
            (self.output / "skills" / "published-skill" / "references" / "guide.md").is_file()
        )
        self.assertFalse((self.output / "skills" / "private-skill").exists())
        self.assertFalse((self.output / "LICENSE").exists())
        marker = json.loads(
            (self.output / ".agent-skills-mirror.json").read_text()
        )
        self.assertEqual(marker["source_repository"], "iamtatsuki05/dotfiles")

    def test_generates_claude_manifests_and_bilingual_readmes(self) -> None:
        result = self.run_export()

        self.assertEqual(result.returncode, 0, result.stderr)
        plugin = json.loads(
            (self.output / ".claude-plugin" / "plugin.json").read_text()
        )
        marketplace = json.loads(
            (self.output / ".claude-plugin" / "marketplace.json").read_text()
        )
        self.assertEqual(plugin["name"], "example-skills")
        self.assertEqual(plugin["skills"], ["./skills/published-skill"])
        self.assertNotIn("license", plugin)
        self.assertEqual(marketplace["name"], "example")
        self.assertEqual(marketplace["plugins"][0]["source"], "./")
        self.assertIn(
            "npx skills@latest add iamtatsuki05/skills",
            (self.output / "README.md").read_text(),
        )
        self.assertIn(
            "claude plugin marketplace add iamtatsuki05/skills",
            (self.output / "README_JA.md").read_text(),
        )

    def test_refuses_nonempty_output_directory(self) -> None:
        self.output.mkdir()
        write(self.output / "keep.txt", "do not delete\n")

        result = self.run_export()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.output / "keep.txt").read_text(), "do not delete\n")

    def test_refuses_output_inside_source_repository(self) -> None:
        output = self.fixture / "generated-skills"

        result = self.run_export(output)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the source repository", result.stderr)
        self.assertFalse(output.exists())

    def test_refuses_tracked_symlink_inside_published_skill(self) -> None:
        link = (
            self.fixture
            / "dotfiles"
            / ".agent"
            / "skills"
            / "published-skill"
            / "linked"
        )
        link.symlink_to("SKILL.md")
        subprocess.run(["git", "-C", self.fixture, "add", str(link)], check=True)

        result = self.run_export()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr.lower())

    def test_refuses_unexpected_destination_repository(self) -> None:
        config_path = self.fixture / "config" / "agent-skills-publish.json"
        config = json.loads(config_path.read_text())
        config["repository"] = "example/other"
        write(config_path, json.dumps(config) + "\n")

        result = self.run_export()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repository must be iamtatsuki05/skills", result.stderr)

    def test_reports_malformed_configuration_without_traceback(self) -> None:
        config_path = self.fixture / "config" / "agent-skills-publish.json"
        write(config_path, "[]\n")

        result = self.run_export()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publish configuration must be an object", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class AgentSkillPublishWorkflowTest(unittest.TestCase):
    def test_publication_configuration_stays_outside_skill_tree(self) -> None:
        self.assertTrue(PUBLISH_CONFIG.is_file())
        self.assertFalse(
            (REPO_ROOT / "dotfiles" / ".agent" / "skills" / "publish.json").exists()
        )

    def test_only_publishes_skill_changes_merged_to_main(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("push:", workflow)
        self.assertIn("- main", workflow)
        self.assertIn('"dotfiles/.agent/skills/**"', workflow)
        self.assertIn("commits/$SOURCE_SHA/pulls", workflow)
        self.assertIn("merged_at != null", workflow)
        self.assertIn("needs.merge_gate.outputs.publish == 'true'", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)

    def test_uses_read_only_source_permissions_and_repository_secret(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("contents: read", workflow)
        self.assertIn("secrets.SKILLS_REPO_TOKEN", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertIn(
            "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            workflow,
        )
        self.assertIn(
            "uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            workflow,
        )
        self.assertIn(
            "uses: actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53",
            workflow,
        )
        self.assertEqual(workflow.count("secrets.SKILLS_REPO_TOKEN"), 1)
        self.assertNotIn(
            "    env:\n"
            "      TARGET_REPOSITORY: iamtatsuki05/skills\n"
            "      GH_TOKEN: ${{ secrets.SKILLS_REPO_TOKEN }}",
            workflow,
        )
        self.assertIn("current_source_sha", workflow)
        self.assertIn("current_skill_tree", workflow)
        self.assertIn("source_skill_tree", workflow)
        self.assertIn("default_branch", workflow)
        self.assertIn(".agent-skills-mirror.json", workflow)


if __name__ == "__main__":
    unittest.main()
