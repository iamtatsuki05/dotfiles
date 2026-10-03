from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

from test_native_backend import FakePopen as _FakePopen
from test_native_backend import FakeTmuxDriver as _FakeTmuxDriver

import agent_team.native_backend as native
from agent_team import copilot_acp, tmux_backend
from agent_team.contracts import (
    ErrorCode,
    Role,
    RolePrompt,
    RoleSpec,
    RuntimeFailure,
    StartSpec,
    TaskDispatch,
)
from agent_team.native_acp_dependencies import (
    CopilotAcpExecutables,
    NativeAcpDependencyError,
)
from agent_team.scoped_acp import (
    COPILOT_SCOPED_ADAPTER_ID,
    SCOPED_CLIENT,
    SCOPED_POLICY,
    checked_digest,
)
from agent_team.task_spec import TaskSpec, VerificationSpec


class CopilotNativeBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="agent-team-copilot-native-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_path = self.root / "state" / "state.json"
        self.launcher = self.root / "agent-team"
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.executables = CopilotAcpExecutables(
            node=Path("/fixture/node"),
            loader=Path("/fixture/node_modules/@github/copilot/npm-loader.js"),
            copilot=Path("/fixture/copilot"),
            sdk=Path("/fixture/sdk/dist/acp.js"),
            node_sha256="n" * 64,
            copilot_sha256="c" * 64,
            sdk_sha256="s" * 64,
            package_manifest_sha256="m" * 64,
            platform_manifest_sha256="p" * 64,
            sdk_manifest_sha256="d" * 64,
        )
        self.adapter_snapshot = {
            "adapter_id": "copilot-acp-1.0.91",
            "revision": "@agentclientprotocol/sdk@1.4.0",
            "executable": str(self.executables.copilot),
            "version": "@github/copilot@1.0.91",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": self.executables.copilot_sha256,
            },
        }
        self.provider_roots: list[Path] = []
        make_directory = cast(Callable[..., str], tempfile.mkdtemp)

        def make_fixture_directory(**kwargs: object) -> str:
            if kwargs.get("dir") == "/tmp":
                kwargs["dir"] = str(self.root)
            directory = make_directory(**kwargs)
            if kwargs.get("prefix") == "agent-team-provider-":
                self.provider_roots.append(Path(directory).resolve())
            if kwargs.get("prefix") in {"agent-team-provider-", "agent-team-snapshot-"}:
                self.addCleanup(native.remove_owned_tree, Path(directory))
            return directory

        self.mkdtemp = self.enterContext(
            mock.patch.object(
                native.tempfile, "mkdtemp", side_effect=make_fixture_directory
            )
        )

    def _role_spec(self, role: Role) -> RoleSpec:
        return RoleSpec(
            provider="copilot",
            transport="acp",
            model="gpt-5.2",
            effort="high",
            permission="workspace-write" if role is Role.WORKER else "read-only",
            instructions=f"{role.value} instructions",
            execution="background",
            adapter_id=COPILOT_SCOPED_ADAPTER_ID,
            acp_executables=self.executables.as_dict(),
        )

    def _spec(self, *, task_specs: tuple[TaskSpec, ...] = ()) -> StartSpec:
        return StartSpec(
            team_id="agent-team-copilot-native-test",
            workspace=self.workspace,
            config_path=self.root / "config.toml",
            state_path=self.state_path,
            role_specs={
                Role.MAIN: RoleSpec(
                    provider="claude",
                    transport="direct",
                    model="claude-test",
                    effort="medium",
                    permission="orchestrator",
                    instructions="main instructions",
                    execution="tui_direct",
                ),
                Role.WORKER: self._role_spec(Role.WORKER),
                Role.REVIEWER: self._role_spec(Role.REVIEWER),
            },
            max_review_rounds=2 if task_specs else None,
            task_specs=task_specs,
        )

    def _task(self) -> TaskSpec:
        return TaskSpec(
            task_id="copilot-task",
            objective="Update one fixture",
            acceptance_criteria=("The fixture remains valid",),
            allowed_paths=("allowed.txt",),
            forbidden_paths=("private/",),
            dependencies=(),
            verification=(VerificationSpec("check", ("python3", "-V"), 30),),
            evidence_requirements=("check output",),
            consultation_conditions=(),
        )

    @contextmanager
    def _native_fakes(self, verify: mock.Mock | None = None) -> Iterator[None]:
        with (
            mock.patch.object(tmux_backend, "TmuxDriver", _FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.native_acp_dependencies.CopilotAcpExecutables,
                "from_dict",
                return_value=self.executables,
            ),
            mock.patch.object(
                native.native_acp_dependencies.CopilotAcpExecutables,
                "verify",
                verify or mock.Mock(return_value=None),
            ),
            mock.patch.object(
                native.native_acp_dependencies,
                "copilot_adapter_snapshot",
                return_value=self.adapter_snapshot,
            ),
        ):
            yield

    def _start(self, spec: StartSpec) -> tmux_backend.TmuxBackend:
        backend = tmux_backend.TmuxBackend(
            tmux_executable="tmux", launcher_path=self.launcher
        )
        backend.start(spec)
        return backend

    def test_start_records_only_the_client_and_policy_digests(self) -> None:
        with self._native_fakes():
            self._start(self._spec())

        saved = native.runtime_read_state(self.state_path)["role_specs"]
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                spec = saved[role]
                self.assertEqual(spec["adapter_id"], COPILOT_SCOPED_ADAPTER_ID)
                self.assertEqual(spec["acp_executables"], self.executables.as_dict())
                self.assertEqual(
                    spec["scoped_client_sha256"], checked_digest(SCOPED_CLIENT)
                )
                self.assertEqual(
                    spec["scoped_policy_sha256"], checked_digest(SCOPED_POLICY)
                )
                self.assertNotIn("scoped_wrapper_sha256", spec)
                self.assertNotIn("scoped_question_client_sha256", spec)
                self.assertNotIn("provider_snapshot", spec)

    def test_start_rejects_binding_drift_before_state_or_terminal(self) -> None:
        verify = mock.Mock(
            side_effect=NativeAcpDependencyError(
                "selected native Copilot ACP copilot changed"
            )
        )
        backend = tmux_backend.TmuxBackend(
            tmux_executable="tmux", launcher_path=self.launcher
        )
        with (
            self._native_fakes(verify=verify),
            mock.patch.object(native, "_save_state") as save_state,
            mock.patch.object(_FakeTmuxDriver, "create") as create,
            self.assertRaisesRegex(RuntimeFailure, "ACP dependencies are unavailable"),
        ):
            backend.start(self._spec())
        save_state.assert_not_called()
        create.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_start_rejects_an_invalid_model_before_state_or_terminal(self) -> None:
        spec = self._spec()
        role_specs = dict(spec.role_specs)
        role_specs[Role.WORKER] = replace(role_specs[Role.WORKER], model="auto")
        backend = tmux_backend.TmuxBackend(
            tmux_executable="tmux", launcher_path=self.launcher
        )
        with (
            self._native_fakes(),
            mock.patch.object(native, "_save_state") as save_state,
            mock.patch.object(_FakeTmuxDriver, "create") as create,
            self.assertRaisesRegex(RuntimeFailure, "Copilot ACP model is invalid"),
        ):
            backend.start(replace(spec, role_specs=role_specs))
        save_state.assert_not_called()
        create.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_dispatch_rejects_binding_drift_before_the_private_root(self) -> None:
        verify = mock.Mock(return_value=None)
        with self._native_fakes(verify=verify):
            backend = self._start(self._spec())
            before = self.state_path.read_bytes()
            verify.side_effect = NativeAcpDependencyError(
                "selected native Copilot ACP Node changed"
            )
            with (
                mock.patch.object(native, "_save_state") as save_state,
                mock.patch.object(native.subprocess, "Popen") as popen,
                mock.patch.object(copilot_acp, "prepare_assignment") as prepare,
                self.assertRaises(RuntimeFailure) as raised,
            ):
                backend.request(RolePrompt(Role.REVIEWER, "review the fixture"))

        save_state.assert_not_called()
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertIn(
            "selected ACP dependencies are unavailable", str(raised.exception)
        )
        prepare.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(self.provider_roots, [])
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_dispatch_rejects_runtime_digest_drift_before_the_private_root(
        self,
    ) -> None:
        with self._native_fakes():
            backend = self._start(self._spec())
            before = self.state_path.read_bytes()
            real_digest = native.checked_digest

            def drifted(path: Path, **kwargs: bool) -> str:
                if path == native.SCOPED_POLICY:
                    return "0" * 64
                return real_digest(path, **kwargs)

            with (
                mock.patch.object(native, "checked_digest", side_effect=drifted),
                mock.patch.object(native, "_save_state") as save_state,
                mock.patch.object(native.subprocess, "Popen") as popen,
                mock.patch.object(copilot_acp, "prepare_assignment") as prepare,
                self.assertRaises(RuntimeFailure) as raised,
            ):
                backend.request(RolePrompt(Role.REVIEWER, "review the fixture"))

        save_state.assert_not_called()
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertIn("scoped ACP runtime changed", str(raised.exception))
        prepare.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(self.provider_roots, [])
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_task_dispatch_prepares_a_copilot_assignment_without_questions(
        self,
    ) -> None:
        task = self._task()

        def prepare_assignment(**kwargs: object) -> dict[str, object]:
            private_root = kwargs["private_root"]
            assert isinstance(private_root, Path)
            policy = private_root / "write-policy.json"
            policy.write_text("{}", encoding="utf-8")
            return {"write_policy_path": str(policy), "write_policy_sha256": "w" * 64}

        prepare = mock.Mock(side_effect=prepare_assignment)
        with self._native_fakes():
            backend = self._start(self._spec(task_specs=(task,)))
            with (
                mock.patch.object(copilot_acp, "prepare_assignment", prepare),
                mock.patch.object(native.subprocess, "Popen", _FakePopen),
                mock.patch.object(native.os, "getpgid", return_value=77_001),
                mock.patch.object(
                    native, "create_write_policy", side_effect=AssertionError
                ) as claude_policy,
                mock.patch.object(
                    native, "build_acp_agent_command", side_effect=AssertionError
                ) as claude_command,
            ):
                backend.request(TaskDispatch(Role.WORKER, task, "update the fixture"))
                backend._runners[Role.WORKER.value].wait()

        saved = native.runtime_read_state(self.state_path)["roles"]["worker"]
        private_root = Path(saved["provider_private_root"])
        self.assertEqual(self.provider_roots, [private_root])
        self.assertEqual(saved["adapter_id"], COPILOT_SCOPED_ADAPTER_ID)
        self.assertEqual(saved["adapter_snapshot"], self.adapter_snapshot)
        self.assertEqual(
            saved["agent_command"],
            copilot_acp.agent_command(
                self.executables,
                permission="workspace-write",
                model="gpt-5.2",
                effort="high",
            ),
        )
        self.assertEqual(
            saved["write_policy_path"], str(private_root / "write-policy.json")
        )
        self.assertEqual(saved["write_policy_sha256"], "w" * 64)
        self.assertEqual(saved["task_spec"], task.as_dict())
        self.assertNotIn("question_socket", saved)
        prepare.assert_called_once()
        self.assertIs(prepare.call_args.kwargs["task"], task)
        self.assertEqual(prepare.call_args.kwargs["permission"], "workspace-write")
        self.assertEqual(prepare.call_args.kwargs["model"], "gpt-5.2")
        self.assertEqual(prepare.call_args.kwargs["effort"], "high")
        self.assertIsNone(
            next(
                call.kwargs.get("dir")
                for call in self.mkdtemp.call_args_list
                if call.kwargs.get("prefix") == "agent-team-provider-"
            )
        )
        claude_policy.assert_not_called()
        claude_command.assert_not_called()
        backend._cleanup_assignment(saved, self.state_path, Role.WORKER)


if __name__ == "__main__":
    unittest.main()
