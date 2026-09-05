from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from agent_team.tmux import (
    CloseEvidence,
    TmuxCloseResult,
    TmuxDriver,
    TmuxError,
    TmuxReceipt,
    TmuxUnavailableError,
)

FAKE_TMUX = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def state_path() -> Path:
    return Path(os.environ["FAKE_TMUX_STATE"])


def load() -> dict[str, object]:
    path = state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(state: dict[str, object]) -> None:
    state_path().write_text(json.dumps(state), encoding="utf-8")


def socket_path(argv: list[str]) -> Path:
    return Path(argv[argv.index("-S") + 1])


def command(argv: list[str]) -> str:
    for item in argv:
        if item in {
            "new-session",
            "set-option",
            "display-message",
            "has-session",
            "kill-session",
            "list-panes",
            "list-sessions",
        }:
            return item
    raise SystemExit("missing fake tmux command")


def append_log(argv: list[str]) -> None:
    path = state_path().with_suffix(".argv.jsonl")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(argv) + "\n")


def bind_socket(path: Path) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()
    path.chmod(0o600)


argv = sys.argv[1:]
append_log(argv)
state = load()
action = command(argv)

if action == "new-session":
    if state.get("session", False):
        print("session already exists", file=sys.stderr)
        raise SystemExit(1)
    path = socket_path(argv)
    if path.exists():
        print("socket already exists", file=sys.stderr)
        raise SystemExit(1)
    bind_socket(path)
    state = {
        "session": True,
        "session_id": "$0",
        "window_id": "@0",
        "pane_id": "%0",
        "pane_pid": 4242,
        "server_pid": 4343,
        "dead": False,
        "exit_status": None,
        "nonce": None,
    }
    save(state)
    print("$0|@0|%0|4242|4343")
    raise SystemExit(0)

if action == "set-option":
    if os.environ.get("FAKE_TMUX_SET_MODE") == "fail":
        raise SystemExit(1)
    state["nonce"] = argv[-1]
    save(state)
    raise SystemExit(0)

if action == "display-message":
    pane = argv[argv.index("-t") + 1]
    if pane != state.get("pane_id"):
        print("unknown pane", file=sys.stderr)
        raise SystemExit(1)
    dead = "1" if state.get("dead") else "0"
    status = state.get("exit_status")
    status_text = "" if status is None else str(status)
    print(
        "|".join(
            (
                str(state["session_id"]),
                "agent-team-run-123",
                str(state["window_id"]),
                str(state["pane_id"]),
                str(state["pane_pid"]),
                str(state["server_pid"]),
                dead,
                status_text,
                str(state.get("nonce") or ""),
            )
        )
    )
    raise SystemExit(0)

if action == "has-session":
    raise SystemExit(0 if state.get("session") else 1)

if action == "list-panes":
    if not state.get("session"):
        raise SystemExit(1)
    print("|".join((str(state["session_id"]), str(state["window_id"]), str(state["pane_id"]), str(state["pane_pid"]))))
    raise SystemExit(0)

if action == "kill-session":
    if os.environ.get("FAKE_TMUX_KILL_MODE") == "unknown":
        raise SystemExit(0)
    state["session"] = False
    save(state)
    raise SystemExit(0)

if action == "list-sessions":
    if state.get("session"):
        print("$0")
        raise SystemExit(0)
    raise SystemExit(1)

raise SystemExit(f"unsupported fake tmux command: {action}")
"""


class TmuxDriverContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.state = self.root / "state.json"
        self.fake = self.root / "fake-tmux"
        self.fake.write_text(textwrap.dedent(FAKE_TMUX), encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.environment = {
            "FAKE_TMUX_STATE": str(self.state),
            "PATH": os.environ.get("PATH", os.defpath),
        }
        self.environment_patcher = mock.patch.dict(os.environ, self.environment)
        self.environment_patcher.start()
        self.driver = TmuxDriver(
            self.fake,
            self.root / "private-socket",
            run_nonce="run-123",
            session_name="agent-team-run-123",
        )

    def tearDown(self) -> None:
        self.environment_patcher.stop()
        self.directory.cleanup()

    def receipt(self) -> TmuxReceipt:
        return self.driver.create(
            ("python3", "-c", "import sys; sys.exit(7)"),
            cwd=self.root,
            env={"ARG": "value"},
            title="worker",
        )

    def test_import_does_not_execute_external_commands(self) -> None:
        script = """
import subprocess
subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError("import executed a process")
)
import agent_team.tmux
"""
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            cwd=Path(__file__).resolve().parents[1],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_selected_cli_fails_before_socket_side_effect(self) -> None:
        socket_path = self.root / "missing-socket"
        with self.assertRaises(TmuxUnavailableError):
            TmuxDriver(
                self.root / "does-not-exist",
                socket_path,
                run_nonce="run-123",
                session_name="agent-team-run-123",
            )
        self.assertFalse(socket_path.exists())
        self.assertEqual(tuple(self.root.iterdir()), (self.fake,))

    def test_create_passes_argv_and_environment_without_shell_reparsing(self) -> None:
        argv = (
            "python3",
            "-c",
            "import sys; sys.exit(0)",
            "argument;$(touch SHOULD_NOT_EXIST)",
            "quote'\"$`\\",
            ";",
        )
        environment = {
            "VALUE": "value;$(touch SHOULD_NOT_EXIST) 'quoted'\nline;",
            "EMPTY": "",
        }
        receipt = self.driver.create(
            argv,
            cwd=self.root,
            env=environment,
            title=";",
        )
        log = self.state.with_suffix(".argv.jsonl").read_text(encoding="utf-8")
        commands = [json.loads(line) for line in log.splitlines()]
        new_session = next(item for item in commands if "new-session" in item)
        command_index = new_session.index("--")
        self.assertEqual(
            new_session[command_index + 1 :],
            [
                "env",
                "-i",
                "EMPTY=",
                "VALUE=value;$(touch SHOULD_NOT_EXIST) 'quoted'\nline\\;",
                "python3",
                "-c",
                "import sys; sys.exit(0)",
                "argument;$(touch SHOULD_NOT_EXIST)",
                "quote'\"$`\\",
                r"\;",
            ],
        )
        self.assertEqual(new_session[new_session.index("-n") + 1], r"\;")
        self.assertEqual(receipt.pane_id, "%0")
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_inspect_rejects_wrong_owner_and_wrong_pane_without_stopping(self) -> None:
        receipt = self.receipt()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["nonce"] = "other-run"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        ownership = self.driver.inspect(receipt)
        self.assertFalse(ownership.identity_verified)
        self.assertIsNone(ownership.running)
        self.assertEqual(ownership.reason, "tmux pane identity changed")

        state["nonce"] = receipt.run_nonce
        state["pane_pid"] = 9999
        self.state.write_text(json.dumps(state), encoding="utf-8")
        respawned = self.driver.inspect(receipt)
        self.assertFalse(respawned.identity_verified)
        self.assertEqual(respawned.reason, "tmux pane identity changed")

        state["pane_pid"] = receipt.pane_pid
        state["pane_id"] = "%9"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        missing_pane = self.driver.inspect(receipt)
        self.assertFalse(missing_pane.identity_verified)
        self.assertIsNone(missing_pane.running)
        self.assertFalse(self.driver.close(receipt).session_terminated)
        self.assertTrue(json.loads(self.state.read_text(encoding="utf-8"))["session"])

    def test_close_does_not_remove_control_paths_with_unknown_directory_owner(
        self,
    ) -> None:
        receipt = self.receipt()
        self.root.chmod(0o755)
        try:
            result = self.driver.close(receipt)
        finally:
            self.root.chmod(0o700)
        self.assertEqual(result.evidence, CloseEvidence.OWNERSHIP_UNPROVEN)
        self.assertFalse(result.session_terminated)
        self.assertTrue(receipt.socket_path.exists())
        self.assertTrue(receipt.config_path.exists())

    def test_close_reports_unknown_when_kill_has_no_postcondition(self) -> None:
        receipt = self.receipt()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["dead"] = True
        state["exit_status"] = 7
        self.state.write_text(json.dumps(state), encoding="utf-8")
        os.environ["FAKE_TMUX_KILL_MODE"] = "unknown"
        try:
            result = self.driver.close(receipt)
        finally:
            os.environ.pop("FAKE_TMUX_KILL_MODE", None)
        self.assertIsInstance(result, TmuxCloseResult)
        self.assertEqual(result.evidence, CloseEvidence.TERMINATION_UNPROVEN)
        self.assertFalse(result.session_terminated)
        self.assertFalse(result.descendants_stopped)

    def test_failed_owner_tag_reclaims_the_partial_session(self) -> None:
        os.environ["FAKE_TMUX_SET_MODE"] = "fail"
        try:
            with self.assertRaises(TmuxError):
                self.receipt()
        finally:
            os.environ.pop("FAKE_TMUX_SET_MODE", None)
        self.assertFalse((self.root / "private-socket").exists())
        self.assertFalse(tuple(self.root.glob("*.tmux.conf")))
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertFalse(state["session"])

    def test_dead_pane_status_is_observed_and_normal_close_is_evidenced(self) -> None:
        receipt = self.receipt()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["dead"] = True
        state["exit_status"] = 7
        self.state.write_text(json.dumps(state), encoding="utf-8")
        inspection = self.driver.inspect(receipt)
        self.assertTrue(inspection.identity_verified)
        self.assertFalse(inspection.running)
        self.assertEqual(inspection.exit_status, 7)
        attach = self.driver.attach_argv(receipt)
        self.assertEqual(
            attach[:3],
            (
                str(self.fake.resolve()),
                "-S",
                str(self.root / "private-socket"),
            ),
        )
        result = self.driver.close(receipt)
        self.assertEqual(result.evidence, CloseEvidence.SERVER_TERMINATED)
        self.assertTrue(result.session_terminated)
        self.assertTrue(result.server_terminated)
        self.assertTrue(result.socket_removed)
        self.assertEqual(result.exit_status, 7)
        self.assertFalse(result.descendants_stopped)
        self.assertFalse((self.root / "private-socket").exists())


if __name__ == "__main__":
    unittest.main()
