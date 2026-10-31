from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_team.tmux import CloseEvidence, TmuxDriver, TmuxReceipt


class LiveTmuxDriverTest(unittest.TestCase):
    def test_private_session_reports_dead_status_and_is_reclaimed(self) -> None:
        executable = shutil.which("tmux")
        if executable is None:
            self.fail("explicit live tmux test requires an installed tmux executable")
        with tempfile.TemporaryDirectory(prefix="agent-team-live-tmux-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            socket_path = root / "s"
            driver = TmuxDriver(
                executable,
                socket_path,
                run_nonce="live-run-123",
                session_name="agent-team-live-run-123",
            )
            receipt: TmuxReceipt | None = None
            try:
                values = (
                    ";",
                    "quoted 'single' \"double\"",
                    "line one\nline two",
                    "$(touch SHOULD_NOT_EXIST)",
                    "`backtick`",
                )
                check_argv = (
                    "import sys; "
                    f"sys.exit(7 if sys.argv[1:] == {list(values)!r} else 9)"
                )
                receipt = driver.create(
                    (
                        sys.executable,
                        "-c",
                        check_argv,
                        *values,
                    ),
                    cwd=root,
                    env={},
                    title="live-title;",
                )
                deadline = time.monotonic() + 5.0
                observed = driver.inspect(receipt)
                while observed.running is True and time.monotonic() < deadline:
                    time.sleep(0.05)
                    observed = driver.inspect(receipt)
                self.assertTrue(observed.identity_verified)
                self.assertFalse(observed.running)
                self.assertEqual(observed.exit_status, 7)
                attach = driver.attach_argv(receipt)
                self.assertEqual(attach[0], str(Path(executable).resolve()))
                self.assertEqual(attach[1:3], ("-S", str(socket_path)))
                self.assertEqual(
                    attach[-3:], ("attach-session", "-t", "=agent-team-live-run-123")
                )
                title_probe = subprocess.run(
                    [
                        *attach[:5],
                        "display-message",
                        "-p",
                        "-t",
                        receipt.pane_id,
                        "#{window_name}",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(title_probe.returncode, 0, title_probe.stderr)
                self.assertEqual(title_probe.stdout.rstrip("\n"), "live-title;")
                self.assertFalse((root / "SHOULD_NOT_EXIST").exists())

                closed = driver.close(receipt)
                self.assertEqual(closed.evidence, CloseEvidence.SERVER_TERMINATED)
                self.assertTrue(closed.session_terminated)
                self.assertTrue(closed.server_terminated)
                self.assertTrue(closed.socket_removed)
                self.assertFalse(closed.descendants_stopped)
                self.assertFalse(socket_path.exists())
                self.assertFalse(receipt.config_path.exists())
            finally:
                if receipt is not None and socket_path.exists():
                    driver.close(receipt)


if __name__ == "__main__":
    unittest.main()
