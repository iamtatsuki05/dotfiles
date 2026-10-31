from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_team.zellij import CloseEvidence, ZellijDriver, ZellijReceipt


class LiveZellijDriverTest(unittest.TestCase):
    @staticmethod
    def _cleanup_diagnostic(driver: ZellijDriver, receipt: ZellijReceipt) -> str:
        current = driver._current_inventory(include_socket=False)
        expected = dict(receipt.owned_paths)
        expected.pop(f"s/contract_version_1/{receipt.session_name}", None)
        if current is None:
            return "current_inventory=None"
        missing = sorted(set(expected) - set(current))
        extra = sorted(set(current) - set(expected))
        drift = sorted(
            relative
            for relative in set(expected) & set(current)
            if expected[relative] != current[relative]
        )
        return f"missing={missing!r} extra={extra!r} drift={drift!r}"

    def test_private_session_holds_natural_exit_and_is_reclaimed(self) -> None:
        if os.environ.get("AGENT_TEAM_RUN_LIVE_ZELLIJ") != "1":
            self.skipTest("set AGENT_TEAM_RUN_LIVE_ZELLIJ=1 to run the live test")
        executable = shutil.which("zellij")
        if executable is None:
            self.fail(
                "explicit live Zellij test requires an installed zellij executable"
            )

        root = Path(tempfile.mkdtemp(prefix="at-zellij-live-", dir="/tmp"))
        root.chmod(0o700)
        cwd_context = tempfile.TemporaryDirectory(prefix="agent-team-live-zellij-cwd-")
        cwd = Path(cwd_context.name)
        nonce = "live-zellij-run-123"
        session = "agent-team-live-zellij-123"
        driver = ZellijDriver(executable, root, nonce, session)
        receipt: ZellijReceipt | None = None
        try:
            receipt = driver.create(
                (
                    sys.executable,
                    "-c",
                    "import time; time.sleep(8.0)",
                ),
                cwd=cwd,
                env={"AGENT_TEAM_LIVE": "1"},
                title="live-main",
            )
            self.assertEqual(receipt.run_nonce, nonce)
            self.assertEqual(receipt.session_name, session)
            self.assertEqual(
                receipt.server_argv[-2:], ("--server", str(receipt.socket_path))
            )
            self.assertNotEqual(receipt.server_pid, receipt.server_pgid)
            restored = ZellijReceipt.from_dict(receipt.as_dict())
            self.assertEqual(restored, receipt)
            reopened = ZellijDriver.from_receipt(restored)
            self.assertTrue(reopened.inspect(restored).identity_verified)
            resume_code = (
                "import json,sys; "
                "from agent_team.zellij import ZellijDriver,ZellijReceipt; "
                "r=ZellijReceipt.from_dict(json.load(sys.stdin)); "
                "d=ZellijDriver.from_receipt(r); "
                "i=d.inspect(r); "
                "print(json.dumps({'identity_verified':i.identity_verified,'presence':i.presence}))"
            )
            resumed = subprocess.run(
                [sys.executable, "-c", resume_code],
                cwd=Path(__file__).resolve().parents[1],
                input=json.dumps(receipt.as_dict()),
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout)["identity_verified"], True)

            deadline = time.monotonic() + 15.0
            observed = driver.inspect(receipt)
            while time.monotonic() < deadline:
                if observed.identity_verified and observed.running is False:
                    break
                time.sleep(0.05)
                observed = driver.inspect(receipt)
            self.assertTrue(observed.identity_verified, observed.reason)
            self.assertEqual(observed.presence, "present")
            self.assertFalse(observed.running)
            self.assertEqual(observed.exit_status, 0)
            self.assertTrue(observed.pane_present)

            attach = driver.attach_argv(receipt)
            self.assertIn("attach", attach)
            self.assertIn(session, attach)

            unexpected = receipt.socket_path.parent / "unexpected-resource"
            unexpected.write_text("retain", encoding="utf-8")
            unexpected_cache = receipt.cache_dir / "unexpected-resource"
            unexpected_cache.write_text("retain", encoding="utf-8")
            blocked = driver.close(receipt)
            self.assertEqual(blocked.evidence, CloseEvidence.OWNERSHIP_UNPROVEN)
            self.assertFalse(blocked.session_terminated)
            self.assertTrue(unexpected.exists())
            self.assertTrue(unexpected_cache.exists())
            unexpected.unlink()
            unexpected_cache.unlink()

            closed = driver.close(receipt)
            self.assertEqual(closed.evidence, CloseEvidence.SERVER_TERMINATED)
            self.assertTrue(closed.session_terminated)
            self.assertTrue(closed.server_terminated)
            self.assertTrue(closed.socket_removed)
            self.assertTrue(closed.ownership_verified)
            self.assertFalse(receipt.socket_path.exists())
            self.assertFalse(
                receipt.config_path.exists(),
                f"closed={closed!r} {self._cleanup_diagnostic(driver, receipt)}",
            )
            self.assertEqual(
                tuple(root.iterdir()),
                (),
                f"closed={closed!r} {self._cleanup_diagnostic(driver, receipt)}",
            )
        finally:
            if receipt is not None and receipt.socket_path.exists():
                driver.close(receipt)
            cwd_context.cleanup()
            if root.exists():
                self.assertEqual(tuple(root.iterdir()), ())
                root.rmdir()

    def test_creator_exit_can_resume_and_stop_from_another_process(self) -> None:
        if os.environ.get("AGENT_TEAM_RUN_LIVE_ZELLIJ") != "1":
            self.skipTest("set AGENT_TEAM_RUN_LIVE_ZELLIJ=1 to run the live test")
        executable = shutil.which("zellij")
        if executable is None:
            self.fail(
                "explicit live Zellij test requires an installed zellij executable"
            )
        root = Path(tempfile.mkdtemp(prefix="at-zellij-live-resume-", dir="/tmp"))
        root.chmod(0o700)
        cwd_context = tempfile.TemporaryDirectory(prefix="agent-team-live-zellij-cwd-")
        cwd = Path(cwd_context.name)
        try:
            creator_code = (
                "import json,sys; "
                "from agent_team.zellij import ZellijDriver; "
                "d=ZellijDriver(sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]); "
                "r=d.create((sys.executable,'-c','import time; time.sleep(20.0)'), "
                "sys.argv[5], {'AGENT_TEAM_LIVE':'1'}, 'resume-main'); "
                "print(json.dumps(r.as_dict()), flush=True)"
            )
            created = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    creator_code,
                    executable,
                    str(root),
                    "live-zellij-resume-123",
                    "agent-team-live-zellij-resume-123",
                    str(cwd),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
                timeout=20.0,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            receipt = ZellijReceipt.from_dict(json.loads(created.stdout))
            driver = ZellijDriver.from_receipt(receipt)
            inspected = driver.inspect(receipt)
            self.assertTrue(inspected.identity_verified, inspected.reason)
            result = driver.close(receipt)
            self.assertTrue(result.session_terminated)
            self.assertTrue(result.server_terminated)
            self.assertTrue(result.socket_removed)
            self.assertFalse(receipt.socket_path.exists())
            self.assertEqual(
                tuple(root.iterdir()),
                (),
                f"closed={result!r} {self._cleanup_diagnostic(driver, receipt)}",
            )
        finally:
            cwd_context.cleanup()
            if root.exists():
                self.assertEqual(tuple(root.iterdir()), ())
                root.rmdir()


if __name__ == "__main__":
    unittest.main()
