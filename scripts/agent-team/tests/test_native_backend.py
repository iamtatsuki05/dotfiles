from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_team.native_backend as native
from agent_team import cli, tmux_backend
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
    TaskDispatch,
    TaskVerify,
)
from agent_team.native_mcp import NativeMcpSession
from agent_team.runtime import RuntimeValidationError
from agent_team.task_spec import TaskSpec, VerificationSpec
from agent_team.tmux import CloseEvidence, TmuxInspection, TmuxReceipt, _PathIdentity


class FakeExecutables:
    node = Path("/bin/node")
    sdk = Path("/bin/sdk.js")
    agent = Path("/bin/claude-agent-acp")
    library = Path("/bin/lib.js")
    node_sha256 = "n" * 64
    sdk_sha256 = "c" * 64
    agent_sha256 = "a" * 64
    library_sha256 = "l" * 64

    def verify(self) -> None:
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "node": str(self.node),
            "sdk": str(self.sdk),
            "agent": str(self.agent),
            "library": str(self.library),
            "node_sha256": self.node_sha256,
            "sdk_sha256": self.sdk_sha256,
            "agent_sha256": self.agent_sha256,
            "library_sha256": self.library_sha256,
        }


class FakePopen:
    def __init__(self, _argv: list[str], **kwargs: object) -> None:
        self.pid = 77_001
        self.returncode = 0
        self._gate_fd: int | None = None
        pass_fds = kwargs.get("pass_fds")
        if isinstance(pass_fds, tuple) and pass_fds:
            self._gate_fd = os.dup(pass_fds[0])

    def poll(self) -> int:
        return self.returncode

    def wait(self, **_kwargs: object) -> int:
        if self._gate_fd is not None:
            os.close(self._gate_fd)
            self._gate_fd = None
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
    def task_spec(self, *, dependencies: tuple[str, ...] = ()) -> TaskSpec:
        return TaskSpec(
            task_id="inspect-project",
            objective="設定と実装の対応を確認する",
            acceptance_criteria=("根拠となるファイルを示す",),
            allowed_paths=(),
            forbidden_paths=("private/",),
            dependencies=dependencies,
            verification=(VerificationSpec("check", ("python3", "-V"), 30),),
            evidence_requirements=("確認したファイルと結論",),
            consultation_conditions=(),
        )

    def test_task_dispatch_is_persisted_through_public_mcp(self) -> None:
        task = self.task_spec()
        with self.planner_backend(task_specs=(task,)) as backend:
            session = NativeMcpSession.__new__(NativeMcpSession)
            session.path = self.state_path
            session.run_id = native.runtime_read_state(self.state_path)["run_id"]
            session.runtime = backend.runtime
            session.backend = backend
            with (
                mock.patch.object(native.subprocess, "Popen", FakePopen),
                mock.patch.object(native.os, "getpgid", return_value=77_001),
            ):
                result = session.execute(
                    "task_dispatch",
                    {
                        "role": "planner",
                        "task": task.as_dict(),
                        "message": "調査してください",
                    },
                )
            state = native.runtime_read_state(self.state_path)
            saved = state["roles"]["planner"]
            self.addCleanup(
                backend._cleanup_assignment, saved, self.state_path, Role.PLANNER
            )
            self.assertEqual(saved["task_spec"], task.as_dict())
            self.assertNotEqual(result["task_id"], task.task_id)
            self.assertEqual(state["tasks"][task.task_id]["status"], "running")
            for damage in ("remove-spec", "change-spec", "change-digest"):
                with self.subTest(damage=damage):
                    changed = json.loads(json.dumps(state))
                    if damage == "remove-spec":
                        del changed["roles"]["planner"]["task_spec"]
                    elif damage == "change-spec":
                        changed["roles"]["planner"]["task_spec"]["objective"] = (
                            "replaced"
                        )
                    else:
                        changed["tasks"][task.task_id]["digest"] = "0" * 64
                    with self.assertRaisesRegex(
                        native.RuntimeValidationError, "TaskSpec"
                    ):
                        native.runtime_write_state(self.state_path, changed)
            prompt = Path(saved["prompt_path"]).read_text()
            self.assertIn(
                json.dumps(task.as_dict(), ensure_ascii=False, sort_keys=True), prompt
            )

    def test_task_dispatch_rejects_unsatisfied_dependency_before_effects(self) -> None:
        task = self.task_spec(dependencies=("missing",))
        dependency = replace(self.task_spec(), task_id="missing")
        with self.planner_backend(task_specs=(dependency, task)) as backend:
            before = self.state_path.read_bytes()
            with (
                mock.patch.object(native.subprocess, "Popen") as popen,
                mock.patch.object(native, "create_prompt_file") as prompt,
                self.assertRaisesRegex(RuntimeFailure, "dependency.*not completed"),
            ):
                backend.request(
                    TaskDispatch(
                        Role.PLANNER,
                        task,
                        "inspect",
                    )
                )
            popen.assert_not_called()
            prompt.assert_not_called()
            self.assertEqual(self.state_path.read_bytes(), before)

    def test_task_dispatch_bad_spec_is_rejected_without_process_or_state_effect(
        self,
    ) -> None:
        with self.planner_backend() as backend:
            session = NativeMcpSession.__new__(NativeMcpSession)
            session.path = self.state_path
            session.run_id = native.runtime_read_state(self.state_path)["run_id"]
            session.runtime = backend.runtime
            session.backend = backend
            before = self.state_path.read_bytes()
            with (
                mock.patch.object(native.subprocess, "Popen") as popen,
                self.assertRaisesRegex(ValueError, "unknown keys"),
            ):
                session.execute(
                    "task_dispatch",
                    {
                        "role": "planner",
                        "task": {**self.task_spec().as_dict(), "extra": True},
                        "message": "inspect",
                    },
                )
            popen.assert_not_called()
            self.assertEqual(self.state_path.read_bytes(), before)

    def test_public_dispatch_rejects_undeclared_or_replaced_verification_before_effects(
        self,
    ) -> None:
        task = self.task_spec()
        with self.planner_backend(task_specs=(task,)) as backend:
            session = NativeMcpSession.__new__(NativeMcpSession)
            session.path = self.state_path
            session.run_id = native.runtime_read_state(self.state_path)["run_id"]
            session.runtime = backend.runtime
            session.backend = backend
            for field, value in (
                ("task_id", "unapproved"),
                ("allowed_paths", ["private/"]),
                (
                    "verification",
                    [
                        {
                            "name": "unapproved",
                            "argv": ["python3", "-c", "print('unapproved')"],
                            "timeout_seconds": 5,
                        }
                    ],
                ),
            ):
                with self.subTest(field=field):
                    supplied = task.as_dict()
                    supplied[field] = value
                    before = self.state_path.read_bytes()
                    with (
                        mock.patch.object(native.subprocess, "Popen") as popen,
                        mock.patch.object(native, "create_prompt_file") as prompt,
                        self.assertRaisesRegex(RuntimeFailure, "declared.*startup"),
                    ):
                        session.execute(
                            "task_dispatch",
                            {"role": "planner", "task": supplied, "message": "inspect"},
                        )
                    popen.assert_not_called()
                    prompt.assert_not_called()
                    self.assertEqual(self.state_path.read_bytes(), before)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        make_directory = tempfile.mkdtemp
        self.fixture_trees: list[Path] = []

        def make_fixture_directory(**kwargs: object) -> str:
            if kwargs.get("dir") == "/tmp":
                kwargs["dir"] = str(self.root)
            path = make_directory(**kwargs)
            if kwargs.get("prefix") in {"agent-team-provider-", "agent-team-snapshot-"}:
                self.fixture_trees.append(Path(path))
            return path

        socket_patch = mock.patch.object(
            native.tempfile, "mkdtemp", side_effect=make_fixture_directory
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
        try:
            for root in self.fixture_trees:
                native.remove_owned_tree(root)
        finally:
            self.directory.cleanup()

    def spec(self, *roles: Role) -> StartSpec:
        return StartSpec(
            team_id="agent-team-native-test",
            workspace=self.workspace,
            config_path=self.config_path,
            state_path=self.state_path,
            role_specs={role: role_spec(role) for role in (Role.MAIN, *roles)},
            max_review_rounds=2,
        )

    def backend(self, spec: StartSpec) -> tmux_backend.TmuxBackend:
        del spec
        backend = tmux_backend.TmuxBackend(
            tmux_executable="tmux",
            launcher_path=self.launcher,
        )
        return backend

    def start_backend(self, spec: StartSpec) -> tmux_backend.TmuxBackend:
        backend = self.backend(spec)
        with (
            mock.patch.object(tmux_backend, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
        ):
            backend.start(spec)
        return backend

    @contextmanager
    def planner_backend(
        self,
        *,
        worker: bool = False,
        reviewer: bool = False,
        task_specs: tuple[TaskSpec, ...] = (),
    ) -> Iterator[tmux_backend.TmuxBackend]:
        executables = FakeExecutables()
        spec = replace(
            self.spec(),
            task_specs=task_specs,
            role_specs={
                Role.MAIN: role_spec(Role.MAIN),
                Role.PLANNER: role_spec(
                    Role.PLANNER,
                    transport="acp",
                    permission="read-only",
                    execution="background",
                    executables=executables.as_dict(),
                ),
                **(
                    {
                        Role.WORKER: replace(
                            role_spec(
                                Role.WORKER,
                                transport="acp",
                                permission="workspace-write",
                                execution="background",
                                executables=executables.as_dict(),
                            ),
                            adapter_id="claude-acp-scoped-0.70.0",
                        )
                    }
                    if worker
                    else {}
                ),
                **(
                    {
                        Role.REVIEWER: role_spec(
                            Role.REVIEWER,
                            transport="acp",
                            permission="read-only",
                            execution="background",
                            executables=executables.as_dict(),
                        )
                    }
                    if reviewer
                    else {}
                ),
            },
        )
        snapshot = {
            "adapter_id": "claude-acp-0.70.0",
            "revision": "@agentclientprotocol/sdk@1.3.0",
            "executable": str(executables.sdk),
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
            mock.patch.object(tmux_backend, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.native_acp_dependencies.NativeAcpExecutables,
                "from_dict",
                return_value=executables,
            ),
            mock.patch.object(
                native.native_acp_dependencies,
                "adapter_snapshot",
                return_value=snapshot,
            ),
        ):
            backend = self.backend(spec)
            backend.start(spec)
            yield backend

    def dispatch_task(
        self, backend: tmux_backend.TmuxBackend, role: Role, task: TaskSpec
    ) -> dict[str, object]:
        with (
            mock.patch.object(native.subprocess, "Popen", FakePopen),
            mock.patch.object(native.os, "getpgid", return_value=77_001),
        ):
            backend.request(TaskDispatch(role, task, "work"))
        return native.runtime_read_state(self.state_path)["roles"][role.value]

    def finish_task(
        self,
        backend: tmux_backend.TmuxBackend,
        role: Role,
        body: str,
        evidence: dict[str, object] | None = None,
    ) -> None:
        state = native.runtime_read_state(self.state_path)
        saved = state["roles"][role.value]
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_completion(
                self.state_path,
                role=role.value,
                run_id=state["run_id"],
                task_id=saved["task_id"],
                dispatch_id=saved["dispatch_id"],
                terminal_handle=saved["terminal_handle"],
                launch_nonce=saved["launch_nonce"],
                outcome="succeeded",
                body=body,
                cleanup_confirmed=True,
                task_evidence=evidence,
            )
        wait = backend.request(RoleWait(role, 1_000))
        backend.request(RoleRead(role, 2_000))
        backend.request(RoleRelease(role))
        backend.request(DeliveryAck(wait.delivery_id))

    def test_plan_review_must_bind_verdict_then_route_back_to_planner(self) -> None:
        task = self.task_spec()
        with self.planner_backend(reviewer=True, task_specs=(task,)) as backend:
            self.dispatch_task(backend, Role.PLANNER, task)
            self.finish_task(backend, Role.PLANNER, "Plan: inspect config and source")
            self.dispatch_task(backend, Role.REVIEWER, task)
            state = native.runtime_read_state(self.state_path)
            record = state["tasks"][task.task_id]
            verdict = {
                "task_id": task.task_id,
                "stage": "plan",
                "revision": record["revision"],
                "decision": "request_changes",
                "findings": ["Add the configuration source"],
            }
            before = self.state_path.read_bytes()
            with self.assertRaisesRegex(RuntimeFailure, "revision"):
                self.finish_task(
                    backend, Role.REVIEWER, "review", {**verdict, "revision": "stale"}
                )
            self.assertEqual(self.state_path.read_bytes(), before)
            self.finish_task(backend, Role.REVIEWER, "review", verdict)
            state = native.runtime_read_state(self.state_path)
            self.assertEqual(
                state["tasks"][task.task_id]["status"], "plan_changes_requested"
            )
            with self.assertRaisesRegex(RuntimeFailure, "writer.*stage"):
                backend.request(TaskDispatch(Role.WORKER, task, "skip revision"))
            self.dispatch_task(backend, Role.PLANNER, task)
            self.finish_task(
                backend, Role.PLANNER, "Revised plan: config.toml and runtime.py"
            )
            self.dispatch_task(backend, Role.REVIEWER, task)
            record = native.runtime_read_state(self.state_path)["tasks"][task.task_id]
            self.finish_task(
                backend,
                Role.REVIEWER,
                "approved",
                {
                    "task_id": task.task_id,
                    "stage": "plan",
                    "revision": record["revision"],
                    "decision": "approve",
                    "findings": [],
                },
            )
            self.assertEqual(
                native.runtime_read_state(self.state_path)["tasks"][task.task_id][
                    "status"
                ],
                "plan_approved",
            )

    def test_fixed_verification_requires_implementation_approval_and_same_revision(
        self,
    ) -> None:
        subprocess.run(["git", "init", "--quiet", str(self.workspace)], check=True)
        task = replace(
            self.task_spec(),
            allowed_paths=("result.txt",),
            verification=(
                VerificationSpec(
                    "check-result",
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('result.txt').read_text() == 'done'; print('verified')",
                    ),
                    10,
                ),
            ),
        )
        with self.planner_backend(
            worker=True, reviewer=True, task_specs=(task,)
        ) as backend:
            self.dispatch_task(backend, Role.WORKER, task)
            (self.workspace / "result.txt").write_text("done")
            self.finish_task(backend, Role.WORKER, "result.txt updated")
            with self.assertRaisesRegex(
                RuntimeFailure, "implementation review approval"
            ):
                backend.request(TaskVerify(task.task_id))
            # Snapshot reads execute real fixed Git commands before the mocked runner spawn.
            revision = native.snapshot_revision(self.workspace)
            with mock.patch.object(native, "snapshot_revision", return_value=revision):
                self.dispatch_task(backend, Role.REVIEWER, task)
            verdict = {
                "task_id": task.task_id,
                "stage": "implementation",
                "revision": revision,
                "decision": "approve",
                "findings": [],
            }
            self.finish_task(backend, Role.REVIEWER, "approved", verdict)
            (self.workspace / "result.txt").write_text("changed")
            with self.assertRaisesRegex(RuntimeFailure, "revision changed"):
                backend.request(TaskVerify(task.task_id))
            (self.workspace / "result.txt").write_text("done")
            result = backend.request(TaskVerify(task.task_id))
            self.assertEqual(result.status, "completed")
            check = result.record["verification"]["commands"][0]
            self.assertEqual(check["argv"], list(task.verification[0].argv))
            self.assertEqual(check["returncode"], 0)

    def test_worker_requires_task_spec_and_binds_scoped_policy(self) -> None:
        task = replace(self.task_spec(), allowed_paths=("result.txt",))
        with self.planner_backend(
            worker=True, reviewer=True, task_specs=(task,)
        ) as backend:
            with (
                mock.patch.object(native.subprocess, "Popen") as popen,
                self.assertRaisesRegex(RuntimeFailure, "Worker requires task_dispatch"),
            ):
                backend.request(RolePrompt(Role.WORKER, "edit files"))
            popen.assert_not_called()
            with (
                mock.patch.object(native.subprocess, "Popen", FakePopen),
                mock.patch.object(native.os, "getpgid", return_value=77_001),
            ):
                backend.request(TaskDispatch(Role.WORKER, task, "create result.txt"))
            state = native.runtime_read_state(self.state_path)
            saved = state["roles"]["worker"]
            self.addCleanup(
                backend._cleanup_assignment, saved, self.state_path, Role.WORKER
            )
            policy = Path(saved["write_policy_path"])
            self.assertEqual(policy.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(policy.read_text())["allowed_paths"], ["result.txt"]
            )
            self.assertEqual(
                json.loads(policy.read_text())["forbidden_paths"], ["private/"]
            )
            self.assertEqual(len(saved["write_policy_sha256"]), 64)
            self.assertEqual(
                len(state["role_specs"]["worker"]["scoped_wrapper_sha256"]), 64
            )

    def test_worker_preflight_failure_publishes_consumable_failed_result(self) -> None:
        task = replace(self.task_spec(), allowed_paths=("result.txt",))
        with self.planner_backend(
            worker=True, reviewer=True, task_specs=(task,)
        ) as backend:
            saved = self.dispatch_task(backend, Role.WORKER, task)
            state = native.runtime_read_state(self.state_path)
            with (
                mock.patch.object(
                    cli,
                    "_acp_assignment",
                    side_effect=cli.ConfigError("adapter changed"),
                ),
                mock.patch.object(cli.ProcessRunner, "run") as provider,
                mock.patch.object(native, "_assert_publisher"),
            ):
                result = cli._acp_run_turn(
                    state=state,
                    role="worker",
                    state_path=self.state_path,
                    task_id=saved["task_id"],
                    dispatch_id=saved["dispatch_id"],
                    terminal_handle=saved["terminal_handle"],
                    prompt_path=Path(saved["prompt_path"]),
                    launch_nonce=saved["launch_nonce"],
                )
            self.assertEqual(result, 1)
            provider.assert_not_called()
            completion = native.runtime_read_state(self.state_path)["native_result"]
            self.assertEqual(completion["outcome"], "failed")
            self.assertIs(completion["cleanup_confirmed"], True)
            wait = backend.request(RoleWait(Role.WORKER, 1_000))
            backend.request(RoleRead(Role.WORKER, 2_000))
            backend.request(RoleRelease(Role.WORKER))
            backend.request(DeliveryAck(wait.delivery_id))
            state = native.runtime_read_state(self.state_path)
            self.assertEqual(state["roles"], {})
            self.assertEqual(state["tasks"][task.task_id]["status"], "failed")

    def test_unconfirmed_verifier_cleanup_retains_state_and_blocks_new_work(
        self,
    ) -> None:
        subprocess.run(["git", "init", "--quiet", str(self.workspace)], check=True)
        task = replace(self.task_spec(), allowed_paths=("result.txt",))
        with self.planner_backend(
            worker=True, reviewer=True, task_specs=(task,)
        ) as backend:
            self.dispatch_task(backend, Role.WORKER, task)
            (self.workspace / "result.txt").write_text("done")
            self.finish_task(backend, Role.WORKER, "result.txt updated")
            revision = native.snapshot_revision(self.workspace)
            with mock.patch.object(native, "snapshot_revision", return_value=revision):
                self.dispatch_task(backend, Role.REVIEWER, task)
            self.finish_task(
                backend,
                Role.REVIEWER,
                "approved",
                {
                    "task_id": task.task_id,
                    "stage": "implementation",
                    "revision": revision,
                    "decision": "approve",
                    "findings": [],
                },
            )
            evidence = {
                "revision": revision,
                "passed": False,
                "commands": [],
                "error": "process cleanup could not be established",
                "cleanup_confirmed": False,
            }
            with mock.patch(
                "agent_team.task_verification.run_verification", return_value=evidence
            ):
                result = backend.request(TaskVerify(task.task_id))
            self.assertEqual(result.status, "verifying")
            before = self.state_path.read_bytes()
            with (
                mock.patch.object(native.subprocess, "Popen") as popen,
                mock.patch.object(backend, "_cancel_runner") as cancel,
                mock.patch.object(backend, "_stop_supervisor") as stop,
            ):
                for request in (
                    TaskVerify(task.task_id),
                    RolePrompt(Role.PLANNER, "start another task"),
                ):
                    with (
                        self.subTest(request=type(request).__name__),
                        self.assertRaisesRegex(RuntimeFailure, "verification"),
                    ):
                        backend.request(request)
                with (
                    mock.patch.object(backend, "_wait_for_main_process_receipt"),
                    self.assertRaisesRegex(RuntimeFailure, "verification"),
                ):
                    backend.stop()
            popen.assert_not_called()
            cancel.assert_not_called()
            stop.assert_not_called()
            self.assertEqual(self.state_path.read_bytes(), before)

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

    def test_runner_provider_gate_is_released_only_after_owned_receipt(self) -> None:
        for index, kernel_executable in enumerate(
            (sys.executable, "/fixture/Python.app/Contents/MacOS/Python")
        ):
            with self.subTest(kernel_executable=kernel_executable):
                self._check_runner_provider_gate(kernel_executable, index)

    def _check_runner_provider_gate(self, kernel_executable: str, index: int) -> None:
        self.state_path = self.root / f"gate-{index}" / "state.json"
        events: list[str] = []
        process = mock.Mock(pid=77_001)
        process.poll.return_value = None
        process.wait.return_value = 0
        original_save = native._save_state

        def save(*args: object, **kwargs: object) -> None:
            events.append("state-save")
            original_save(*args, **kwargs)

        def kernel_argv(argv: list[str]) -> tuple[str, ...]:
            self.assertEqual(argv[0], sys.executable)
            return (kernel_executable, *argv[1:])

        def read_identity(_pid: int) -> tuple[str, ...]:
            events.append("identity")
            return (kernel_executable, *popen.call_args.args[0][1:])

        def release(fd: int, data: bytes) -> int:
            events.append("release")
            del fd
            return len(data)

        with (
            self.planner_backend() as backend,
            mock.patch.object(native, "_save_state", side_effect=save),
            mock.patch.object(
                native.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(native, "python_process_argv", side_effect=kernel_argv),
            mock.patch.object(native.os, "getpgid", return_value=77_001),
            mock.patch.object(native.os, "getpgrp", return_value=1),
            mock.patch.object(native, "_process_group_alive", return_value=True),
            mock.patch.object(native, "read_process_argv", side_effect=read_identity),
            mock.patch.object(native, "_terminate_process_group") as terminate,
            mock.patch.object(native.os, "write", side_effect=release),
        ):
            backend.request(RolePrompt(Role.PLANNER, "inspect"))

        self.assertEqual(events, ["state-save", "identity", "state-save", "release"])
        launch = list(popen.call_args.args[0])
        self.assertIn("-c", launch)
        self.assertIn("_acp-run", launch)
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (int(launch[3]),))
        saved = native.runtime_read_state(self.state_path)["roles"]["planner"]
        self.assertEqual(tuple(saved["runner_argv"]), (kernel_executable, *launch[5:]))
        self.assertEqual(
            tuple(saved["runner_startup_argv"]),
            (kernel_executable, *launch[1:]),
        )
        terminate.assert_not_called()
        backend._cleanup_assignment(saved, self.state_path, Role.PLANNER)

    def test_startup_state_failure_stops_owned_runner_group_before_provider_exec(
        self,
    ) -> None:
        for index, kernel_executable in enumerate(
            (sys.executable, "/fixture/Python.app/Contents/MacOS/Python")
        ):
            with self.subTest(kernel_executable=kernel_executable):
                self._check_startup_state_failure(kernel_executable, index)

    def _check_startup_state_failure(self, kernel_executable: str, index: int) -> None:
        self.state_path = self.root / f"gate-failure-{index}" / "state.json"
        events: list[str] = []
        process = mock.Mock(pid=77_001)
        process.poll.return_value = None
        process.wait.return_value = 0
        original_save = native._save_state

        def save(*args: object, **kwargs: object) -> None:
            events.append("state-save")
            if events.count("state-save") == 2:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "post-spawn state save failed",
                )
            original_save(*args, **kwargs)

        def kernel_argv(argv: list[str]) -> tuple[str, ...]:
            self.assertEqual(argv[0], sys.executable)
            return (kernel_executable, *argv[1:])

        def read_identity(_pid: int) -> tuple[str, ...]:
            return (kernel_executable, *popen.call_args.args[0][1:])

        with (
            self.planner_backend() as backend,
            mock.patch.object(native, "_save_state", side_effect=save),
            mock.patch.object(
                native.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(native, "python_process_argv", side_effect=kernel_argv),
            mock.patch.object(native.os, "getpgid", return_value=77_001),
            mock.patch.object(native.os, "getpgrp", return_value=1),
            mock.patch.object(native, "_process_group_alive", return_value=True),
            mock.patch.object(native, "read_process_argv", side_effect=read_identity),
            mock.patch.object(native, "_terminate_process_group") as terminate,
            mock.patch.object(native.os, "write") as release,
            self.assertRaisesRegex(RuntimeFailure, "post-spawn state save failed"),
        ):
            backend.request(RolePrompt(Role.PLANNER, "inspect"))

        terminate.assert_called_once_with(
            77_001,
            77_001,
            grace_seconds=native.ACP_CANCEL_GRACE_SECONDS,
        )
        process.terminate.assert_not_called()
        process.wait.assert_called_once_with(timeout=native.PROCESS_WAIT_SECONDS)
        release.assert_not_called()
        self.assertEqual(events, ["state-save", "state-save"])
        saved = native.runtime_read_state(self.state_path)["roles"]["planner"]
        for key in ("prompt_path", "provider_private_root", "snapshot_root"):
            self.assertTrue(Path(saved[key]).exists())
        backend._cleanup_assignment(saved, self.state_path, Role.PLANNER)

    def test_startup_pid_reuse_does_not_signal_unowned_runner_group(self) -> None:
        process = mock.Mock(pid=77_001)
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            self.planner_backend() as backend,
            mock.patch.object(native.subprocess, "Popen", return_value=process),
            mock.patch.object(native.os, "getpgid", return_value=77_001),
            mock.patch.object(native.os, "getpgrp", return_value=1),
            mock.patch.object(native, "_process_group_alive", return_value=True),
            mock.patch.object(native, "read_process_argv", return_value=("foreign",)),
            mock.patch.object(native.os, "killpg") as killpg,
            self.assertRaisesRegex(RuntimeFailure, "argv does not match state"),
        ):
            backend.request(RolePrompt(Role.PLANNER, "inspect"))

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=native.PROCESS_WAIT_SECONDS)
        killpg.assert_not_called()
        saved = native.runtime_read_state(self.state_path)["roles"]["planner"]
        self.assertEqual(saved["runner_pid"], 77_001)
        self.assertEqual(saved["runner_process_group_id"], 77_001)
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

    def test_framework_process_identity_keeps_the_original_python_launch(self) -> None:
        def framework_argv(launch: list[str]) -> tuple[str, ...]:
            return ("/Framework/Python.app/Contents/MacOS/Python", *launch[1:])

        with self.planner_backend() as backend:
            with (
                mock.patch.object(
                    native, "python_process_argv", side_effect=framework_argv
                ),
                mock.patch.object(
                    native.subprocess, "Popen", side_effect=FakePopen
                ) as popen,
                mock.patch.object(native.os, "getpgid", return_value=77_001),
            ):
                backend.request(RolePrompt(Role.PLANNER, "inspect"))
            gate = popen.call_args.args[0]
            launch = gate[4:]
            self.assertEqual(launch[0], sys.executable)
            self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "/bin"})
            saved = native.runtime_read_state(self.state_path)["roles"]["planner"]
            self.assertEqual(tuple(saved["runner_argv"]), framework_argv(launch))
            backend._cleanup_assignment(saved, self.state_path, Role.PLANNER)

    def test_unsupported_selected_role_is_rejected_before_state_creation(self) -> None:
        spec = self.spec(Role.WORKER)
        backend = self.backend(spec)
        with (
            mock.patch.object(tmux_backend, "TmuxDriver", FakeTmuxDriver),
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
            mock.patch.object(tmux_backend, "TmuxDriver", FakeTmuxDriver),
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
        self.assertEqual(native_state["supervisor_argv"][1:], list(_argv[1:]))
        self.assertTrue(Path(native_state["supervisor_argv"][0]).is_absolute())
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
        resumed = tmux_backend.TmuxBackend(
            launcher_path=self.launcher, resume_existing=True
        )
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
                tmux_backend.TmuxBackend._saved_runner_status(assignment), "unknown"
            )
        with mock.patch.object(native, "_process_group_alive", return_value=False):
            self.assertEqual(
                tmux_backend.TmuxBackend._saved_runner_status(assignment), "exited"
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
            max_review_rounds=2,
            task_specs=(self.task_spec(),),
        )
        backend = self.backend(spec)
        snapshot = {
            "adapter_id": "claude-acp-0.70.0",
            "revision": "@agentclientprotocol/sdk@1.3.0",
            "executable": str(executables.sdk),
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
            mock.patch.object(tmux_backend, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.native_acp_dependencies.NativeAcpExecutables,
                "from_dict",
                return_value=executables,
            ),
            mock.patch.object(
                native.native_acp_dependencies,
                "adapter_snapshot",
                return_value=snapshot,
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
                assignment = backend.request(
                    TaskDispatch(Role.PLANNER, self.task_spec(), "inspect")
                )
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
        self.assertEqual(
            final_state["tasks"][self.task_spec().task_id]["status"],
            "awaiting_plan_review",
        )

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
            "revision": "@agentclientprotocol/sdk@1.3.0",
            "executable": str(executables.sdk),
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
            mock.patch.object(tmux_backend, "TmuxDriver", FakeTmuxDriver),
            mock.patch.object(native.shutil, "which", return_value="/usr/bin/true"),
            mock.patch.object(native, "acp_environment", return_value={"PATH": "/bin"}),
            mock.patch.object(
                native.native_acp_dependencies.NativeAcpExecutables,
                "from_dict",
                return_value=executables,
            ),
            mock.patch.object(
                native.native_acp_dependencies,
                "adapter_snapshot",
                return_value=snapshot,
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

    def test_stop_reclaims_an_owned_empty_host_after_main_cleanup(self) -> None:
        self.state_path = self.root / "agent-team-native-test" / "state.json"
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        state["native"]["main_process"] = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "a" * 32,
            "phase": "exited",
            "returncode": 0,
            "group_stopped": True,
        }
        native.runtime_write_state(self.state_path, state)
        observed = SimpleNamespace(
            presence="absent",
            running=False,
            exit_status=None,
            identity_verified=True,
            pane_present=False,
            session_present=True,
            pane_pid=None,
            server_pid=receipt.server_pid,
            observed_nonce=receipt.run_nonce,
            reason="owned server has no terminal",
        )
        driver = backend._driver
        assert isinstance(driver, FakeTmuxDriver)
        with (
            mock.patch.object(driver, "inspect", return_value=observed),
            mock.patch.object(native, "_remove_socket_root") as remove_root,
            mock.patch.object(native.os, "kill") as kill,
        ):
            with (
                self.subTest(operation="attach"),
                mock.patch.object(
                    native.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as attach,
            ):
                with self.assertRaises(RuntimeFailure):
                    backend.request(Attach(Role.MAIN))
                attach.assert_not_called()
            reopened = tmux_backend.TmuxBackend(
                launcher_path=self.launcher, resume_existing=True
            )
            with mock.patch.object(
                reopened, "_driver_from_receipt", return_value=driver
            ):
                reopened.start(self.spec())
                result = reopened.stop()
        self.assertEqual(result.run_id._value, state["run_id"])
        self.assertTrue(driver.closed)
        self.assertFalse(self.state_path.exists())
        remove_root.assert_called_once_with(receipt.socket_path.parent)
        kill.assert_not_called()

    def test_main_process_state_rejects_incomplete_or_inconsistent_cleanup(
        self,
    ) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        valid = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "a" * 32,
            "phase": "exited",
            "returncode": 0,
            "group_stopped": True,
        }
        cases = [
            {key: value for key, value in valid.items() if key != missing}
            for missing in valid
        ]
        cases.extend(
            {**valid, **change}
            for change in (
                {"agent_pid": True},
                {"supervisor_pid": 0},
                {"process_group_id": 70_009},
                {"process_group_id": None},
                {"launch_nonce": ""},
                {"returncode": False},
                {"group_stopped": 1},
                {"phase": "not-a-phase"},
            )
        )
        original = self.state_path.read_bytes()
        for process in cases:
            with self.subTest(process=process):
                state["native"]["main_process"] = process
                with self.assertRaisesRegex(RuntimeValidationError, "Main process"):
                    native.runtime_write_state(self.state_path, state)
                self.assertEqual(self.state_path.read_bytes(), original)

    def test_stop_rejects_a_truncated_main_cleanup_record_before_any_effect(
        self,
    ) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        state["native"]["main_process"] = {
            "supervisor_pid": receipt.pane_pid,
            "phase": "exited",
            "group_stopped": True,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        original = self.state_path.read_bytes()
        driver = backend._driver
        assert isinstance(driver, FakeTmuxDriver)
        with (
            mock.patch.object(native.os, "kill") as kill,
            mock.patch.object(native, "_remove_socket_root") as remove_root,
            self.assertRaises(RuntimeFailure),
        ):
            backend.stop()
        self.assertFalse(driver.closed)
        self.assertEqual(self.state_path.read_bytes(), original)
        kill.assert_not_called()
        remove_root.assert_not_called()

    def test_stop_rejects_unproven_terminal_absence(self) -> None:
        cases = (
            ({"phase": "running"}, {}),
            ({"group_stopped": False}, {}),
            ({"supervisor_pid": 70_009}, {}),
            ({}, {"pane_pid": 70_001}),
            ({}, {"presence": "unknown", "pane_pid": 70_001}),
            ({}, {"server_pid": 70_009}),
            ({}, {"observed_nonce": "different-run"}),
        )
        for index, (main_changes, host_changes) in enumerate(cases):
            with self.subTest(main=main_changes, host=host_changes):
                self.state_path = (
                    self.root
                    / f"absence-{index}"
                    / "agent-team-native-test"
                    / "state.json"
                )
                backend = self.start_backend(self.spec())
                state = native.runtime_read_state(self.state_path)
                receipt = backend._receipt_from_state(state["native"])
                state["native"]["main_process"] = {
                    "supervisor_pid": receipt.pane_pid,
                    "agent_pid": 70_003,
                    "process_group_id": 70_003,
                    "launch_nonce": "a" * 32,
                    "phase": "exited",
                    "returncode": 0,
                    "group_stopped": True,
                    **main_changes,
                }
                if main_changes.get("phase") == "running":
                    state["native"]["main_process"].pop("returncode")
                    state["native"]["main_process"].pop("group_stopped")
                native.runtime_write_state(self.state_path, state)
                fields = {
                    "presence": "absent",
                    "running": False,
                    "exit_status": None,
                    "identity_verified": True,
                    "pane_present": False,
                    "session_present": True,
                    "pane_pid": None,
                    "server_pid": receipt.server_pid,
                    "observed_nonce": receipt.run_nonce,
                    "reason": "owned server has no terminal",
                    **host_changes,
                }
                driver = backend._driver
                assert isinstance(driver, FakeTmuxDriver)
                with (
                    mock.patch.object(
                        driver, "inspect", return_value=SimpleNamespace(**fields)
                    ),
                    mock.patch.object(native, "_remove_socket_root") as remove_root,
                    mock.patch.object(native.os, "kill") as kill,
                    self.assertRaises(RuntimeFailure),
                ):
                    backend.stop()
                self.assertTrue(self.state_path.exists())
                self.assertFalse(driver.closed)
                remove_root.assert_not_called()
                kill.assert_not_called()

    def test_stop_accepts_same_supervisor_cleanup_published_during_role_cancel(
        self,
    ) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        running = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "a" * 32,
            "phase": "running",
        }
        state["native"]["main_process"] = {
            **running,
            "phase": "exited",
            "returncode": 0,
            "group_stopped": True,
        }
        native.runtime_write_state(self.state_path, state)
        with mock.patch.object(native.os, "kill") as kill:
            backend._stop_supervisor(state, receipt, running)
            kill.assert_not_called()
        state["native"]["main_process"]["launch_nonce"] = "b" * 32
        native.runtime_write_state(self.state_path, state)
        with (
            mock.patch.object(native.os, "kill") as kill,
            self.assertRaisesRegex(RuntimeFailure, "identity changed"),
        ):
            backend._stop_supervisor(state, receipt, running)
        kill.assert_not_called()

    def test_stop_does_not_signal_a_reused_or_unidentified_supervisor_pid(self) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        running = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "a" * 32,
            "phase": "running",
        }
        state["native"]["supervisor_argv"] = [
            sys.executable,
            "-m",
            "agent_team",
            "_native-main",
            "--state",
            str(self.state_path),
            "--run-id",
            state["run_id"],
        ]
        for observed in (("/bin/other", "user-process"), None):
            with self.subTest(observed=observed):
                with (
                    mock.patch.object(
                        backend, "_supervisor_cleanup_confirmed", return_value=False
                    ),
                    mock.patch.object(
                        native, "read_process_argv", return_value=observed
                    ),
                    mock.patch.object(native, "_pid_alive", return_value=False),
                    mock.patch.object(native, "PROCESS_WAIT_SECONDS", 0),
                    mock.patch.object(native.os, "kill") as kill,
                    self.assertRaisesRegex(RuntimeFailure, "identity"),
                ):
                    backend._stop_supervisor(state, receipt, running)
                kill.assert_not_called()

    def test_stop_requires_a_supervisor_argv_bound_to_the_run(self) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        running = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "a" * 32,
            "phase": "running",
        }
        wrong_run = [*state["native"]["supervisor_argv"][:-1], "different-run"]
        for saved in (None, wrong_run):
            with self.subTest(saved=saved):
                state["native"]["supervisor_argv"] = saved
                with (
                    mock.patch.object(
                        backend, "_supervisor_cleanup_confirmed", return_value=False
                    ),
                    mock.patch.object(native, "read_process_argv") as read_argv,
                    mock.patch.object(native.os, "kill") as kill,
                    self.assertRaisesRegex(RuntimeFailure, "snapshot"),
                ):
                    backend._stop_supervisor(state, receipt, running)
                read_argv.assert_not_called()
                kill.assert_not_called()

    def test_stop_accepts_cleanup_published_before_supervisor_identity_probe(
        self,
    ) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        running = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "a" * 32,
            "phase": "running",
        }
        with (
            mock.patch.object(
                backend, "_supervisor_cleanup_confirmed", side_effect=(False, True)
            ),
            mock.patch.object(native, "read_process_argv", return_value=None),
            mock.patch.object(native.os, "kill") as kill,
        ):
            backend._stop_supervisor(state, receipt, running)
        kill.assert_not_called()

    def test_stop_compares_the_frozen_supervisor_argv_before_signal(self) -> None:
        backend = self.start_backend(self.spec())
        state = native.runtime_read_state(self.state_path)
        receipt = backend._receipt_from_state(state["native"])
        running = {
            "supervisor_pid": receipt.pane_pid,
            "agent_pid": 70_003,
            "process_group_id": 70_003,
            "launch_nonce": "a" * 32,
            "phase": "running",
        }
        frozen = (
            "/fixture/Python.app/Contents/MacOS/Python",
            "-m",
            "agent_team",
            "_native-main",
            "--state",
            str(self.state_path),
            "--run-id",
            state["run_id"],
        )
        state["native"]["supervisor_argv"] = list(frozen)
        with (
            mock.patch.object(
                backend, "_supervisor_cleanup_confirmed", side_effect=(False, True)
            ),
            mock.patch.object(
                native, "read_process_argv", return_value=frozen
            ) as read_argv,
            mock.patch.object(native, "_pid_alive", return_value=False),
            mock.patch.object(native.os, "kill") as kill,
        ):
            backend._stop_supervisor(state, receipt, running)
        read_argv.assert_called_once_with(receipt.pane_pid)
        self.assertEqual(
            kill.call_args_list,
            [
                mock.call(receipt.pane_pid, 0),
                mock.call(receipt.pane_pid, native.signal.SIGTERM),
            ],
        )


if __name__ == "__main__":
    unittest.main()
