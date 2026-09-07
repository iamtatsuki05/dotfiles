from __future__ import annotations

import socket
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from agent_team.herdr import (
    CloseEvidence,
    HerdrDriver,
    HerdrError,
    HerdrInspection,
    HerdrOwnershipError,
    HerdrReceipt,
    HerdrValidationError,
    _PathIdentity,
)


def _identity(path: Path, *, socket_path: bool = False) -> _PathIdentity:
    info = path.lstat()
    return _PathIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
    )


class HerdrDriverContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(
            prefix="at-herdr-test-", dir="/tmp"
        )
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.executable = Path("/bin/sh")
        self.driver = HerdrDriver(
            self.executable,
            self.root,
            run_nonce="a" * 32,
            session_name="agent-team-main-aaaaaaaaaaaaaaaa",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_missing_dependency_is_rejected_before_private_root_side_effect(
        self,
    ) -> None:
        root = self.root / "missing-root"
        with self.assertRaises(HerdrError):
            HerdrDriver(
                root / "does-not-exist",
                root,
                run_nonce="a" * 32,
                session_name="agent-team-main-aaaaaaaaaaaaaaaa",
            )
        self.assertFalse(root.exists())

    def test_existing_private_root_or_control_paths_are_not_taken_over(self) -> None:
        (self.root / "c/herdr").mkdir(parents=True, mode=0o700)
        (self.root / "c/herdr/config.toml").write_text("existing", encoding="utf-8")
        with self.assertRaises(HerdrOwnershipError):
            HerdrDriver(
                self.executable,
                self.root,
                run_nonce="b" * 32,
                session_name="agent-team-main-bbbbbbbbbbbbbbbb",
            ).create(("/bin/echo", "hello"), self.root, {}, "main")

    def test_receipt_round_trip_is_strict_and_preserves_lexical_tmp_path(self) -> None:
        session_name = "agent-team-main-aaaaaaaaaaaaaaaa"
        config = self.root / "c/herdr/config.toml"
        socket_path = self.root / f"c/herdr/sessions/{session_name}/herdr.sock"
        client_socket = socket_path.with_name("herdr-client.sock")
        session_dir = socket_path.parent
        config.parent.mkdir(parents=True, mode=0o700)
        session_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        config.write_text("config", encoding="utf-8")
        config.chmod(0o600)
        for path in (socket_path, client_socket):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(path))
            finally:
                sock.close()
            path.chmod(0o600)
        receipt = HerdrReceipt(
            executable=self.executable,
            private_root=self.root,
            config_path=config,
            socket_path=socket_path,
            client_socket_path=client_socket,
            session_dir=session_dir,
            run_nonce="a" * 32,
            session_name=session_name,
            workspace_id="w1",
            tab_id="w1:t1",
            pane_id="w1:p1",
            terminal_id="term_test",
            pane_pid=101,
            pane_pgid=101,
            server_pid=102,
            server_pgid=102,
            supervisor_ppid=102,
            server_argv=(
                str(self.executable),
                "--session",
                session_name,
                "server",
            ),
            supervisor_argv=("/usr/bin/python3", "-m", "agent_team", "_native-main"),
            server_cwd=self.root,
            cwd=Path("/tmp/workspace"),
            version="0.8.2",
            protocol=20,
            config_identity=_identity(config),
            socket_identity=_identity(socket_path),
            client_socket_identity=_identity(client_socket),
            private_root_identity=_identity(self.root),
            session_dir_identity=_identity(session_dir),
            owned_paths=(
                ("c/herdr/config.toml", _identity(config)),
                (
                    f"c/herdr/sessions/{session_name}",
                    _identity(session_dir),
                ),
                (
                    f"c/herdr/sessions/{session_name}/herdr.sock",
                    _identity(socket_path),
                ),
                (
                    f"c/herdr/sessions/{session_name}/herdr-client.sock",
                    _identity(client_socket),
                ),
            ),
        )

        restored = HerdrReceipt.from_dict(receipt.as_dict())

        self.assertEqual(restored, receipt)
        self.assertEqual(restored.private_root, Path("/tmp") / self.root.name)
        with self.assertRaises(HerdrValidationError):
            HerdrReceipt.from_dict({**receipt.as_dict(), "unexpected": True})
        with self.assertRaises(HerdrValidationError):
            HerdrReceipt.from_dict({**receipt.as_dict(), "server_pid": True})

    def test_create_rejects_reserved_herdr_environment_without_starting_server(
        self,
    ) -> None:
        with (
            mock.patch("agent_team.herdr.subprocess.Popen") as popen,
            self.assertRaises(HerdrValidationError),
        ):
            self.driver.create(
                ("/bin/echo", "hello"),
                self.root,
                {"HERDR_ENV": "1"},
                "main",
            )
        popen.assert_not_called()

    def test_create_uses_one_explicit_quoted_exec_and_response_ids(self) -> None:
        cwd = Path("/tmp")
        fake = SimpleNamespace(pid=701, poll=lambda: None, wait=lambda **_: 0)
        requests: list[tuple[str, dict[str, object]]] = []
        socket_path = (
            self.root / "c/herdr/sessions/agent-team-main-aaaaaaaaaaaaaaaa/herdr.sock"
        )
        client_socket = socket_path.with_name("herdr-client.sock")
        supervisor = ("/usr/bin/python3", "-m", "agent_team", "_native-main", "x;y")
        created = False

        def ready(_process: object) -> None:
            nonlocal socket_path, client_socket
            socket_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            for path in (socket_path, client_socket):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.bind(str(path))
                finally:
                    sock.close()
                path.chmod(0o600)

        def request(method: str, params: dict[str, object]) -> dict[str, object]:
            nonlocal created
            requests.append((method, params))
            if method == "ping":
                return {"type": "pong", "version": "0.8.2", "protocol": 20}
            if method == "events.subscribe":
                return {"type": "subscription_started"}
            if method == "workspace.create":
                created = True
                return {
                    "type": "workspace_created",
                    "workspace": {"workspace_id": "w1"},
                    "tab": {"tab_id": "w1:t1", "workspace_id": "w1"},
                    "root_pane": {
                        "pane_id": "w1:p1",
                        "terminal_id": "term1",
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "cwd": str(cwd.resolve()),
                    },
                }
            if method == "pane.send_input":
                return {"type": "ok"}
            if method == "session.snapshot":
                if not created:
                    return {
                        "type": "session_snapshot",
                        "snapshot": {
                            "version": "0.8.2",
                            "protocol": 20,
                            "workspaces": [],
                            "tabs": [],
                            "panes": [],
                            "layouts": [],
                            "agents": [],
                        },
                    }
                return {
                    "type": "session_snapshot",
                    "snapshot": {
                        "version": "0.8.2",
                        "protocol": 20,
                        "workspaces": [{"workspace_id": "w1"}],
                        "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1"}],
                        "panes": [
                            {
                                "pane_id": "w1:p1",
                                "terminal_id": "term1",
                                "workspace_id": "w1",
                                "tab_id": "w1:t1",
                                "cwd": str(cwd.resolve()),
                            }
                        ],
                        "layouts": [
                            {
                                "workspace_id": "w1",
                                "tab_id": "w1:t1",
                                "panes": [{"pane_id": "w1:p1"}],
                            }
                        ],
                        "agents": [],
                    },
                }
            if method == "pane.process_info":
                return {
                    "type": "pane_process_info",
                    "process_info": {
                        "pane_id": "w1:p1",
                        "shell_pid": 703,
                        "foreground_process_group_id": 703,
                        "foreground_processes": [
                            {
                                "pid": 703,
                                "argv": list(supervisor),
                                "cwd": str(cwd.resolve()),
                            }
                        ],
                    },
                }
            raise AssertionError(method)

        with (
            mock.patch("agent_team.herdr.subprocess.Popen", return_value=fake),
            mock.patch.object(self.driver, "_request", side_effect=request),
            mock.patch(
                "agent_team.herdr.read_process_argv",
                side_effect=lambda pid: (
                    (
                        str(self.driver.executable),
                        "--session",
                        self.driver.session_name,
                        "server",
                    )
                    if pid == 701
                    else supervisor
                ),
            ),
            mock.patch(
                "agent_team.herdr.os.getpgid",
                side_effect=lambda pid: 701 if pid == 701 else 703,
            ),
            mock.patch(
                "agent_team.herdr._read_process_ppid",
                side_effect=lambda pid: 700 if pid == 701 else 701,
            ),
            mock.patch("agent_team.herdr._pid_state", return_value=True),
            mock.patch.object(self.driver, "_wait_for_socket", side_effect=ready),
            mock.patch.object(
                self.driver, "_supervisor_process_info", return_value=(703, 703)
            ),
            mock.patch(
                "agent_team.herdr._read_process_cwd",
                side_effect=lambda pid: self.root if pid == 701 else cwd.resolve(),
            ),
        ):
            receipt = self.driver.create(
                ("/usr/bin/python3", "-m", "agent_team", "_native-main", "x;y"),
                cwd,
                {"PATH": "/usr/bin", "VALUE": "x;y"},
                "main",
            )

        self.assertEqual(receipt.workspace_id, "w1")
        self.assertEqual(receipt.pane_id, "w1:p1")
        pane_inputs = [
            params for method, params in requests if method == "pane.send_input"
        ]
        self.assertEqual(len(pane_inputs), 1)
        command = cast(str, pane_inputs[0]["text"])
        self.assertIn("exec env -i", command)
        self.assertIn("'x;y'", command)
        self.assertEqual(pane_inputs[0]["keys"], ["Enter"])

    def test_inspect_rejects_nonempty_snapshot_with_an_extra_workspace(self) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        snapshot = self._snapshot(receipt, extra_workspace=True)
        with (
            mock.patch.object(driver, "_request", return_value=snapshot),
            mock.patch(
                "agent_team.herdr.read_process_argv", return_value=receipt.server_argv
            ),
            mock.patch(
                "agent_team.herdr._read_process_cwd",
                return_value=receipt.server_cwd,
            ),
            mock.patch("agent_team.herdr.os.getpgid", return_value=receipt.server_pgid),
        ):
            observed = driver.inspect(receipt)
        self.assertEqual(observed.presence, "unknown")
        self.assertFalse(observed.identity_verified)

    def test_malformed_snapshot_resource_is_unknown_and_close_has_no_effect(
        self,
    ) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        malformed = self._snapshot(receipt)
        malformed["snapshot"] = {
            **cast(dict[str, object], malformed["snapshot"]),
            "workspaces": ["not-an-object"],
        }
        with (
            mock.patch.object(driver, "_request", return_value=malformed),
            mock.patch(
                "agent_team.herdr.read_process_argv", return_value=receipt.server_argv
            ),
            mock.patch(
                "agent_team.herdr._read_process_cwd",
                return_value=receipt.server_cwd,
            ),
            mock.patch("agent_team.herdr.os.getpgid", return_value=receipt.server_pgid),
            mock.patch("agent_team.herdr._pid_state", return_value=True),
            mock.patch.object(driver, "_session_command") as session_command,
        ):
            observed = driver.inspect(receipt)
            closed = driver.close(receipt)
        self.assertEqual(observed.presence, "unknown")
        self.assertEqual(closed.evidence, CloseEvidence.OWNERSHIP_UNPROVEN)
        session_command.assert_not_called()

    def test_snapshot_focus_and_unknown_resource_fields_are_checked(self) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        mutations: tuple[dict[str, object], ...] = (
            {"focused_workspace_id": receipt.workspace_id},
            {"terminals": []},
        )
        for mutation in mutations:
            snapshot = self._empty_snapshot()
            snapshot["snapshot"] = {
                **cast(dict[str, object], snapshot["snapshot"]),
                **mutation,
            }
            with (
                mock.patch.object(driver, "_request", return_value=snapshot),
                mock.patch(
                    "agent_team.herdr.read_process_argv",
                    return_value=receipt.server_argv,
                ),
                mock.patch(
                    "agent_team.herdr._read_process_cwd",
                    return_value=receipt.server_cwd,
                ),
                mock.patch(
                    "agent_team.herdr.os.getpgid", return_value=receipt.server_pgid
                ),
                mock.patch("agent_team.herdr._pid_state", return_value=True),
            ):
                observed = driver.inspect(receipt)
            self.assertEqual(observed.presence, "unknown")

    def test_private_inventory_rejects_empty_receipt_unknown_paths_and_symlinks(
        self,
    ) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        self.assertFalse(driver._known_paths_owned(replace(receipt, owned_paths=())))
        encoded = receipt.as_dict()
        encoded["owned_paths"] = [
            {"relative": "c/unknown", "identity": encoded["config_identity"]}
        ]
        with self.assertRaises(HerdrValidationError):
            HerdrReceipt.from_dict(encoded)
        unknown = receipt.private_root / "c/unknown"
        unknown.write_text("unknown", encoding="utf-8")
        self.assertFalse(driver._known_paths_owned(receipt))
        unknown.unlink()
        (receipt.session_dir / "unowned-link").symlink_to(receipt.config_path)
        self.assertFalse(driver._known_paths_owned(receipt))

    def test_socket_inode_replacement_blocks_close_for_both_sockets(self) -> None:
        for socket_name in ("socket_path", "client_socket_path"):
            with (
                self.subTest(socket_name=socket_name),
                tempfile.TemporaryDirectory(
                    prefix="at-herdr-socket-", dir="/tmp"
                ) as raw,
            ):
                previous_root = self.root
                self.root = Path(raw)
                self.root.chmod(0o700)
                try:
                    receipt = self._sample_receipt()
                    driver = self._restored_driver(receipt)
                    path = getattr(receipt, socket_name)
                    path.unlink()
                    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        replacement.bind(str(path))
                    finally:
                        replacement.close()
                    path.chmod(0o600)
                    absent = HerdrInspection(
                        presence="absent",
                        running=False,
                        exit_status=None,
                        identity_verified=True,
                        pane_present=False,
                        session_present=True,
                        pane_pid=None,
                        server_pid=receipt.server_pid,
                        observed_nonce=receipt.run_nonce,
                    )
                    with (
                        mock.patch.object(driver, "inspect", return_value=absent),
                        mock.patch.object(driver, "_session_command") as stop,
                    ):
                        result = driver.close(receipt)
                    self.assertEqual(result.evidence, CloseEvidence.OWNERSHIP_UNPROVEN)
                    stop.assert_not_called()
                finally:
                    self.root = previous_root

    def test_socket_inode_replacement_after_workspace_close_still_blocks_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="at-hsc-", dir="/tmp") as raw:
            previous_root = self.root
            self.root = Path(raw)
            self.root.chmod(0o700)
            try:
                receipt = self._sample_receipt()
                driver = self._restored_driver(receipt)
                present = HerdrInspection(
                    presence="present",
                    running=True,
                    exit_status=None,
                    identity_verified=True,
                    pane_present=True,
                    session_present=True,
                    pane_pid=receipt.pane_pid,
                    server_pid=receipt.server_pid,
                    observed_nonce=receipt.run_nonce,
                    pane_pgid=receipt.pane_pgid,
                )
                absent = replace(
                    present,
                    presence="absent",
                    running=False,
                    pane_present=False,
                    pane_pid=None,
                )
                calls = 0

                def inspect(_receipt: HerdrReceipt) -> HerdrInspection:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        path = receipt.socket_path
                        path.unlink()
                        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            replacement.bind(str(path))
                        finally:
                            replacement.close()
                        path.chmod(0o600)
                    return present if calls == 1 else absent

                with (
                    mock.patch.object(driver, "inspect", side_effect=inspect),
                    mock.patch.object(driver, "_request", return_value={"type": "ok"}),
                    mock.patch.object(driver, "_session_command") as stop,
                ):
                    result = driver.close(receipt)
                self.assertEqual(result.evidence, CloseEvidence.OWNERSHIP_UNPROVEN)
                stop.assert_not_called()
            finally:
                self.root = previous_root

    def test_inspect_reports_owned_absent_only_for_complete_empty_snapshot_and_dead_pane(
        self,
    ) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        with (
            mock.patch.object(driver, "_request", return_value=self._empty_snapshot()),
            mock.patch(
                "agent_team.herdr.read_process_argv", return_value=receipt.server_argv
            ),
            mock.patch(
                "agent_team.herdr._read_process_cwd",
                return_value=receipt.server_cwd,
            ),
            mock.patch("agent_team.herdr.os.getpgid", return_value=receipt.server_pgid),
            mock.patch("agent_team.herdr._pid_state", side_effect=(True, False)),
        ):
            observed = driver.inspect(receipt)
        self.assertEqual(observed.presence, "absent")
        self.assertTrue(observed.identity_verified)
        self.assertFalse(observed.pane_present)
        self.assertIsNone(observed.pane_pid)
        self.assertFalse(observed.running)
        self.assertTrue(observed.session_present)
        self.assertEqual(observed.server_pid, receipt.server_pid)
        self.assertEqual(observed.observed_nonce, receipt.run_nonce)

    def test_inspect_keeps_unknown_when_empty_snapshot_has_unproven_pane_pid(
        self,
    ) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        with (
            mock.patch.object(driver, "_request", return_value=self._empty_snapshot()),
            mock.patch(
                "agent_team.herdr.read_process_argv", return_value=receipt.server_argv
            ),
            mock.patch("agent_team.herdr.os.getpgid", return_value=receipt.server_pgid),
            mock.patch("agent_team.herdr._pid_state", return_value=None),
        ):
            observed = driver.inspect(receipt)
        self.assertEqual(observed.presence, "unknown")
        self.assertFalse(observed.identity_verified)

    def test_close_does_not_stop_or_delete_when_inspection_is_unknown(self) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        stop = mock.Mock()
        with (
            mock.patch.object(
                driver, "inspect", return_value=HerdrInspection.unknown("drift")
            ),
            mock.patch.object(driver, "_session_command", stop),
        ):
            result = driver.close(receipt)
        self.assertEqual(result.evidence, CloseEvidence.OWNERSHIP_UNPROVEN)
        self.assertFalse(result.session_terminated)
        stop.assert_not_called()

    def test_close_keeps_artifacts_when_session_stop_response_is_unknown(self) -> None:
        receipt = self._sample_receipt()
        driver = self._restored_driver(receipt)
        with (
            mock.patch.object(
                driver,
                "inspect",
                return_value=HerdrInspection(
                    presence="absent",
                    running=False,
                    exit_status=None,
                    identity_verified=True,
                    pane_present=False,
                    session_present=True,
                    pane_pid=None,
                    server_pid=receipt.server_pid,
                    observed_nonce=receipt.run_nonce,
                ),
            ),
            mock.patch.object(
                driver,
                "_session_command",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="lost"),
            ) as stop,
        ):
            result = driver.close(receipt)
        self.assertEqual(result.evidence, CloseEvidence.TERMINATION_UNPROVEN)
        self.assertTrue(result.ownership_verified)
        self.assertFalse(result.session_terminated)
        stop.assert_called_once_with("stop")

    def _sample_receipt(self) -> HerdrReceipt:
        session_name = "agent-team-main-aaaaaaaaaaaaaaaa"
        paths = {
            "private": self.root,
            "home": self.root / "h",
            "config_root": self.root / "c",
            "state": self.root / "s",
            "tmp": self.root / "t",
            "herdr": self.root / "c/herdr",
            "sessions": self.root / "c/herdr/sessions",
            "config": self.root / "c/herdr/config.toml",
            "socket": self.root / f"c/herdr/sessions/{session_name}/herdr.sock",
            "client": self.root / f"c/herdr/sessions/{session_name}/herdr-client.sock",
            "session": self.root / f"c/herdr/sessions/{session_name}",
        }
        for name in ("home", "config_root", "state", "tmp"):
            paths[name].mkdir(mode=0o700)
        paths["herdr"].mkdir(mode=0o700)
        paths["sessions"].mkdir(mode=0o700)
        paths["config"].parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        paths["config"].write_text("config", encoding="utf-8")
        paths["config"].chmod(0o600)
        paths["session"].mkdir(mode=0o700, parents=True)
        for key in ("socket", "client"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(paths[key]))
            finally:
                sock.close()
            paths[key].chmod(0o600)
        return HerdrReceipt(
            executable=self.executable,
            private_root=self.root,
            config_path=paths["config"],
            socket_path=paths["socket"],
            client_socket_path=paths["client"],
            session_dir=paths["session"],
            run_nonce="a" * 32,
            session_name=session_name,
            workspace_id="w1",
            tab_id="w1:t1",
            pane_id="w1:p1",
            terminal_id="term1",
            pane_pid=703,
            pane_pgid=703,
            server_pid=701,
            server_pgid=701,
            supervisor_ppid=701,
            server_argv=(
                str(self.executable),
                "--session",
                "agent-team-main-aaaaaaaaaaaaaaaa",
                "server",
            ),
            supervisor_argv=("/usr/bin/python3", "-m", "agent_team", "_native-main"),
            server_cwd=self.root,
            cwd=self.root,
            version="0.8.2",
            protocol=20,
            config_identity=_identity(paths["config"]),
            socket_identity=_identity(paths["socket"]),
            client_socket_identity=_identity(paths["client"]),
            private_root_identity=_identity(self.root),
            session_dir_identity=_identity(paths["session"]),
            owned_paths=tuple(
                (
                    path.relative_to(self.root).as_posix(),
                    _identity(path),
                )
                for path in (
                    paths["home"],
                    paths["config_root"],
                    paths["herdr"],
                    paths["config"],
                    paths["sessions"],
                    paths["session"],
                    paths["socket"],
                    paths["client"],
                    paths["state"],
                    paths["tmp"],
                )
            ),
        )

    def _restored_driver(self, receipt: HerdrReceipt) -> HerdrDriver:
        driver = object.__new__(HerdrDriver)
        driver._executable = receipt.executable
        driver._private_root = receipt.private_root
        driver._run_nonce = receipt.run_nonce
        driver._session_name = receipt.session_name
        driver._config_path = receipt.config_path
        driver._session_dir = receipt.session_dir
        driver._socket_path = receipt.socket_path
        driver._client_socket_path = receipt.client_socket_path
        driver._receipt = receipt
        return driver

    @staticmethod
    def _empty_snapshot() -> dict[str, object]:
        return {
            "type": "session_snapshot",
            "snapshot": {
                "version": "0.8.2",
                "protocol": 20,
                "workspaces": [],
                "tabs": [],
                "panes": [],
                "layouts": [],
                "agents": [],
            },
        }

    @staticmethod
    def _snapshot(
        receipt: HerdrReceipt, *, extra_workspace: bool = False
    ) -> dict[str, object]:
        workspaces: list[dict[str, object]] = [{"workspace_id": receipt.workspace_id}]
        if extra_workspace:
            workspaces.append({"workspace_id": "w-extra"})
        return {
            "type": "session_snapshot",
            "snapshot": {
                "version": receipt.version,
                "protocol": receipt.protocol,
                "workspaces": workspaces,
                "tabs": [
                    {"tab_id": receipt.tab_id, "workspace_id": receipt.workspace_id}
                ],
                "panes": [
                    {
                        "pane_id": receipt.pane_id,
                        "terminal_id": receipt.terminal_id,
                        "workspace_id": receipt.workspace_id,
                        "tab_id": receipt.tab_id,
                        "cwd": str(receipt.cwd),
                    }
                ],
                "layouts": [
                    {
                        "workspace_id": receipt.workspace_id,
                        "tab_id": receipt.tab_id,
                        "panes": [{"pane_id": receipt.pane_id}],
                    }
                ],
                "agents": [],
            },
        }


if __name__ == "__main__":
    unittest.main()
