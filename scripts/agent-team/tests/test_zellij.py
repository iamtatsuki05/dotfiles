from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team.zellij import (
    CloseEvidence,
    ZellijDriver,
    ZellijError,
    ZellijInspection,
    ZellijOwnershipError,
    ZellijReceipt,
    ZellijUnavailableError,
    ZellijValidationError,
    _parse_panes,
    _PathIdentity,
)


class ZellijDriverContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.executable = self.root / "zellij"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_missing_selected_cli_fails_before_private_root_side_effect(self) -> None:
        private_root = Path("/tmp/at-zellij-missing-proof")
        with self.assertRaises(ZellijUnavailableError):
            ZellijDriver(
                self.root / "missing-zellij",
                private_root,
                run_nonce="run-123",
                session_name="agent-team-run-123",
            )

    def test_private_root_keeps_lexical_tmp_path_and_rejects_traversal(self) -> None:
        with self.assertRaises(ZellijValidationError):
            ZellijDriver(
                self.executable,
                Path("/tmp/at-zellij/../other"),
                run_nonce="run-123",
                session_name="agent-team-run-123",
            )

        root = Path("/tmp/at-zellij-lexical")
        with (
            mock.patch("agent_team.zellij._safe_path_identity"),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch.object(Path, "is_symlink", return_value=False),
        ):
            driver = ZellijDriver(
                self.executable,
                root,
                run_nonce="run-123",
                session_name="agent-team-run-123",
            )
        self.assertEqual(driver.private_root, root)
        self.assertEqual(
            driver.socket_path, root / "s/contract_version_1/agent-team-run-123"
        )

    def test_existing_socket_is_not_taken_over(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="at-zellij-existing-", dir="/tmp"))
        root.chmod(0o700)
        socket = root / "s/contract_version_1/run-123"
        socket.parent.mkdir(parents=True, mode=0o700)
        socket.touch(mode=0o600)
        try:
            driver = ZellijDriver(
                self.executable,
                root,
                run_nonce="run-123",
                session_name="run-123",
            )
            with self.assertRaises(ZellijOwnershipError):
                driver.create(("/bin/true",), root, {}, "main")
        finally:
            socket.unlink()
            socket.parent.rmdir()
            (root / "s").rmdir()
            root.rmdir()

    def test_pane_parser_requires_one_terminal_and_only_known_plugin(self) -> None:
        panes = [
            {
                "id": 0,
                "is_plugin": True,
                "is_focused": False,
                "is_fullscreen": False,
                "is_floating": False,
                "is_suppressed": True,
                "title": "(.) - zellij:link",
                "exited": False,
                "exit_status": None,
                "is_held": False,
                "terminal_command": None,
                "plugin_url": "zellij:link",
                "tab_id": 0,
                "tab_position": 0,
                "tab_name": "Tab #1",
                "pane_cwd": None,
            },
            {
                "id": 0,
                "is_plugin": False,
                "is_focused": True,
                "is_fullscreen": False,
                "is_floating": False,
                "is_suppressed": False,
                "title": "main",
                "exited": False,
                "exit_status": None,
                "is_held": False,
                "terminal_command": "/usr/bin/env -i python",
                "plugin_url": None,
                "tab_id": 0,
                "tab_position": 0,
                "tab_name": "Tab #1",
                "pane_cwd": "/private/tmp/at-zellij/work",
            },
        ]
        terminal, plugins = _parse_panes(panes)
        self.assertEqual(terminal.pane_id, 0)
        self.assertEqual(terminal.pane_kind, "terminal")
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0].plugin_url, "zellij:link")

        with self.assertRaises(ZellijValidationError):
            _parse_panes(panes + [dict(panes[1], id=1)])
        with self.assertRaises(ZellijValidationError):
            _parse_panes(panes + [dict(panes[0], plugin_url="about")])

    def test_receipt_round_trip_is_strict_and_preserves_kind_distinguished_ids(
        self,
    ) -> None:
        identity = _PathIdentity(1, 2, 0o600, os.geteuid())
        receipt = ZellijReceipt(
            executable=self.executable.resolve(),
            private_root=Path("/tmp/at-zellij-proof"),
            socket_path=Path(
                "/tmp/at-zellij-proof/s/contract_version_1/agent-team-run-123"
            ),
            config_path=Path("/tmp/at-zellij-proof/config.kdl"),
            config_dir=Path("/tmp/at-zellij-proof/config-dir"),
            data_dir=Path("/tmp/at-zellij-proof/data-dir"),
            cache_dir=Path("/tmp/at-zellij-proof/cache"),
            tmp_dir=Path("/tmp/at-zellij-proof/tmp"),
            run_nonce="run-123",
            session_name="agent-team-run-123",
            tab_id=0,
            pane_id=0,
            tab_kind="tab",
            pane_kind="terminal",
            pane_pid=101,
            pane_pgid=101,
            server_pid=102,
            server_pgid=103,
            server_ppid=1,
            supervisor_ppid=102,
            server_argv=(
                str(self.executable.resolve()),
                "--server",
                "/tmp/at-zellij-proof/s/contract_version_1/agent-team-run-123",
            ),
            supervisor_argv=("/usr/bin/python3", "-m", "agent_team", "_native-main"),
            cwd=Path("/tmp/at-zellij-proof/work"),
            pane_command_argv=("/usr/bin/env", "-i", "VAR=value"),
            initial_pane={
                "pane_kind": "terminal",
                "pane_id": 0,
                "tab_id": 0,
                "tab_name": "Tab #1",
                "title": "main",
                "is_plugin": False,
                "is_suppressed": False,
                "plugin_url": None,
                "exited": False,
                "is_held": False,
                "exit_status": None,
                "terminal_command": "/usr/bin/env -i python",
                "pane_cwd": "/private/tmp/at-zellij-proof/work",
            },
            initial_plugins=(
                {
                    "pane_kind": "plugin",
                    "pane_id": 0,
                    "tab_id": 0,
                    "tab_name": "Tab #1",
                    "title": "(.) - zellij:link",
                    "is_plugin": True,
                    "is_suppressed": True,
                    "plugin_url": "zellij:link",
                    "exited": False,
                    "is_held": False,
                    "exit_status": None,
                    "terminal_command": None,
                    "pane_cwd": None,
                },
            ),
            socket_identity=identity,
            config_identity=identity,
            private_root_identity=identity,
            owned_paths=self._owned_paths(identity),
        )
        restored = ZellijReceipt.from_dict(receipt.as_dict())
        self.assertEqual(restored, receipt)
        self.assertEqual(restored.pane_kind, "terminal")
        with self.assertRaises(ZellijValidationError):
            ZellijReceipt.from_dict({**receipt.as_dict(), "unexpected": True})
        with self.assertRaises(ZellijValidationError):
            ZellijReceipt.from_dict({**receipt.as_dict(), "pane_id": "0"})
        with self.assertRaises(ZellijValidationError):
            ZellijReceipt.from_dict({**receipt.as_dict(), "owned_paths": []})
        inventory = list(cast(list[object], receipt.as_dict()["owned_paths"]))
        inventory.append(
            {
                "relative": "plugin/unknown",
                "identity": {
                    "device": 1,
                    "inode": 2,
                    "mode": 0o600,
                    "uid": os.geteuid(),
                },
            }
        )
        with self.assertRaises(ZellijValidationError):
            ZellijReceipt.from_dict({**receipt.as_dict(), "owned_paths": inventory})
        for relative in ("cache/unlisted-by-receipt", "home/user-file"):
            forged = list(cast(list[object], receipt.as_dict()["owned_paths"]))
            forged.append(
                {
                    "relative": relative,
                    "identity": {
                        "device": 1,
                        "inode": 2,
                        "mode": 0o600,
                        "uid": os.geteuid(),
                    },
                }
            )
            with self.assertRaises(ZellijValidationError):
                ZellijReceipt.from_dict({**receipt.as_dict(), "owned_paths": forged})

    def test_layout_uses_shellless_env_and_keeps_held_pane(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="at-zellij-layout-", dir="/tmp"))
        root.chmod(0o700)
        try:
            driver = ZellijDriver(
                self.executable,
                root,
                run_nonce="run-123",
                session_name="agent-team-run-123",
            )
            layout = driver._layout_string(
                ("/usr/bin/python3", "-c", "print('x')"),
                root / "work",
                {"VALUE": "a;$(touch NOPE)"},
                "main",
            )
            self.assertNotIn("max-panes", layout)
            self.assertIn('command="/usr/bin/env"', layout)
            self.assertIn("close_on_exit=false", layout)
            self.assertIn('args "-i"', layout)
            self.assertIn("a;$(touch NOPE)", layout)
        finally:
            root.rmdir()

    def test_large_pane_response_is_bounded(self) -> None:
        with self.assertRaises(ZellijError):
            ZellijDriver._decode_response("x" * (2 * 1024 * 1024 + 1))

    def test_malformed_list_sessions_response_is_unknown(self) -> None:
        driver = object.__new__(ZellijDriver)
        driver._session_name = "agent-team-run-123"
        driver._receipt = None
        with (
            mock.patch.object(
                driver, "_list_sessions_argv", return_value=("zellij", "list-sessions")
            ),
            mock.patch.object(
                driver,
                "_run",
                return_value=subprocess.CompletedProcess(
                    ["zellij", "list-sessions"], 0, "malformed", ""
                ),
            ),
        ):
            self.assertIsNone(driver._session_absent())

    def test_session_listing_requires_the_known_zellij_shape(self) -> None:
        from agent_team.zellij import _parse_session_listing

        self.assertEqual(
            _parse_session_listing("agent-team-run-123 [Created 1s ago] "),
            {"agent-team-run-123"},
        )
        for value in ("", "other [Created garbage]", "other [Created 1s ago] trailing"):
            self.assertIsNone(_parse_session_listing(value))

    def test_partial_cleanup_keeps_replaced_config(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="at-zellij-partial-", dir="/tmp"))
        root.chmod(0o700)
        config = root / "config.kdl"
        config.write_text("original", encoding="utf-8")
        original_identity = _PathIdentity(
            config.stat().st_dev,
            config.stat().st_ino,
            stat.S_IMODE(config.stat().st_mode),
            os.geteuid(),
        )
        config.unlink()
        config.write_text("replacement", encoding="utf-8")
        driver = object.__new__(ZellijDriver)
        driver._config_path = config
        try:
            self.assertFalse(driver._cleanup_unchecked_tree(original_identity))
            self.assertEqual(config.read_text(encoding="utf-8"), "replacement")
        finally:
            config.unlink(missing_ok=True)
            root.rmdir()

    def test_attach_argv_is_explicitly_session_scoped(self) -> None:
        identity = _PathIdentity(1, 2, 0o600, os.geteuid())
        receipt = self._minimal_receipt(identity)
        with (
            mock.patch("agent_team.zellij._safe_path_identity", return_value=identity),
            mock.patch("agent_team.zellij._same_path_identity", return_value=True),
            mock.patch.object(
                ZellijDriver,
                "inspect",
                return_value=ZellijInspection(
                    presence="present",
                    running=True,
                    exit_status=None,
                    identity_verified=True,
                    pane_present=True,
                    session_present=True,
                    pane_pid=101,
                    server_pid=102,
                    observed_nonce="run-123",
                ),
            ),
        ):
            driver = ZellijDriver.from_receipt(receipt)
            argv = driver.attach_argv(receipt)
        self.assertIn("attach", argv)
        self.assertIn(receipt.session_name, argv)
        self.assertIn(str(receipt.config_path), argv)
        self.assertNotIn("--max-panes", argv)

    def test_close_does_not_signal_server_group_and_retains_unknown_resources(
        self,
    ) -> None:
        identity = _PathIdentity(1, 2, 0o600, os.geteuid())
        receipt = self._minimal_receipt(identity)
        driver = object.__new__(ZellijDriver)
        driver._executable = receipt.executable
        driver._private_root = receipt.private_root
        driver._socket_path = receipt.socket_path
        driver._config_path = receipt.config_path
        driver._config_dir = receipt.config_dir
        driver._data_dir = receipt.data_dir
        driver._cache_dir = receipt.cache_dir
        driver._tmp_dir = receipt.tmp_dir
        driver._run_nonce = receipt.run_nonce
        driver._session_name = receipt.session_name
        driver._receipt = receipt
        with (
            mock.patch.object(
                driver,
                "inspect",
                return_value=ZellijInspection(
                    presence="present",
                    running=False,
                    exit_status=7,
                    identity_verified=True,
                    pane_present=True,
                    session_present=True,
                    pane_pid=101,
                    server_pid=102,
                    observed_nonce="run-123",
                ),
            ),
            mock.patch.object(driver, "_known_paths_owned", return_value=False),
            mock.patch("agent_team.zellij.os.killpg") as killpg,
        ):
            result = driver.close(receipt)
        self.assertEqual(result.evidence, CloseEvidence.OWNERSHIP_UNPROVEN)
        self.assertFalse(result.session_terminated)
        killpg.assert_not_called()

    def _minimal_receipt(self, identity: _PathIdentity) -> ZellijReceipt:
        root = Path("/tmp/at-zellij-proof")
        return ZellijReceipt(
            executable=self.executable.resolve(),
            private_root=root,
            socket_path=root / "s/contract_version_1/agent-team-run-123",
            config_path=root / "config.kdl",
            config_dir=root / "config-dir",
            data_dir=root / "data-dir",
            cache_dir=root / "cache",
            tmp_dir=root / "tmp",
            run_nonce="run-123",
            session_name="agent-team-run-123",
            tab_id=0,
            pane_id=0,
            tab_kind="tab",
            pane_kind="terminal",
            pane_pid=101,
            pane_pgid=101,
            server_pid=102,
            server_pgid=103,
            server_ppid=1,
            supervisor_ppid=102,
            server_argv=(
                str(self.executable.resolve()),
                "--server",
                str(root / "s/contract_version_1/agent-team-run-123"),
            ),
            supervisor_argv=("/usr/bin/python3", "-m", "agent_team", "_native-main"),
            cwd=root / "work",
            pane_command_argv=("/usr/bin/env", "-i"),
            initial_pane={
                "pane_kind": "terminal",
                "pane_id": 0,
                "tab_id": 0,
                "tab_name": "Tab #1",
                "title": "main",
                "is_plugin": False,
                "is_suppressed": False,
                "plugin_url": None,
                "exited": False,
                "is_held": False,
                "exit_status": None,
                "terminal_command": "/usr/bin/env -i",
                "pane_cwd": str(root / "work"),
            },
            initial_plugins=(
                {
                    "pane_kind": "plugin",
                    "pane_id": 0,
                    "tab_id": 0,
                    "tab_name": "Tab #1",
                    "title": "(.) - zellij:link",
                    "is_plugin": True,
                    "is_suppressed": True,
                    "plugin_url": "zellij:link",
                    "exited": False,
                    "is_held": False,
                    "exit_status": None,
                    "terminal_command": None,
                    "pane_cwd": None,
                },
            ),
            socket_identity=identity,
            config_identity=identity,
            private_root_identity=identity,
            owned_paths=self._owned_paths(identity),
        )

    @staticmethod
    def _owned_paths(
        identity: _PathIdentity,
    ) -> tuple[tuple[str, _PathIdentity], ...]:
        return tuple(
            (relative, identity)
            for relative in (
                "config.kdl",
                "config-dir",
                "data-dir",
                "cache",
                "tmp",
                "home",
                "runtime",
                "state",
                "s",
                "s/contract_version_1",
                "s/contract_version_1/agent-team-run-123",
            )
        )


if __name__ == "__main__":
    unittest.main()
