from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agent_team.native_backend as native
from agent_team.contracts import (
    Attach,
    DeliveryAck,
    DeliveryRef,
    ErrorCode,
    Role,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleSpec,
    RoleWait,
    RuntimeFailure,
    StartSpec,
    Status,
)
from agent_team.tmux import CloseEvidence, TmuxInspection, TmuxReceipt, _PathIdentity


class FakeExecutables:
    node = Path("/bin/node")
    client = Path("/bin/acpx")
    agent = Path("/bin/claude-agent-acp")
    node_sha256 = "n" * 64
    client_sha256 = "c" * 64
    agent_sha256 = "a" * 64

    def verify(self) -> None:
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "node": str(self.node),
            "client": str(self.client),
            "agent": str(self.agent),
            "node_sha256": self.node_sha256,
            "client_sha256": self.client_sha256,
            "agent_sha256": self.agent_sha256,
        }


class FakePopen:
    def __init__(self, _argv: list[str], **_kwargs: object) -> None:
        self.pid = 77_001
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def wait(self, **_kwargs: object) -> int:
        return self.returncode


class FakeTmuxDriver:
    receipt = TmuxReceipt(
        executable=Path("/usr/bin/tmux"),
        socket_path=Path("/tmp/agent-team-test/s"),
        config_path=Path("/tmp/agent-team-test/config"),
        run_nonce="a" * 32,
        session_name="agent-team-main-aaaaaaaaaaaaaaaa",
        session_id="$1",
        window_id="@1",
        pane_id="%1",
        pane_pid=70_001,
        server_pid=70_002,
        socket_identity=_PathIdentity(1, 2, 0o600, os.getuid()),
        config_identity=_PathIdentity(1, 3, 0o600, os.getuid()),
    )

    def __init__(self, *_args: object) -> None:
        if len(_args) >= 4:
            self.receipt = replace(
                type(self).receipt,
                run_nonce=str(_args[2]),
                session_name=str(_args[3]),
            )
        else:
            self.receipt = type(self).receipt
        self.created: tuple[tuple[str, ...], Path, dict[str, str], str] | None = None
        self.closed = False

    @classmethod
    def from_receipt(cls, _receipt: TmuxReceipt) -> FakeTmuxDriver:
        return cls()

    def create(
        self,
        argv: tuple[str, ...],
        cwd: str | Path,
        env: dict[str, str],
        title: str,
    ) -> TmuxReceipt:
        self.created = (argv, Path(cwd), env, title)
        return self.receipt

    def inspect(self, _receipt: TmuxReceipt) -> TmuxInspection:
        return TmuxInspection(
            running=True,
            exit_status=None,
            identity_verified=True,
            pane_present=True,
            session_present=True,
            pane_pid=self.receipt.pane_pid,
            server_pid=self.receipt.server_pid,
            observed_nonce=self.receipt.run_nonce,
        )

    def attach_argv(self, _receipt: TmuxReceipt) -> tuple[str, ...]:
        return ("tmux", "-S", str(self.receipt.socket_path), "attach-session")

    def close(self, _receipt: TmuxReceipt) -> object:
        self.closed = True
        return mock.Mock(
            ownership_verified=True,
            session_terminated=True,
            server_terminated=True,
            socket_removed=True,
            evidence=CloseEvidence.SERVER_TERMINATED,
        )


def role_spec(
    role: Role,
    *,
    transport: str = "direct",
    provider: str = "claude",
    permission: str = "orchestrator",
    execution: str = "tui_direct",
    executables: dict[str, object] | None = None,
) -> RoleSpec:
    return RoleSpec(
        provider=provider,
        transport=transport,
        model="claude-test",
        effort="medium",
        permission=permission,
        instructions=f"{role.value} instructions",
        execution=execution,
        adapter_id="claude-acp-0.70.0" if transport == "acp" else None,
        acp_executables=executables,
    )


class NativeBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        make_directory = tempfile.mkdtemp
        socket_patch = mock.patch.object(
            native.tempfile,
            "mkdtemp",
            side_effect=lambda **kwargs: make_directory(
                **(
                    {**kwargs, "dir": str(self.root)}
                    if kwargs.get("dir") == "/tmp"
                    else kwargs
                )
            ),
        )
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_path = self.root / "state" / "state.json"
        self.config_path = self.root / "config.toml"
        self.launcher = self.root / "agent-team"
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        self.launcher.chmod(0o700)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def spec(self, *roles: Role) -> StartSpec:
        return StartSpec(
            team_id="agent-team-native-test",
            workspace=self.workspace,
            config_path=self.config_path,
            state_path=self.state_path,
            role_specs={role: role_spec(role) for role in (Role.MAIN, *roles)},
        )

    def backend(self, spec: StartSpec) -> native.TmuxBackend:
        del spec
        backend = native.TmuxBackend(
            tmux_executable="tmux",
            launcher_path=self.launcher,
        )
        return backend

    def start_backend(self, spec: StartSpec) -> native.TmuxBackend:
        backend = self.backend(spec)
        with (
            mock.patch.object(native, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
        ):
            backend.start(spec)
        return backend

    @contextmanager
    def planner_backend(self) -> Iterator[native.TmuxBackend]:
        executables = FakeExecutables()
        spec = replace(
            self.spec(),
            role_specs={
                Role.MAIN: role_spec(Role.MAIN),
                Role.PLANNER: role_spec(
                    Role.PLANNER,
                    transport="acp",
                    permission="read-only",
                    execution="background",
                    executables=executables.as_dict(),
                ),
            },
        )
        snapshot = {
            "adapter_id": "claude-acp-0.70.0",
            "revision": "acpx@0.13.2",
            "executable": str(executables.client),
            "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "c" * 64,
            },
        }
        with (
            mock.patch.object(
                native, "build_acp_agent_command", return_value="fixture-agent"
            ),
            mock.patch.object(native, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.acp_dependencies.AcpExecutables,
                "from_dict",
                return_value=executables,
            ),
            mock.patch.object(
                native.acp_dependencies, "adapter_snapshot", return_value=snapshot
            ),
        ):
            backend = self.backend(spec)
            backend.start(spec)
            yield backend

    def test_state_failure_before_spawn_reclaims_unpublished_resources(self) -> None:
        with self.planner_backend() as backend:
            with (
                mock.patch.object(
                    native,
                    "_save_state",
                    side_effect=RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE, "state save failed"
                    ),
                ),
                mock.patch.object(native.subprocess, "Popen") as popen,
                mock.patch.object(
                    backend, "_cleanup_unpersisted", wraps=backend._cleanup_unpersisted
                ) as cleanup,
                self.assertRaisesRegex(RuntimeFailure, "state save failed"),
            ):
                backend.request(RolePrompt(Role.PLANNER, "inspect"))
            popen.assert_not_called()
            cleanup.assert_called_once()
            for path in cleanup.call_args.args[:3]:
                self.assertFalse(path.exists(), str(path))
            self.assertEqual(native.runtime_read_state(self.state_path)["roles"], {})

    def test_unproven_runner_group_stops_only_the_known_process(self) -> None:
        for observed in (77_002, PermissionError("group unavailable")):
            with self.subTest(observed=type(observed).__name__):
                self.state_path = (
                    self.root / f"state-{type(observed).__name__}" / "state.json"
                )
                with self.planner_backend() as backend:
                    process = mock.Mock(pid=77_001)
                    process.poll.return_value = None
                    process.wait.return_value = 0
                    with (
                        mock.patch.object(
                            native.subprocess, "Popen", return_value=process
                        ),
                        mock.patch.object(
                            native.os,
                            "getpgid",
                            side_effect=observed
                            if isinstance(observed, Exception)
                            else None,
                            return_value=observed,
                        ),
                        mock.patch.object(native.os, "killpg") as killpg,
                        self.assertRaises(RuntimeFailure),
                    ):
                        backend.request(RolePrompt(Role.PLANNER, "inspect"))
                    process.terminate.assert_called_once()
                    process.wait.assert_called_once_with(
                        timeout=native.PROCESS_WAIT_SECONDS
                    )
                    killpg.assert_not_called()
                    saved = native.runtime_read_state(self.state_path)["roles"][
                        "planner"
                    ]
                    self.assertEqual(saved["runner_pid"], 77_001)
                    self.assertEqual(
                        saved["runner_process_group_id"],
                        observed if isinstance(observed, int) else None,
                    )
                    backend._cleanup_assignment(saved, self.state_path, Role.PLANNER)

    def test_published_but_not_durable_assignment_keeps_its_resources(self) -> None:
        def published_failure(
            path: Path,
            state: dict[str, object],
            *,
            require_existing: bool,
            reservation_held: bool,
        ) -> None:
            native.runtime_write_state(
                path,
                state,
                require_existing=require_existing,
                reservation_held=reservation_held,
            )
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "durability unknown"
            ) from native.StatePublishError("published")

        with self.planner_backend() as backend:
            with (
                mock.patch.object(native, "_save_state", side_effect=published_failure),
                mock.patch.object(native.subprocess, "Popen") as popen,
                self.assertRaisesRegex(RuntimeFailure, "durability unknown"),
            ):
                backend.request(RolePrompt(Role.PLANNER, "inspect"))
            popen.assert_not_called()
            saved = native.runtime_read_state(self.state_path)["roles"]["planner"]
            for key in ("prompt_path", "provider_private_root", "snapshot_root"):
                self.assertTrue(Path(saved[key]).exists())
            backend._cleanup_assignment(saved, self.state_path, Role.PLANNER)

    def test_cancel_does_not_signal_a_reused_runner_group(self) -> None:
        backend = self.backend(self.spec())
        backend._runners["planner"] = FakePopen([])
        assignment = {
            "runner_pid": 77_001,
            "runner_process_group_id": 77_001,
            "runner_argv": ["/usr/bin/python3", "expected"],
        }
        state = {"state_path": str(self.state_path), "roles": {"planner": assignment}}
        with (
            mock.patch.object(native, "_validate_runner_argv"),
            mock.patch.object(native, "_process_group_alive", return_value=True),
            mock.patch.object(native.os, "getpgid", return_value=77_001),
            mock.patch.object(native, "read_process_argv", return_value=("foreign",)),
            mock.patch.object(native, "_terminate_process_group") as terminate,
            self.assertRaises(RuntimeFailure),
        ):
            backend._cancel_runner(state, Role.PLANNER)
        terminate.assert_not_called()

    def test_unsupported_selected_role_is_rejected_before_state_creation(self) -> None:
        spec = self.spec(Role.WORKER)
        backend = self.backend(spec)
        with (
            mock.patch.object(native, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            self.assertRaises(RuntimeFailure),
        ):
            backend.start(spec)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(backend.last_start_response, None)

    def test_start_publishes_native_state_and_frozen_main_supervisor(self) -> None:
        spec = self.spec()
        backend = self.backend(spec)
        with (
            mock.patch.object(native, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
        ):
            result = backend.start(spec)
        self.assertEqual(result.team_id, spec.team_id)
        state = native.runtime_read_state(self.state_path)
        self.assertEqual(state["runtime"], "tmux")
        self.assertNotIn("orca_socket", state)
        self.assertNotIn("worktree_id", state)
        native_state = state["native"]
        self.assertIsInstance(native_state, dict)
        assert isinstance(native_state, dict)
        self.assertEqual(native_state["phase"], "running")
        main_argv = native_state["main_argv"]
        self.assertIsInstance(main_argv, list)
        assert isinstance(main_argv, list)
        self.assertTrue(Path(main_argv[0]).is_absolute())
        driver = backend._driver
        assert isinstance(driver, FakeTmuxDriver) and driver.created is not None
        _argv, child_cwd, child_env, _title = driver.created
        self.assertEqual(child_cwd, native.PACKAGE_ROOT)
        self.assertNotIn("PYTHONPATH", child_env)
        source_probe = subprocess.run(
            [sys.executable, "-S", "-m", "agent_team", "--help"],
            cwd=child_cwd,
            env={"PATH": os.defpath},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(source_probe.returncode, 0, source_probe.stderr)

    def test_starting_without_receipt_can_resume_status_and_retains_unknown_effect(
        self,
    ) -> None:
        spec = self.spec()
        self.start_backend(spec)
        state = native.runtime_read_state(self.state_path)
        state["native"].pop("tmux_receipt")
        state["native"]["phase"] = "starting"
        native.runtime_write_state(self.state_path, state)
        resumed = native.TmuxBackend(launcher_path=self.launcher, resume_existing=True)
        resumed.start(spec)
        status = resumed.request(Status())
        self.assertEqual(status.status, "starting")
        with self.assertRaisesRegex(RuntimeFailure, "startup is unresolved"):
            resumed.stop()
        self.assertTrue(self.state_path.exists())
        self.assertEqual(
            native.runtime_read_state(self.state_path)["native"]["startup_socket_path"],
            state["native"]["startup_socket_path"],
        )

    def test_runner_with_unreadable_argv_is_not_owned(self) -> None:
        assignment = {
            "runner_pid": 77_001,
            "runner_process_group_id": 77_001,
            "runner_argv": ["/bin/python3", "-m", "agent_team"],
        }
        with (
            mock.patch.object(native, "_process_group_alive", return_value=True),
            mock.patch.object(native.os, "getpgid", return_value=77_001),
            mock.patch.object(native, "read_process_argv", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeFailure, "ownership is unproven"):
                native._runner_identity_is_owned(assignment)
            self.assertEqual(
                native.TmuxBackend._saved_runner_status(assignment), "unknown"
            )
        with mock.patch.object(native, "_process_group_alive", return_value=False):
            self.assertEqual(
                native.TmuxBackend._saved_runner_status(assignment), "exited"
            )

    def test_acp_completion_observes_read_release_ack_in_durable_order(self) -> None:
        executables = FakeExecutables()
        planner = role_spec(
            Role.PLANNER,
            transport="acp",
            permission="read-only",
            execution="background",
            executables=executables.as_dict(),
        )
        spec = StartSpec(
            team_id="agent-team-native-test",
            workspace=self.workspace,
            config_path=self.config_path,
            state_path=self.state_path,
            role_specs={Role.MAIN: role_spec(Role.MAIN), Role.PLANNER: planner},
        )
        backend = self.backend(spec)
        snapshot = {
            "adapter_id": "claude-acp-0.70.0",
            "revision": "acpx@0.13.2",
            "executable": str(executables.client),
            "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "c" * 64,
            },
        }
        with (
            mock.patch.object(native, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.acp_dependencies.AcpExecutables,
                "from_dict",
                return_value=executables,
            ),
            mock.patch.object(
                native.acp_dependencies, "adapter_snapshot", return_value=snapshot
            ),
        ):
            backend.start(spec)
            with (
                mock.patch.object(native.subprocess, "Popen", FakePopen),
                mock.patch.object(native.os, "getpgid", return_value=77_001),
                mock.patch.object(
                    native,
                    "build_acp_agent_command",
                    return_value="env AGENT_TEAM_ACP_MARKER=test /bin/node /bin/claude-agent-acp",
                ),
            ):
                assignment = backend.request(RolePrompt(Role.PLANNER, "inspect"))
        self.assertEqual(assignment.role, Role.PLANNER)
        state = native.runtime_read_state(self.state_path)
        roles = state["roles"]
        assert isinstance(roles, dict)
        saved = roles["planner"]
        assert isinstance(saved, dict)
        with self.assertRaisesRegex(RuntimeFailure, "publisher ownership"):
            native.publish_completion(
                self.state_path,
                role="planner",
                run_id=state["run_id"],
                task_id=saved["task_id"],
                dispatch_id=saved["dispatch_id"],
                terminal_handle=saved["terminal_handle"],
                launch_nonce=saved["launch_nonce"],
                outcome="succeeded",
                body="verified output\nsecond line",
                cleanup_confirmed=True,
            )
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_completion(
                self.state_path,
                role="planner",
                run_id=state["run_id"],
                task_id=saved["task_id"],
                dispatch_id=saved["dispatch_id"],
                terminal_handle=saved["terminal_handle"],
                launch_nonce=saved["launch_nonce"],
                outcome="succeeded",
                body="verified output\nsecond line",
                cleanup_confirmed=True,
            )
        stopped_state = native.runtime_read_state(self.state_path)
        stopped_state["native"]["phase"] = "stopping"
        native.runtime_write_state(self.state_path, stopped_state)
        before = self.state_path.read_bytes()
        for request in (
            RoleWait(Role.PLANNER, 1),
            RoleRead(Role.PLANNER, 20),
            RoleRelease(Role.PLANNER),
            DeliveryAck(DeliveryRef("unobserved")),
        ):
            with self.subTest(operation=type(request).__name__):
                with self.assertRaises(RuntimeFailure) as raised:
                    backend.request(request)
                self.assertEqual(raised.exception.code, ErrorCode.BUSY)
                self.assertEqual(self.state_path.read_bytes(), before)
        stopped_state["native"]["phase"] = "running"
        native.runtime_write_state(self.state_path, stopped_state)
        reload_state = backend._reload_locked

        def competing_wait(path: Path) -> dict[str, object]:
            current = reload_state(path)
            current[native.PENDING_DELIVERY_ID] = "already-observed"
            return current

        with (
            mock.patch.object(backend, "_reload_locked", side_effect=competing_wait),
            self.assertRaises(RuntimeFailure) as raised,
        ):
            backend.request(RoleWait(Role.PLANNER, 1_000))
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        waited = backend.request(RoleWait(Role.PLANNER, 1_000))
        self.assertEqual(len(waited.events), 1)
        read = backend.request(RoleRead(Role.PLANNER, 1))
        self.assertEqual(read.output, "verified output")
        runner = backend._runners["planner"]
        with (
            mock.patch.object(runner, "poll", return_value=None),
            mock.patch.object(runner, "wait", return_value=0) as wait,
        ):
            backend.request(RoleRelease(Role.PLANNER))
        wait.assert_called_once_with(timeout=native.PROCESS_WAIT_SECONDS)
        delivery = waited.delivery_id
        assert delivery is not None
        backend.request(DeliveryAck(delivery))
        final_state = native.runtime_read_state(self.state_path)
        self.assertEqual(final_state["roles"], {})
        self.assertNotIn("native_result", final_state)
        self.assertEqual(final_state["native"]["last_ack"], delivery._value)

    def test_acp_role_cannot_be_attached_as_a_tmux_pane(self) -> None:
        executables = FakeExecutables()
        spec = StartSpec(
            team_id="agent-team-native-test",
            workspace=self.workspace,
            config_path=self.config_path,
            state_path=self.state_path,
            role_specs={
                Role.MAIN: role_spec(Role.MAIN),
                Role.PLANNER: role_spec(
                    Role.PLANNER,
                    transport="acp",
                    permission="read-only",
                    execution="background",
                    executables=executables.as_dict(),
                ),
            },
        )
        backend = self.backend(spec)
        snapshot = {
            "adapter_id": "claude-acp-0.70.0",
            "revision": "acpx@0.13.2",
            "executable": str(executables.client),
            "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "c" * 64,
            },
        }
        with (
            mock.patch.object(native, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.acp_dependencies.AcpExecutables,
                "from_dict",
                return_value=executables,
            ),
            mock.patch.object(
                native.acp_dependencies, "adapter_snapshot", return_value=snapshot
            ),
        ):
            backend.start(spec)
        with self.assertRaises(RuntimeFailure) as raised:
            backend.request(Attach(Role.PLANNER))
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertIn("no TTY", str(raised.exception))

    def test_stop_does_not_infer_supervisor_cleanup_from_missing_receipt(self) -> None:
        backend = self.start_backend(self.spec())
        with (
            mock.patch.object(native, "PROCESS_WAIT_SECONDS", 0.01),
            mock.patch.object(native, "PROCESS_POLL_SECONDS", 0),
            self.assertRaises(RuntimeFailure) as raised,
        ):
            backend.stop()
        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        state = native.runtime_read_state(self.state_path)
        native_state = state["native"]
        assert isinstance(native_state, dict)
        self.assertEqual(native_state["phase"], "running")
        driver = backend._driver
        assert isinstance(driver, FakeTmuxDriver)
        self.assertFalse(driver.closed)

    def test_stop_accepts_same_supervisor_cleanup_published_during_role_cancel(
        self,
    ) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = native._receipt_from_state(state["native"])
        running = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "supervisor-nonce",
            "phase": "running",
        }
        state["native"]["main_process"] = {
            **running,
            "phase": "exited",
            "group_stopped": True,
        }
        native.runtime_write_state(self.state_path, state)
        with mock.patch.object(native.os, "kill") as kill:
            backend._stop_supervisor(state, receipt, running)
            kill.assert_not_called()
        state["native"]["main_process"]["launch_nonce"] = "different-supervisor"
        native.runtime_write_state(self.state_path, state)
        with (
            mock.patch.object(native.os, "kill") as kill,
            self.assertRaisesRegex(RuntimeFailure, "identity changed"),
        ):
            backend._stop_supervisor(state, receipt, running)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
