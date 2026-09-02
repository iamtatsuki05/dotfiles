from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "agent-run-compact"


class GuardedUnterminatedLog:
    def __init__(self, byte_count: int) -> None:
        self.remaining = byte_count

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self) -> GuardedUnterminatedLog:
        raise AssertionError("log collection must not request unbounded lines")

    def read(self, size: int = -1) -> bytes:
        if size <= 0 or size > 65_536:
            raise AssertionError(
                f"log collection requested an unsafe read size: {size}"
            )
        chunk_size = min(size, self.remaining)
        self.remaining -= chunk_size
        return b"X" * chunk_size


class GuardedLogPath:
    def __init__(self, byte_count: int) -> None:
        self.log = GuardedUnterminatedLog(byte_count)

    def open(self, mode: str) -> GuardedUnterminatedLog:
        if mode != "rb":
            raise AssertionError(f"unexpected mode: {mode}")
        return self.log


class AgentRunCompactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir_context = tempfile.TemporaryDirectory(
            prefix="agent-run-compact-test-"
        )
        self.temp_dir = Path(self.temp_dir_context.name)
        self.env = os.environ.copy()
        self.env["TMPDIR"] = str(self.temp_dir)

    def tearDown(self) -> None:
        self.temp_dir_context.cleanup()

    def run_wrapper(
        self, *args: str, timeout: float = 5
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [str(WRAPPER), *args],
                check=False,
                capture_output=True,
                text=True,
                env=self.env,
                timeout=timeout,
            )
        except FileNotFoundError:
            self.fail(f"wrapper is not implemented: {WRAPPER}")

    def test_success_reports_bounded_summary_and_preserves_command_arguments(
        self,
    ) -> None:
        result = self.run_wrapper(
            "--",
            sys.executable,
            "-c",
            (
                "import sys; "
                "[print(f'noise-{i}') for i in range(200)]; "
                "print('summary: 200 passed'); "
                "print(sys.argv[1:])"
            ),
            "hello world",
            "--flag",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertIn("PASS", result.stdout)
        self.assertIn("output_lines=202", result.stdout)
        self.assertIn("summary: 200 passed", result.stdout)
        self.assertIn("['hello world', '--flag']", result.stdout)
        self.assertNotIn("noise-0\n", result.stdout)
        self.assertEqual(list(self.temp_dir.glob("agent-run-compact-*.log")), [])

    def test_failure_returns_original_status_and_retains_private_full_log(self) -> None:
        result = self.run_wrapper(
            "--",
            sys.executable,
            "-c",
            (
                "import sys; "
                "[print(f'noise-{i}') for i in range(250)]; "
                "print('fatal diagnostic'); "
                "sys.exit(7)"
            ),
        )

        self.assertEqual(result.returncode, 7)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("exit=7", result.stdout)
        self.assertIn("fatal diagnostic", result.stdout)
        log_line = next(
            line for line in result.stdout.splitlines() if line.startswith("full_log=")
        )
        log_path = Path(log_line.removeprefix("full_log="))
        self.assertTrue(log_path.is_file())
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("noise-0", log_text)
        self.assertIn("fatal diagnostic", log_text)

    def test_success_and_failure_bound_single_line_output_by_characters(self) -> None:
        success = self.run_wrapper(
            "--",
            sys.executable,
            "-c",
            "print('S' * 1_000_000)",
        )
        failure = self.run_wrapper(
            "--",
            sys.executable,
            "-c",
            "import sys; print('F' * 1_000_000); print('fatal end'); sys.exit(9)",
        )

        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertLessEqual(len(success.stdout), 5_000)
        self.assertIn("[line truncated]", success.stdout)
        self.assertEqual(failure.returncode, 9, failure.stderr)
        self.assertLessEqual(len(failure.stdout), 22_000)
        self.assertIn("[line truncated]", failure.stdout)
        self.assertIn("fatal end", failure.stdout)

    def test_log_collection_reads_unterminated_lines_in_bounded_chunks(self) -> None:
        loader = SourceFileLoader("agent_run_compact", str(WRAPPER))
        spec = spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        module = module_from_spec(spec)
        loader.exec_module(module)

        line_count, head, tail = module.collect_log(GuardedLogPath(5_000_000), 0, 5)

        self.assertEqual(line_count, 1)
        self.assertEqual(head, [])
        self.assertEqual(len(tail), 1)
        self.assertIn("[line truncated]", tail[0])

    @unittest.skipUnless(os.name == "posix", "exec format errors require POSIX")
    def test_exec_format_error_uses_shell_status_and_retained_log(self) -> None:
        invalid_executable = self.temp_dir / "invalid-executable"
        invalid_executable.write_text("missing shebang\n", encoding="utf-8")
        invalid_executable.chmod(0o755)

        result = self.run_wrapper("--", str(invalid_executable))

        self.assertEqual(result.returncode, 126)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("exit=126", result.stdout)
        self.assertIn("Exec format error", result.stdout)
        log_line = next(
            line for line in result.stdout.splitlines() if line.startswith("full_log=")
        )
        self.assertTrue(Path(log_line.removeprefix("full_log=")).is_file())

    def test_verbose_mode_streams_complete_output_without_wrapper_summary(self) -> None:
        result = self.run_wrapper(
            "--verbose",
            "--",
            sys.executable,
            "-c",
            "[print(f'line-{i}') for i in range(20)]",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("line-0", result.stdout)
        self.assertIn("line-19", result.stdout)
        self.assertNotIn("PASS", result.stdout)
        self.assertNotIn("full_log=", result.stdout)

    def test_long_running_command_emits_heartbeat(self) -> None:
        result = self.run_wrapper(
            "--heartbeat-seconds",
            "0.05",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(0.16); print('done')",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still running", result.stderr)
        self.assertIn("done", result.stdout)

    @unittest.skipUnless(os.name == "posix", "signal forwarding requires POSIX")
    def test_termination_is_forwarded_to_child_process_group(self) -> None:
        self.assertTrue(WRAPPER.is_file(), f"wrapper is not implemented: {WRAPPER}")
        child_pid_file = self.temp_dir / "child.pid"
        wrapper = subprocess.Popen(
            [
                str(WRAPPER),
                "--heartbeat-seconds",
                "0",
                "--",
                sys.executable,
                "-c",
                (
                    "import os, pathlib, time; "
                    f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
                    "time.sleep(30)"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(self._terminate_if_running, wrapper)

        deadline = time.monotonic() + 3
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(child_pid_file.exists(), "child did not start")
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))

        wrapper.send_signal(signal.SIGTERM)
        stdout, stderr = wrapper.communicate(timeout=3)

        self.assertEqual(wrapper.returncode, 128 + signal.SIGTERM, stderr)
        self.assertIn("INTERRUPTED", stdout)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    @staticmethod
    def _terminate_if_running(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
