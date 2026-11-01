#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "dotfiles/.agent/skills"


class SkillHooksTest(unittest.TestCase):
    def settings(self, host):
        file = "claude/settings.json" if host == "claude-code" else "codex/hooks.json"
        return json.loads((ROOT / "dotfiles/.agent/apps" / file).read_text())["hooks"]

    def handler(self, host, event, script):
        matches = [h for row in self.settings(host).get(event, []) for h in row["hooks"]
                   if script in h.get("command", "")]
        self.assertEqual(len(matches), 1, (host, event, script))
        return matches[0]

    def invoke(self, handler, payload, home, cwd):
        env = os.environ.copy()
        env.update(HOME=str(home), YOMIYASU_STATE_DIR=str(cwd / ".yomiyasu"))
        env.pop("CLAUDE_PROJECT_DIR", None)
        result = subprocess.run(["/bin/sh", "-c", handler["command"]],
                                input=json.dumps(payload), capture_output=True, text=True,
                                cwd=cwd, env=env, timeout=35)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def make_home(self, root):
        home = root / "home with spaces"
        for agent in (".claude", ".codex"):
            (home / agent).mkdir(parents=True)
            (home / agent / "skills").symlink_to(SKILLS, target_is_directory=True)
        return home

    def test_yomiyasu_event_contract_matches_upstream(self):
        script = SKILLS / "yomiyasu/scripts/yomiyasu_hook.py"
        for host in ("claude-code", "codex"):
            with self.subTest(host=host):
                expected = json.loads(subprocess.check_output(
                    [sys.executable, str(script), "--print-config", host], text=True))["hooks"]
                for event, rows in expected.items():
                    actual = self.handler(host, event, "yomiyasu_hook.py")
                    self.assertEqual(actual["timeout"], rows[0]["hooks"][0]["timeout"])
                    matched = [r for r in self.settings(host)[event] if actual in r["hooks"]]
                    self.assertEqual(matched[0].get("matcher"), rows[0].get("matcher"))
                    self.assertIn("--host " + host, actual["command"])

    def test_hallmark_reports_target_markup_but_ignores_other_files(self):
        handler = self.handler("claude-code", "PostToolUse", "lint-hook.mjs")
        self.assertEqual(handler["timeout"], 10)
        self.assertFalse(any("lint-hook.mjs" in h.get("command", "")
                             for rows in self.settings("codex").values()
                             for row in rows for h in row["hooks"]))
        with tempfile.TemporaryDirectory(prefix="skill-hooks-") as temp:
            root = Path(temp)
            home = self.make_home(root)
            target = root / "index.html"
            target.write_text('<style>/* Hallmark */ body { color: #aaa; background: #fff; }</style><img src="x">')
            payload = {"tool_input": {"file_path": str(target)}, "tool_name": "Write"}
            result = self.invoke(handler, payload, home, root)
            self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PostToolUse")
            self.assertIn("Hallmark Floor check", result["hookSpecificOutput"]["additionalContext"])
            target.write_text("<p>Unrelated page</p>")
            self.assertEqual(self.invoke(handler, payload, home, root), {})

    def test_yomiyasu_stop_only_blocks_registered_unchecked_manuscripts(self):
        check = SKILLS / "yomiyasu/scripts/yomiyasu_check.py"
        for host in ("claude-code", "codex"):
            with self.subTest(host=host), tempfile.TemporaryDirectory(prefix="skill-hooks-") as temp:
                root = Path(temp)
                home = self.make_home(root)
                handler = self.handler(host, "Stop", "yomiyasu_hook.py")
                payload = {"hook_event_name": "Stop", "session_id": "fixture", "cwd": str(root),
                           "stop_hook_active": False}
                self.assertEqual(self.invoke(handler, payload, home, root), {})
                self.assertFalse((root / ".yomiyasu").exists())
                source, final = root / "source.md", root / "final.md"
                source.write_text("設定を変更することができます。\n")
                final.write_text("設定を変更できます。\n")
                common = ["--source", str(source), "--final", str(final), "--state-dir", str(root / ".yomiyasu")]
                subprocess.run([sys.executable, str(check), "start", *common, "--host", host,
                                "--session", "fixture"], check=True, capture_output=True, text=True)
                result = self.invoke(handler, payload, home, root)
                self.assertEqual(result.get("decision"), "block", result)
                subprocess.run([sys.executable, str(check), "run", *common], check=True,
                               capture_output=True, text=True)
                result = self.invoke(handler, payload, home, root)
                self.assertNotEqual(result.get("decision"), "block", result)


if __name__ == "__main__":
    unittest.main()
