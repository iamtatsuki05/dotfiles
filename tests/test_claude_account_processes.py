#!/usr/bin/env python3
"""Exercise process commands using disposable lock holders, never real accounts."""

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/claude_account.sh"
FAKE_CLAUDE = """#!/bin/sh
if [ "$1" = --version ]; then
    echo '2.1.261 (Claude Code)'
elif [ "$1" = auth ] && [ "$2" = login ]; then
    exit 0
elif [ "$1" = auth ] && [ "$2" = status ]; then
    echo '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","apiKeySource":null,"email":"test@example.test","orgId":"test-org","subscriptionType":"max"}'
else
    sleep 120 &
    echo "$!"
    wait
fi
"""


class ProcessCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-account-processes-")
        self.addCleanup(self.temp.cleanup)
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        claude = fake_bin / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.env = dict(
            os.environ,
            HOME=self.temp.name,
            XDG_CONFIG_HOME=self.temp.name,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )
        self.accounts = Path(self.temp.name) / "claude-account/accounts"
        self.holders = []
        self.addCleanup(self.stop_holders)

    def stop_holders(self):
        for process in self.holders:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()

    def holder(self, profile):
        if not (self.accounts / profile / "login-profiles.json").exists():
            self.run_cli("auth-login", profile)
        process = subprocess.Popen(
            [str(SCRIPT), profile],
            env=self.env,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.holders.append(process)
        child = int(process.stdout.readline())
        return process, child

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            [str(SCRIPT), *args],
            check=False,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_listing_scopes_and_force_kill_releases_lock(self):
        personal, child = self.holder("personal")
        second, _ = self.holder("personal")
        work, work_child = self.holder("work")
        listing = self.run_cli("ps", "personal").stdout
        self.assertIn(str(personal.pid), listing)
        self.assertIn(str(child), listing)
        self.assertIn(str(second.pid), listing)
        self.assertNotIn(str(work.pid), listing)
        self.assertNotIn(str(work_child), listing)
        self.run_cli("kill", "personal", success=False)
        self.assertIsNone(personal.poll())
        self.run_cli("kill", "personal", "--force")
        personal.wait(timeout=5)
        second.wait(timeout=5)
        self.assertIsNone(work.poll())
        with open(self.accounts / "personal/full-login.lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for _ in range(50):
            state = subprocess.run(
                ["ps", "-p", str(child), "-o", "stat="],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.02)
        else:
            self.fail("child survived forced termination")
        self.run_cli("kill", "--all", "--force")
        work.wait(timeout=5)
        self.assertIn("No managed processes", self.run_cli("ps").stdout)

    def test_empty_scope_and_invalid_arguments(self):
        self.assertIn("No managed processes", self.run_cli("ps").stdout)
        for args in [
            ("kill", "--force"),
            ("kill", "--all"),
            ("ps", "../outside"),
            ("ps", "--all", "extra"),
            ("kill", "personal", "--force", "extra"),
        ]:
            self.run_cli(*args, success=False)
        self.run_cli("kill", "--all", "--force")

    def test_config_path_with_spaces_and_trailing_slash(self):
        self.env["XDG_CONFIG_HOME"] = self.temp.name + "/config with spaces/"
        self.accounts = Path(self.env["XDG_CONFIG_HOME"]) / "claude-account/accounts"
        process, _ = self.holder("personal")
        self.assertIn(str(process.pid), self.run_cli("ps", "personal").stdout)
        self.run_cli("kill", "personal", "--force")
        process.wait(timeout=5)

    def test_symlink_scope_is_rejected(self):
        other = Path(self.temp.name) / "outside"
        other.mkdir()
        self.accounts.mkdir(parents=True)
        (self.accounts / "personal").symlink_to(other)
        self.run_cli("ps", "personal", success=False)
        self.run_cli("kill", "--all", "--force", success=False)

    def test_open_lock_reader_is_excluded(self):
        directory = self.accounts / "personal"
        directory.mkdir(parents=True)
        lock = directory / "full-login.lock"
        lock.touch()
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                'import sys,time; f=open(sys.argv[1]); print("ready",flush=True); time.sleep(120)',
                str(lock),
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.holders.append(process)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        self.assertIn("No managed processes", self.run_cli("ps").stdout)
        self.run_cli("kill", "--all", "--force")
        self.assertIsNone(process.poll())

    def test_native_process_is_excluded(self):
        process = subprocess.Popen(
            ["sleep", "120"], start_new_session=True, stdout=subprocess.PIPE, text=True
        )
        self.holders.append(process)
        self.run_cli("kill", "--all", "--force")
        self.assertIsNone(process.poll())

    def test_help_does_not_launch_a_profile(self):
        for args in [("--help",), ("help",), ("ps", "--help"), ("kill", "--help")]:
            result = self.run_cli(*args)
            self.assertIn("--force", result.stdout)
        self.assertFalse(self.accounts.exists())


if __name__ == "__main__":
    unittest.main()
