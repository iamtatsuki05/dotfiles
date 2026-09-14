from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from unittest import mock

from test_named_orca_state import _graph, _role_spec, _state
from test_orca_backend import FakeOrcaClient, existing_state, start_spec

from agent_team import backend as backend_module
from agent_team import orca as orca_module
from agent_team.adapters import ProcessResult
from agent_team.backend import OrcaBackend
from agent_team.cleanup import StartupCleanup
from agent_team.contracts import RoleSpec, RuntimeFailure, Status
from agent_team.runtime import (
    RuntimeValidationError,
    StatePublishError,
    read_state,
    write_state,
)


def _mcp_catalog(state_path: Path) -> tuple[set[str], list[str]]:
    request = (
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            separators=(",", ":"),
        )
        + "\n"
    )
    package_root = Path(__import__("agent_team").__file__).resolve().parent.parent
    environment = dict(
        os.environ,
        PYTHONPATH=str(package_root),
        AGENT_TEAM_STATE_PATH=str(state_path),
    )
    result = subprocess.run(
        [sys.executable, "-m", "agent_team", "_mcp-server"],
        input=request,
        text=True,
        capture_output=True,
        cwd=package_root,
        env=environment,
        check=False,
    )
    response = json.loads(result.stdout.splitlines()[-1])
    if result.returncode != 0 or "error" in response:
        raise AssertionError(response)
    tools = response["result"]["tools"]
    role_enums = {
        value
        for tool in tools
        if isinstance(tool, dict)
        for schema in [tool.get("inputSchema")]
        if isinstance(schema, dict)
        for properties in [schema.get("properties")]
        if isinstance(properties, dict)
        for role_schema in [properties.get("role")]
        if isinstance(role_schema, dict)
        for value in role_schema.get("enum", [])
        if isinstance(value, str)
    }
    return role_enums, [tool["name"] for tool in tools]


class _CatalogRunner:
    def __init__(self, state_path: Path, expected_roles: set[str]) -> None:
        self.state_path = state_path
        self.expected_roles = expected_roles
        self.catalog_at_send: tuple[set[str], list[str]] | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 900.0,
    ) -> ProcessResult:
        del cwd, env, input_text, timeout_seconds
        if tuple(argv[1:3]) != ("terminal", "send"):
            raise AssertionError(argv)
        self.catalog_at_send = _mcp_catalog(self.state_path)
        self.assert_catalog()
        text = argv[argv.index("--text") + 1]
        payload = {
            "ok": True,
            "result": {
                "send": {
                    "handle": "term-main",
                    "accepted": True,
                    "bytesWritten": len((text + "\r").encode("utf-8")),
                }
            },
        }
        return ProcessResult(0, json.dumps(payload), "")

    def assert_catalog(self) -> None:
        assert self.catalog_at_send is not None
        roles, tools = self.catalog_at_send
        if roles != self.expected_roles:
            raise AssertionError((roles, self.expected_roles))
        if len(tools) != 10:
            raise AssertionError(tools)


class OrcaTerminalSendContractTest(unittest.TestCase):
    def test_terminal_send_checks_handle_acceptance_and_utf8_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            state_path = spec.state_path
            write_state(state_path, existing_state(spec))
            runner = _CatalogRunner(state_path, {"planner", "worker", "reviewer"})
            client = orca_module.OrcaClient(runner=runner)
            with mock.patch.dict(
                os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}
            ):
                verdict = client.terminal_send(
                    terminal_id="term-main", text="echo 日本語", cwd=Path(directory)
                )
        self.assertEqual(verdict.handle, "term-main")
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.bytes_written, len("echo 日本語\r".encode()))

    def test_terminal_create_none_omits_command_flag(self) -> None:
        class Runner:
            def __init__(self) -> None:
                self.argv: tuple[str, ...] | None = None

            def run(self, argv, **_kwargs):
                self.argv = tuple(argv)
                return ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "terminal": {
                                    "handle": "term-main",
                                    "worktreeId": "repo::workspace",
                                }
                            },
                        }
                    ),
                    "",
                )

        runner = Runner()
        client = orca_module.OrcaClient(runner=runner)
        client.terminal_create(
            worktree_id="repo::workspace",
            title="team-main",
            command=None,
            cwd=Path("/tmp"),
        )
        assert runner.argv is not None
        self.assertNotIn("--command", runner.argv)

    def test_terminal_send_rejects_boolean_bytes_written(self) -> None:
        class Runner:
            def run(self, _argv, **_kwargs):
                return ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "send": {
                                    "handle": "term-main",
                                    "accepted": True,
                                    "bytesWritten": True,
                                }
                            },
                        }
                    ),
                    "",
                )

        client = orca_module.OrcaClient(runner=Runner())
        with self.assertRaises(orca_module.OrcaProtocolError):
            client.terminal_send(terminal_id="term-main", text="echo", cwd=Path("/tmp"))


class _StartupFake(FakeOrcaClient):
    def __init__(self, state_path: Path, *, wait_error: Exception | None = None):
        super().__init__()
        self.state_path = state_path
        self.wait_error = wait_error
        self.send_error: Exception | None = None
        self.send_phases: list[str] = []
        self.send_markers: list[dict[str, object]] = []
        self.send_calls = 0
        self.created_command: str | None = "unset"

    def terminal_create(
        self, *, worktree_id: str, title: str, command: str | None, cwd: Path
    ) -> dict[str, object]:
        self.created_command = command
        return super().terminal_create(
            worktree_id=worktree_id, title=title, command=command, cwd=cwd
        )

    def terminal_send(self, *, terminal_id: str, text: str, cwd: Path):
        del cwd
        self.send_calls += 1
        self.calls.append(("terminal-send", terminal_id))
        state = read_state(self.state_path)
        marker = state.get("pending_role_start")
        if not isinstance(marker, dict):
            raise TypeError(state)
        self.send_phases.append(str(marker.get("phase")))
        self.send_markers.append(dict(marker))
        if self.send_error is not None:
            raise self.send_error
        return orca_module.TerminalSendVerdict(
            handle=terminal_id,
            accepted=True,
            bytes_written=len((text + "\r").encode("utf-8")),
        )

    def terminal_wait(self, *, terminal_id: str, cwd: Path) -> None:
        state = read_state(self.state_path)
        marker = state.get("pending_role_start")
        if not isinstance(marker, dict) or marker.get("phase") != "main_sent":
            raise AssertionError(marker)
        if self.wait_error is not None:
            raise self.wait_error
        super().terminal_wait(terminal_id=terminal_id, cwd=cwd)


class OrcaMainStartupTest(unittest.TestCase):
    def _backend(self, root: Path, client: _StartupFake):
        backend = OrcaBackend(
            client,
            launcher_path=Path("/tmp/agent-team"),
            main_command_factory=lambda _socket: "claude --mcp-config 日本語",
            user_data_path=root,
        )
        return backend

    def test_backend_start_exposes_real_mcp_catalog_at_send(self):
        for named in (False, True):
            with self.subTest(named=named), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = start_spec(root)
                spec.workspace.mkdir()
                expected_roles = {"planner", "worker", "reviewer"}
                if named:
                    graph = _graph()
                    role_specs = {}
                    for node in graph.nodes:
                        raw = _role_spec(node.kind.value)
                        raw.pop("kind")
                        role_specs[node] = RoleSpec(**raw)
                    spec = replace(
                        spec, graph=graph, role_specs=role_specs, max_review_rounds=2
                    )
                    expected_roles = {"worker-a", "reviewer-a"}
                client = _StartupFake(spec.state_path)
                client.run_create_result = "run_1"
                original_send = client.terminal_send

                def send_with_catalog(
                    *,
                    state_path=spec.state_path,
                    roles=expected_roles,
                    send=original_send,
                    **kwargs,
                ):
                    role_enums, tools = _mcp_catalog(state_path)
                    self.assertEqual(role_enums, roles)
                    self.assertEqual(len(tools), 10)
                    return send(**kwargs)

                client.terminal_send = send_with_catalog
                with (
                    mock.patch.object(
                        OrcaBackend,
                        "_ensure_orca_ready",
                        return_value=("repo::project", root / "orca.sock"),
                    ),
                    mock.patch.object(backend_module, "preflight_scoped_role"),
                ):
                    self._backend(root, client).start(spec)
                self.assertEqual(client.send_calls, 1)

    def test_main_command_failure_does_not_prepare_or_create_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            client = _StartupFake(spec.state_path)
            prepare = mock.Mock()
            backend = OrcaBackend(
                client,
                main_command_factory=mock.Mock(side_effect=ValueError("bad command")),
                prepare_start=prepare,
            )
            with (
                mock.patch.object(
                    OrcaBackend,
                    "_ensure_orca_ready",
                    return_value=("repo::project", root / "orca.sock"),
                ),
                self.assertRaises(RuntimeFailure),
            ):
                backend.start(spec)
            prepare.assert_not_called()
            self.assertEqual(client.calls, [])
            self.assertFalse(spec.state_path.parent.exists())

    def test_state_is_published_before_send_and_marker_is_removed_after_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            (root / "orca-runtime.json").write_text(
                json.dumps(
                    {
                        "transports": [
                            {"kind": "unix", "endpoint": str(root / "orca.sock")}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            client = _StartupFake(spec.state_path)
            client.run_create_result = "run_1"
            backend = self._backend(root, client)
            with mock.patch.object(
                OrcaBackend,
                "_ensure_orca_ready",
                return_value=("repo::project", root / "orca.sock"),
            ):
                backend.start(spec)
            self.assertIsNone(client.created_command)
            self.assertEqual(client.send_calls, 1)
            self.assertEqual(client.send_phases, ["main_send_started"])
            self.assertEqual(client.send_markers[0]["run_id"], "run_1")
            self.assertEqual(client.send_markers[0]["main_terminal"], "term_main")
            self.assertEqual(len(client.send_markers[0]["command_sha256"]), 64)
            final_state = read_state(spec.state_path)
            self.assertNotIn("pending_role_start", final_state)
            names = [call[0] for call in client.calls]
            self.assertLess(names.index("run-create"), names.index("terminal-send"))
            self.assertGreater(names.index("terminal-wait"), names.index("run-create"))

    def test_send_unknown_retains_main_marker_and_does_not_close_or_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            client = _StartupFake(spec.state_path)
            client.run_create_result = "run_1"
            client.send_error = orca_module.OrcaTransportError()
            backend = self._backend(root, client)
            with (
                mock.patch.object(
                    OrcaBackend,
                    "_ensure_orca_ready",
                    return_value=("repo::project", root / "orca.sock"),
                ),
                self.assertRaises(RuntimeFailure),
            ):
                backend.start(spec)
            state = read_state(spec.state_path)
            self.assertEqual(state["pending_role_start"]["phase"], "main_send_started")
            self.assertTrue(
                (spec.state_path.parent / ".startup-recovery.json").exists()
            )
            self.assertNotIn(("terminal-close", "term_main"), client.calls)
            retry_client = _StartupFake(spec.state_path)
            retry = OrcaBackend(
                retry_client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda _socket: "claude --mcp-config 日本語",
                resume_existing=True,
                user_data_path=root,
            )
            retry.start(spec)  # resume existing; state remains inspectable
            self.assertEqual(retry.request(Status()).status, "cleanup_pending")
            self.assertEqual(retry_client.send_calls, 0)

    def test_uncertain_send_retains_prepared_ownership_on_duplicate_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            local_path = spec.state_path.parent / "codex-home"
            cleanup = mock.Mock()

            def prepare():
                local_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                local_path.mkdir(mode=0o700)
                return StartupCleanup(((str(local_path), False, "dir"),), cleanup)

            client = _StartupFake(spec.state_path)
            client.run_create_result = "run_1"
            client.send_error = orca_module.OrcaTransportError()
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda _: "selected-main",
                prepare_start=prepare,
            )
            with (
                mock.patch.object(
                    OrcaBackend,
                    "_ensure_orca_ready",
                    return_value=("repo::project", root / "orca.sock"),
                ),
                self.assertRaises(RuntimeFailure) as raised,
            ):
                backend.start(spec)
            recovery_path = spec.state_path.parent / ".startup-recovery.json"
            self.assertEqual(client.send_calls, 1, str(raised.exception))
            recovery = json.loads(recovery_path.read_text())
            self.assertEqual(
                recovery["local_tracked"],
                [{"path": str(local_path), "existed": False, "kind": "dir"}],
            )
            for resume in (False, True):
                retry = OrcaBackend(client, resume_existing=resume)
                if resume:
                    retry.start(spec)
                    self.assertEqual(retry.request(Status()).status, "cleanup_pending")
                else:
                    with self.assertRaises(RuntimeFailure):
                        retry.start(spec)
                self.assertEqual(json.loads(recovery_path.read_text()), recovery)
            self.assertEqual(client.send_calls, 1)
            cleanup.assert_not_called()
            self.assertTrue(local_path.is_dir())
            self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_wait_failure_retains_main_sent_marker_without_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            client = _StartupFake(
                spec.state_path, wait_error=orca_module.OrcaTransportError()
            )
            client.run_create_result = "run_1"
            backend = self._backend(root, client)
            with (
                mock.patch.object(
                    OrcaBackend,
                    "_ensure_orca_ready",
                    return_value=("repo::project", root / "orca.sock"),
                ),
                self.assertRaises(RuntimeFailure),
            ):
                backend.start(spec)
            self.assertEqual(
                read_state(spec.state_path)["pending_role_start"]["phase"], "main_sent"
            )
            self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_marker_clear_save_failure_retains_main_marker_without_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            client = _StartupFake(spec.state_path)
            client.run_create_result = "run_1"
            backend = self._backend(root, client)
            original_write_state = backend_module.write_state
            write_calls = 0

            def fail_marker_clear(
                path: Path, state: dict[str, object], **kwargs: object
            ) -> None:
                nonlocal write_calls
                write_calls += 1
                if write_calls == 4:
                    raise RuntimeValidationError("marker clear save failed")
                original_write_state(path, state, **kwargs)

            with (
                mock.patch.object(
                    OrcaBackend,
                    "_ensure_orca_ready",
                    return_value=("repo::project", root / "orca.sock"),
                ),
                mock.patch.object(
                    backend_module, "write_state", side_effect=fail_marker_clear
                ),
                self.assertRaises(RuntimeFailure),
            ):
                backend.start(spec)
            self.assertEqual(
                read_state(spec.state_path)["pending_role_start"]["phase"], "main_sent"
            )
            self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_published_marker_clear_failure_retains_existing_durability_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            client = _StartupFake(spec.state_path)
            client.run_create_result = "run_1"
            original_write = backend_module.write_state

            def fail_after_clear_publication(path, state, **kwargs):
                original_write(path, state, **kwargs)
                if "pending_role_start" not in state:
                    raise StatePublishError("directory durability unknown")

            with (
                mock.patch.object(
                    OrcaBackend,
                    "_ensure_orca_ready",
                    return_value=("repo::project", root / "orca.sock"),
                ),
                mock.patch.object(
                    backend_module,
                    "write_state",
                    side_effect=fail_after_clear_publication,
                ),
                self.assertRaises(RuntimeFailure),
            ):
                self._backend(root, client).start(spec)
            self.assertNotIn("pending_role_start", read_state(spec.state_path))
            recovery_path = spec.state_path.parent / ".startup-recovery.json"
            recovery = json.loads(recovery_path.read_text())
            self.assertEqual(recovery["phase"], "state_published")
            self.assertTrue(recovery["state_published"])
            self.assertEqual(client.send_calls, 1)
            self.assertNotIn(("terminal-close", "term_main"), client.calls)
            retry = OrcaBackend(client, resume_existing=True)
            retry.start(spec)
            self.assertFalse(recovery_path.exists())
            self.assertEqual(retry.request(Status()).status, "running")
            self.assertEqual(client.send_calls, 1)

    def test_failure_before_state_publication_never_sends_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = start_spec(root)
            spec.workspace.mkdir()
            client = _StartupFake(spec.state_path)
            client.run_create_result = orca_module.OrcaCommandError("run-create")
            backend = self._backend(root, client)
            with (
                mock.patch.object(
                    OrcaBackend,
                    "_ensure_orca_ready",
                    return_value=("repo::project", root / "orca.sock"),
                ),
                self.assertRaises(RuntimeFailure),
            ):
                backend.start(spec)
            self.assertEqual(client.send_calls, 0)
            self.assertFalse(spec.state_path.exists())


class NamedCatalogAtSendTest(unittest.TestCase):
    def test_real_mcp_catalog_at_terminal_send_uses_exact_named_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            state_path = Path(str(state["state_path"]))
            write_state(state_path, state)
            runner = _CatalogRunner(state_path, {"worker-a", "reviewer-a"})
            client = orca_module.OrcaClient(runner=runner)
            with mock.patch.dict(
                os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}
            ):
                client.terminal_send(
                    terminal_id="term-main", text="claude --mcp-config named", cwd=root
                )
            assert runner.catalog_at_send is not None
            self.assertEqual(runner.catalog_at_send[0], {"worker-a", "reviewer-a"})


if __name__ == "__main__":
    unittest.main()
