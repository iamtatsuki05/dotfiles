from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest import mock

from test_native_backend import FakePopen as _FakePopen
from test_native_backend import FakeTmuxDriver as _FakeTmuxDriver

import agent_team.native_backend as native
from agent_team import codex_acp, tmux_backend
from agent_team.contracts import (
    DeliveryAck,
    ErrorCode,
    Role,
    RoleRead,
    RoleRelease,
    RoleSpec,
    RoleWait,
    RuntimeFailure,
    StartSpec,
    TaskDispatch,
)
from agent_team.native_acp_dependencies import (
    CodexAcpExecutables,
    NativeAcpDependencyError,
)
from agent_team.scoped_acp import CODEX_SCOPED_ADAPTER_ID
from agent_team.task_spec import TaskSpec, VerificationSpec


class CodexNativeBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-codex-native-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_path = self.root / "state" / "state.json"
        self.config_path = self.root / "config.toml"
        self.launcher = self.root / "agent-team"
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.executables = CodexAcpExecutables(
            node=Path("/fixture/node"),
            agent=Path("/fixture/codex-acp"),
            sdk=Path("/fixture/sdk.js"),
            codex=Path("/fixture/codex"),
            node_sha256="n" * 64,
            agent_sha256="a" * 64,
            sdk_sha256="s" * 64,
            codex_sha256="x" * 64,
            agent_manifest_sha256="m" * 64,
            sdk_manifest_sha256="d" * 64,
        )
        self.adapter_snapshot = {
            "adapter_id": "codex-acp-1.10.0",
            "revision": "@agentclientprotocol/sdk@1.4.0",
            "executable": str(self.executables.sdk),
            "version": "@agentclientprotocol/codex-acp@1.10.0",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": self.executables.sdk_sha256,
            },
        }
        self.provider_snapshot = {
            "runtime_sha256": {"codex_scoped_launch.mjs": "r" * 64},
            "auth_path": str(self.root / "auth.json"),
            "auth_sha256": "h" * 64,
            "auth_expires_at": 2_000_000_000,
            "project_configs": {str(self.root / ".codex" / "config.toml"): "c" * 64},
        }

        make_directory = cast(Callable[..., str], tempfile.mkdtemp)

        def make_fixture_directory(**kwargs: object) -> str:
            if kwargs.get("dir") == "/tmp":
                kwargs["dir"] = str(self.root)
            directory = make_directory(**kwargs)
            if kwargs.get("prefix") in {
                "agent-team-provider-",
                "agent-team-snapshot-",
            }:
                self.addCleanup(native.remove_owned_tree, Path(directory))
            return directory

        patch = mock.patch.object(
            native.tempfile, "mkdtemp", side_effect=make_fixture_directory
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _main_spec(self) -> RoleSpec:
        return RoleSpec(
            provider="claude",
            transport="direct",
            model="claude-test",
            effort="medium",
            permission="orchestrator",
            instructions="main instructions",
            execution="tui_direct",
        )

    def _codex_spec(
        self,
        *,
        role: Role = Role.PLANNER,
        adapter_id: str = CODEX_SCOPED_ADAPTER_ID,
        executables: CodexAcpExecutables | None = None,
        provider_snapshot: dict[str, object] | None = None,
    ) -> RoleSpec:
        permission = "workspace-write" if role is Role.WORKER else "read-only"
        selected_executables = executables or self.executables
        return RoleSpec(
            provider="codex",
            transport="acp",
            model="codex-test",
            effort="high",
            permission=permission,
            instructions=f"{role.value} instructions",
            execution="background",
            adapter_id=adapter_id,
            acp_executables=selected_executables.as_dict(),
            provider_snapshot=provider_snapshot or self.provider_snapshot,
        )

    def _spec(
        self,
        *,
        role: Role = Role.PLANNER,
        codex: RoleSpec | None = None,
        executables: CodexAcpExecutables | None = None,
        task_specs: tuple[TaskSpec, ...] = (),
    ) -> StartSpec:
        return StartSpec(
            team_id="agent-team-codex-native-test",
            workspace=self.workspace,
            config_path=self.config_path,
            state_path=self.state_path,
            role_specs={
                Role.MAIN: self._main_spec(),
                role: codex or self._codex_spec(role=role, executables=executables),
                **(
                    {
                        Role.REVIEWER: self._codex_spec(
                            role=Role.REVIEWER, executables=executables
                        )
                    }
                    if role is Role.WORKER
                    else {}
                ),
            },
            max_review_rounds=2 if task_specs else None,
            task_specs=task_specs,
        )

    @contextmanager
    def _native_fakes(
        self,
        *,
        executables: CodexAcpExecutables | None = None,
        dependency_verify: mock.Mock | None = None,
        snapshot: mock.Mock | None = None,
        prepare: mock.Mock | None = None,
    ) -> Iterator[None]:
        with (
            mock.patch.object(tmux_backend, "TmuxDriver", _FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.native_acp_dependencies.CodexAcpExecutables,
                "from_dict",
                return_value=executables or self.executables,
            ),
            mock.patch.object(
                native.native_acp_dependencies.CodexAcpExecutables,
                "verify",
                dependency_verify or mock.Mock(return_value=None),
            ),
            mock.patch.object(
                native.native_acp_dependencies,
                "codex_adapter_snapshot",
                return_value=self.adapter_snapshot,
            ),
            mock.patch.object(codex_acp, "verify_snapshot", snapshot or mock.Mock()),
        ):
            if prepare is None:
                yield
            else:
                with mock.patch.object(codex_acp, "prepare_assignment", prepare):
                    yield

    def _task(self) -> TaskSpec:
        return TaskSpec(
            task_id="codex-task",
            objective="Update one fixture",
            acceptance_criteria=("The fixture remains valid",),
            allowed_paths=("allowed.txt",),
            forbidden_paths=("private/",),
            dependencies=(),
            verification=(VerificationSpec("check", ("python3", "-V"), 30),),
            evidence_requirements=("check output",),
            consultation_conditions=(),
        )

    def _start(self, spec: StartSpec) -> tmux_backend.TmuxBackend:
        backend = tmux_backend.TmuxBackend(
            tmux_executable="tmux", launcher_path=self.launcher
        )
        backend.start(spec)
        return backend

    def test_codex_start_profile_freezes_provider_snapshot(self) -> None:
        verify = mock.Mock()
        original = json.loads(json.dumps(self.provider_snapshot))
        with self._native_fakes(snapshot=verify):
            backend = self._start(self._spec())

        saved = native.runtime_read_state(self.state_path)
        saved_spec = saved["role_specs"][Role.PLANNER.value]
        self.assertEqual(saved_spec["provider"], "codex")
        self.assertEqual(saved_spec["adapter_id"], CODEX_SCOPED_ADAPTER_ID)
        self.assertEqual(saved_spec["acp_executables"], self.executables.as_dict())
        self.assertEqual(saved_spec["provider_snapshot"], original)
        self.assertEqual(verify.call_args.args[0], original)
        self.assertEqual(verify.call_args.args[1], self.workspace)

        cast_snapshot = saved_spec["provider_snapshot"]
        self.assertIsInstance(cast_snapshot, dict)
        cast_snapshot["project_configs"]["mutated"] = "bad"
        project_configs = self.provider_snapshot["project_configs"]
        assert isinstance(project_configs, dict)
        self.assertNotIn("mutated", project_configs)
        self.assertNotIn("mutated", original["project_configs"])
        self.assertEqual(backend.runtime, "tmux")

    def test_codex_start_rejects_missing_binding_before_snapshot_or_effects(
        self,
    ) -> None:
        for binding in ("node", "agent", "sdk", "codex"):
            with self.subTest(binding=binding):
                self.state_path = self.root / f"state-start-{binding}" / "state.json"
                executables = self.executables
                dependency_verify = mock.Mock(
                    side_effect=NativeAcpDependencyError(f"missing Codex ACP {binding}")
                )
                snapshot = mock.Mock()
                backend = tmux_backend.TmuxBackend(
                    tmux_executable="tmux", launcher_path=self.launcher
                )
                with (
                    self._native_fakes(
                        executables=executables,
                        dependency_verify=dependency_verify,
                        snapshot=snapshot,
                    ),
                    mock.patch.object(native, "_save_state") as save_state,
                    mock.patch.object(_FakeTmuxDriver, "create") as create,
                    self.assertRaisesRegex(
                        RuntimeFailure, "selected planner ACP dependencies"
                    ),
                ):
                    backend.start(self._spec(executables=executables))
                dependency_verify.assert_called_once()
                snapshot.assert_not_called()
                save_state.assert_not_called()
                create.assert_not_called()
                self.assertFalse(self.state_path.exists())
                self.assertEqual(tuple(self.root.glob("agent-team-provider-*")), ())

    def test_codex_profile_rejects_claude_adapter_before_state_or_terminal(
        self,
    ) -> None:
        spec = self._spec(codex=self._codex_spec(adapter_id="claude-acp-0.70.0"))
        backend = tmux_backend.TmuxBackend(
            tmux_executable="tmux", launcher_path=self.launcher
        )
        with (
            self._native_fakes(),
            self.assertRaisesRegex(RuntimeFailure, "scoped ACP profile"),
            mock.patch.object(_FakeTmuxDriver, "create") as create,
        ):
            backend.start(spec)
        create.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_codex_task_dispatch_uses_codex_assignment_without_claude_branch(
        self,
    ) -> None:
        task = self._task()
        prepare = mock.Mock()
        verify = mock.Mock()
        original_snapshot = json.loads(json.dumps(self.provider_snapshot))

        def prepare_assignment(**kwargs: object) -> dict[str, object]:
            private_root = kwargs["private_root"]
            assert isinstance(private_root, Path)
            write_policy = private_root / "write-policy.json"
            write_policy.write_text("fixture", encoding="utf-8")
            return {
                "write_policy_path": str(write_policy),
                "write_policy_sha256": "p" * 64,
                "codex_launch_path": str(private_root / "codex-launch.json"),
                "codex_config_snapshot": "q" * 64,
            }

        prepare.side_effect = prepare_assignment
        with self._native_fakes(snapshot=verify, prepare=prepare):
            backend = self._start(self._spec(role=Role.WORKER, task_specs=(task,)))
            verify.reset_mock()
            project_configs = self.provider_snapshot["project_configs"]
            assert isinstance(project_configs, dict)
            project_configs["mutated"] = "changed"
            with (
                mock.patch.object(native.subprocess, "Popen", _FakePopen),
                mock.patch.object(native.os, "getpgid", return_value=77_001),
                mock.patch.object(
                    native, "create_write_policy", side_effect=AssertionError
                ) as claude_policy,
                mock.patch.object(
                    native, "build_acp_agent_command", side_effect=AssertionError
                ) as claude_command,
            ):
                assignment = backend.request(
                    TaskDispatch(Role.WORKER, task, "update the fixture")
                )
                backend._runners[Role.WORKER.value].wait()

        self.assertEqual(assignment.role, Role.WORKER)
        state = native.runtime_read_state(self.state_path)
        saved = state["roles"]["worker"]
        self.assertEqual(saved["adapter_id"], CODEX_SCOPED_ADAPTER_ID)
        self.assertEqual(saved["adapter_snapshot"], self.adapter_snapshot)
        self.assertEqual(saved["task_spec"], task.as_dict())
        self.assertEqual(saved["codex_config_snapshot"], "q" * 64)
        self.assertEqual(saved["write_policy_sha256"], "p" * 64)
        self.assertEqual(verify.call_args.args[0], original_snapshot)
        prepare.assert_called_once()
        self.assertIs(prepare.call_args.kwargs["task"], task)
        self.assertEqual(prepare.call_args.kwargs["permission"], "workspace-write")
        claude_policy.assert_not_called()
        claude_command.assert_not_called()
        changed = json.loads(json.dumps(state))
        changed["roles"]["worker"]["task_spec"]["objective"] = "replaced objective"
        with self.assertRaisesRegex(native.RuntimeValidationError, "TaskSpec"):
            native.runtime_write_state(self.state_path, changed)
        self.assertEqual(native.runtime_read_state(self.state_path), state)
        backend._cleanup_assignment(saved, self.state_path, Role.WORKER)

    def test_codex_dispatch_rejects_missing_binding_before_snapshot_or_effects(
        self,
    ) -> None:
        task = self._task()
        for binding in ("node", "agent", "sdk", "codex"):
            with self.subTest(binding=binding):
                self.state_path = self.root / f"state-dispatch-{binding}" / "state.json"
                executables = self.executables
                dependency_verify = mock.Mock(return_value=None)
                snapshot = mock.Mock()
                backend = tmux_backend.TmuxBackend(
                    tmux_executable="tmux", launcher_path=self.launcher
                )
                with self._native_fakes(
                    executables=executables,
                    dependency_verify=dependency_verify,
                    snapshot=snapshot,
                ):
                    backend.start(
                        self._spec(
                            role=Role.WORKER,
                            executables=executables,
                            task_specs=(task,),
                        )
                    )
                    before = self.state_path.read_bytes()
                    dependency_verify.reset_mock()
                    snapshot.reset_mock()
                    dependency_verify.side_effect = NativeAcpDependencyError(
                        f"missing Codex ACP {binding}"
                    )
                    with (
                        mock.patch.object(
                            native, "prepare_dispatch", wraps=native.prepare_dispatch
                        ) as prepare_dispatch,
                        mock.patch.object(native, "_save_state") as save_state,
                        mock.patch.object(native.subprocess, "Popen") as popen,
                        self.assertRaisesRegex(
                            RuntimeFailure, "selected ACP dependencies"
                        ),
                    ):
                        backend.request(
                            TaskDispatch(Role.WORKER, task, "update the fixture")
                        )
                    prepare_dispatch.assert_called_once()
                    dependency_verify.assert_called_once()
                    snapshot.assert_not_called()
                    save_state.assert_not_called()
                    popen.assert_not_called()
                    self.assertEqual(self.state_path.read_bytes(), before)
                    self.assertEqual(tuple(self.root.glob("agent-team-provider-*")), ())

    def test_codex_public_completion_checks_identity_and_publishes_result(self) -> None:
        task = self._task()
        prepare = mock.Mock()

        def prepare_assignment(**kwargs: object) -> dict[str, object]:
            private_root = kwargs["private_root"]
            assert isinstance(private_root, Path)
            return {
                "write_policy_path": str(private_root / "write-policy.json"),
                "write_policy_sha256": "p" * 64,
                "codex_launch_path": str(private_root / "codex-launch.json"),
                "codex_config_snapshot": "q" * 64,
            }

        prepare.side_effect = prepare_assignment
        with self._native_fakes(prepare=prepare):
            backend = self._start(self._spec(task_specs=(task,)))
            with (
                mock.patch.object(native.subprocess, "Popen", _FakePopen),
                mock.patch.object(native.os, "getpgid", return_value=77_001),
            ):
                backend.request(TaskDispatch(Role.PLANNER, task, "update"))
            state = native.runtime_read_state(self.state_path)
            saved = state["roles"]["planner"]
            baseline = json.loads(json.dumps(state))
            common = {
                "role": "planner",
                "run_id": state["run_id"],
                "dispatch_id": saved["dispatch_id"],
                "terminal_handle": saved["terminal_handle"],
                "launch_nonce": saved["launch_nonce"],
                "outcome": "succeeded",
                "body": "Codex completed the fixture",
                "cleanup_confirmed": True,
            }
            for field, value in (
                ("provider", "unsupported-provider"),
                ("permission", "workspace-write"),
                ("adapter_id", "wrong-codex-adapter"),
            ):
                with self.subTest(corrupted_field=field):
                    changed = json.loads(json.dumps(baseline))
                    changed["role_specs"]["planner"][field] = value
                    if field == "adapter_id":
                        changed["roles"]["planner"]["adapter_id"] = value
                    native.runtime_write_state(self.state_path, changed)
                    with (
                        mock.patch.object(native, "_assert_publisher") as publisher,
                        self.assertRaises(RuntimeFailure) as raised,
                    ):
                        native.publish_completion(
                            self.state_path,
                            **{**common, "task_id": saved["task_id"]},
                        )
                    self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
                    publisher.assert_not_called()
                    self.assertNotIn(
                        "native_result", native.runtime_read_state(self.state_path)
                    )
                    native.runtime_write_state(self.state_path, baseline)

            with (
                mock.patch.object(native, "_assert_publisher"),
                self.assertRaisesRegex(RuntimeFailure, "identity"),
            ):
                native.publish_completion(
                    self.state_path,
                    **{**common, "task_id": "wrong-task"},
                )
            self.assertNotIn(
                "native_result", native.runtime_read_state(self.state_path)
            )

            with mock.patch.object(native, "_assert_publisher"):
                native.publish_completion(
                    self.state_path,
                    **{**common, "task_id": saved["task_id"]},
                )
            result = native.runtime_read_state(self.state_path)["native_result"]
            self.assertEqual(result["role"], "planner")
            self.assertEqual(result["task_id"], saved["task_id"])
            self.assertEqual(result["body"], common["body"])
            wait = backend.request(RoleWait(Role.PLANNER, 1_000))
            self.assertEqual(wait.events[0].body, common["body"])
            self.assertEqual(wait.events[0].outcome.value, "succeeded")
            self.assertEqual(
                backend.request(RoleRead(Role.PLANNER, 2_000)).output, common["body"]
            )
            backend.request(RoleRelease(Role.PLANNER))
            self.assertEqual(
                backend.request(DeliveryAck(wait.delivery_id)).acknowledged, True
            )
            self.assertEqual(native.runtime_read_state(self.state_path)["roles"], {})


if __name__ == "__main__":
    unittest.main()
