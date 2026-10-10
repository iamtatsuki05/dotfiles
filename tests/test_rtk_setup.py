from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ZSH_BIN = os.environ.get("DOTFILES_TEST_ZSH_BIN", "/bin/zsh")


class RtkSetupTest(unittest.TestCase):
    def test_sync_deploys_all_supported_integrations_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rtk-setup-test-") as temp:
            root = Path(temp)
            repo = root / "repo"
            home = root / "home"
            config = home / "custom-config"
            agent = repo / "dotfiles/.agent"
            (repo / "scripts").mkdir(parents=True)
            home.mkdir()
            for name in ("setup_agent_files.sh", "agent-run-compact"):
                shutil.copy2(REPO_ROOT / "scripts" / name, repo / "scripts" / name)
            for name in ("apps", "hooks"):
                shutil.copytree(REPO_ROOT / "dotfiles/.agent" / name, agent / name)
            for name in ("skills", "pets"):
                (agent / name).mkdir(parents=True)
            (agent / "AGENTS.md").write_text("# Existing shared policy\n")
            env = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(config),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            }
            preview = subprocess.run(
                [TEST_ZSH_BIN, str(repo / "scripts/setup_agent_files.sh"), "--dry-run"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(list(home.rglob("*")), [])
            antigravity_settings = home / ".gemini/config/config.json"
            for attempt in range(3):
                result = subprocess.run(
                    [TEST_ZSH_BIN, str(repo / "scripts/setup_agent_files.sh")],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(antigravity_settings.is_file())
                settings = json.loads(antigravity_settings.read_text())
                self.assertTrue(settings["plugins"]["rtk"]["enabled"])
                if attempt == 0:
                    settings["plugins"]["rtk"]["enabled"] = False
                    settings["plugins"]["other-plugin"] = {"enabled": False}
                    settings["userSettings"] = {"theme": "custom"}
                    antigravity_settings.write_text(json.dumps(settings))
                else:
                    self.assertEqual(settings["plugins"]["other-plugin"], {"enabled": False})
                    self.assertEqual(settings["userSettings"], {"theme": "custom"})
            self.assertEqual(
                (agent / "AGENTS.md").read_text(), "# Existing shared policy\n"
            )
            for name, relative in (
                ("claude", ".claude/settings.json"),
                ("codex", ".codex/hooks.json"),
            ):
                with self.subTest(agent=name):
                    hooks = json.loads((home / relative).read_text())["hooks"]
                    entries = [
                        hook
                        for group in hooks.get("PreToolUse", [])
                        if group.get("matcher") == "Bash"
                        for hook in group["hooks"]
                        if hook.get("command") == f"rtk hook {name}"
                    ]
                    self.assertEqual(len(entries), 1)
                    self.assertEqual(entries[0]["type"], "command")
                    self.assertIn("SessionStart", hooks)
                    self.assertIn("PostToolUse", hooks)
            cursor = json.loads((home / ".cursor/hooks.json").read_text())
            self.assertEqual(
                [
                    h
                    for h in cursor["hooks"]["preToolUse"]
                    if h["command"] == "rtk hook cursor"
                ],
                [{"command": "rtk hook cursor", "matcher": "Shell"}],
            )
            copilot = home / ".copilot/hooks/rtk-rewrite.json"
            self.assertTrue(copilot.is_symlink())
            self.assertEqual(
                json.loads(copilot.read_text())["hooks"]["PreToolUse"],
                [{
                    "type": "command", "command": "rtk hook copilot",
                    "cwd": ".", "timeout": 5,
                }],
            )
            antigravity = json.loads(
                (home / ".gemini/config/plugins/rtk/hooks.json").read_text()
            )
            self.assertEqual(
                antigravity["rtk-rewrite"]["PreToolUse"],
                [{
                    "matcher": "run_command",
                    "hooks": [{
                        "type": "command", "command": "rtk hook antigravity",
                        "timeout": 10,
                    }],
                }],
            )
            self.assertTrue((home / ".gemini/config/plugins/rtk/plugin.json").is_file())
            hermes = home / ".hermes/plugins/rtk-rewrite"
            self.assertTrue(hermes.is_symlink())
            self.assertTrue((hermes / "__init__.py").is_file())
            self.assertTrue((hermes / "plugin.yaml").is_file())
            self.assertIn(
                "    - rtk-rewrite\n", (home / ".hermes/config.yaml").read_text()
            )
            self.assertTrue((config / "opencode/plugins/rtk.ts").is_file())
            openclaw = json.loads((home / ".openclaw/openclaw.json").read_text())
            self.assertTrue(openclaw["plugins"]["entries"]["rtk-rewrite"]["enabled"])
            self.assertIn(
                "~/.openclaw/extensions/rtk-rewrite", openclaw["plugins"]["load"]["paths"]
            )
            extension = home / ".openclaw/extensions/rtk-rewrite"
            self.assertTrue(extension.is_symlink())
            self.assertTrue((extension / "index.ts").is_file())
            self.assertEqual(
                json.loads((extension / "openclaw.plugin.json").read_text())["id"],
                "rtk-rewrite",
            )

    def test_mise_installs_matching_official_rtk_versions(self) -> None:
        versions = []
        for relative in (
            "config/mise/config.toml", "home/.chezmoitemplates/mise-config.toml"
        ):
            tools = tomllib.loads((REPO_ROOT / relative).read_text())["tools"]
            self.assertIn("github:rtk-ai/rtk", tools)
            versions.append(tools["github:rtk-ai/rtk"])
        self.assertEqual(versions[0], versions[1])


if __name__ == "__main__":
    unittest.main()
