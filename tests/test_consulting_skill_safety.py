from __future__ import annotations

import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = Path(os.environ.get("CONSULTING_SKILL_DIR", REPO_ROOT / "dotfiles/.agent/skills/consulting-pptx-skill"))


class SnapshotSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.main = runpy.run_path(str(SKILL_ROOT / "scripts/snapshot.py"))["main"]

    def run_snapshot(self, stem: str, *args: str, failure: str | None = None) -> None:
        deck = self.root / f"{stem}.html"
        deck.write_text("<html><body>fixture</body></html>")

        def run(command, **kwargs):
            if command[0] == failure:
                raise subprocess.CalledProcessError(1, command)
            if command[0] == "fixture-chrome":
                self.browser_profile = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--user-data-dir=")), None)
                self.profile_existed = bool(self.browser_profile and Path(self.browser_profile).is_dir())
                deck.with_suffix(".pdf").write_bytes(b"fixture PDF boundary")
            elif command[0] == "pdftoppm":
                Image.new("RGB", (100, 60), "white").save(command[-1] + "-1.png")
            else:
                self.fail(f"unexpected external command: {command[0]}")
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(self.main.__globals__, chrome=lambda: "fixture-chrome"), patch.object(subprocess, "run", run), patch.object(sys, "argv", ["snapshot.py", str(deck), *args]):
            self.main()

    def test_browser_uses_a_disposable_profile(self) -> None:
        self.run_snapshot("deck")
        self.assertTrue(self.profile_existed)
        self.assertFalse(Path(self.browser_profile).exists())

    def test_only_literal_deck_numbered_stale_sheets_are_removed(self) -> None:
        for stem in ["deck*", "deck[1]", "deck?", "deck"]:
            with self.subTest(stem=stem):
                protected = ["deck1_sheet-1.png", "deck-other_sheet-2.png", f"{stem}_sheet-notes.png"]
                for name in protected:
                    (self.root / name).write_bytes(b"keep")
                stale = self.root / f"{stem}_sheet-9.png"
                stale.write_bytes(b"stale")
                self.run_snapshot(stem)
                for name in protected:
                    self.assertTrue((self.root / name).exists(), name)
                    self.assertEqual((self.root / name).read_bytes(), b"keep")
                self.assertFalse(stale.exists())
                self.assertTrue((self.root / f"{stem}_sheet-1.png").exists())

    def test_single_page_preserves_contact_sheets(self) -> None:
        sheet = self.root / "deck_sheet-9.png"
        sheet.write_bytes(b"keep")
        self.run_snapshot("deck", "--page", "1")
        self.assertTrue(sheet.exists())
        self.assertEqual(sheet.read_bytes(), b"keep")
        self.assertTrue((self.root / "deck_page-1.png").exists())

    def test_render_failure_preserves_contact_sheets(self) -> None:
        for failure in ["fixture-chrome", "pdftoppm"]:
            with self.subTest(failure=failure):
                sheet = self.root / "deck_sheet-9.png"
                sheet.write_bytes(b"keep")
                with self.assertRaises(subprocess.CalledProcessError):
                    self.run_snapshot("deck", failure=failure)
                self.assertTrue(sheet.exists())
                self.assertEqual(sheet.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
