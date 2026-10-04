"""Opt-in live contract for the private Herdr terminal driver.

Run only with ``AGENT_TEAM_RUN_LIVE_HERDR=1``.  The fixture starts a named
Herdr server with private XDG/HOME/config roots and runs a short local Python
helper; it never uses a provider or the user's Herdr session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from agent_team.herdr import CloseEvidence, HerdrDriver, HerdrReceipt

RUN_LIVE = os.environ.get("AGENT_TEAM_RUN_LIVE_HERDR") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LiveHerdrDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        if not RUN_LIVE:
            self.skipTest("set AGENT_TEAM_RUN_LIVE_HERDR=1 for the explicit live test")
        executable = shutil.which("herdr")
        if executable is None:
            self.skipTest("live Herdr contract requires the installed herdr executable")
        self.executable = Path(executable).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="at-herdr-live-", dir="/tmp"))
        self.root.chmod(0o700)
        self.nonce = "live" + os.urandom(12).hex()
        self.session = f"agent-team-live-{self.nonce[:16]}"
        self.cwd = Path("/tmp").resolve()
        self.driver = HerdrDriver(
            self.executable,
            self.root,
            run_nonce=self.nonce,
            session_name=self.session,
        )
        self.receipt: HerdrReceipt | None = None

    def tearDown(self) -> None:
        if self.receipt is not None and self.receipt.socket_path.exists():
            self.driver.close(self.receipt)
        if self.root.exists() and not any(self.root.iterdir()):
            self.root.rmdir()

    def test_private_create_natural_exit_and_close(self) -> None:
        helper = (
            sys.executable,
            "-c",
            "import time; time.sleep(2.0)",
        )
        receipt = self.driver.create(helper, self.cwd, {}, "agent-team-live")
        self.receipt = receipt
        self.assertEqual(receipt.private_root, self.root)
        self.assertEqual(
            receipt.socket_path,
            self.root / "c/herdr/sessions" / self.session / "herdr.sock",
        )
        self.assertEqual(self.driver.inspect(receipt).presence, "present")

        deadline = time.monotonic() + 10.0
        observed = self.driver.inspect(receipt)
        while observed.presence != "absent" and time.monotonic() < deadline:
            time.sleep(0.1)
            observed = self.driver.inspect(receipt)
        self.assertEqual(observed.presence, "absent", observed.reason)
        self.assertTrue(observed.identity_verified)
        self.assertFalse(observed.pane_present)
        self.assertIsNone(observed.pane_pid)
        self.assertTrue(observed.session_present)

        closed = self.driver.close(receipt)
        self.assertEqual(
            closed.evidence, CloseEvidence.SERVER_TERMINATED, closed.reason
        )
        self.assertTrue(closed.session_terminated)
        self.assertTrue(closed.server_terminated)
        self.assertTrue(closed.socket_removed)
        self.assertFalse(receipt.socket_path.exists())
        self.assertFalse(receipt.client_socket_path.exists())
        self.assertFalse(receipt.session_dir.exists())
        self.assertFalse(receipt.config_path.exists())

    def test_cross_process_creator_resume_natural_exit_and_close(self) -> None:
        creator = textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

            from agent_team.herdr import HerdrDriver

            executable = Path(sys.argv[1])
            root = Path(sys.argv[2])
            nonce = sys.argv[3]
            session = sys.argv[4]
            driver = HerdrDriver(executable, root, nonce, session)
            receipt = driver.create(
                (sys.executable, "-c", "import time; time.sleep(2.0)"),
                Path("/tmp"),
                {},
                "agent-team-cross-process",
            )
            print(json.dumps(receipt.as_dict(), ensure_ascii=False), flush=True)
            """
        )
        child_env = dict(os.environ)
        child_env.pop("HERDR_ENV", None)
        child_env["PYTHONPATH"] = str(PROJECT_ROOT)
        created = subprocess.run(
            [
                sys.executable,
                "-c",
                creator,
                str(self.executable),
                str(self.root),
                self.nonce,
                self.session,
            ],
            cwd=PROJECT_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20.0,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertTrue(created.stdout.strip())
        receipt = HerdrReceipt.from_dict(json.loads(created.stdout))
        resumed = HerdrDriver.from_receipt(receipt)
        self.driver = resumed
        self.receipt = receipt
        self.assertEqual(resumed.inspect(receipt).presence, "present")

        deadline = time.monotonic() + 10.0
        observed = resumed.inspect(receipt)
        while observed.presence != "absent" and time.monotonic() < deadline:
            time.sleep(0.1)
            observed = resumed.inspect(receipt)
        self.assertEqual(observed.presence, "absent", observed.reason)
        self.assertTrue(observed.identity_verified)
        closed = resumed.close(receipt)
        self.assertEqual(
            closed.evidence, CloseEvidence.SERVER_TERMINATED, closed.reason
        )
        self.assertTrue(closed.server_terminated)
        self.assertTrue(closed.socket_removed)
        self.assertEqual(tuple(self.root.iterdir()), ())


if __name__ == "__main__":
    unittest.main()
